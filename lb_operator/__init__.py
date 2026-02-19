"""bare-service-lb: ServiceLB for bare-metal, cloud VMs, and anywhere without a cloud LoadBalancer.

Inspired by k3s ServiceLB, this operator brings zero-configuration LoadBalancer services
to any Kubernetes distribution with pluggable proxy backends.

Backends:
  - nginx (default): NGINX stream proxy with access logs for observability
  - klipper: iptables/nftables DNAT via klipper-lb (from k3s) for better performance

Flow:
  1. User creates Service type=LoadBalancer (port 80, Kubernetes auto-assigns e.g. NodePort 30764)
  2. Operator marks it as managed (keeps type as LoadBalancer)
  3. Operator creates proxy resources based on the selected backend:
       - nginx: ConfigMap with NGINX stream config + DaemonSet with hostPort
       - klipper: DaemonSet with hostNetwork and iptables DNAT rules (no ConfigMap)
  4. Operator patches Service .status.loadBalancer.ingress with node IPs
  5. On delete, operator tears down the DaemonSet (+ ConfigMap if nginx)
"""

# Import handlers so Kopf discovers the decorated functions
from . import handlers  # noqa: F401
