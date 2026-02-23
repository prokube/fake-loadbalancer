"""NGINX stream proxy backend: config generation, ConfigMap, and DaemonSet builders."""

from __future__ import annotations

import textwrap
from typing import Any

from kubernetes import client as k8s
from kubernetes.client.rest import ApiException

from ..constants import (
    CONFIGMAP_PREFIX,
    DAEMONSET_PREFIX,
    MANAGED_BY_LABEL,
    NGINX_IMAGE,
    MANAGED_ANNOTATION,
)
from ..utils import NAMESPACE, safe_name


# Top-level nginx.conf that includes the stream block
NGINX_CONF = textwrap.dedent("""\
    worker_processes auto;
    error_log /var/log/nginx/error.log warn;
    pid /tmp/nginx.pid;

    events {
        worker_connections 1024;
    }

    include /etc/nginx/stream.conf;
""")


def build_nginx_stream_config(ports: list[dict[str, Any]]) -> str:
    """Build an NGINX stream config that proxies hostPort -> backend.

    The upstream target is chosen based on the ``upstream`` key in each port
    mapping (set by the handler):
      - ``<ClusterIP>:<port>`` when the service has
        ``externalTrafficPolicy: Local`` (NodePort only listens on nodes
        with a local endpoint, so 127.0.0.1 would fail on other nodes).
      - ``127.0.0.1:<NodePort>`` for the default ``Cluster`` policy.
    """
    blocks: list[str] = []
    for p in ports:
        host_port = p["host_port"]
        upstream_target = p["upstream"]
        protocol = p.get("protocol", "TCP").upper()

        if protocol == "UDP":
            blocks.append(
                textwrap.dedent(f"""\
                upstream upstream_{host_port}_udp {{
                    server {upstream_target};
                }}
                server {{
                    listen {host_port} udp;
                    proxy_pass upstream_{host_port}_udp;
                    proxy_timeout 3s;
                    proxy_responses 1;
                }}
            """)
            )
        else:
            blocks.append(
                textwrap.dedent(f"""\
                upstream upstream_{host_port} {{
                    server {upstream_target};
                }}
                server {{
                    listen {host_port};
                    proxy_pass upstream_{host_port};
                }}
            """)
            )

    inner = textwrap.indent("".join(blocks), "    ")
    return f"stream {{\n{inner}}}\n"


def build_configmap(
    svc_namespace: str,
    svc_name: str,
    stream_conf: str,
    owner_uid: str,
) -> k8s.V1ConfigMap:
    """Build the ConfigMap that holds the NGINX stream config."""
    cm_name = CONFIGMAP_PREFIX + safe_name(svc_namespace, svc_name)
    return k8s.V1ConfigMap(
        metadata=k8s.V1ObjectMeta(
            name=cm_name,
            namespace=NAMESPACE,
            labels={
                MANAGED_ANNOTATION: "true",
                MANAGED_BY_LABEL: "bare-service-lb",
                "bare-service-lb.prokube.ai/service-namespace": svc_namespace,
                "bare-service-lb.prokube.ai/service-name": svc_name,
            },
            annotations={
                "bare-service-lb.prokube.ai/owner-uid": owner_uid,
            },
        ),
        data={
            "stream.conf": stream_conf,
            "nginx.conf": NGINX_CONF,
        },
    )


