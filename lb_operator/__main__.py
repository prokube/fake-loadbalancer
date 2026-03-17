"""Main entry point for the Load Balancer Operator."""

import logging
import os
from typing import Any, cast

import kopf
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

# Get log level from environment variable, default to INFO if not set or invalid
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("bare-service-lb")

# Suppress aiohttp access logs to avoid cluttering the output
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def patch_loadbalancer(name: str, namespace: str, ips: list[str]) -> None:
    """Patch the LoadBalancer service with the external IPs from the nodes."""
    v1 = k8s.CoreV1Api()
    patch_body: dict[str, Any] = {
        "metadata": {
            "annotations": {
                "app.kubernetes.io/managed-by": "bare-service-lb",
            },
        },
        "status": {
            "loadBalancer": {
                "ingress": [{"ip": ip, "ipMode": "VIP"} for ip in ips],
            },
        },
    }
    v1.patch_namespaced_service_status(name, namespace, patch_body)
    logger.info("Reconciled %s/%s ", namespace, name)


async def get_node_ips() -> list[str]:
    """Get the internal IPs of all nodes in the cluster."""
    v1 = k8s.CoreV1Api()
    nodes = cast(k8s.V1NodeList, v1.list_node())
    ips: list[str] = []
    if not nodes.items:
        raise kopf.TemporaryError("No nodes available to assign external IP", delay=30)
    for node in nodes.items:
        node = cast(k8s.V1Node, node)
        node_status = cast(k8s.V1NodeStatus, node.status)
        if node_status and node_status.addresses:
            for addr in node_status.addresses:
                addr = cast(k8s.V1NodeAddress, addr)
                if addr.type == "InternalIP" or addr.type == "ExternalIP":
                    ips.append(getattr(addr, "address"))
    return ips


@kopf.on.create("services", field="spec.type", value="LoadBalancer")  # type: ignore[arg-type]
async def handle_loadbalancer_create(
    name: str,
    namespace: str,
    **_: Any,
):
    """Handle creation of a LoadBalancer service."""
    try:
        ips = await get_node_ips()
        await patch_loadbalancer(name, namespace, ips)
    except ApiException as e:
        raise kopf.TemporaryError(f"Failed to patch service {namespace}/{name}: {e}") from e


@kopf.on.create("nodes", field="status.addresses")
@kopf.on.update("nodes", field="status.addresses")
@kopf.on.resume("nodes", field="status.addresses")
@kopf.on.delete("nodes", field="status.addresses")  # type: ignore[arg-type]
async def handle_node_event(
    name: str,
    **_: Any,
):
    """Handle node events to update LoadBalancer services with new IPs."""
    try:
        svcs = k8s.CoreV1Api().list_service_for_all_namespaces(
            field_selector="spec.type=LoadBalancer"
        )
        ips = await get_node_ips()
        for svc in getattr(svcs, "items"):
            name = getattr(getattr(svc, "metadata"), "name")
            namespace = getattr(getattr(svc, "metadata"), "namespace")
            await patch_loadbalancer(name, namespace, ips)
    except ApiException as e:
        raise kopf.TemporaryError(f"Failed to patch services on node event: {e}") from e

# Entry point for the operator
if __name__ == "__main__":
    kopf.run(
        standalone=True,
        clusterwide=True,
        liveness_endpoint="http://0.0.0.0:8080/healthz",
    )
