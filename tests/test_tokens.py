import base64
import json
import ssl

import pytest
import requests

from app.api import AUTH_URL, TeslaAPIClient, TeslaAPIError, TeslaAuthError, TLS13Adapter, TokenManager

from .helpers import make_config, make_jwt


class TestJwtDecode:
    def test_decodes_urlsafe_payload(self):
        # "ÿÿ" forces '_'/'-' characters into the base64url payload; the old
        # implementation used standard b64decode and failed on such tokens.
        payload = {"iat": 1700000000, "sub": "ÿÿÿÿ"}
        token = make_jwt(payload)
        b64_part = token.split(".")[1]
        assert "_" in b64_part or "-" in b64_part, "test payload must exercise base64url alphabet"

        assert TokenManager.jwt_decode(token) == payload

    def test_decodes_standard_payload(self):
        payload = {"iat": 123, "exp": 456}
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        assert TokenManager.jwt_decode(f"h.{body}.s") == payload

    def test_invalid_token_returns_empty(self):
        assert TokenManager.jwt_decode("") == {}
        assert TokenManager.jwt_decode("not-a-jwt") == {}
        assert TokenManager.jwt_decode("a.%%%.c") == {}


class TestDetermineBestToken:
    def test_no_tokens_raises(self, tmp_path):
        tm = TokenManager(make_config(tmp_path))
        with pytest.raises(TeslaAuthError):
            tm._determine_best_token("access")

    def test_newer_file_token_wins(self, tmp_path):
        file_token = make_jwt({"iat": 200})
        env_token = make_jwt({"iat": 100})
        config = make_config(tmp_path, env_access_token=env_token)
        config.access_token_path.write_text(file_token)

        assert TokenManager(config)._determine_best_token("access") == file_token

    def test_newer_env_token_wins(self, tmp_path):
        file_token = make_jwt({"iat": 100})
        env_token = make_jwt({"iat": 200})
        config = make_config(tmp_path, env_access_token=env_token)
        config.access_token_path.write_text(file_token)

        assert TokenManager(config)._determine_best_token("access") == env_token

    def test_env_only_token_is_persisted(self, tmp_path):
        env_token = make_jwt({"iat": 100})
        config = make_config(tmp_path, env_refresh_token=env_token)

        assert TokenManager(config)._determine_best_token("refresh") == env_token
        assert config.refresh_token_path.read_text() == env_token

    def test_persisted_tokens_are_not_world_readable(self, tmp_path):
        env_token = make_jwt({"iat": 100})
        config = make_config(tmp_path, env_refresh_token=env_token)

        TokenManager(config)._determine_best_token("refresh")
        assert config.refresh_token_path.stat().st_mode & 0o777 == 0o600

    def test_refresh_token_alone_is_enough(self, tmp_path, monkeypatch):
        # Users often only have a refresh token; the access token must be
        # bootstrapped via the refresh flow instead of failing at startup.
        config = make_config(tmp_path, env_refresh_token=make_jwt({"iat": 100}))
        tm = TokenManager(config)
        tm.load_tokens()
        assert tm.access_token == ""

        monkeypatch.setattr(
            tm, "_auth_post", lambda payload: {"access_token": make_jwt({"iat": 200, "exp": 99999999999})}
        )

        tm.refresh_access_token_if_needed()
        assert TokenManager.jwt_decode(tm.access_token).get("iat") == 200
        assert config.access_token_path.read_text() == tm.access_token

    def test_no_tokens_at_all_raises_on_load(self, tmp_path):
        tm = TokenManager(make_config(tmp_path))
        with pytest.raises(TeslaAuthError):
            tm.load_tokens()

    def test_access_token_alone_works_until_expiry(self, tmp_path, monkeypatch):
        # Standalone users may prefer a short-lived access token over handing
        # the app a long-lived refresh token; it must work until it expires.
        config = make_config(tmp_path, env_access_token=make_jwt({"iat": 100, "exp": 99999999999}))
        tm = TokenManager(config)
        tm.load_tokens()

        def fail(_payload):
            raise AssertionError("the token endpoint must not be contacted without a refresh token")

        monkeypatch.setattr(tm, "_auth_post", fail)
        tm.refresh_access_token_if_needed()  # must not raise or refresh
        assert tm.refresh_token == ""

    def test_expired_access_token_without_refresh_token_raises(self, tmp_path):
        config = make_config(tmp_path, env_access_token=make_jwt({"iat": 100, "exp": 1000}))
        tm = TokenManager(config)
        tm.load_tokens()
        with pytest.raises(TeslaAuthError):
            tm.refresh_access_token_if_needed()

    def test_refresh_without_refresh_token_raises(self, tmp_path):
        tm = TokenManager(make_config(tmp_path))
        with pytest.raises(TeslaAuthError):
            tm.refresh_access_token()

    def test_rotated_refresh_token_is_persisted(self, tmp_path, monkeypatch):
        config = make_config(tmp_path, env_refresh_token=make_jwt({"iat": 100}))
        tm = TokenManager(config)
        tm.load_tokens()

        rotated = make_jwt({"iat": 300})
        monkeypatch.setattr(
            tm,
            "_auth_post",
            lambda payload: {
                "access_token": make_jwt({"iat": 300, "exp": 99999999999}),
                "refresh_token": rotated,
            },
        )

        tm.refresh_access_token_if_needed()
        assert tm.refresh_token == rotated
        assert config.refresh_token_path.read_text() == rotated


