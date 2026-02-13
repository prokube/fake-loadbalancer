# nginx-lb-operator

A Kubernetes operator that provides LoadBalancer services for **MicroK8s, kubeadm, and bare-metal Kubernetes clusters** without requiring network infrastructure changes.

Unlike MetalLB (requires ARP/BGP), nginx-lb-operator uses NGINX + hostPort for plug-and-play LoadBalancer support with excellent observability.

## Why nginx-lb-operator?

- **Zero Configuration**: Works on any Kubernetes cluster without network infrastructure changes
- **Any Distribution**: MicroK8s, kubeadm, K3s, vanilla Kubernetes — all supported
- **Excellent Observability**: NGINX logs show actual traffic patterns (unlike iptables-based solutions)
- **Smart Traffic Policy**: Handles `externalTrafficPolicy: Local` correctly to prevent endpoint loss
- **Production Ready**: Sophisticated pod readiness tracking and status management

### When to use nginx-lb-operator

| Your Situation | Best Solution |
|----------------|---------------|
| **Traditional datacenter with BGP routers** | MetalLB (BGP mode) — Real routing protocols |
| **K3s cluster** | K3s ServiceLB — Built-in, free |
| **MicroK8s / kubeadm / vanilla K8s** | **nginx-lb-operator** — Only standalone, observable solution |
| **Need detailed traffic logs** | **nginx-lb-operator** — NGINX access logs |
| **Behind NAT, no network control** | **nginx-lb-operator** OR inlets-operator |
| **Development (KIND/minikube)** | cloud-provider-kind — Purpose-built |

If you have network control (BGP routers, ARP access), use MetalLB. If you need a zero-configuration LoadBalancer that "just works" without network changes, use nginx-lb-operator.

## How It Works

```
User creates Service (type: LoadBalancer, port 80)
        │
        ▼
Operator patches to type: NodePort (K8s assigns e.g. 30764)
        │
        ▼
Operator deploys NGINX DaemonSet (hostPort: 80)
  with stream config: listen 80 → proxy_pass 127.0.0.1:30764
        │
        ▼
Operator patches Service .status.loadBalancer.ingress with node IPs
```

The result: your `LoadBalancer` Service becomes reachable on every node at the original port.

## Prerequisites

- Kubernetes 1.32+
- `kubectl` access with cluster-admin privileges
- Docker or Podman (to build the operator image)

## Installation

### 1. Build the operator image

```bash
docker build -t nginx-lb-operator:latest .
```

For MicroK8s, import the image directly:

```bash
docker save nginx-lb-operator:latest | microk8s ctr image import -
```

### 2. Deploy RBAC and operator

```bash
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/deployment.yaml
```

### 3. Verify the operator is running

```bash
kubectl -n nginx-lb-operator get pods
```

## Usage

Create a standard `LoadBalancer` Service:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-app
spec:
  type: LoadBalancer
  selector:
    app: my-app
  ports:
    - port: 80
      targetPort: 8080
```

The operator will automatically:

1. Convert it to `NodePort`
2. Deploy an NGINX DaemonSet proxying `hostPort:80 → NodePort`
3. Set `EXTERNAL-IP` to all node IPs

Check the result:

```bash
kubectl get svc my-app
# NAME     TYPE       CLUSTER-IP     EXTERNAL-IP            PORT(S)        AGE
# my-app   NodePort   10.152.183.5   192.168.1.10,...       80:30764/TCP   5s
```

## Multi-port Services

Services with multiple ports are fully supported:

```yaml
spec:
  type: LoadBalancer
  ports:
    - name: http
      port: 80
      targetPort: 8080
    - name: https
      port: 443
      targetPort: 8443
```

The operator creates a single DaemonSet with both `hostPort: 80` and `hostPort: 443`.

## UDP Support

UDP ports are also handled:

```yaml
spec:
  type: LoadBalancer
  ports:
    - name: dns
      port: 53
      targetPort: 53
      protocol: UDP
