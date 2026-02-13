# GCP External IP Annotator

A lightweight DaemonSet that annotates Kubernetes nodes with their GCP external
IP addresses. Designed to work with the
[nginx-lb-operator](../README.md), which reads the
`nginx-lb-operator.prokube.ai/external-ip` annotation to populate
`status.loadBalancer.ingress` on managed services.

## Why

On GCP, external IPs are NAT'd and don't appear as `ExternalIP` in the node's
status addresses. This DaemonSet queries the
[GCP instance metadata server](https://cloud.google.com/compute/docs/metadata/overview)
from each node and writes the external IP as a node annotation.

## Deploy

```bash
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/daemonset.yaml
```

## Verify

```bash
# Check the DaemonSet is running on all nodes
kubectl get ds -n nginx-lb-operator gcp-external-ip-annotator

# Check annotations on nodes
kubectl get nodes -o custom-columns='NAME:.metadata.name,EXTERNAL-IP:.metadata.annotations.nginx-lb-operator\.prokube\.ai/external-ip'
```

## Configuration

The sync interval can be adjusted via the `SYNC_INTERVAL` environment variable
in `manifests/daemonset.yaml` (default: `300` seconds).

## How it works

Each pod:

1. Reads its node name from the downward API (`spec.nodeName`)
2. Queries `http://metadata.google.internal/...` for the instance's external IP
3. Runs `kubectl annotate node <name> nginx-lb-operator.prokube.ai/external-ip=<ip>`
4. Sleeps for `SYNC_INTERVAL` seconds and repeats
