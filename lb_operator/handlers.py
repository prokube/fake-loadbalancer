"""Kopf event handlers: service lifecycle, node/pod watchers, and startup config."""

from __future__ import annotations

import logging
import os
import random
import textwrap
from typing import Any

import kopf
import kubernetes
from kubernetes import client as k8s

from .backends import get_backend
from .backends import klipper as klipper_backend
from .backends import nginx as nginx_backend
from .constants import (
    CONFIGMAP_PREFIX,
    DAEMONSET_PREFIX,
    FINALIZER,
    OPERATOR_LABEL,
    RECONCILED_ANNOTATION,
)
from .utils import (
    NAMESPACE,
    get_ready_node_ips,
    patch_service_status,
)
from .utils import safe_name

logger = logging.getLogger("bare-service-lb")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    """Operator-wide settings."""
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = FINALIZER
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix="bare-service-lb.prokube.ai",
    )

    # Enable leader election for high availability
    # Multiple operator replicas can run, but only one will be active.
    # A random priority ensures that when replicas have different priorities,
    # exactly one becomes active; equal priorities cause all to freeze.
    settings.peering.standalone = False
    settings.peering.priority = random.randint(0, 32767)
    settings.peering.name = os.environ.get(
        "LEADER_ELECTION_LEASE_NAME", "bare-service-lb-leader"
    )
    settings.peering.mandatory = True


# ---------------------------------------------------------------------------
# Service handlers
# ---------------------------------------------------------------------------


