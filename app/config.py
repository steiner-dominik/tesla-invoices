import json
import os
from dataclasses import dataclass
from pathlib import Path

HA_OPTIONS_PATH = Path("/data/options.json")


class ConfigurationError(Exception):
    pass


def _to_int(value: object, name: str) -> int:
    """Convert an option to int, raising ConfigurationError (not a raw
    ValueError traceback) on garbage like POLLING_INTERVAL=abc."""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be a number, got {value!r}") from None


@dataclass
class Config:
    homeassistant: bool
    refresh_token_path: Path
    access_token_path: Path
    invoice_path: Path
    enable_email_export: bool
    enable_subscription_invoice: bool
    polling_interval: int

    env_refresh_token: str
    env_access_token: str

    # Preferred display currency in the dashboard; empty = auto-detect from
    # the invoices themselves. Costs are never converted between currencies.
    default_currency: str = ""
    email_from: str = ""
    email_to: str = ""
    email_server: str = ""
    email_server_port: int = 587
    email_user: str = ""
    email_pass: str = ""

    def __post_init__(self) -> None:
        if self.polling_interval < 1:
            raise ConfigurationError(f"polling_interval must be >= 1 minute, got {self.polling_interval}")
        if self.enable_email_export and not (self.email_server and self.email_from and self.email_to):
            raise ConfigurationError(
                "enable_email_export is set, but email settings (from/to/mailserver) are incomplete"
            )

    @classmethod
    def from_ha(cls, options_path: Path = HA_OPTIONS_PATH) -> Config:
        try:
            with options_path.open() as f:
                options = json.load(f)
        except OSError as e:
            raise ConfigurationError(f"Could not read HA options file {options_path}: {e}") from e
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"HA options file {options_path} is not valid JSON: {e}") from e

        email_opts = options.get("email") or {}

        return cls(
            homeassistant=True,
            refresh_token_path=Path("/data/refresh_token.txt"),
            access_token_path=Path("/data/access_token.txt"),
            invoice_path=Path("/data/invoices/"),
            enable_email_export=options.get("enable_email_export", False),
            enable_subscription_invoice=options.get("enable_subscription_invoice", True),
            polling_interval=_to_int(options.get("polling_interval", 15), "polling_interval"),
            default_currency=(options.get("default_currency") or "").strip().upper(),
            env_refresh_token=options.get("refresh_token", ""),
            # The HA app intentionally has no access_token option: the refresh
            # token is all that's needed (access tokens are obtained/rotated
            # automatically and persisted in /data). Only the standalone
            # deployment supports supplying one via ACCESS_TOKEN.
            env_access_token="",
            email_from=email_opts.get("from") or "",
            email_to=email_opts.get("to") or "",
            email_server=email_opts.get("mailserver") or "",
            email_server_port=_to_int(email_opts.get("port") or 587, "email.port"),
            email_user=email_opts.get("user") or "",
            email_pass=email_opts.get("password") or "",
        )

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            homeassistant=False,
            refresh_token_path=Path(
                os.environ.get("REFRESH_TOKEN_PATH", "/opt/tesla-invoices/secrets/refresh_token.txt")
            ),
            access_token_path=Path(
                os.environ.get("ACCESS_TOKEN_PATH", "/opt/tesla-invoices/secrets/access_token.txt")
            ),
            invoice_path=Path(os.environ.get("INVOICE_PATH", "/opt/tesla-invoices/invoices/")),
            enable_email_export=os.environ.get("ENABLE_EMAIL_EXPORT", "False").lower() == "true",
            enable_subscription_invoice=os.environ.get("ENABLE_SUBSCRIPTION_INVOICE", "True").lower() == "true",
            polling_interval=_to_int(os.environ.get("POLLING_INTERVAL", "15"), "POLLING_INTERVAL"),
            default_currency=os.environ.get("DEFAULT_CURRENCY", "").strip().upper(),
            env_refresh_token=os.environ.get("REFRESH_TOKEN", ""),
            env_access_token=os.environ.get("ACCESS_TOKEN", ""),
            email_from=os.environ.get("EMAIL_FROM", ""),
            email_to=os.environ.get("EMAIL_TO", ""),
            email_server=os.environ.get("EMAIL_SERVER", ""),
            email_server_port=_to_int(os.environ.get("EMAIL_SERVER_PORT", "587"), "EMAIL_SERVER_PORT"),
            email_user=os.environ.get("EMAIL_USER", ""),
            email_pass=os.environ.get("EMAIL_PASS", ""),
        )

    @classmethod
    def load(cls) -> Config:
        if HA_OPTIONS_PATH.exists():
            return cls.from_ha()
        return cls.from_env()
