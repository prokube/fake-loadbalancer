"""
nginx-lb-operator: Converts LoadBalancer Services to hostPort-based NGINX TCP proxies.

Flow:
  1. User creates Service type=LoadBalancer (port 80)
  2. Operator patches it to type=NodePort (Kubernetes assigns e.g. 30764)
  3. Operator creates/updates a ConfigMap with NGINX stream config:
       stream { upstream backend { server 127.0.0.1:30764; } server { listen 80; proxy_pass backend; } }
  4. Operator creates/updates a DaemonSet running NGINX with hostPort on the original port
  5. Operator patches Service .status.loadBalancer.ingress with node IPs
  6. On delete, operator tears down the DaemonSet + ConfigMap and (optionally) reverts the Service
"""

from __future__ import annotations

import logging
import textwrap
from typing import Any

import kopf
import kubernetes
from kubernetes import client as k8s

logger = logging.getLogger("nginx-lb-operator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OPERATOR_LABEL = "nginx-lb-operator.io/managed"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
SERVICE_ANNOTATION = "nginx-lb-operator.io/original-type"
FINALIZER = "nginx-lb-operator.io/cleanup"
NGINX_IMAGE = "nginx:1.27-alpine"
DAEMONSET_PREFIX = "nginx-lb-"
CONFIGMAP_PREFIX = "nginx-lb-cfg-"
NAMESPACE = "nginx-lb-operator"  # namespace where DaemonSets/ConfigMaps live

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    """Operator-wide settings."""
    settings.posting.level = logging.WARNING
    settings.persistence.finalizer = FINALIZER
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix="nginx-lb-operator.io",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_name(namespace: str, name: str) -> str:
    """Derive a deterministic, DNS-safe resource name from a service reference."""
    raw = f"{namespace}-{name}"
    # Kubernetes names max 253 chars; truncate and strip trailing hyphens
    safe = raw[:245].rstrip("-")
    return safe


def _build_nginx_config(ports: list[dict[str, Any]]) -> str:
    """Build an NGINX stream config that proxies hostPort -> NodePort on localhost."""
    blocks: list[str] = []
    for p in ports:
        host_port = p["host_port"]
        node_port = p["node_port"]
        protocol = p.get("protocol", "TCP").upper()

        if protocol == "UDP":
            blocks.append(
                textwrap.dedent(f"""\
                upstream upstream_{host_port}_udp {{
                    server 127.0.0.1:{node_port};
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
                    server 127.0.0.1:{node_port};
                }}
                server {{
                    listen {host_port};
                    proxy_pass upstream_{host_port};
                }}
            """)
            )

    inner = textwrap.indent("".join(blocks), "    ")
    return f"stream {{\n{inner}}}\n"


def _build_configmap(
    svc_namespace: str,
    svc_name: str,
    nginx_conf: str,
    owner_uid: str,
) -> k8s.V1ConfigMap:
    """Build the ConfigMap that holds the NGINX stream config."""
    cm_name = CONFIGMAP_PREFIX + _safe_name(svc_namespace, svc_name)
    return k8s.V1ConfigMap(
        metadata=k8s.V1ObjectMeta(
            name=cm_name,
            namespace=NAMESPACE,
            labels={
                OPERATOR_LABEL: "true",
                MANAGED_BY_LABEL: "nginx-lb-operator",
                "nginx-lb-operator.io/service-namespace": svc_namespace,
                "nginx-lb-operator.io/service-name": svc_name,
            },
            annotations={
                "nginx-lb-operator.io/owner-uid": owner_uid,
            },
        ),
        data={
            "stream.conf": nginx_conf,
        },
    )


def _build_daemonset(
    svc_namespace: str,
    svc_name: str,
    ports: list[dict[str, Any]],
    owner_uid: str,
) -> k8s.V1DaemonSet:
    """Build the NGINX DaemonSet with hostPort mappings."""
    ds_name = DAEMONSET_PREFIX + _safe_name(svc_namespace, svc_name)
    cm_name = CONFIGMAP_PREFIX + _safe_name(svc_namespace, svc_name)

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
                OPERATOR_LABEL: "true",
                MANAGED_BY_LABEL: "nginx-lb-operator",
                "nginx-lb-operator.io/service-namespace": svc_namespace,
                "nginx-lb-operator.io/service-name": svc_name,
            },
            annotations={
                "nginx-lb-operator.io/owner-uid": owner_uid,
            },
        ),
        spec=k8s.V1DaemonSetSpec(
            selector=k8s.V1LabelSelector(
                match_labels={
                    "nginx-lb-operator.io/daemonset": ds_name,
                },
            ),
            template=k8s.V1PodTemplateSpec(
                metadata=k8s.V1ObjectMeta(
                    labels={
                        "nginx-lb-operator.io/daemonset": ds_name,
                        OPERATOR_LABEL: "true",
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
                    # Use host network: false — we rely on hostPort instead
                    host_network=False,
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


def _get_node_ips() -> list[str]:
    """Return list of InternalIP addresses for all Ready nodes."""
    v1 = k8s.CoreV1Api()
    nodes = v1.list_node()
    ips: list[str] = []
    for node in nodes.items:
        if node.status and node.status.addresses:
            for addr in node.status.addresses:
                if addr.type == "InternalIP":
                    ips.append(addr.address)
                    break
    return sorted(ips)


def _patch_service_status(namespace: str, name: str, ips: list[str]) -> None:
    """Set .status.loadBalancer.ingress on the Service with node IPs."""
    v1 = k8s.CoreV1Api()
    ingress = [{"ip": ip} for ip in ips]
    body = {"status": {"loadBalancer": {"ingress": ingress}}}
    v1.patch_namespaced_service_status(name, namespace, body)
    logger.info("Patched status on %s/%s with IPs: %s", namespace, name, ips)


def _ensure_namespace() -> None:
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
                        labels={MANAGED_BY_LABEL: "nginx-lb-operator"},
                    )
                )
            )
            logger.info("Created namespace %s", NAMESPACE)
        else:
            raise


# ---------------------------------------------------------------------------
# Handlers
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
      1. Patch it to NodePort
      2. Build NGINX config
      3. Deploy ConfigMap + DaemonSet
      4. Patch status with node IPs
    """
    # Skip services we didn't create or that are already managed
    if meta.get("labels", {}).get(OPERATOR_LABEL) == "true":
        logger.debug("Skipping already-managed service %s/%s", namespace, name)
        return {"managed": False}

    # Skip if the service has an annotation saying the operator already processed it
    # (the update handler would re-fire after we patch type to NodePort, but the field
    # filter above prevents that because spec.type will no longer be LoadBalancer)

    logger.info("Processing LoadBalancer service %s/%s", namespace, name)

    v1 = k8s.CoreV1Api()
    apps_v1 = k8s.AppsV1Api()

    # ------------------------------------------------------------------
    # 1. Read current service to get the latest resourceVersion
    # ------------------------------------------------------------------
    svc = v1.read_namespaced_service(name, namespace)
    svc_ports = svc.spec.ports

    # ------------------------------------------------------------------
    # 2. Patch service to NodePort and annotate it
    # ------------------------------------------------------------------
    patch_body: dict[str, Any] = {
        "metadata": {
            "annotations": {
                SERVICE_ANNOTATION: "LoadBalancer",
                "nginx-lb-operator.io/managed": "true",
            },
        },
        "spec": {
            "type": "NodePort",
        },
    }
    v1.patch_namespaced_service(name, namespace, patch_body)
    logger.info("Patched %s/%s to NodePort", namespace, name)

    # Re-read to get assigned NodePorts
    svc = v1.read_namespaced_service(name, namespace)
    svc_ports = svc.spec.ports

    # ------------------------------------------------------------------
    # 3. Build port mapping
    # ------------------------------------------------------------------
    ports: list[dict[str, Any]] = []
    for p in svc_ports:
        ports.append(
            {
                "host_port": p.port,
                "node_port": p.node_port,
                "protocol": p.protocol or "TCP",
                "name": p.name or f"port-{p.port}",
            }
        )

    # ------------------------------------------------------------------
    # 4. Generate NGINX config and create/update ConfigMap
    # ------------------------------------------------------------------
    _ensure_namespace()

    nginx_stream_conf = _build_nginx_config(ports)
    # Also generate a top-level nginx.conf that includes the stream block
    nginx_main_conf = textwrap.dedent("""\
        worker_processes auto;
        error_log /var/log/nginx/error.log warn;
        pid /tmp/nginx.pid;

        events {
            worker_connections 1024;
        }

        include /etc/nginx/stream.conf;
    """)

    cm = _build_configmap(namespace, name, nginx_stream_conf, uid)
    cm.data["nginx.conf"] = nginx_main_conf
    cm_name = cm.metadata.name

    try:
        apps_v1_core = k8s.CoreV1Api()
        apps_v1_core.read_namespaced_config_map(cm_name, NAMESPACE)
        apps_v1_core.replace_namespaced_config_map(cm_name, NAMESPACE, cm)
        logger.info("Updated ConfigMap %s/%s", NAMESPACE, cm_name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            k8s.CoreV1Api().create_namespaced_config_map(NAMESPACE, cm)
            logger.info("Created ConfigMap %s/%s", NAMESPACE, cm_name)
        else:
            raise

    # ------------------------------------------------------------------
    # 5. Create/update DaemonSet
    # ------------------------------------------------------------------
    ds = _build_daemonset(namespace, name, ports, uid)
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
    # 6. Patch service status with node IPs
    # ------------------------------------------------------------------
    node_ips = _get_node_ips()
    _patch_service_status(namespace, name, node_ips)

    return {
        "managed": True,
        "daemonset": f"{NAMESPACE}/{ds_name}",
        "configmap": f"{NAMESPACE}/{cm_name}",
        "ports": ports,
        "node_ips": node_ips,
    }


@kopf.on.delete("services")
async def handle_delete(
    namespace: str,
    name: str,
    meta: dict[str, Any],
    **_: Any,
) -> None:
    """Clean up DaemonSet and ConfigMap when the service is deleted."""
    annotations = meta.get("annotations", {})
    if annotations.get("nginx-lb-operator.io/managed") != "true":
        return

    logger.info("Cleaning up resources for deleted service %s/%s", namespace, name)

    apps_v1 = k8s.AppsV1Api()
    v1 = k8s.CoreV1Api()

    ds_name = DAEMONSET_PREFIX + _safe_name(namespace, name)
    cm_name = CONFIGMAP_PREFIX + _safe_name(namespace, name)

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
# Node watcher — update ingress IPs when nodes change
# ---------------------------------------------------------------------------


@kopf.on.create("nodes")
@kopf.on.delete("nodes")
async def handle_node_change(**_: Any) -> None:
    """When nodes are added/removed, update all managed service statuses."""
    v1 = k8s.CoreV1Api()
    node_ips = _get_node_ips()

    # Find all services we manage (across all namespaces)
    all_services = v1.list_service_for_all_namespaces()
    for svc in all_services.items:
        annotations = svc.metadata.annotations or {}
        if annotations.get("nginx-lb-operator.io/managed") == "true":
            try:
                _patch_service_status(
                    svc.metadata.namespace,
                    svc.metadata.name,
                    node_ips,
                )
            except kubernetes.client.exceptions.ApiException:
                logger.exception(
                    "Failed to update status for %s/%s",
                    svc.metadata.namespace,
                    svc.metadata.name,
                )
