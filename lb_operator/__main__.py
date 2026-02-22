import logging

import kopf

from . import handlers  # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Entry point for the operator
if __name__ == "__main__":
    kopf.run(
        standalone=True,
        clusterwide=True,
        liveness_endpoint="http://0.0.0.0:8080/healthz",
    )
