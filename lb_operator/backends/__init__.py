"""Backend selection for the proxy layer."""

from __future__ import annotations

import logging

from ..constants import BACKEND_ANNOTATION, DEFAULT_BACKEND

logger = logging.getLogger("bare-service-lb")


def get_backend(annotations: dict[str, str]) -> str:
    """Return the proxy backend for a service.

    Resolution order:
      1. Service annotation ``bare-service-lb.prokube.ai/backend``
      2. ``BSL_DEFAULT_BACKEND`` environment variable
      3. ``"nginx"`` (hardcoded default)
    """
    backend = annotations.get(BACKEND_ANNOTATION, DEFAULT_BACKEND).lower()
    if backend not in ("nginx", "klipper"):
        logger.warning(
            "Unknown backend %r, falling back to nginx",
            backend,
        )
        return "nginx"
    return backend
