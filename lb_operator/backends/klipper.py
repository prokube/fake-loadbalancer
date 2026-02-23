"""Klipper-lb backend: iptables/nftables DNAT via klipper-lb DaemonSet."""

from __future__ import annotations

from typing import Any

from kubernetes import client as k8s
from kubernetes.client.rest import ApiException

from ..constants import (
    DAEMONSET_PREFIX,
    KLIPPER_IMAGE,
    MANAGED_BY_LABEL,
    MANAGED_ANNOTATION,
)
from ..utils import NAMESPACE, safe_name


def build_daemonset(
    svc_namespace: str,
    svc_name: str,
    ports: list[dict[str, Any]],
    owner_uid: str,
) -> k8s.V1DaemonSet:
    """Build a klipper-lb DaemonSet with iptables DNAT rules.

    Like the NGINX backend, klipper-lb uses ``hostPort`` (not ``hostNetwork``)
    so that iptables rules created by the klipper-lb entry script live inside
    the pod's network namespace.  When the pod is terminated the namespace is
    destroyed and all rules are cleaned up automatically — no stale iptables
    on the host.  This matches the approach used by k3s ServiceLB.

    Key differences from the NGINX backend:
      - Needs ``NET_ADMIN`` capability (iptables inside the pod netns)
      - Takes one container per port (each handles one SRC_PORT/DEST_PORT pair)
      - Requires no ConfigMap — all configuration via environment variables
    """
    ds_name = DAEMONSET_PREFIX + safe_name(svc_namespace, svc_name)

    containers: list[k8s.V1Container] = []
    for p in ports:
        host_port = p["host_port"]
        upstream = p["upstream"]
        protocol = p.get("protocol", "TCP").upper()
        port_name = p.get("name", f"port-{host_port}")

        # Parse upstream "host:port" into dest_ip and dest_port
        upstream_host, upstream_port = upstream.rsplit(":", 1)

        containers.append(
            k8s.V1Container(
                name=f"lb-{port_name}"[:63],
                image=KLIPPER_IMAGE,
                ports=[
                    k8s.V1ContainerPort(
                        name=f"lb-{port_name}"[:15],
                        container_port=host_port,
                        host_port=host_port,
                        protocol=protocol,
                    ),
                ],
                env=[
                    k8s.V1EnvVar(name="SRC_PORT", value=str(host_port)),
                    k8s.V1EnvVar(name="DEST_PORT", value=upstream_port),
                    k8s.V1EnvVar(name="DEST_PROTO", value=protocol.lower()),
                    k8s.V1EnvVar(name="DEST_IPS", value=upstream_host),
                ],
                security_context=k8s.V1SecurityContext(
                    capabilities=k8s.V1Capabilities(add=["NET_ADMIN"]),
                ),
                resources=k8s.V1ResourceRequirements(
                    requests={"cpu": "10m", "memory": "16Mi"},
                    limits={"memory": "32Mi"},
                ),
            )
        )

    return k8s.V1DaemonSet(
        metadata=k8s.V1ObjectMeta(
            name=ds_name,
            namespace=NAMESPACE,
            labels={
                MANAGED_ANNOTATION: "true",
                MANAGED_BY_LABEL: "bare-service-lb",
                "bare-service-lb.prokube.ai/service-namespace": svc_namespace,
                "bare-service-lb.prokube.ai/service-name": svc_name,
                "bare-service-lb.prokube.ai/backend": "klipper",
            },
            annotations={
                "bare-service-lb.prokube.ai/owner-uid": owner_uid,
            },
        ),
        spec=k8s.V1DaemonSetSpec(
            selector=k8s.V1LabelSelector(
                match_labels={
                    "bare-service-lb.prokube.ai/daemonset": ds_name,
                },
            ),
            template=k8s.V1PodTemplateSpec(
                metadata=k8s.V1ObjectMeta(
                    labels={
                        "bare-service-lb.prokube.ai/daemonset": ds_name,
                        MANAGED_ANNOTATION: "true",
                    },
                ),
                spec=k8s.V1PodSpec(
                    tolerations=[
                        k8s.V1Toleration(
                            key="node-role.kubernetes.io/control-plane",
                            operator="Exists",
                            effect="NoSchedule",
                        ),
                        k8s.V1Toleration(
                            key="node-role.kubernetes.io/master",
                            operator="Exists",
                            effect="NoSchedule",
                        ),
                    ],
                    host_network=False,
                    containers=containers,
                ),
            ),
        ),
    )


def ensure_backend(
    svc_namespace: str,
    svc_name: str,
    ports: list[dict[str, Any]],
    owner_uid: str,
) -> str:
    """Ensure the klipper-lb DaemonSet exists and is up to date."""
    ds = build_daemonset(svc_namespace, svc_name, ports, owner_uid)
    ds_name, ds_namespace = getattr(ds.metadata, "name"), getattr(ds.metadata, "namespace")
    apps_v1 = k8s.AppsV1Api()
    try:
        apps_v1.read_namespaced_daemon_set(ds_name, ds_namespace)
    except ApiException as e:
        if e.status == 409: # Already exists
            apps_v1.replace_namespaced_daemon_set(ds_name, ds_namespace, ds)
        elif e.status == 404: # Not found
            apps_v1.create_namespaced_daemon_set(ds_namespace, ds)
        else:
            raise
    return ds_name

def cleanup_backend(svc_namespace: str, svc_name: str) -> None:
    """Delete the klipper-lb DaemonSet for the given service."""
    ds_name = DAEMONSET_PREFIX + safe_name(svc_namespace, svc_name)
    apps_v1 = k8s.AppsV1Api()
    try:
        apps_v1.delete_namespaced_daemon_set(ds_name, NAMESPACE)
    except ApiException as e:
        if e.status != 404: # Not found
            raise
