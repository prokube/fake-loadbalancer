# AGENTS.md — bare-service-lb

## Project Overview

**bare-service-lb** is a Kubernetes operator that provides `LoadBalancer`-type Service support on bare-metal clusters, cloud VMs, and anywhere without a native cloud load balancer. Inspired by k3s ServiceLB (klipper-lb), it deploys NGINX DaemonSets with `hostPort` to make services reachable on every node at the original port.

**Language:** Python 3.11+ (single-file operator)
**Framework:** [Kopf](https://kopf.readthedocs.io/) (Kubernetes Operator Pythonic Framework)
**K8s client:** `kubernetes` Python SDK

## Architecture at a Glance

```
User creates Service type=LoadBalancer (port 80)
  → K8s auto-assigns NodePort (e.g. 30764)
  → Operator creates ConfigMap with NGINX stream config
  → Operator creates DaemonSet (NGINX with hostPort:80 → upstream 127.0.0.1:30764)
  → Operator patches Service .status.loadBalancer.ingress with node IPs
  → On delete: tears down DaemonSet + ConfigMap
```

All operator-managed resources (DaemonSets, ConfigMaps) live in the `bare-service-lb` namespace.

## File Map

| Path | What it is | When to look here |
|------|-----------|-------------------|
| `lb_operator.py` | **The entire operator logic** (718 lines). All handlers, helpers, K8s interactions. | Any bug or feature — this is the only source file. |
| `pyproject.toml` | Dependencies (`kopf`, `kubernetes`, `pyyaml`), dev tools (`ruff`, `mypy`, `pytest`). | Dependency issues, version constraints, lint/type config. |
| `Dockerfile` | Multi-stage build using `uv`. Entrypoint runs `kopf run`. | Build failures, image issues. |
| `helm/` | Helm chart for deployment (RBAC, Deployment, ServiceAccount, ClusterKopfPeering CRD). | RBAC permission errors, deployment config, leader election issues. |
| `helm/values.yaml` | Default Helm values (image, replicas, resources, probes, env vars). | Runtime configuration questions. |
| `helm/templates/clusterrole.yaml` | All RBAC permissions the operator needs. | 403 Forbidden errors from the K8s API. |
| `helm/templates/peering.yaml` | ClusterKopfPeering CRD + instance for leader election. | Leader election / HA issues. |
| `helm/templates/deployment.yaml` | Operator Deployment spec. | Pod scheduling, resource, or probe issues. |
| `examples/sample-service.yaml` | Example LoadBalancer Service + Deployment for testing. | Quick test reference. |
| `gcp-external-ip-annotator/` | Optional sidecar DaemonSet that annotates nodes with GCP external IPs. | GCP-specific external IP problems. |
| `.github/workflows/` | CI: build+push container image and Helm chart to GHCR. | CI/CD issues. |

## Key Concepts

### Single-File Operator
All logic is in `lb_operator.py`. There are no models, no separate modules, no test files. When debugging, start (and likely finish) here.

### Kopf Handler Decorators
The operator uses Kopf decorators to register event handlers:

- **`@kopf.on.create/update/resume("services", field="spec.type", value="LoadBalancer")`** → `handle_loadbalancer()` (line 408-572): Main reconciliation loop. Creates/updates ConfigMap + DaemonSet, patches Service status.
- **`@kopf.on.delete("services", annotations={...})`** → `handle_delete()` (line 575-620): Cleanup on Service deletion.
- **`@kopf.on.create/delete("nodes")`** → `handle_node_change()` (line 662-666): Refreshes all managed Service statuses when nodes change.
- **`@kopf.on.event("pods", labels={...})`** → `handle_pod_event()` (line 674-717): Watches DaemonSet pods for readiness changes, updates Service status with accurate IPs.
- **`@kopf.on.startup()`** → `configure()` (line 48-63): Sets operator settings including leader election.

### Resource Naming Convention
- DaemonSet: `bsl-<namespace>-<service-name>` (prefix: `DAEMONSET_PREFIX = "bsl-"`)
- ConfigMap: `bsl-cfg-<namespace>-<service-name>` (prefix: `CONFIGMAP_PREFIX = "bsl-cfg-"`)
- Long names are truncated with a SHA256 hash suffix via `_safe_name()` (line 71-93) to stay within the 63-char Kubernetes label limit.

### Annotations Used
| Annotation | On | Purpose |
|-----------|-----|---------|
| `bare-service-lb.prokube.ai/managed` | Service | Marks a Service as owned by this operator |
| `bare-service-lb.prokube.ai/reconciled` | Service | Set after full reconciliation; prevents re-processing loops |
| `bare-service-lb.prokube.ai/external-ip` | Node | User/tool-provided external IP (overrides node status addresses) |
| `bare-service-lb.prokube.ai/owner-uid` | ConfigMap, DaemonSet | Links managed resources back to the owning Service |

### Labels Used
| Label | Purpose |
|-------|---------|
| `bare-service-lb.prokube.ai/managed=true` | Identifies operator-managed resources |
| `app.kubernetes.io/managed-by=bare-service-lb` | Standard K8s managed-by label |
| `bare-service-lb.prokube.ai/daemonset=<name>` | Pod selector for DaemonSets; used to track readiness |
| `bare-service-lb.prokube.ai/service-namespace` | On DaemonSet/ConfigMap, traces back to source Service |
| `bare-service-lb.prokube.ai/service-name` | On DaemonSet/ConfigMap, traces back to source Service |

### externalTrafficPolicy Handling
- **`Cluster` (default):** NGINX upstream targets `127.0.0.1:<NodePort>` (works on every node).
- **`Local`:** NGINX upstream targets `<ClusterIP>:<port>` instead, because NodePort only listens on nodes with a local endpoint. This logic is in `handle_loadbalancer()` around line 466-480.

### Status Reporting (IP Selection)
Service `.status.loadBalancer.ingress` only includes IPs of nodes where the DaemonSet pod is **Running and Ready** (`_get_ready_node_ips()`, line 333-371). This means:
- Port conflicts cause pods to fail → those node IPs are excluded → visible to user.
- Freshly created DaemonSets show empty EXTERNAL-IP until pods start → `handle_pod_event()` patches status once pods become Ready.

### Node IP Resolution Order (`_get_node_ip()`, line 294-318)
1. `bare-service-lb.prokube.ai/external-ip` annotation (user/tool provided)
2. `ExternalIP` from node status
3. `InternalIP` from node status

### Leader Election / HA
- Uses Kopf's built-in peering mechanism via `ClusterKopfPeering` CRD.
- Configured in `configure()` (line 57-63) and deployed via `helm/templates/peering.yaml`.
- Lease name configurable via `LEADER_ELECTION_LEASE_NAME` env var (default: `bare-service-lb-leader`).
- `settings.peering.mandatory = True` means the operator **will not run** if peering is unavailable.

## Debugging Guide

### "Service stuck with no EXTERNAL-IP"
1. Check if DaemonSet pods are running: `kubectl -n bare-service-lb get pods`
2. If pods are in CrashLoopBackOff or Pending, check for **port conflicts** (another process or DaemonSet using the same hostPort).
3. Check the operator logs: `kubectl -n bare-service-lb logs deploy/bare-service-lb`
4. Look for the `reconciled` annotation on the Service — if missing, the handler may have failed partway through.
5. Code path: `handle_loadbalancer()` → `_get_ready_node_ips()` → `_patch_service_status()`

### "Service shows wrong IPs"
- Check node IP resolution: `_get_node_ip()` (line 294). Annotation overrides status.
- For GCP: check if `gcp-external-ip-annotator` DaemonSet is running and annotations exist on nodes.
- Status is only refreshed on pod events (`handle_pod_event()`) or node create/delete (`handle_node_change()`). Node IP changes (without add/remove) won't auto-update.

### "Operator keeps re-processing the same Service"
- The `RECONCILED_ANNOTATION` (`bare-service-lb.prokube.ai/reconciled`) gates the skip check at the top of `handle_loadbalancer()` (line 432-434).
- If reconciliation fails before reaching the annotation-set step (line 559-563), it will retry — this is by design.
- Kopf also stores handler state in annotations (prefix `bare-service-lb.prokube.ai`), which can trigger update events. This is the standard Kopf pattern.

### "RBAC / 403 Forbidden errors"
- All required permissions are in `helm/templates/clusterrole.yaml`.
- The operator needs: services (get/list/watch/patch/update), services/status (patch/update), nodes (get/list/watch/patch), configmaps + daemonsets (full CRUD), events, leases, namespaces (get/create), CRDs (get/list/watch).
- Kopf patches nodes for its internal handler annotations, so `patch` on nodes is required.

### "Leader election not working"
- Ensure `ClusterKopfPeering` CRD and instance exist: `kubectl get clusterkopfpeering`
- Check that `LEADER_ELECTION_LEASE_NAME` env var matches the peering resource name.
- `settings.peering.mandatory = True` means the operator halts if peering fails.
- Inspect: `kubectl get clusterkopfpeering bare-service-lb-leader -o yaml`

### "DaemonSet or ConfigMap not being cleaned up"
- Cleanup handler (`handle_delete()`, line 575) fires on Service deletion for services with the `managed=true` annotation.
- The Kopf finalizer (`bare-service-lb.prokube.ai/cleanup`) must be present on the Service for cleanup to run.
- If the finalizer was removed manually, cleanup won't happen. Delete orphaned resources manually from the `bare-service-lb` namespace.

### "NGINX config issues"
- ConfigMap holds two keys: `stream.conf` (generated by `_build_nginx_config()`, line 96) and `nginx.conf` (static main config, line 498-508).
- The NGINX container mounts both via `subPath`. Changes to the ConfigMap require a pod restart (no config reload mechanism).
- UDP ports use `listen <port> udp;` with `proxy_timeout 3s; proxy_responses 1;`.

## Development Commands

```bash
# Install dev dependencies
uv sync --dev           # or: pip install -e ".[dev]"

# Run operator locally (requires kubeconfig)
kopf run lb_operator.py --all-namespaces --verbose

# Lint
ruff check lb_operator.py

# Type check
mypy lb_operator.py

# Format
ruff format lb_operator.py

# Build container image
docker build -t bare-service-lb:latest .

# For MicroK8s
docker save bare-service-lb:latest | microk8s ctr image import -

# Deploy via Helm
helm install bare-service-lb ./helm --namespace bare-service-lb --create-namespace
```

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `kopf` | >=1.37, <2 | Kubernetes operator framework (decorators, event loop, leader election) |
| `kubernetes` | >=31, <33 | Official Kubernetes Python client |
| `pyyaml` | >=6, <7 | YAML parsing (kopf dependency) |

Dev-only: `pytest`, `pytest-asyncio`, `ruff`, `mypy`

## CI/CD

- `.github/workflows/bare-service-lb.yaml` — Builds and pushes container image to `ghcr.io/prokube/bare-service-lb` on version tags (`v*`).
- `.github/workflows/bare-service-lb-dev.yaml` — Dev image builds (likely on branches/PRs).
- `.github/workflows/helm-push-main.yaml` / `helm-push-dev.yaml` — Helm chart OCI push to GHCR.

## Important Constants (lb_operator.py, lines 34-42)

```python
OPERATOR_LABEL = "bare-service-lb.prokube.ai/managed"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
FINALIZER = "bare-service-lb.prokube.ai/cleanup"
EXTERNAL_IP_ANNOTATION = "bare-service-lb.prokube.ai/external-ip"
RECONCILED_ANNOTATION = "bare-service-lb.prokube.ai/reconciled"
NGINX_IMAGE = "nginx:1.27-alpine"
DAEMONSET_PREFIX = "bsl-"
CONFIGMAP_PREFIX = "bsl-cfg-"
NAMESPACE = "bare-service-lb"
```
