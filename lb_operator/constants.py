"""Operator-wide constants: labels, annotations, image refs, and defaults."""

from __future__ import annotations

import os

OPERATOR_LABEL = "bare-service-lb.prokube.ai/managed"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
FINALIZER = "bare-service-lb.prokube.ai/cleanup"
EXTERNAL_IP_ANNOTATION = "bare-service-lb.prokube.ai/external-ip"
RECONCILED_ANNOTATION = "bare-service-lb.prokube.ai/reconciled"
BACKEND_ANNOTATION = "bare-service-lb.prokube.ai/backend"

NGINX_IMAGE = "nginx:1.27-alpine"
KLIPPER_IMAGE = os.environ.get("BSL_KLIPPER_IMAGE", "rancher/klipper-lb:v0.4.9")
DEFAULT_BACKEND = os.environ.get("BSL_DEFAULT_BACKEND", "nginx")

DAEMONSET_PREFIX = "bsl-"
CONFIGMAP_PREFIX = "bsl-cfg-"
