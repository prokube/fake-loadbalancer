"""Main entry point for the Load Balancer Operator."""

import logging
import kopf
import os
from . import handlers # pylint: disable=unused-import # needed to register handlers

def get_log_level() -> int:
    """Get the log level from the environment variable, defaulting to INFO."""
    level_str = os.getenv("BSL_LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_str, logging.INFO)

logging.basicConfig(
    level=get_log_level(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Entry point for the operator
if __name__ == "__main__":
    kopf.run(
        standalone=True,
        clusterwide=True,
        liveness_endpoint="http://0.0.0.0:8080/healthz",
    )
