# fake-loadbalancer

A tiny Kubernetes controller that makes `Service type: LoadBalancer` work on clusters that have no load balancer: MicroK8s, bare metal, plain VMs.

It is **not** a real load balancer. It writes the IPs of your Ready nodes into `status.loadBalancer.ingress` of every `LoadBalancer` Service, and `kube-proxy` does the rest. About 120 lines of Python, no proxy pods, no custom image.

## How it works

1. You create a `Service` of `type: LoadBalancer`. Without a controller it stays `<pending>` forever.
2. The controller (a [Kopf](https://kopf.readthedocs.io/) operator) patches `.status.loadBalancer.ingress` with the `InternalIP` of every Ready node.
3. `kube-proxy` sees the status on every node and programs rules for `<node IP>:<service port>`. Traffic to any node IP on the service port reaches the Service's endpoints.

The same status field is what a cloud controller, MetalLB or kube-vip fills in. Kubernetes treats it as an interface; who fills it in is up to you.

## Install

```bash
kubectl apply -k deploy/
```

This runs the script from `deploy/controller.py` on the stock `ghcr.io/nolar/kopf` image, mounted from a ConfigMap. Nothing to build.

## Try it

```bash
kubectl apply -f examples/whoami.yaml
kubectl get svc whoami          # EXTERNAL-IP: one IP per Ready node
curl http://<any node IP>/
```

It isn’t only for HTTP. A NodePort has to be 30000 or higher by default, but a LoadBalancer Service keeps its real port, for example Postgres on 5432:

```bash
kubectl apply -f examples/postgres.yaml
psql -h <any node IP> -p 5432 -U postgres     # password: demo
```

You can see the rules `kube-proxy` created on a node:

```bash
sudo iptables-save -t nat | grep 'loadbalancer IP'            # kubeadm, RKE2 (iptables-nft)
sudo iptables-legacy -t nat -L -n -v | grep loadbalancer      # MicroK8s (iptables-legacy)
```

**Security note:** every node IP in `EXTERNAL-IP` is reachable wherever that IP is reachable. If your nodes advertise public IPs, the Service is open to the internet on all of them.

## Keeping the client IP

With the default `externalTrafficPolicy: Cluster` the pod usually sees a node or tunnel IP, not the client. With `Local` it sees the client IP, but a node without a local pod drops the traffic. Real load balancers avoid this by health-checking each node through `healthCheckNodePort`. This controller is kept simple on purpose and only checks that a node is Ready, so with `Local` a DaemonSet is the workaround: it puts a pod on every advertised node. See `examples/whoami-local.yaml`. With the default `Cluster` policy you don’t need it.

Every Ready node is advertised, control-plane nodes included. kubeadm taints those, so the DaemonSet in the example tolerates `node-role.kubernetes.io/control-plane`.

## What it is not

| | Cloud load balancer | fake-loadbalancer |
|---|---|---|
| Entry point | one stable address | every node IP; the client picks |
| Health checks | probes nodes, skips unhealthy ones | only skips nodes that are not Ready |
| Provisions anything outside the cluster | yes | no |

We couldn’t find a formal LoadBalancer specification. The API only defines hooks such as `status.loadBalancer.ingress` and `healthCheckNodePort`. Real health checks could be added in the future, but this controller deliberately stays minimal.

## Status

Tested on two-to-three-node clusters of Ubuntu 24.04 VMs, with requests sent from a VM outside the cluster:

| Distribution | CNI | Service routing | Result |
|---|---|---|---|
| MicroK8s 1.35 | Calico | kube-proxy, iptables-legacy | works |
| kubeadm 1.35 | Calico | kube-proxy, iptables-nft | works (the DaemonSet example tolerates the control-plane taint) |
| RKE2 1.35 | Canal | kube-proxy, iptables-nft | works |
| RKE2 1.35 | Cilium | eBPF, no kube-proxy | works. With the `Cluster` policy the pod can see the real client IP when it runs on the node that was hit |

On RKE2 the bundled ingress controller uses hostPort 80 and 443. A Service on those ports still answered in our test (Canal only). Other distributions, CNIs and `kube-proxy` modes are untested.

## History

This repository used to be `bare-service-lb`: an operator that created an NGINX proxy DaemonSet per Service, with proxy protocol support, leader election and a Helm chart. NGINX never received the traffic, because `kube-proxy` handles it first, and all of it turned out to be unnecessary. That version is preserved at tag [`v0.5.0`](../../tree/v0.5.0) (commit `696bf03`).

## License

Apache License 2.0, see [LICENSE](LICENSE).
