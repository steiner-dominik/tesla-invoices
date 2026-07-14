import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

SUPERVISOR_INFO_URL = "http://supervisor/info"
CORE_CONFIG_URL = "http://supervisor/core/api/config"


def _apply_home_assistant_timezone() -> None:
    """Adopt Home Assistant's configured time zone for this process.

    Containers default to UTC, which makes log timestamps and the
    current/previous-month window confusing around midnight. The Supervisor
    exposes the HA time zone via its API (requires ``hassio_api: true`` in the
    app's config.yaml). Standalone deployments set the ``TZ`` environment
    variable instead, which always takes precedence and is honored natively
    (tzdata is installed in the image).
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if os.environ.get("TZ") or not token:
        return
    try:
        import requests

        response = requests.get(
            SUPERVISOR_INFO_URL, headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        response.raise_for_status()
        timezone = (response.json().get("data") or {}).get("timezone")
    except Exception as e:
        logger.warning(f"Could not read the time zone from Home Assistant, staying on UTC: {e}")
        return
    if timezone:
        os.environ["TZ"] = timezone
        time.tzset()
        logger.info(f"Using the Home Assistant time zone: {timezone}")


def _apply_home_assistant_language() -> None:
    """Adopt Home Assistant's configured language as the dashboard default.

    The Core API exposes it via /core/api/config (requires
    ``homeassistant_api: true`` in the app's config.yaml). Standalone
    deployments set the ``LANGUAGE`` environment variable instead, which
    always takes precedence. Either way this is only the *default*: the
    dashboard has its own language toggle, stored per browser.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if os.environ.get("LANGUAGE") or not token:
        return
    try:
        import requests

        response = requests.get(
            CORE_CONFIG_URL, headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        response.raise_for_status()
        language = (response.json() or {}).get("language")
    except Exception as e:
        logger.warning(f"Could not read the language from Home Assistant, defaulting to English: {e}")
        return
    if language:
        os.environ["LANGUAGE"] = str(language)
        logger.info(f"Using the Home Assistant language: {language}")


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)

    # Before anything computes dates or logs timestamps.
    _apply_home_assistant_timezone()
    # Before app.config is imported (it reads the LANGUAGE env var).
    _apply_home_assistant_language()

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
    # access_log=False: the Supervisor watchdog and Docker HEALTHCHECK poll
    # /health constantly, which would drown the log in one line per request.
    # The app logs every meaningful event (syncs, downloads, emails) itself.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9000")), log_level="info", access_log=False)


if __name__ == "__main__":
    main()
