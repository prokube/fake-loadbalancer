"""Klipper-lb backend: iptables/nftables DNAT via klipper-lb DaemonSet."""

from __future__ import annotations

from typing import Any

from kubernetes import client as k8s

from ..constants import (
    DAEMONSET_PREFIX,
    KLIPPER_IMAGE,
    MANAGED_BY_LABEL,
    NAMESPACE,
    OPERATOR_LABEL,
)
from ..utils import safe_name


def build_daemonset(
    svc_namespace: str,
    svc_name: str,
    ports: list[dict[str, Any]],
    owner_uid: str,
) -> k8s.V1DaemonSet:
    """Build a klipper-lb DaemonSet with iptables DNAT rules.

    Unlike the NGINX backend, klipper-lb:
      - Uses ``hostNetwork: true`` (iptables PREROUTING rules)
      - Needs ``NET_ADMIN`` capability
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
                OPERATOR_LABEL: "true",
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
                        OPERATOR_LABEL: "true",
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
                    host_network=True,
                    containers=containers,
                ),
            ),
        ),
    )
