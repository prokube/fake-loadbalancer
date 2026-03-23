"""Main entry point for the Load Balancer Operator."""

import logging
import os
from typing import Any

import kopf
from kubernetes import client as k8s, config
from kubernetes.client.exceptions import ApiException


def get_log_level() -> int:
    """Get the log level from the environment variable, defaulting to INFO."""
    level_str = os.getenv("BSL_LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_str, logging.INFO)


logging.basicConfig(
    level=get_log_level(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("lb-operator")


async def get_node_ips() -> list[str]:
    """Get the IP addresses of all nodes in the cluster."""
    v1 = k8s.CoreV1Api()
    try:
        nodes = v1.list_node()
    except ApiException as e:
        logger.error(f"Failed to list nodes: {e}")
        return []
    ips = []
    for node in nodes.items:
        for addr in node.status.addresses or []:
            if addr.type == "InternalIP":
                ips.append(addr.address)
    return ips


async def set_loadbalancer_status(namespace: str, name: str):
    """Set the status of the LoadBalancer service with the given IPs."""
    ips = await get_node_ips()
    if not ips:
        logger.warning(f"No node IPs found, cannot set LoadBalancer status for {namespace}/{name}")
        return
    v1 = k8s.CoreV1Api()
    status = k8s.V1ServiceStatus(
        load_balancer=k8s.V1LoadBalancerStatus(
            ingress=[k8s.V1LoadBalancerIngress(ip=ip) for ip in ips]
        )
    )
    try:
        v1.patch_namespaced_service_status(name=name, namespace=namespace, body={"status": status})
        logger.info(f"Updated status of LoadBalancer service {namespace}/{name} with IPs: {ips}")
    except ApiException as e:
        logger.error(f"Failed to update status of service {namespace}/{name}: {e}")


@kopf.on.create("services", field="spec.type", value="LoadBalancer")  # type: ignore[arg-type]
async def handle_loadbalancer_create(
    namespace: str,
    name: str,
    **_: Any,
):
    await set_loadbalancer_status(namespace, name)


@kopf.on.update("nodes", field="status.addresses")  # type: ignore[arg-type]
async def handle_node_update(
    **_: Any,
):
    # When node IPs change, we need to update the status of all LoadBalancer services
    v1 = k8s.CoreV1Api()
    try:
        services = v1.list_service_for_all_namespaces(field_selector="spec.type=LoadBalancer")
    except ApiException as e:
        logger.error(f"Failed to list services: {e}")
        return
    for svc in services.items:
        await set_loadbalancer_status(svc.metadata.namespace, svc.metadata.name)


def main():
    try:
        config.load_config()
    except config.ConfigException:
        logger.critical(
            "Failed to load Kubernetes configuration. Ensure that the operator is running in a Kubernetes environment or that kubeconfig is properly set."
        )
        exit(1)

    kopf.run(
        standalone=True,
        clusterwide=True,
        liveness_endpoint="http://0.0.0.0:8080/healthz",
    )


if __name__ == "__main__":
    main()
