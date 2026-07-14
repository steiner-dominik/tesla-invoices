"""Tesla OAuth2 login (PKCE), so a user with no token can sign in from the UI.

This mirrors what the tesla_auth desktop tool does (adriankumpf/tesla_auth,
MIT): the same ``ownerapi`` OAuth2 authorization-code + PKCE flow against
auth.tesla.com. A web app cannot embed Tesla's login page (Tesla blocks
framing, and the sign-in may present a captcha that needs a real browser), so
the flow is split in two:

  1. build the authorize URL — the user opens it in their own browser and
     signs in;
  2. Tesla redirects to ``tesla://auth/callback?code=…`` (the mobile app's
     deep link — on a computer the browser cannot open it and shows an error
     page instead); the user copies that address back and we exchange the
     code for tokens.

Tesla deregistered the old ``https://auth.tesla.com/void/callback`` redirect
for ``ownerapi`` (~2026-04): it now fails with "The 'redirect_uri' supplied
is not registered for this 'client_id'". The mobile-app deep link is the one
that keeps working (same fix as tesla_auth v0.13.0, adriankumpf/tesla_auth#99).

The token exchange itself lives in TokenManager, because it needs the same
browser-TLS-fingerprint transport (curl_cffi) as the refresh — Tesla issues
down-scoped tokens to plain Python TLS stacks.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
REDIRECT_URI = "tesla://auth/callback"
CLIENT_ID = "ownerapi"
SCOPE = "openid email offline_access"


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for the PKCE flow."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(challenge: str, state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def parse_callback(value: str) -> tuple[str, str]:
    """Extract ``(code, state)`` from what the user pastes back.

    Accepts the full callback URL (``tesla://auth/callback?code=…&state=…``,
    or the legacy ``https://auth.tesla.com/void/callback?…`` form) or, as a
    fallback, a bare authorization code.
    """
    value = (value or "").strip()
    # A URL (has a scheme or a query string) is always parsed as such — its
    # code may legitimately be absent (login cancelled). Only a plain string
    # with neither is taken to be a bare authorization code.
    if "://" in value or "?" in value:
        query = parse_qs(urlparse(value).query)
        return (query.get("code") or [""])[0], (query.get("state") or [""])[0]
    return value, ""