@kopf.on.create("services", field="spec.type", value="LoadBalancer")
@kopf.on.update("services", field="spec.type", value="LoadBalancer")
@kopf.on.resume("services", field="spec.type", value="LoadBalancer")
async def handle_loadbalancer(
    namespace: str,
    name: str,
    spec: dict[str, Any],
    meta: dict[str, Any],
    uid: str,
    **_: Any,
) -> dict[str, Any]:
    """
    React to a Service of type LoadBalancer:
      1. Mark it as managed (keep type as LoadBalancer so status.loadBalancer.ingress is allowed)
      2. Build NGINX config
      3. Deploy ConfigMap + DaemonSet
      4. Patch status with node IPs
    """
    # Skip services that have already been fully reconciled to avoid
    # re-processing loops.  The "managed" annotation is set early (ownership
    # claim), but "reconciled" is only set after all resources have been
    # created successfully.  This means a failed handler run will retry on
    # the next event/restart because "reconciled" won't be present yet.
    annotations = meta.get("annotations", {})
    if annotations.get(RECONCILED_ANNOTATION) == "true":
        logger.debug("Skipping already-reconciled service %s/%s", namespace, name)
        return {"managed": True, "reconciled": True}

    logger.info("Processing LoadBalancer service %s/%s", namespace, name)

    v1 = k8s.CoreV1Api()
    apps_v1 = k8s.AppsV1Api()

    # ------------------------------------------------------------------
    # 1. Mark the service as managed (ownership claim)
    # ------------------------------------------------------------------
    # Keep the service as LoadBalancer — Kubernetes auto-assigns NodePorts
    # for LoadBalancer services, and this allows us to patch
    # status.loadBalancer.ingress (forbidden on NodePort services in K8s 1.24+).
    # The "managed" annotation is set early so other controllers know we
    # own this service, but it does NOT prevent retries — only "reconciled"
    # (set at the very end) prevents re-processing.
    if annotations.get("bare-service-lb.prokube.ai/managed") != "true":
        patch_body: dict[str, Any] = {
            "metadata": {
                "annotations": {
                    "bare-service-lb.prokube.ai/managed": "true",
                },
            },
        }
        v1.patch_namespaced_service(name, namespace, patch_body)
        logger.info("Marked %s/%s as managed", namespace, name)

    # Read the service to get auto-assigned NodePorts
    svc = v1.read_namespaced_service(name, namespace)
    svc_ports = svc.spec.ports
    cluster_ip = svc.spec.cluster_ip
    etp = svc.spec.external_traffic_policy or "Cluster"
    use_cluster_ip = etp == "Local"

    # ------------------------------------------------------------------
    # 3. Build port mapping
    # ------------------------------------------------------------------
    ports: list[dict[str, Any]] = []
    for p in svc_ports:
        # When externalTrafficPolicy is Local, NodePort only accepts
        # traffic on nodes that have a local endpoint.  Since our NGINX
        # DaemonSet runs on every node, 127.0.0.1:<NodePort> would fail
        # on nodes without a backing pod.  Route via ClusterIP instead.
        if use_cluster_ip:
            upstream = f"{cluster_ip}:{p.port}"
        else:
            upstream = f"127.0.0.1:{p.node_port}"
        ports.append(
            {
                "host_port": p.port,
                "node_port": p.node_port,
                "upstream": upstream,
                "protocol": p.protocol or "TCP",
                "name": p.name or f"port-{p.port}",
            }
        )

    # ------------------------------------------------------------------
    # 4. Determine backend and create proxy resources
    # ------------------------------------------------------------------
    backend = get_backend(annotations)
    cm_name = CONFIGMAP_PREFIX + safe_name(namespace, name)

    if backend == "klipper":
        # Klipper backend: no ConfigMap needed, just a DaemonSet
        # Clean up any leftover ConfigMap from a previous nginx backend
        try:
            v1.delete_namespaced_config_map(cm_name, NAMESPACE)
            logger.info("Cleaned up old nginx ConfigMap %s/%s", NAMESPACE, cm_name)
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

        ds = klipper_backend.build_daemonset(namespace, name, ports, uid)
    else:
        # NGINX backend: ConfigMap + DaemonSet
        nginx_stream_conf = nginx_backend.build_nginx_config(ports)
        nginx_main_conf = textwrap.dedent("""\
            worker_processes auto;
            error_log /var/log/nginx/error.log warn;
            pid /tmp/nginx.pid;

            events {
                worker_connections 1024;
            }

            include /etc/nginx/stream.conf;
        """)

        cm = nginx_backend.build_configmap(namespace, name, nginx_stream_conf, uid)
        cm.data["nginx.conf"] = nginx_main_conf

        try:
            v1.read_namespaced_config_map(cm_name, NAMESPACE)
            v1.replace_namespaced_config_map(cm_name, NAMESPACE, cm)
            logger.info("Updated ConfigMap %s/%s", NAMESPACE, cm_name)
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status == 404:
                v1.create_namespaced_config_map(NAMESPACE, cm)
                logger.info("Created ConfigMap %s/%s", NAMESPACE, cm_name)
            else:
                raise

        ds = nginx_backend.build_daemonset(namespace, name, ports, uid)

    # ------------------------------------------------------------------
    # 5. Create/update DaemonSet
    # ------------------------------------------------------------------
    ds_name = ds.metadata.name

    try:
        apps_v1.read_namespaced_daemon_set(ds_name, NAMESPACE)
        apps_v1.replace_namespaced_daemon_set(ds_name, NAMESPACE, ds)
        logger.info("Updated DaemonSet %s/%s", NAMESPACE, ds_name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            apps_v1.create_namespaced_daemon_set(NAMESPACE, ds)
            logger.info("Created DaemonSet %s/%s", NAMESPACE, ds_name)
        else:
            raise

    # ------------------------------------------------------------------
    # 6. Patch service status with node IPs (only where pods are Ready)
    # ------------------------------------------------------------------
    # Right after DaemonSet creation pods won't be Ready yet, so this will
    # typically set an empty ingress list.  The pod watcher below will
    # re-patch the status once pods come up.  This is the k3s ServiceLB
    # pattern: status only reflects nodes that are actually serving traffic.
    ready_ips = get_ready_node_ips(ds_name)
    patch_service_status(namespace, name, ready_ips)

    # ------------------------------------------------------------------
    # 7. Mark the service as fully reconciled
    # ------------------------------------------------------------------
    # This annotation gates the skip-check at the top of this handler.
    # It is set LAST so that if any step above fails, the operator will
    # retry on the next event/restart instead of silently skipping.
    v1.patch_namespaced_service(
        name,
        namespace,
        {"metadata": {"annotations": {RECONCILED_ANNOTATION: "true"}}},
    )
    logger.info("Marked %s/%s as reconciled (backend=%s)", namespace, name, backend)

    return {
        "managed": True,
        "backend": backend,
        "daemonset": f"{NAMESPACE}/{ds_name}",
        "configmap": f"{NAMESPACE}/{cm_name}" if backend == "nginx" else None,
        "ports": ports,
        "node_ips": ready_ips,
    }


@kopf.on.delete(
    "services",
    annotations={"bare-service-lb.prokube.ai/managed": "true"},
)
async def handle_delete(
    namespace: str,
    name: str,
    meta: dict[str, Any],
    **_: Any,
) -> None:
    """Clean up DaemonSet and ConfigMap when the service is deleted."""
    annotations = meta.get("annotations", {})
    is_managed = annotations.get("bare-service-lb.prokube.ai/managed") == "true"
    is_reconciled = annotations.get(RECONCILED_ANNOTATION) == "true"
    if not (is_managed or is_reconciled):
        return

    logger.info("Cleaning up resources for deleted service %s/%s", namespace, name)

    apps_v1 = k8s.AppsV1Api()
    v1 = k8s.CoreV1Api()

    ds_name = DAEMONSET_PREFIX + safe_name(namespace, name)
    cm_name = CONFIGMAP_PREFIX + safe_name(namespace, name)

    # Delete DaemonSet
    try:
        apps_v1.delete_namespaced_daemon_set(
            ds_name,
            NAMESPACE,
            body=k8s.V1DeleteOptions(propagation_policy="Foreground"),
        )
        logger.info("Deleted DaemonSet %s/%s", NAMESPACE, ds_name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
        logger.debug("DaemonSet %s/%s already gone", NAMESPACE, ds_name)

    # Delete ConfigMap
    try:
        v1.delete_namespaced_config_map(cm_name, NAMESPACE)
        logger.info("Deleted ConfigMap %s/%s", NAMESPACE, cm_name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
        logger.debug("ConfigMap %s/%s already gone", NAMESPACE, cm_name)


# ---------------------------------------------------------------------------
# Shared helper — refresh status for all managed services
# ---------------------------------------------------------------------------


def _refresh_all_managed_statuses() -> None:
    """Re-evaluate and patch status for every managed Service.

    For each managed Service we look up its DaemonSet and report only the
    node IPs where pods are Ready.  This is called when pods or nodes
    change so that Service status always reflects reality.
    """
    v1 = k8s.CoreV1Api()
    all_services = v1.list_service_for_all_namespaces()
    for svc in all_services.items:
        annotations = svc.metadata.annotations or {}
        is_managed = annotations.get("bare-service-lb.prokube.ai/managed") == "true"
        is_reconciled = annotations.get(RECONCILED_ANNOTATION) == "true"
        if not (is_managed or is_reconciled):
            continue
        svc_ns = svc.metadata.namespace
        svc_name = svc.metadata.name
        ds_name = DAEMONSET_PREFIX + safe_name(svc_ns, svc_name)
        try:
            ready_ips = get_ready_node_ips(ds_name)
            patch_service_status(svc_ns, svc_name, ready_ips)
        except kubernetes.client.exceptions.ApiException:
            logger.exception(
                "Failed to update status for %s/%s",
                svc_ns,
                svc_name,
            )


# ---------------------------------------------------------------------------
# Node watcher — update ingress IPs when nodes change
# ---------------------------------------------------------------------------


@kopf.on.create("nodes")
@kopf.on.delete("nodes")
async def handle_node_change(**_: Any) -> None:
    """When nodes are added/removed, re-evaluate all managed service statuses."""
    _refresh_all_managed_statuses()


# ---------------------------------------------------------------------------
# Pod watcher — update ingress IPs when DaemonSet pods become Ready
# ---------------------------------------------------------------------------


@kopf.on.event(
    "pods",
    labels={OPERATOR_LABEL: "true"},
    when=lambda namespace, **_: namespace == NAMESPACE,
)
async def handle_pod_event(
    event: dict[str, Any],
    meta: dict[str, Any],
    **_: Any,
) -> None:
    """When a managed DaemonSet pod changes, refresh the owning Service status.

    This is the key piece that makes the k3s-style status work: after
    ``handle_loadbalancer`` creates the DaemonSet, pods take a moment to
    schedule and become Ready.  This watcher picks up those transitions
    and patches the Service status with accurate node IPs.
    """
    labels = meta.get("labels", {})
    ds_label = labels.get("bare-service-lb.prokube.ai/daemonset", "")
    if not ds_label:
        return

    # Resolve the DaemonSet back to its owning Service via DaemonSet labels
    apps_v1 = k8s.AppsV1Api()
    try:
        ds = apps_v1.read_namespaced_daemon_set(ds_label, NAMESPACE)
    except kubernetes.client.exceptions.ApiException:
        return  # DaemonSet may already be deleted

    ds_labels = ds.metadata.labels or {}
    svc_ns = ds_labels.get("bare-service-lb.prokube.ai/service-namespace")
    svc_name = ds_labels.get("bare-service-lb.prokube.ai/service-name")
    if not svc_ns or not svc_name:
        return

    try:
        ready_ips = get_ready_node_ips(ds_label)
        patch_service_status(svc_ns, svc_name, ready_ips)
    except kubernetes.client.exceptions.ApiException:
        logger.exception(
            "Failed to update status for %s/%s from pod event",
            svc_ns,
            svc_name,
        )
