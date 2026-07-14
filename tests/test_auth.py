import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from app import auth


def test_generate_pkce_challenge_matches_verifier():
    verifier, challenge = auth.generate_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge  # base64url, unpadded


def test_build_authorize_url_has_required_params():
    url = auth.build_authorize_url("CHAL", "STATE")
    parsed = urlparse(url)
    assert parsed.hostname == "auth.tesla.com"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["ownerapi"]
    assert qs["response_type"] == ["code"]
    assert qs["code_challenge"] == ["CHAL"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["state"] == ["STATE"]
    assert qs["redirect_uri"] == ["https://auth.tesla.com/void/callback"]
    assert qs["scope"] == ["openid email offline_access"]


def test_parse_callback_full_url():
    code, state = auth.parse_callback(
        "https://auth.tesla.com/void/callback?code=ABC123&state=xyz&issuer=https://auth.tesla.com/oauth2/v3"
    )
    assert code == "ABC123"
    assert state == "xyz"


def test_parse_callback_bare_code():
    code, state = auth.parse_callback("  BARECODE  ")
    assert code == "BARECODE"
    assert state == ""


def test_parse_callback_missing_code():
    code, state = auth.parse_callback("https://auth.tesla.com/void/callback?state=xyz")
    assert code == ""
