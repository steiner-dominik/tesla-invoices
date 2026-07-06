import base64
import json
from pathlib import Path

from app.config import Config


def make_jwt(payload: dict) -> str:
    """Build an unsigned JWT with a base64url-encoded payload."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    # ensure_ascii=False keeps raw UTF-8 bytes so payloads can exercise the
    # base64url-specific alphabet ('-' and '_')
    body = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.signature"


def make_config(tmp_path: Path, **overrides) -> Config:
    defaults = dict(
        homeassistant=False,
        refresh_token_path=tmp_path / "refresh_token.txt",
        access_token_path=tmp_path / "access_token.txt",
        invoice_path=tmp_path / "invoices",
        enable_email_export=False,
        enable_subscription_invoice=True,
        polling_interval=15,
        env_refresh_token="",
        env_access_token="",
    )
    defaults.update(overrides)
    return Config(**defaults)
