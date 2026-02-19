"""Helpers: resource naming, node IP resolution, service status patching, namespace management."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import kubernetes
from kubernetes import client as k8s

from .constants import (
    EXTERNAL_IP_ANNOTATION,
    MANAGED_BY_LABEL,
    NAMESPACE,
)

logger = logging.getLogger("bare-service-lb")


def safe_name(namespace: str, name: str) -> str:
    """Derive a deterministic, DNS-safe resource name from a service reference.

    The result is used with prefixes like ``DAEMONSET_PREFIX`` (9 chars) and
    ``CONFIGMAP_PREFIX`` (13 chars) to build both Kubernetes resource names
    *and* label values.  Label values are limited to 63 characters, so with
    the longest prefix (13 chars) this function must return at most 50
    characters.

    When the raw ``namespace-name`` string exceeds that limit we truncate
    and append a short hash to preserve uniqueness.
    """
    # The longest prefix is CONFIGMAP_PREFIX (13 chars).
    # 63 (label limit) - 13 (prefix) = 50 chars max for the safe name.
    max_len = 50
    raw = f"{namespace}-{name}"
    if len(raw) <= max_len:
        return raw
    # Truncate and append an 8-char hash for uniqueness
    suffix = hashlib.sha256(raw.encode()).hexdigest()[:8]
    # max_len - 1 (hyphen) - 8 (hash) = 41 chars for truncated prefix
    truncated = raw[: max_len - 9].rstrip("-")
    return f"{truncated}-{suffix}"


def get_node_ip(node: k8s.V1Node) -> str | None:
    """Return the best available IP address for a single node.

    Resolution order:
      1. ``bare-service-lb.prokube.ai/external-ip`` annotation (user/tool provided)
      2. ``ExternalIP`` from node status addresses
      3. ``InternalIP`` from node status addresses
    """
    # 1. Check for operator annotation (set by external tooling)
    annotations = (node.metadata.annotations or {}) if node.metadata else {}
    annotated_ip = annotations.get(EXTERNAL_IP_ANNOTATION)
    if annotated_ip:
        return annotated_ip

    # 2. Fall back to node status addresses
    if not (node.status and node.status.addresses):
        return None
    external = None
    internal = None
    for addr in node.status.addresses:
        if addr.type == "ExternalIP":
            external = addr.address
        elif addr.type == "InternalIP":
            internal = addr.address
    return external or internal


def get_node_ips() -> list[str]:
    """Return the best available IP address for each node."""
    v1 = k8s.CoreV1Api()
    nodes = v1.list_node()
    ips: list[str] = []
    for node in nodes.items:
        ip = get_node_ip(node)
        if ip:
            ips.append(ip)
    return sorted(ips)


def get_ready_node_ips(ds_name: str) -> list[str]:
    """Return IPs only for nodes where the DaemonSet pod is Ready.

    This mirrors k3s ServiceLB behaviour: if a pod can't bind hostPort
    (e.g. port conflict) it never becomes Ready, so its node IP is excluded
    from the Service status.  A Service whose DaemonSet has zero ready pods
    will show no EXTERNAL-IP, making the conflict visible to the user.
    """
    v1 = k8s.CoreV1Api()
    pods = v1.list_namespaced_pod(
        namespace=NAMESPACE,
        label_selector=f"bare-service-lb.prokube.ai/daemonset={ds_name}",
    )

    ready_node_names: set[str] = set()
    for pod in pods.items:
        if not pod.spec.node_name or not pod.status:
            continue
        # Check that the pod is Running and all containers are Ready
        if pod.status.phase != "Running":
            continue
        conditions = pod.status.conditions or []
        is_ready = any(c.type == "Ready" and c.status == "True" for c in conditions)
        if is_ready:
            ready_node_names.add(pod.spec.node_name)

    if not ready_node_names:
        return []

    # Resolve node names to IPs
    nodes = v1.list_node()
    ips: list[str] = []
    for node in nodes.items:
        if node.metadata.name not in ready_node_names:
            continue
        ip = get_node_ip(node)
        if ip:
            ips.append(ip)
    return sorted(ips)


def patch_service_status(namespace: str, name: str, ips: list[str]) -> None:
    """Set .status.loadBalancer.ingress on the Service with node IPs."""
    v1 = k8s.CoreV1Api()
    ingress = [{"ip": ip} for ip in ips]
    body: dict[str, Any] = {"status": {"loadBalancer": {"ingress": ingress}}}
    v1.patch_namespaced_service_status(name, namespace, body)
    logger.info("Patched status on %s/%s with IPs: %s", namespace, name, ips)


def ensure_namespace() -> None:
    """Create the operator namespace if it doesn't exist."""
    v1 = k8s.CoreV1Api()
    try:
        v1.read_namespace(NAMESPACE)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            v1.create_namespace(
                k8s.V1Namespace(
                    metadata=k8s.V1ObjectMeta(
                        name=NAMESPACE,
                        labels={MANAGED_BY_LABEL: "bare-service-lb"},
                    )
                )
            )
            logger.info("Created namespace %s", NAMESPACE)
        else:
            raise
