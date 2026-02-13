# bare-service-lb Helm Chart

ServiceLB for bare-metal, cloud VMs, and anywhere without a cloud LoadBalancer.

## Installation

### Prerequisites

- Kubernetes 1.32+
- Helm 3.0+

### Installing the Chart

To install the chart with the release name `bare-service-lb`:

```bash
helm install bare-service-lb ./helm --namespace bare-service-lb --create-namespace
```

Or from a packaged chart:

```bash
helm package ./helm
helm install bare-service-lb bare-service-lb-0.1.0.tgz --namespace bare-service-lb --create-namespace
```

### Uninstalling the Chart

To uninstall/delete the `bare-service-lb` deployment:

```bash
helm uninstall bare-service-lb --namespace bare-service-lb
```

## Configuration

The following table lists the configurable parameters of the bare-service-lb chart and their default values.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `replicaCount` | Number of operator replicas (should be 1) | `1` |
| `image.repository` | Operator image repository | `ghcr.io/prokube/bare-service-lb` |
| `image.pullPolicy` | Image pull policy | `Always` |
| `image.tag` | Overrides the image tag | `""` (uses Chart.appVersion) |
| `imagePullSecrets` | Image pull secrets | `[]` |
| `nameOverride` | Override chart name | `""` |
| `fullnameOverride` | Override full name | `""` |
| `serviceAccount.create` | Create service account | `true` |
| `serviceAccount.automount` | Automount service account token | `true` |
| `serviceAccount.annotations` | Service account annotations | `{}` |
| `serviceAccount.name` | Service account name | `""` |
| `podAnnotations` | Pod annotations | `{}` |
| `podLabels` | Pod labels | `{}` |
| `podSecurityContext` | Pod security context | `{}` |
| `securityContext` | Container security context | `{}` |
| `resources.requests.cpu` | CPU request | `50m` |
| `resources.requests.memory` | Memory request | `128Mi` |
| `livenessProbe` | Liveness probe configuration | See values.yaml |
| `readinessProbe` | Readiness probe configuration | See values.yaml |
| `nodeSelector` | Node selector | `{}` |
| `tolerations` | Tolerations | `[]` |
| `affinity` | Affinity rules | `{}` |
| `env` | Environment variables | See values.yaml |

### Example: Custom Values

Create a `custom-values.yaml` file:

```yaml
replicaCount: 1

image:
  tag: "latest"

resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    cpu: 200m
    memory: 512Mi

nodeSelector:
  kubernetes.io/os: linux
```

Install with custom values:

```bash
helm install bare-service-lb ./helm \
  --namespace bare-service-lb \
  --create-namespace \
  --values custom-values.yaml
```

## Testing the Installation

After installing the chart, test it with a sample LoadBalancer Service:

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: test-lb
spec:
  type: LoadBalancer
  selector:
    app: nginx
  ports:
    - port: 80
      targetPort: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
spec:
  replicas: 2
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
      - name: nginx
        image: nginx:alpine
        ports:
        - containerPort: 80
EOF
```

Check the Service status:

```bash
kubectl get svc test-lb
```

The `EXTERNAL-IP` field should show your node IPs.

## More Information

For more information about bare-service-lb, see the [main README](../README.md) or visit the [GitHub repository](https://github.com/prokube/bare-service-lb).