def build_daemonset(
    svc_namespace: str,
    svc_name: str,
    ports: list[dict[str, Any]],
    owner_uid: str,
) -> k8s.V1DaemonSet:
    """Build the NGINX DaemonSet with hostPort mappings."""
    ds_name = DAEMONSET_PREFIX + safe_name(svc_namespace, svc_name)
    cm_name = CONFIGMAP_PREFIX + safe_name(svc_namespace, svc_name)

    container_ports: list[k8s.V1ContainerPort] = []
    for p in ports:
        protocol = p.get("protocol", "TCP").upper()
        container_ports.append(
            k8s.V1ContainerPort(
                name=f"port-{p['host_port']}-{protocol.lower()}"[:15],
                container_port=p["host_port"],
                host_port=p["host_port"],
                protocol=protocol,
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
                    # Tolerate control-plane nodes so LB is available on all nodes
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
                    # Use host network so NGINX can reach 127.0.0.1:<NodePort>
                    # (kube-proxy binds NodePorts in the host netns).  Without
                    # this, some CNI/interface combinations silently fail when
                    # traffic arrives on secondary interfaces (e.g. lima0).
                    # hostPort declarations on the container are redundant with
                    # hostNetwork but kept for documentation / port awareness.
                    host_network=True,
                    containers=[
                        k8s.V1Container(
                            name="nginx",
                            image=NGINX_IMAGE,
                            ports=container_ports,
                            volume_mounts=[
                                k8s.V1VolumeMount(
                                    name="stream-config",
                                    mount_path="/etc/nginx/stream.conf",
                                    sub_path="stream.conf",
                                    read_only=True,
                                ),
                                k8s.V1VolumeMount(
                                    name="main-config",
                                    mount_path="/etc/nginx/nginx.conf",
                                    sub_path="nginx.conf",
                                    read_only=True,
                                ),
                            ],
                            resources=k8s.V1ResourceRequirements(
                                requests={"cpu": "10m", "memory": "32Mi"},
                                limits={"memory": "64Mi"},
                            ),
                            liveness_probe=k8s.V1Probe(
                                _exec=k8s.V1ExecAction(
                                    command=["nginx", "-t"],
                                ),
                                initial_delay_seconds=5,
                                period_seconds=30,
                            ),
                        ),
                    ],
                    volumes=[
                        k8s.V1Volume(
                            name="stream-config",
                            config_map=k8s.V1ConfigMapVolumeSource(
                                name=cm_name,
                            ),
                        ),
                        k8s.V1Volume(
                            name="main-config",
                            config_map=k8s.V1ConfigMapVolumeSource(
                                name=cm_name,
                                items=[
                                    k8s.V1KeyToPath(
                                        key="nginx.conf",
                                        path="nginx.conf",
                                    ),
                                ],
                            ),
                        ),
                    ],
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
    """Ensure the NGINX backend resources (ConfigMap and DaemonSet) exist and are up to date for the given service."""
    cm = build_configmap(
        svc_namespace, svc_name, build_nginx_stream_config(ports), owner_uid
    )
    ds = build_daemonset(svc_namespace, svc_name, ports, owner_uid)
    cm_name, cm_namespace = (
        getattr(cm.metadata, "name"),
        getattr(cm.metadata, "namespace"),
    )
    ds_name, ds_namespace = (
        getattr(ds.metadata, "name"),
        getattr(ds.metadata, "namespace"),
    )
    apps_v1 = k8s.AppsV1Api()
    core_v1 = k8s.CoreV1Api()

    try:
        core_v1.read_namespaced_config_map(cm_name, cm_namespace)
    except ApiException as e:
        if e.status == 409:  # Already exists
            core_v1.replace_namespaced_config_map(cm_name, cm_namespace, cm)
        elif e.status == 404:  # Not found
            core_v1.create_namespaced_config_map(cm_namespace, cm)
        else:
            raise

    try:
        apps_v1.read_namespaced_daemon_set(ds_name, ds_namespace)
    except ApiException as e:
        if e.status == 409:  # Already exists
            apps_v1.replace_namespaced_daemon_set(ds_name, ds_namespace, ds)
        elif e.status == 404:  # Not found
            apps_v1.create_namespaced_daemon_set(ds_namespace, ds)
        else:
            raise
    return ds_name


def cleanup_backend(svc_namespace: str, svc_name: str) -> None:
    """Delete the NGINX backend resources (ConfigMap and DaemonSet) for the given service."""
    cm_name = CONFIGMAP_PREFIX + safe_name(svc_namespace, svc_name)
    ds_name = DAEMONSET_PREFIX + safe_name(svc_namespace, svc_name)
    apps_v1 = k8s.AppsV1Api()
    core_v1 = k8s.CoreV1Api()

    try:
        apps_v1.delete_namespaced_daemon_set(ds_name, NAMESPACE)
    except ApiException as e:
        if e.status != 404:  # Ignore not found
            raise

    try:
        core_v1.delete_namespaced_config_map(cm_name, NAMESPACE)
    except ApiException as e:
        if e.status != 404:  # Ignore not found
            raise
