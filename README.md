# nginx-lb-operator

A Kubernetes operator that converts `LoadBalancer` Services into hostPort-based NGINX TCP/UDP proxies. Designed for bare-metal and MicroK8s clusters that lack a cloud load balancer.

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

## Limitations

- Two services cannot claim the same `hostPort` — the second DaemonSet will fail to schedule
- Ports below 1024 require the NGINX container to run with sufficient capabilities (hostPort handles this)
- The operator uses a single namespace (`nginx-lb-operator`) for all managed resources

## Development

### Local development (outside cluster)

```bash
pip install -e ".[dev]"
kopf run operator.py --all-namespaces --verbose
```

This requires a valid kubeconfig pointing at your cluster.

### Running tests

```bash
pytest
```

## Project Structure

```
.
├── operator.py              # Operator logic (Kopf handlers)
├── pyproject.toml            # Python project config
├── Dockerfile                # Operator container image
├── manifests/
│   ├── rbac.yaml             # Namespace, ServiceAccount, ClusterRole, Binding
│   └── deployment.yaml       # Operator Deployment
└── examples/
    └── sample-service.yaml   # Example LoadBalancer Service
```
