"""
nginx-lb-operator: Converts LoadBalancer Services to hostPort-based NGINX TCP proxies.

Flow:
  1. User creates Service type=LoadBalancer (port 80, Kubernetes auto-assigns e.g. NodePort 30764)
  2. Operator marks it as managed (keeps type as LoadBalancer)
  3. Operator creates/updates a ConfigMap with NGINX stream config:
       stream { upstream backend { server 127.0.0.1:30764; } server { listen 80; proxy_pass backend; } }
  4. Operator creates/updates a DaemonSet running NGINX with hostPort on the original port
  5. Operator patches Service .status.loadBalancer.ingress with node IPs
  6. On delete, operator tears down the DaemonSet + ConfigMap
"""

from __future__ import annotations

import hashlib
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
OPERATOR_LABEL = "nginx-lb-operator.prokube.ai/managed"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
FINALIZER = "nginx-lb-operator.prokube.ai/cleanup"
EXTERNAL_IP_ANNOTATION = "nginx-lb-operator.prokube.ai/external-ip"
RECONCILED_ANNOTATION = "nginx-lb-operator.prokube.ai/reconciled"
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
        prefix="nginx-lb-operator.prokube.ai",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_name(namespace: str, name: str) -> str:
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
                "nginx-lb-operator.prokube.ai/service-namespace": svc_namespace,
                "nginx-lb-operator.prokube.ai/service-name": svc_name,
            },
            annotations={
                "nginx-lb-operator.prokube.ai/owner-uid": owner_uid,
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
                "nginx-lb-operator.prokube.ai/service-namespace": svc_namespace,
                "nginx-lb-operator.prokube.ai/service-name": svc_name,
            },
            annotations={
                "nginx-lb-operator.prokube.ai/owner-uid": owner_uid,
            },
        ),
        spec=k8s.V1DaemonSetSpec(
            selector=k8s.V1LabelSelector(
                match_labels={
                    "nginx-lb-operator.prokube.ai/daemonset": ds_name,
                },
            ),
            template=k8s.V1PodTemplateSpec(
                metadata=k8s.V1ObjectMeta(
                    labels={
                        "nginx-lb-operator.prokube.ai/daemonset": ds_name,
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


def _get_node_ip(node: k8s.V1Node) -> str | None:
    """Return the best available IP address for a single node.

    Resolution order:
      1. ``nginx-lb-operator.prokube.ai/external-ip`` annotation (user/tool provided)
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


def _get_node_ips() -> list[str]:
    """Return the best available IP address for each node."""
    v1 = k8s.CoreV1Api()
    nodes = v1.list_node()
    ips: list[str] = []
    for node in nodes.items:
        ip = _get_node_ip(node)
        if ip:
            ips.append(ip)
    return sorted(ips)


def _get_ready_node_ips(ds_name: str) -> list[str]:
    """Return IPs only for nodes where the DaemonSet pod is Ready.

    This mirrors k3s ServiceLB behaviour: if a pod can't bind hostPort
    (e.g. port conflict) it never becomes Ready, so its node IP is excluded
    from the Service status.  A Service whose DaemonSet has zero ready pods
    will show no EXTERNAL-IP, making the conflict visible to the user.
    """
    v1 = k8s.CoreV1Api()
    pods = v1.list_namespaced_pod(
        namespace=NAMESPACE,
        label_selector=f"nginx-lb-operator.prokube.ai/daemonset={ds_name}",
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
        ip = _get_node_ip(node)
        if ip:
            ips.append(ip)
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
    if annotations.get("nginx-lb-operator.prokube.ai/managed") != "true":
        patch_body: dict[str, Any] = {
            "metadata": {
                "annotations": {
                    "nginx-lb-operator.prokube.ai/managed": "true",
                },
            },
        }
        v1.patch_namespaced_service(name, namespace, patch_body)
        logger.info("Marked %s/%s as managed", namespace, name)

    # Read the service to get auto-assigned NodePorts
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
    # 6. Patch service status with node IPs (only where pods are Ready)
    # ------------------------------------------------------------------
    # Right after DaemonSet creation pods won't be Ready yet, so this will
    # typically set an empty ingress list.  The pod watcher below will
    # re-patch the status once pods come up.  This is the k3s ServiceLB
    # pattern: status only reflects nodes that are actually serving traffic.
    ready_ips = _get_ready_node_ips(ds_name)
    _patch_service_status(namespace, name, ready_ips)

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
    logger.info("Marked %s/%s as reconciled", namespace, name)

    return {
        "managed": True,
        "daemonset": f"{NAMESPACE}/{ds_name}",
        "configmap": f"{NAMESPACE}/{cm_name}",
        "ports": ports,
        "node_ips": ready_ips,
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
    is_managed = annotations.get("nginx-lb-operator.prokube.ai/managed") == "true"
    is_reconciled = annotations.get(RECONCILED_ANNOTATION) == "true"
    if not (is_managed or is_reconciled):
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
        is_managed = annotations.get("nginx-lb-operator.prokube.ai/managed") == "true"
        is_reconciled = annotations.get(RECONCILED_ANNOTATION) == "true"
        if not (is_managed or is_reconciled):
            continue
        svc_ns = svc.metadata.namespace
        svc_name = svc.metadata.name
        ds_name = DAEMONSET_PREFIX + _safe_name(svc_ns, svc_name)
        try:
            ready_ips = _get_ready_node_ips(ds_name)
            _patch_service_status(svc_ns, svc_name, ready_ips)
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
    ds_label = labels.get("nginx-lb-operator.prokube.ai/daemonset", "")
    if not ds_label:
        return

    # Resolve the DaemonSet back to its owning Service via DaemonSet labels
    apps_v1 = k8s.AppsV1Api()
    try:
        ds = apps_v1.read_namespaced_daemon_set(ds_label, NAMESPACE)
    except kubernetes.client.exceptions.ApiException:
        return  # DaemonSet may already be deleted

    ds_labels = ds.metadata.labels or {}
    svc_ns = ds_labels.get("nginx-lb-operator.prokube.ai/service-namespace")
    svc_name = ds_labels.get("nginx-lb-operator.prokube.ai/service-name")
    if not svc_ns or not svc_name:
        return

    try:
        ready_ips = _get_ready_node_ips(ds_label)
        _patch_service_status(svc_ns, svc_name, ready_ips)
    except kubernetes.client.exceptions.ApiException:
        logger.exception(
            "Failed to update status for %s/%s from pod event",
            svc_ns,
            svc_name,
        )
