"""Operator-wide constants: labels, annotations, image refs, and defaults."""

from __future__ import annotations

MANAGED_ANNOTATION = "bare-service-lb.prokube.ai/managed"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
FINALIZER = "bare-service-lb.prokube.ai/cleanup"
EXTERNAL_IP_ANNOTATION = "bare-service-lb.prokube.ai/external-ip"

NGINX_IMAGE = "nginx:1.27-alpine"

DAEMONSET_PREFIX = "bsl-"
CONFIGMAP_PREFIX = "bsl-cfg-"
