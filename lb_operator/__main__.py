import kopf
from . import handlers  # noqa: F401

# Entry point for the operator
if __name__ == "__main__":
    kopf.run(
        standalone=True,
        clusterwide=True,
        liveness_endpoint="http://0.0.0.0:8080/healthz",
    )
