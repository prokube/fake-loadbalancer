# bare-service-lb (bsl)

## How It Works

The operator exploits a kube-proxy behavior for `Service` objects of type `LoadBalancer` when
`.status.loadBalancer.ingress[].ipMode` is set to `VIP`.

Official documentation for this behavior is limited. The main public explanation is the Kubernetes
1.29 blog post on Load Balancer IP Mode:
* [Kubernetes 1.29: New (alpha) Feature, Load Balancer IP Mode for Services](https://kubernetes.io/blog/2023/12/18/kubernetes-1-29-feature-loadbalancer-ip-mode-alpha/).
* [Virtual IPs and Service Proxies](https://kubernetes.io/docs/reference/networking/virtual-ips/)

When the **status** of a `Service` of type `LoadBalancer` is patched like this:

```yaml
status:
  loadBalancer:
    ingress:
      - ip: <IP_ADDRESS>
        ipMode: VIP
      - ip: <IP_ADDRESS_2>
        ipMode: VIP
      # ... more IPs
```

kube-proxy sets up routing rules so that traffic sent to those load-balancer IPs is intercepted on
the node and forwarded to the `Service` backends. In effect, kube-proxy treats those IPs as
additional virtual addresses for the `Service`.

In `iptables` mode, this is implemented through forwarding and DNAT rules. In `ipvs` mode,
kube-proxy can also bind the load-balancer IP to a node interface.

The operator watches `Service` objects with `spec.type = LoadBalancer` and patches their status with
node IPs taken from the Nodes' `.status.addresses`. Once those IPs appear under
`.status.loadBalancer.ingress` with `ipMode: VIP`, kube-proxy applies the corresponding proxying
behavior on each node.

If you want to preserve the client IPs, you can set `externalTrafficPolicy: Local` on the `Service`. However, this will result in traffic being routed only to backends that are local to the node receiving the request and will be dropped if there are no local backends available on the receiving node. Further explanation: [Kubernetes Virtual IPs - Traffic Policies](https://kubernetes.io/docs/reference/networking/virtual-ips/#traffic-policies)

