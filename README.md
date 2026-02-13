# bare-service-lb (bsl)

ServiceLB for bare-metal, cloud VMs, and anywhere without a cloud LoadBalancer.

Inspired by [k3s ServiceLB](https://github.com/k3s-io/klipper-lb), bare-service-lb brings the same zero-configuration LoadBalancer experience to **any Kubernetes distribution** with better observability and smarter traffic handling.

## Why bare-service-lb?

- **Zero Configuration**: No BGP, no ARP, no routing setup required
- **Works Anywhere**: MicroK8s, kubeadm, K3s, vanilla Kubernetes, cloud VMs
- **Excellent Observability**: NGINX logs (default) show actual traffic patterns
- **Smart Traffic Policy**: Handles `externalTrafficPolicy: Local` correctly to prevent endpoint loss
- **Production Ready**: Sophisticated pod readiness tracking and status management
- **Implementation Flexible**: Currently uses NGINX proxy, iptables/nftables backends planned

## When to Use What

We believe in honest positioning. Here's when to use each solution:

| Your Situation | Best Solution | Why |
|----------------|---------------|-----|
| **Traditional datacenter with BGP routers** | [MetalLB](https://metallb.universe.tf/) (BGP mode) | Real routing protocols, best performance, enterprise-grade |
| **Have Layer 2 network control (ARP)** | [MetalLB](https://metallb.universe.tf/) (L2 mode) | Standard networking approach, mature solution |
| **Using K3s exclusively** | [k3s ServiceLB](https://github.com/k3s-io/klipper-lb) | Built-in, zero config, battle-tested |
| **MicroK8s, kubeadm, vanilla K8s** | **bare-service-lb** | Only standalone ServiceLB implementation |
| **Need detailed traffic logs/debugging** | **bare-service-lb** | NGINX access logs vs iptables rules |
| **Cloud VMs (Hetzner, DigitalOcean, etc.)** | **bare-service-lb** | Works without network infrastructure access |
| **Behind NAT, no network admin access** | **bare-service-lb** | No special network permissions needed |
| **Home lab / development clusters** | **bare-service-lb** or cloud-provider-kind | Simple setup, good for learning |

**Our positioning:** We're not competing with MetalLB (different architecture, different requirements). We're extending k3s ServiceLB's concept to all Kubernetes distributions with better observability.

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
docker build -t bare-service-lb:latest .
```

For MicroK8s, import the image directly:

```bash
docker save bare-service-lb:latest | microk8s ctr image import -
```

### 2. Deploy RBAC and operator

```bash
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/peering.yaml  # Leader election CRD and peering object
kubectl apply -f manifests/deployment.yaml
```

The operator runs with 1 replica by default. For high availability, you can scale it to multiple replicas.

### 3. Verify the operator is running

```bash
kubectl -n bare-service-lb get pods
```

For high availability, scale to 2 replicas:
```bash
kubectl -n bare-service-lb scale deployment bare-service-lb --replicas=2
```

To check which pod is the leader (when running multiple replicas):
```bash
kubectl get clusterkopfpeering bare-service-lb-leader -o yaml
# Look at the status field for the current leader
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
| Operator Deployment | `bare-service-lb` | Watches services, manages lifecycle (supports HA via leader election) |
| NGINX DaemonSet (per service) | `bare-service-lb` | Runs on every node with `hostPort` |
| ConfigMap (per service) | `bare-service-lb` | Holds NGINX `stream {}` config |
| ClusterKopfPeering | cluster-wide | Coordinates leader election between operator replicas |

### High Availability

The operator supports running with **multiple replicas** for high availability:
- By default, the operator runs with 1 replica
- You can scale to multiple replicas (e.g., 2) for high availability
- When running with multiple replicas, only one is active (leader) at any time, processing Kubernetes events
- If the leader fails, another replica automatically takes over within seconds
- Rolling updates provide zero-downtime upgrades
- Leader election is managed by Kopf using Kubernetes Lease objects

To enable HA, scale the deployment:
```bash
kubectl -n bare-service-lb scale deployment bare-service-lb --replicas=2
```

Or set `replicaCount: 2` in your Helm values.

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
| ClusterKopfPeering | full CRUD | Operator peering for HA |

## Honest Comparison with Alternatives

We believe in transparency. Here's how bare-service-lb compares to other solutions:

| Feature | bare-service-lb | k3s ServiceLB | MetalLB | inlets-operator |
|---------|-----------------|---------------|---------|-----------------|
| **Any K8s Distribution** | ✅ Yes | ❌ K3s only | ✅ Yes | ✅ Yes |
| **Network Control Needed** | ❌ No | ❌ No | ⚠️ ARP/BGP required | ❌ No |
| **Observability** | ✅ Excellent (NGINX logs) | 🟡 Basic (iptables) | 🟡 Basic | ✅ Good |
| **externalTrafficPolicy=Local** | ✅ Full support | ⚠️ Known issues ([NAT+Local](https://docs.k3s.io/networking/networking-services#known-issue)) | ✅ Full support | ✅ Full support |
| **Performance** | 🟡 Good (userspace proxy) | 🟢 Better (kernel NAT) | 🟢 Best (routing) | 🟡 Good (tunnel) |
| **Resource Overhead** | 🟡 Moderate (NGINX pods) | 🟢 Low (iptables) | 🟢 Low (no proxies) | 🟡 Moderate |
| **Cost** | 🟢 Free | 🟢 Free | 🟢 Free | 🟡 ~$5/mo per LB |
| **Maturity** | 🆕 New project | ✅ Battle-tested | ✅ CNCF Sandbox | ✅ Commercial |
| **Implementation** | NGINX proxy | iptables NAT | ARP/BGP routing | Cloud tunnel |

### Architectural Differences

**bare-service-lb** (this project):
```
User → Node:80 (hostPort) → NGINX proxy → 127.0.0.1:30764 (NodePort) → Service ClusterIP → Pods
                                                    ↓
                                          kube-proxy does load balancing
```

**k3s ServiceLB** (iptables-based):
```
User → Node:80 (hostPort) → iptables DNAT → Service ClusterIP → Pods
                                    ↓
                          kube-proxy does load balancing
```

**MetalLB** (Layer 2 mode):
```
User → Floating IP (ARP announcement) → kube-proxy → Service ClusterIP → Pods
              ↓                                ↓
    One node "owns" IP               kube-proxy does load balancing
```

**MetalLB** (BGP mode):
```
User → Routed IP (BGP advertisement to routers) → kube-proxy → Service ClusterIP → Pods
              ↓                                         ↓
    All nodes advertise route                 kube-proxy does load balancing
```

**Key Insight:** None of these solutions actually "load balance" - they all just provide external access. kube-proxy handles the actual traffic distribution to pods.

## Limitations

- **Port Conflicts**: Two services cannot claim the same `hostPort` — the second DaemonSet's pods will fail to schedule and the Service will show no `EXTERNAL-IP` (the operator only reports IPs for nodes where pods are actually Ready)
- **Performance**: Additional userspace hop through NGINX (vs kernel-level NAT in k3s ServiceLB or routing in MetalLB). This is the trade-off for better observability.
- **Single Namespace**: The operator uses a single namespace (`bare-service-lb`) for all managed resources
- **No Node Pools**: Unlike k3s ServiceLB, there's no built-in label-based node selection (use standard K8s node selectors/affinity instead)
- **Maturity**: New project (2025), while alternatives have years of production use. We recommend testing thoroughly in non-production first.

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

## Related Projects & Inspiration

This project stands on the shoulders of giants:

- **[k3s ServiceLB (klipper-lb)](https://github.com/k3s-io/klipper-lb)** — Our primary inspiration. Built into K3s, uses iptables. We extend this concept to all K8s distributions with better observability.
- **[MetalLB](https://metallb.universe.tf/)** — The gold standard for bare-metal LoadBalancers. CNCF Sandbox project. Use this if you have network control (ARP/BGP).
- **[kube-vip](https://kube-vip.io/)** — Another mature solution similar to MetalLB. Great for HA control planes too.
- **[inlets-operator](https://github.com/inlets/inlets-operator)** — Commercial solution that tunnels via cloud VMs. Great for home labs behind NAT.
- **[cloud-provider-kind](https://github.com/kubernetes-sigs/cloud-provider-kind)** — Official Kubernetes SIG project for KIND clusters (development only).

**Special thanks to k3s ServiceLB** for pioneering the hostPort-based LoadBalancer approach that made this possible.

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

## Roadmap

- [ ] iptables/nftables backend for better performance (maintain NGINX as default for observability)
- [ ] Helm chart for easier installation
- [ ] Node pool support (label-based node selection like k3s ServiceLB)
- [ ] Tunnel Custom Resource for IPVS compatibility
- [ ] Prometheus metrics exporter
- [ ] Support for Service annotations (custom node IPs, backend selection)

## License

Apache License 2.0

## Contributing

Contributions welcome! This project follows a Kubernetes operator pattern using [Kopf](https://kopf.readthedocs.io/).

**Note:** This is a new project (2025). While inspired by battle-tested solutions like k3s ServiceLB, we recommend thorough testing before production use.