class TestAPIClient:
    def test_all_tesla_hosts_are_pinned_to_tls13(self, tmp_path):
        # Tesla hosts reject TLS < 1.3 with 403: auth.tesla.com first
        # (teslamate-org/teslamate#5406), then the Owner API hosts too
        # (bassmaster187/TeslaLogger@b244443).
        config = make_config(tmp_path)
        client = TeslaAPIClient(config, TokenManager(config))

        for url in (AUTH_URL, "https://owner-api.teslamotors.com/api/1/products"):
            adapter = client.sess.get_adapter(url)
            assert isinstance(adapter, TLS13Adapter), url
            context = adapter.poolmanager.connection_pool_kw["ssl_context"]
            assert context.minimum_version == ssl.TLSVersion.TLSv1_3, url

    def test_403_forces_token_refresh_and_retries(self, tmp_path, monkeypatch):
        # Tesla can reject an access token before its exp (e.g. one poisoned
        # by an HTTP/1.1 refresh); the client must not trust exp alone.
        config = make_config(tmp_path)
        tm = TokenManager(config)
        tm.access_token = make_jwt({"iat": 100, "exp": 99999999999})
        client = TeslaAPIClient(config, tm)
        monkeypatch.setattr(tm, "refresh_access_token_if_needed", lambda: None)

        refreshed_token = make_jwt({"iat": 200, "exp": 99999999999})

        def fake_refresh():
            tm.access_token = refreshed_token

        monkeypatch.setattr(tm, "refresh_access_token", fake_refresh)

        class FakeResponse:
            def __init__(self, status_code, token):
                self.status_code = status_code
                self.token = token
                self.headers = {"Content-Type": "application/json"}

            def raise_for_status(self):
                if self.status_code >= 400:
                    error = requests.exceptions.HTTPError(response=self)
                    raise error

            def json(self):
                return {"used_token": self.token}

        calls = []

        def fake_request(method, url, headers, **kwargs):
            token = headers["Authorization"].removeprefix("Bearer ")
            calls.append(token)
            return FakeResponse(403 if token != refreshed_token else 200, token)

        monkeypatch.setattr(client.sess, "request", fake_request)

        assert client.base_req("https://owner-api.teslamotors.com/api/1/products") == {
            "used_token": refreshed_token
        }
        assert len(calls) == 2

    def test_connection_reset_retries_on_fresh_connection(self, tmp_path, monkeypatch):
        # The Akamai gateway RSTs connections (104) — the request must be
        # retried after dropping the pooled sockets, POST included.
        config = make_config(tmp_path)
        tm = TokenManager(config)
        tm.access_token = make_jwt({"iat": 100, "exp": 99999999999})
        client = TeslaAPIClient(config, tm)
        monkeypatch.setattr(tm, "refresh_access_token_if_needed", lambda: None)
        monkeypatch.setattr("app.api.time.sleep", lambda s: None)

        pool_flushes = []
        monkeypatch.setattr(client.sess, "close", lambda: pool_flushes.append(True))

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True}

        attempts = []

        def fake_request(method, url, **kwargs):
            attempts.append(method)
            if len(attempts) == 1:
                raise requests.exceptions.ConnectionError(
                    "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
                )
            return FakeResponse()

        monkeypatch.setattr(client.sess, "request", fake_request)

        assert client.base_req("https://example.tesla.com/graphql", method="post") == {"ok": True}
        assert len(attempts) == 2
        assert pool_flushes, "pooled connections must be dropped before the retry"

    def test_non_pdf_invoice_response_raises(self, tmp_path):
        config = make_config(tmp_path)
        client = TeslaAPIClient(config, TokenManager(config))

        with pytest.raises(TeslaAPIError):
            client._expect_pdf({"error": "not found"}, "charging invoice x")
        # A PDF Content-Type with an HTML error body must not reach the disk
        with pytest.raises(TeslaAPIError):
            client._expect_pdf(b"<html>Access Denied</html>", "charging invoice x")
        assert client._expect_pdf(b"%PDF-1.4", "charging invoice x") == b"%PDF-1.4"

    def test_charging_history_paginates_and_filters_by_vin(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        client = TeslaAPIClient(config, TokenManager(config))

        payloads = []

        def fake_base_req(url, method="get", json_data=None, params=None, extra_headers=None):
            payloads.append(json_data)
            page = json_data["variables"]["pageNumber"]
            return {
                "data": {
                    "me": {
                        "charging": {
                            "historyV2": {
                                "data": [{"chargeSessionId": f"session-{page}"}],
                                "totalResults": 2,
                                "hasMoreData": page < 2,
                                "pageNumber": page,
                            }
                        }
                    }
                }
            }

        monkeypatch.setattr(client, "base_req", fake_base_req)

        sessions = client.get_charging_history("VIN123")

        assert [s["chargeSessionId"] for s in sessions] == ["session-1", "session-2"]
        assert [p["variables"]["pageNumber"] for p in payloads] == [1, 2]
        # Without the vin variable the gateway returns account-wide history,
        # duplicating every invoice once per vehicle on multi-vehicle accounts
        assert all(p["variables"]["vin"] == "VIN123" for p in payloads)

    def test_charging_history_reports_graphql_errors(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        client = TeslaAPIClient(config, TokenManager(config))
        monkeypatch.setattr(
            client, "base_req", lambda *a, **kw: {"errors": [{"message": "boom"}], "data": None}
        )

        with pytest.raises(TeslaAPIError):
            client.get_charging_history("VIN123")