```

## How Cleanup Works

When you delete the Service, the operator automatically deletes the associated DaemonSet and ConfigMap.

## Architecture

| Component | Namespace | Purpose |
|-----------|-----------|---------|
| Operator Deployment | `nginx-lb-operator` | Watches services, manages lifecycle |
| NGINX DaemonSet (per service) | `nginx-lb-operator` | Runs on every node with `hostPort` |
| ConfigMap (per service) | `nginx-lb-operator` | Holds NGINX `stream {}` config |

### RBAC Permissions

| Resource | Verbs | Reason |
|----------|-------|--------|
| Services | get, list, watch, patch, update | Convert type and update status |
| Services/status | patch, update | Set EXTERNAL-IP |
| Nodes | get, list, watch | Read node IPs |
| DaemonSets | full CRUD | Manage NGINX proxies |
| ConfigMaps | full CRUD | Manage NGINX configs |
| Events | create, patch | Kopf event posting |
| Leases | full CRUD | Kopf leader election |

## Comparison with Alternatives

| Feature | nginx-lb-operator | k3s ServiceLB | MetalLB | inlets-operator |
|---------|-------------------|---------------|---------|-----------------|
| **Any K8s Distribution** | ✅ Yes | ❌ K3s only | ✅ Yes | ✅ Yes |
| **Network Control Needed** | ❌ No | ❌ No | ⚠️ ARP/BGP required | ❌ No |
| **Observability** | ✅ Excellent (NGINX logs) | 🟡 Basic (iptables) | 🟡 Basic | ✅ Good |
| **externalTrafficPolicy=Local** | ✅ Full support | ⚠️ Known issues | ✅ Full support | ✅ Full support |
| **Performance** | 🟡 Good (userspace proxy) | 🟢 Better (kernel NAT) | 🟢 Best (routing) | 🟡 Good (tunnel) |
| **Resource Overhead** | 🟡 Moderate (NGINX) | 🟢 Low (iptables) | 🟢 Low | 🟡 Moderate |
| **Cost** | 🟢 Free | 🟢 Free | 🟢 Free | 🟡 ~$5/mo per LB |
| **Implementation** | Application proxy | Kernel NAT | Routing protocols | Cloud tunnel |

### Architectural Differences

**nginx-lb-operator** (this project):
```
User → Node:80 (hostPort) → NGINX → 127.0.0.1:30764 (NodePort) → Service → Pods
```

**k3s ServiceLB** (iptables-based):
```
User → Node:80 (hostPort) → iptables DNAT → Service ClusterIP → Pods
```

**MetalLB** (Layer 2 mode):
```
User → Floating IP (ARP announcement) → kube-proxy → Service ClusterIP → Pods
```

**MetalLB** (BGP mode):
```
User → Routed IP (BGP advertisement) → kube-proxy → Service ClusterIP → Pods
```

## Limitations

- **Port Conflicts**: Two services cannot claim the same `hostPort` — the second DaemonSet's pods will fail to schedule and the Service will show no `EXTERNAL-IP` (the operator only reports IPs for nodes where pods are actually Ready)
- **Performance**: Additional userspace hop through NGINX (vs kernel-level NAT in k3s ServiceLB or routing in MetalLB)
- **Single Namespace**: The operator uses a single namespace (`nginx-lb-operator`) for all managed resources
- **No Node Pools**: Unlike k3s ServiceLB, there's no built-in label-based node selection (use standard K8s node selectors/affinity instead)

## Development

### Local development (outside cluster)

```bash
pip install -e ".[dev]"
kopf run lb_operator.py --all-namespaces --verbose
```

This requires a valid kubeconfig pointing at your cluster.

### Running tests

```bash
pytest
```

## Related Projects

- [MetalLB](https://metallb.universe.tf/) — CNCF Sandbox project, requires network control (ARP/BGP)
- [k3s ServiceLB (klipper-lb)](https://github.com/k3s-io/klipper-lb) — Built into K3s, iptables-based
- [kube-vip](https://kube-vip.io/) — Similar to MetalLB, requires ARP/BGP
- [inlets-operator](https://github.com/inlets/inlets-operator) — Tunnels via cloud VMs (~$5/mo per LB)
- [cloud-provider-kind](https://github.com/kubernetes-sigs/cloud-provider-kind) — For KIND clusters (development only)

## Project Structure

```
.
├── lb_operator.py            # Operator logic (Kopf handlers, 706 lines)
├── pyproject.toml            # Python project config
├── Dockerfile                # Multi-stage build with uv
├── manifests/
│   ├── rbac.yaml             # Namespace, ServiceAccount, ClusterRole, Binding
│   └── deployment.yaml       # Operator Deployment
├── examples/
│   └── sample-service.yaml   # Example LoadBalancer Service
└── gcp-external-ip-annotator/
    └── ...                   # Optional: Annotates nodes with GCP external IPs
```

## License

Apache License 2.0

## Contributing

Contributions welcome! This project follows a Kubernetes operator pattern using [Kopf](https://kopf.readthedocs.io/).
