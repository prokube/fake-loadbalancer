# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 prokube.ai GmbH

"""This module implements a simple Kubernetes operator that simulates LoadBalancer services in
environments without a real load-balancer (e.g., microk8s, kind). It watches for Service resources
of type LoadBalancer and patches their status with the internal IPs of cluster nodes to
unblock them from Pending state. `kube-proxy` then takes care of routing traffic
according to the updated status."""

import logging

import kopf
import kubernetes

logger = logging.getLogger(__name__)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Configure the operator settings."""
    # Disable cluster discovery; we only watch built-in Service and Node resources.
    settings.scanning.disabled = True


def get_node_ips() -> list[str]:
    """Get internal IPs of all Ready nodes.

    Non-ready nodes are excluded — when a node transitions to NotReady, the node event
    fires and this function is called again, so the ingress is updated promptly.
    """
    v1 = kubernetes.client.CoreV1Api()
    nodes = v1.list_node()
    ips = []
    for node in nodes.items:
        is_ready = any(
            c.type == "Ready" and c.status == "True"
            for c in (node.status.conditions or [])
        )
        if not is_ready:
            continue
        for addr in node.status.addresses:
            if addr.type == "InternalIP":
                ips.append(addr.address)
    return sorted(ips)


def patch_lb_service(name: str, namespace: str, ips: list[str]):
    """Patch the status of a service type LoadBalancer ingress status, which is what kube-proxy uses to route traffic.

    Normally, a load-balancer controller patches Service status with the IP of an external load balancer.

    kube-proxy routes traffic to Service endpoints based on:
    - spec.clusterIP (in-cluster traffic)
    - spec.ports[].nodePort (NodePort on all nodes)
    - spec.externalIPs (if configured)
    - status.loadBalancer.ingress (if any)
    kube-proxy uses linux tools (such as iptables, IPVS, etc.) to program NAT rules that rewrite
    packets destined for any of these source IPs: it DNATs the destination to a selected pod IP
    and SNATs the return path when the pod lives on a different node, so the response flows back
    through the same node the client hit.

    At the time of writing, microk8s kube-proxy uses iptables-legacy tool.
    So you can verify that kube-proxy has programmed routing for the Service endpoints by running:
    sudo iptables-legacy -t nat -L -n -v | grep loadbalancer # Filter for LoadBalancer services

    By patching status.loadBalancer.ingress with the internal IPs of all nodes, we expose
    reachable entrypoints for the Service in environments without a real load balancer: traffic
    to <ingressIP>:<servicePort> gets routed to the Service's endpoints, so clients don't have
    to target <nodeIP>:<nodePort> directly.
    """
    v1 = kubernetes.client.CoreV1Api()
    v1.patch_namespaced_service_status(
        name=name,
        namespace=namespace,
        body={
            "status": {
                "loadBalancer": {
                    "ingress": [{"ip": ip} for ip in ips],
                },
            },
        },
    )
    logger.info("Patched %s/%s with LoadBalancer ingress IPs=%s", namespace, name, ips)


# Trigger on create/update, and on operator restart (resume) to catch Services we missed.
# @kopf.on.field("services", field="spec.externalIPs") is required alongside the type-based
# handlers: @kopf.on.update only fires when spec.type changes, so removing externalIPs would
# not trigger reconciliation without this extra handler.
@kopf.on.create("services", field="spec.type", value="LoadBalancer")
@kopf.on.update("services", field="spec.type", value="LoadBalancer")
@kopf.on.resume("services", field="spec.type", value="LoadBalancer")
@kopf.on.field("services", field="spec.externalIPs")
def reconcile_service(name: str, namespace: str, spec: dict, **_):
    """Reconcile a LoadBalancer service by patching its status with NodeIPs to unblock it from Pending state.

    When externalIPs are set by the user, we clear status.loadBalancer.ingress — Kubernetes does
    not do this automatically, and stale ingress entries would confuse clients.
    When externalIPs are not set, we patch status.loadBalancer.ingress with the IPs of all
    nodes so that kube-proxy programs the necessary NAT rules.
    """
    if spec.get("type") != "LoadBalancer":
        return
    if spec.get("externalIPs"):
        patch_lb_service(name, namespace, [])
    else:
        patch_lb_service(name, namespace, get_node_ips())


# @kopf.on.event is stateless — kopf does not patch the watched resource to store handler
# state, so no patch permission on nodes is required. @kopf.on.field("nodes", ...) would
# cause a 403 loop since the RBAC only grants get/list/watch on nodes, not patch.
@kopf.on.event("nodes")
def reconcile_on_node_change(**_):
    """Reconcile all LoadBalancer services when a node event is received."""
    v1 = kubernetes.client.CoreV1Api()
    api = kubernetes.client.ApiClient()
    services = v1.list_service_for_all_namespaces()
    for svc in services.items:
        if svc.spec.type == "LoadBalancer":
            spec = api.sanitize_for_serialization(svc.spec)
            reconcile_service(svc.metadata.name, svc.metadata.namespace, spec)
