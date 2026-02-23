"""Kopf event handlers: service lifecycle, node/pod watchers, and startup config."""

from __future__ import annotations

import logging
import os
import random
from typing import Any, cast

import kopf
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

from .backends import nginx as nginx_backend
from .constants import (
    DAEMONSET_PREFIX,
    FINALIZER,
    MANAGED_ANNOTATION,
)
from .utils import (
    NAMESPACE,
    get_ready_node_ips,
    patch_service_status,
    safe_name,
)

logger = logging.getLogger("bare-service-lb")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_port_mapping(
    svc_ports: list[k8s.V1ServicePort],
    cluster_ip: str,
    use_cluster_ip: bool,
) -> list[dict[str, Any]]:
    """Build the port mapping list from service ports."""
    ports: list[dict[str, Any]] = []
    for p in svc_ports:
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
    return ports


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@kopf.on.startup()  # type: ignore
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
    settings.peering.name = os.environ.get("LEADER_ELECTION_LEASE_NAME", "bare-service-lb-leader")
    settings.peering.mandatory = True


# ---------------------------------------------------------------------------
# Service handlers
# ---------------------------------------------------------------------------


@kopf.on.create("services", field="spec.type", value="LoadBalancer")  # type: ignore[arg-type]
async def handle_loadbalancer_create(
    namespace: str,
    name: str,
    uid: str,
    **_: Any,
):
    """Handle creation of a LoadBalancer service."""
    v1 = k8s.CoreV1Api()
    patch_body: dict[str, Any] = {
        "metadata": {
            "annotations": {
                MANAGED_ANNOTATION: "true",
            },
        },
    }
    v1.patch_namespaced_service(name, namespace, patch_body)
    logger.info("Marked %s/%s as managed", namespace, name)

    # Read the service to get auto-assigned NodePorts
    svc = cast(k8s.V1Service, v1.read_namespaced_service(name, namespace))
    svc_ports: list[k8s.V1ServicePort] = getattr(svc.spec, "ports", [])
    cluster_ip: str = getattr(svc.spec, "cluster_ip", "")
    etp: str = getattr(svc.spec, "external_traffic_policy", "Cluster")

    ports = _build_port_mapping(svc_ports, cluster_ip, use_cluster_ip=etp == "Local")

    ds_name = nginx_backend.ensure_backend(namespace, name, ports, uid)
    ready_ips = get_ready_node_ips(ds_name)
    patch_service_status(namespace, name, ready_ips)


@kopf.on.delete(
    "services",
    field="spec.type",
    value="LoadBalancer",
    annotations={"bare-service-lb.prokube.ai/managed": "true"},
)  # type: ignore
async def handle_delete(
    namespace: str,
    name: str,
    meta: dict[str, Any],
    **_: Any,
) -> None:
    """Clean up DaemonSet and ConfigMap when the service is deleted."""
    annotations = meta.get("annotations", {})
    is_managed = annotations.get("bare-service-lb.prokube.ai/managed") == "true"
    if not is_managed:
        return

    logger.info("Cleaning up resources for deleted service %s/%s", namespace, name)
    nginx_backend.cleanup_backend(namespace, name)


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
        if not is_managed:
            continue
        svc_ns = svc.metadata.namespace
        svc_name = svc.metadata.name
        ds_name = DAEMONSET_PREFIX + safe_name(svc_ns, svc_name)
        try:
            ready_ips = get_ready_node_ips(ds_name)
            patch_service_status(svc_ns, svc_name, ready_ips)
        except ApiException:
            logger.exception(
                "Failed to update status for %s/%s",
                svc_ns,
                svc_name,
            )


# ---------------------------------------------------------------------------
# Node watcher — update ingress IPs when nodes change
# ---------------------------------------------------------------------------


@kopf.on.create("nodes")
@kopf.on.delete("nodes")  # type: ignore
async def handle_node_change(**_: Any) -> None:
    """When nodes are added/removed, re-evaluate all managed service statuses."""
    _refresh_all_managed_statuses()


# ---------------------------------------------------------------------------
# Pod watcher — update ingress IPs when DaemonSet pods become Ready
# ---------------------------------------------------------------------------


@kopf.on.event(
    "pods",
    labels={MANAGED_ANNOTATION: "true"},
    when=lambda namespace, **_: namespace == NAMESPACE,  # type: ignore
)  # type: ignore
async def handle_pod_event(
    event: dict[
        str, Any
    ],  # pylint: disable=unused-argument # event is required for the handler signature but not used
    meta: dict[str, Any],
    **_: Any,
) -> None:
    """When a managed DaemonSet pod changes, refresh the owning Service status.

    This is the key piece that makes the k3s-style status work: after
    ``handle_loadbalancer_create`` creates the DaemonSet, pods take a moment
    to schedule and become Ready.  This watcher picks up those transitions
    and patches the Service status with accurate node IPs.
    """
    labels = meta.get("labels", {})
    ds_label = labels.get("bare-service-lb.prokube.ai/daemonset", "")
    if not ds_label:
        return

    # Resolve the DaemonSet back to its owning Service via DaemonSet labels
    apps_v1 = k8s.AppsV1Api()
    try:
        ds = cast(k8s.V1DaemonSet, apps_v1.read_namespaced_daemon_set(ds_label, NAMESPACE))
    except ApiException:
        return  # DaemonSet may already be deleted

    ds_labels = getattr(ds.metadata, "labels", {})
    svc_ns = ds_labels.get("bare-service-lb.prokube.ai/service-namespace")
    svc_name = ds_labels.get("bare-service-lb.prokube.ai/service-name")
    if not svc_ns or not svc_name:
        return

    try:
        ready_ips = get_ready_node_ips(ds_label)
        patch_service_status(svc_ns, svc_name, ready_ips)
    except ApiException as e:
        if (
            e.status != 404
        ):  # DaemonSet might have been deleted because the service was deleted, in which case we can ignore the error
            logger.exception(
                "Failed to update status for %s/%s from pod event",
                svc_ns,
                svc_name,
            )
