import logging
import os
import sys

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

    # Imported here so configuration errors are logged cleanly after logging
    # is set up, and the process exits non-zero for the supervisor/watchdog.
    from app.config import ConfigurationError

    try:
        from app.server import app
    except ConfigurationError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    import uvicorn

    # PORT is only for standalone deployments; the HA app ingress expects
    # the fixed port 9000 (ingress_port in config.yaml).
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9000")), log_level="info")


if __name__ == "__main__":
    main()
