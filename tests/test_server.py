import pytest
from fastapi.testclient import TestClient

import app.server as server
from app.api import TokenManager

from .helpers import make_jwt

REAL_RUN_SYNC = server._run_sync


def fake_tesla_login(tm, sub):
    """What a successful Tesla login leaves in a token manager's account."""
    tm.refresh_token = make_jwt({"sub": sub, "iat": 1})
    tm.access_token = make_jwt({"sub": sub, "exp": 9999999999})
    tm._persist_token(tm.account.refresh_token_path, tm.refresh_token)
    tm._persist_token(tm.account.access_token_path, tm.access_token)


@pytest.fixture
def client(tmp_path, monkeypatch):
    invoice_dir = tmp_path / "invoices"
    invoice_dir.mkdir()
    monkeypatch.setattr(server.config, "invoice_path", invoice_dir)
    monkeypatch.setattr(server.config, "refresh_token_path", tmp_path / "secrets" / "refresh_token.txt")
    monkeypatch.setattr(server.config, "access_token_path", tmp_path / "secrets" / "access_token.txt")
    server._init_accounts()
    server._sync_state["accounts"].clear()
    # No `with` block: the download loop (lifespan) must not start in tests.
    # The X-Requested-With header is what the real frontend sends on every
    # request; mutating endpoints reject requests without it (CSRF).
    return TestClient(server.app, headers={"X-Requested-With": "tesla-invoices"})


def test_mutating_requests_require_csrf_header(client):
    (server.config.invoice_path / "invoice.pdf").write_bytes(b"%PDF-fake")
    bare = TestClient(server.app)  # simulates a cross-site form POST

    assert bare.post("/api/sync?month=cur").status_code == 403
    assert bare.post("/api/email/send-skipped").status_code == 403
    assert bare.delete("/api/files/invoice.pdf").status_code == 403
    assert (server.config.invoice_path / "invoice.pdf").exists()
    # The expensive export GETs also require the header: hot-linking them
    # cross-site (e.g. via an <img> tag) for resource exhaustion is blocked.
    assert bare.get("/api/export.csv").status_code == 403
    assert bare.get("/api/export.zip").status_code == 403
    # Other reads stay open — forms cannot exfiltrate GET responses cross-origin
    assert bare.get("/health").status_code == 200


def test_basic_auth_protects_everything_but_health(client, monkeypatch):
    import base64

    monkeypatch.setattr(server.config, "basic_auth_user", "admin")
    monkeypatch.setattr(server.config, "basic_auth_pass", "secret")

    assert client.get("/api/analytics").status_code == 401
    assert client.get("/").status_code == 401
    # The Docker healthcheck and HA watchdog poll without credentials
    assert client.get("/health").status_code == 200

    good = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
    assert client.get("/api/analytics", headers=good).status_code == 200

    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
    assert client.get("/api/analytics", headers=bad).status_code == 401
    assert client.get("/api/analytics", headers={"Authorization": "Basic %%%"}).status_code == 401
    assert client.get("/api/analytics", headers={"Authorization": "Bearer token"}).status_code == 401



def test_basic_auth_accepts_non_ascii_credentials(client, monkeypatch):
    # secrets.compare_digest raises TypeError on non-ASCII str — that used to
    # turn every request into a 500 once such a password was configured.
    import base64

    monkeypatch.setattr(server.config, "basic_auth_user", "admin")
    monkeypatch.setattr(server.config, "basic_auth_pass", "Grüße€")

    def header(credentials: str) -> dict[str, str]:
        return {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}

    assert client.get("/api/analytics", headers=header("admin:Grüße€")).status_code == 200
    assert client.get("/api/analytics", headers=header("admin:Grüsse")).status_code == 401
    assert client.get("/api/analytics", headers=header("ädmin:Grüße€")).status_code == 401

def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_download_serves_invoice(client):
    (server.config.invoice_path / "invoice.pdf").write_bytes(b"%PDF-fake")
    response = client.get("/api/download/invoice.pdf")
    assert response.status_code == 200
    assert response.content == b"%PDF-fake"
    assert response.headers["content-type"] == "application/pdf"


def test_download_missing_file_is_404(client):
    assert client.get("/api/download/nope.pdf").status_code == 404


def test_download_rejects_path_traversal(client, tmp_path):
    # A token file next to the invoice dir must never be reachable
    secret = tmp_path / "refresh_token.txt"
    secret.write_text("SECRET")

    for path in (
        "/api/download/..%2Frefresh_token.txt",
        "/api/download/%2E%2E%2Frefresh_token.txt",
        "/api/download/..%5Crefresh_token.txt",
    ):
        response = client.get(path)
        assert response.status_code in (404, 422), path
        assert b"SECRET" not in response.content, path


def test_download_rejects_non_invoice_suffix(client):
    (server.config.invoice_path / "notes.txt").write_text("hello")
    assert client.get("/api/download/notes.txt").status_code == 404


def test_analytics_aggregates(client):
    import json as jsonlib

    meta = {
        "type": "charging",
        "vin": "V1",
        "vehicle_name": "Car",
        "date": "2026-07-01T10:00:00",
        "filename": "a.pdf",
        "energy_kwh": 10.5,
        "total_cost": 4.2,
        "currency": "EUR",
    }
    (server.config.invoice_path / "a.json").write_text(jsonlib.dumps(meta))
    meta_usd = {**meta, "filename": "b.pdf", "date": "2026-06-01T10:00:00", "total_cost": 9.0, "currency": "USD"}
    (server.config.invoice_path / "b.json").write_text(jsonlib.dumps(meta_usd))

    result = client.get("/api/analytics").json()
    assert result["summary"]["invoice_count"] == 2
    assert result["summary"]["total_kwh"] == 21.0
    # Costs are grouped per currency, never blended into one number
    assert result["summary"]["cost_by_currency"] == {"EUR": 4.2, "USD": 9.0}
    # Auto-detected primary: the currency with the largest share
    assert result["summary"]["primary_currency"] == "USD"
    assert result["summary"]["vehicles"] == ["Car"]
    assert result["summary"]["email_configured"] is False


def test_analytics_respects_default_currency(client, monkeypatch):
    import json as jsonlib

    monkeypatch.setattr(server.config, "default_currency", "EUR")
    for name, currency, cost in (("a", "EUR", 1.0), ("b", "USD", 99.0)):
        meta = {"type": "subscription", "date": "2026-07-01", "filename": f"{name}.pdf",
                "total_cost": cost, "currency": currency}
        (server.config.invoice_path / f"{name}.json").write_text(jsonlib.dumps(meta))

    result = client.get("/api/analytics").json()
    assert result["summary"]["primary_currency"] == "EUR"


def test_download_inline_disposition(client):
    (server.config.invoice_path / "invoice.pdf").write_bytes(b"%PDF-fake")
    attachment = client.get("/api/download/invoice.pdf")
    assert attachment.headers["content-disposition"].startswith("attachment")
    inline = client.get("/api/download/invoice.pdf?inline=true")
    assert inline.headers["content-disposition"].startswith("inline")


def test_email_endpoint_requires_configuration(client):
    (server.config.invoice_path / "invoice.pdf").write_bytes(b"%PDF-fake")
    response = client.post("/api/email/invoice.pdf")
    assert response.status_code == 400


def test_email_endpoint_missing_file_is_404(client):
    assert client.post("/api/email/nope.pdf").status_code == 404


def test_email_endpoint_sends_single_pdf(client, monkeypatch):
    (server.config.invoice_path / "invoice.pdf").write_bytes(b"%PDF-fake")
    monkeypatch.setattr(server.config, "email_server", "mail.example.com")
    monkeypatch.setattr(server.config, "email_from", "a@example.com")
    monkeypatch.setattr(server.config, "email_to", "b@example.com")

    sent = []
    monkeypatch.setattr(server.emailer, "send_single", lambda pdf, to=None: sent.append((pdf.name, to)))

    response = client.post("/api/email/invoice.pdf")
    assert response.status_code == 200
    assert response.json() == {"status": "sent", "to": "b@example.com"}
    assert sent == [("invoice.pdf", "b@example.com")]


def test_email_endpoint_custom_recipient(client, monkeypatch):
    (server.config.invoice_path / "invoice.pdf").write_bytes(b"%PDF-fake")
    monkeypatch.setattr(server.config, "email_server", "mail.example.com")
    monkeypatch.setattr(server.config, "email_from", "a@example.com")
    # No configured default recipient: manual sends must still work
    monkeypatch.setattr(server.config, "email_to", "")

    sent = []
    monkeypatch.setattr(server.emailer, "send_single", lambda pdf, to=None: sent.append((pdf.name, to)))

    response = client.post("/api/email/invoice.pdf", json={"to": "custom@example.com"})
    assert response.status_code == 200
    assert response.json() == {"status": "sent", "to": "custom@example.com"}
    assert sent == [("invoice.pdf", "custom@example.com")]

    # Neither a valid body "to" nor a configured default -> 422
    assert client.post("/api/email/invoice.pdf", json={"to": "notanaddress"}).status_code == 422
    assert client.post("/api/email/invoice.pdf").status_code == 422


def test_send_skipped_endpoint(client, monkeypatch):
    monkeypatch.setattr(server.config, "email_server", "mail.example.com")
    monkeypatch.setattr(server.config, "email_from", "a@example.com")
    monkeypatch.setattr(server.config, "email_to", "b@example.com")

    calls = []

    def fake_send_skipped(to):
        calls.append(to)
        return {"sent": 3, "emails": 1}

    monkeypatch.setattr(server.emailer, "send_skipped", fake_send_skipped)

    response = client.post("/api/email/send-skipped")
    assert response.status_code == 200
    assert response.json() == {"status": "sent", "to": "b@example.com", "sent": 3, "emails": 1}
    assert calls == ["b@example.com"]

    # Custom recipient overrides the configured default
    response = client.post("/api/email/send-skipped", json={"to": "custom@example.com"})
    assert response.json()["to"] == "custom@example.com"

    assert client.post("/api/email/send-skipped", json={"to": "notanaddress"}).status_code == 422


def test_send_skipped_requires_configuration(client):
    assert client.post("/api/email/send-skipped").status_code == 400


def test_analytics_counts_skipped_and_reports_export_flag(client):
    import json as jsonlib

    base = {"type": "charging", "date": "2026-07-01T10:00:00", "total_cost": 1.0, "currency": "EUR"}
    for name in ("skipped", "sent", "fresh"):
        (server.config.invoice_path / f"{name}.pdf").write_bytes(b"%PDF-fake")
    (server.config.invoice_path / "skipped.json").write_text(jsonlib.dumps({**base, "email_skipped": 1}))
    (server.config.invoice_path / "sent.json").write_text(
        jsonlib.dumps({**base, "email_skipped": 1, "email_sent": 2})
    )
    (server.config.invoice_path / "fresh.json").write_text(jsonlib.dumps(base))
    # Orphan sidecar without its PDF: the combined export sends PDFs, so it
    # must not inflate the count
    (server.config.invoice_path / "orphan.json").write_text(jsonlib.dumps({**base, "email_skipped": 1}))

    summary = client.get("/api/analytics").json()["summary"]
    assert summary["email_skipped_count"] == 1
    assert summary["email_export_enabled"] is False


def test_analytics_reports_default_language(client, monkeypatch):
    assert client.get("/api/analytics").json()["summary"]["language"] == ""
    monkeypatch.setattr(server.config, "language", "de")
    assert client.get("/api/analytics").json()["summary"]["language"] == "de"


def test_internal_state_files_are_hidden(client):
    import json as jsonlib

    state = server.config.invoice_path / ".email_export_state.json"
    state.write_text(jsonlib.dumps({"export_enabled": True}))
    (server.config.invoice_path / "invoice.pdf").write_bytes(b"%PDF-fake")

    files = client.get("/api/files").json()["files"]
    assert [entry["name"] for entry in files] == ["invoice.pdf"]

    # The state file must not appear as an invoice row in analytics/CSV either
    assert client.get("/api/analytics").json()["summary"]["invoice_count"] == 0
    assert len(client.get("/api/export.csv").text.strip().splitlines()) == 1  # header only


def test_csv_export(client):
    import json as jsonlib

    meta = {
        "type": "charging",
        "vin": "V1",
        "vehicle_name": "Car",
        "date": "2026-07-01T10:00:00",
        "filename": "a.pdf",
        "energy_kwh": 10.5,
        "total_cost": 4.2,
        "currency": "EUR",
        "site_name": "Example City, Germany",
    }
    (server.config.invoice_path / "a.json").write_text(jsonlib.dumps(meta))

    response = client.get("/api/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("date,type,vehicle_name,vin,site_name")
    assert '2026-07-01T10:00:00,charging,Car,V1,"Example City, Germany"' in lines[1]


def test_zip_export_bundles_all_pdfs(client):
    import io
    import zipfile

    (server.config.invoice_path / "a.pdf").write_bytes(b"%PDF-a")
    (server.config.invoice_path / "b.pdf").write_bytes(b"%PDF-b")
    # Sidecars, internal state files and foreign suffixes must not leak in
    (server.config.invoice_path / "a.json").write_text('{"type": "charging"}')
    (server.config.invoice_path / ".email_export_state.json").write_text("{}")
    (server.config.invoice_path / "notes.txt").write_text("hello")

    response = client.get("/api/export.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "tesla_invoices.zip" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == ["a.pdf", "b.pdf"]
        assert archive.read("a.pdf") == b"%PDF-a"


def test_zip_export_without_invoices_is_valid_empty_zip(client):
    import io
    import zipfile

    response = client.get("/api/export.zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == []


def test_files_listing_returns_debug_info(client):
    (server.config.invoice_path / "a.json").write_text('{"type": "charging"}')
    (server.config.invoice_path / "invoice.pdf").write_bytes(b"%PDF-fake")

    result = client.get("/api/files").json()

    assert result["path"] == str(server.config.invoice_path.resolve())
    assert {entry["name"] for entry in result["files"]} == {"a.json", "invoice.pdf"}

    json_entry = next(entry for entry in result["files"] if entry["name"] == "a.json")
    assert json_entry["type"] == "json"
    assert '"type": "charging"' in json_entry["preview"]


def test_files_sorted_by_invoice_date_not_mtime(client):
    import json as jsonlib

    # Written last (newest mtime) but oldest invoice date
    old = server.config.invoice_path / "old.json"
    new = server.config.invoice_path / "new.json"
    new.write_text(jsonlib.dumps({"type": "charging", "date": "2026-06-01T10:00:00"}))
    old.write_text(jsonlib.dumps({"type": "charging", "date": "2024-01-01T10:00:00"}))

    result = client.get("/api/files").json()
    assert [entry["name"] for entry in result["files"]] == ["new.json", "old.json"]


def test_delete_file(client):
    pdf = server.config.invoice_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-fake")

    response = client.delete("/api/files/invoice.pdf")
    assert response.status_code == 200
    assert not pdf.exists()

    assert client.delete("/api/files/invoice.pdf").status_code == 404


def test_delete_pdf_removes_sidecar_too(client):
    pdf = server.config.invoice_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-fake")
    sidecar = pdf.with_suffix(".json")
    sidecar.write_text('{"type": "charging"}')

    response = client.delete("/api/files/invoice.pdf")
    assert response.status_code == 200
    assert response.json()["sidecar_deleted"] is True
    # No orphan sidecar: it would keep a ghost row in the dashboard and CSV
    assert not pdf.exists() and not sidecar.exists()


def test_rescan_conflicts_with_running_sync(client, monkeypatch):
    import types

    monkeypatch.setattr(server, "_sync_lock", types.SimpleNamespace(locked=lambda: True))
    assert client.post("/api/files/rescan").status_code == 409


def test_delete_rejects_traversal_and_foreign_suffixes(client, tmp_path):
    secret = tmp_path / "refresh_token.txt"
    secret.write_text("SECRET")
    (server.config.invoice_path / "notes.txt").write_text("hello")

    assert client.delete("/api/files/..%2Frefresh_token.txt").status_code in (404, 422)
    assert secret.exists()
    assert client.delete("/api/files/notes.txt").status_code == 404
    assert (server.config.invoice_path / "notes.txt").exists()


def test_rescan_pdfs_updates_metadata(client, monkeypatch):
    import json as jsonlib

    pdf_path = server.config.invoice_path / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    metadata_path = pdf_path.with_suffix(".json")
    metadata_path.write_text(jsonlib.dumps({"type": "subscription", "currency": ""}))

    monkeypatch.setattr(server.downloader, "extract_cost_from_pdf", lambda *_args, **_kwargs: (12.34, "EUR"))

    response = client.post("/api/files/rescan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["updated"] == 1
    assert jsonlib.loads(metadata_path.read_text())["total_cost"] == 12.34
    assert jsonlib.loads(metadata_path.read_text())["currency"] == "EUR"


def test_sync_rejects_bad_month(client):
    assert client.post("/api/sync?month=banana").status_code == 422


def test_auth_login_start_returns_authorize_url(client):
    body = client.post("/api/auth/login/start").json()
    assert body["url"].startswith("https://auth.tesla.com/oauth2/v3/authorize?")
    assert "code_challenge=" in body["url"]


def test_auth_login_complete_exchanges_and_kicks_off_sync(client, monkeypatch):
    # Start the flow so the server holds the PKCE verifier + state
    client.post("/api/auth/login/start")
    state = server._pending_login["state"]

    calls = []

    def fake_exchange(tm, code, verifier):
        calls.append((code, verifier))
        fake_tesla_login(tm, "user-1")

    monkeypatch.setattr(TokenManager, "exchange_authorization_code", fake_exchange)
    started = []

    async def fake_run_sync(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr(server, "_run_sync", fake_run_sync)

    response = client.post("/api/auth/login/complete", json={
        "callback_url": f"tesla://auth/callback?code=THECODE&state={state}",
    })
    assert response.status_code == 200
    assert response.json() == {"status": "connected", "account": "default"}
    assert calls and calls[0][0] == "THECODE"
    assert server._pending_login == {}  # cleared after use
    assert server.downloader.client.token_manager.user_id() == "user-1"


def test_auth_login_complete_rejects_state_mismatch(client, monkeypatch):
    client.post("/api/auth/login/start")
    monkeypatch.setattr(
        TokenManager,
        "exchange_authorization_code",
        lambda tm, code, verifier: (_ for _ in ()).throw(AssertionError("must not exchange on mismatch")),
    )
    response = client.post("/api/auth/login/complete", json={
        "callback_url": "tesla://auth/callback?code=X&state=WRONG",
    })
    assert response.status_code == 422


def test_auth_login_complete_without_pending_is_409(client):
    server._pending_login.clear()
    response = client.post("/api/auth/login/complete", json={
        "callback_url": "tesla://auth/callback?code=ABC&state=x",
    })
    assert response.status_code == 409


def test_auth_login_complete_requires_code(client):
    client.post("/api/auth/login/start")
    response = client.post("/api/auth/login/complete", json={
        "callback_url": "tesla://auth/callback?state=only",
    })
    assert response.status_code == 422


def test_analytics_reports_token_configured(client):
    # The fixture config has no token files and no env tokens
    assert client.get("/api/analytics").json()["summary"]["token_configured"] is False


def test_health_fails_when_download_loop_died(client, monkeypatch):
    import types

    monkeypatch.setattr(server, "_loop_task", types.SimpleNamespace(done=lambda: True))
    assert client.get("/health").status_code == 500

    monkeypatch.setattr(server, "_loop_task", types.SimpleNamespace(done=lambda: False))
    assert client.get("/health").status_code == 200


def test_csv_export_escapes_formula_injection(client):
    import json as jsonlib

    meta = {
        "type": "charging",
        "date": "2026-07-01T10:00:00",
        "filename": "a.pdf",
        "site_name": "=HYPERLINK(\"http://evil\")",
        "total_cost": -4.2,
        "currency": "EUR",
    }
    (server.config.invoice_path / "a.json").write_text(jsonlib.dumps(meta))

    response = client.get("/api/export.csv")
    assert "'=HYPERLINK" in response.text  # formula neutralized
    assert "-4.2" in response.text  # negative numbers stay numbers


def test_rescan_negates_credit_notes(client, monkeypatch):
    import json as jsonlib

    pdf_path = server.config.invoice_path / "creditnote.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    metadata_path = pdf_path.with_suffix(".json")
    metadata_path.write_text(jsonlib.dumps({"type": "subscription", "is_credit_note": True}))

    monkeypatch.setattr(server.downloader, "extract_cost_from_pdf", lambda *_args, **_kwargs: (9.99, "EUR"))

    response = client.post("/api/files/rescan")

    assert response.status_code == 200
    assert jsonlib.loads(metadata_path.read_text())["total_cost"] == -9.99


def test_auth_token_stores_refresh_token_and_reports_connected(client, monkeypatch):
    stored = []

    def fake_set(tm, token):
        stored.append(token)
        fake_tesla_login(tm, "user-1")

    monkeypatch.setattr(TokenManager, "set_refresh_token", fake_set)

    async def fake_run_sync(*args, **kwargs):
        pass

    monkeypatch.setattr(server, "_run_sync", fake_run_sync)

    response = client.post("/api/auth/token", json={"refresh_token": "  eyJx.token  "})
    assert response.status_code == 200
    assert response.json() == {"status": "connected", "account": "default"}
    assert stored == ["eyJx.token"]  # whitespace from copy-paste stripped


def test_auth_token_rejects_empty(client, monkeypatch):
    monkeypatch.setattr(
        TokenManager,
        "set_refresh_token",
        lambda tm, token: (_ for _ in ()).throw(AssertionError("must not be called for empty input")),
    )
    assert client.post("/api/auth/token", json={"refresh_token": "   "}).status_code == 422


def test_auth_token_invalid_token_is_502(client, monkeypatch):
    from app.api import TeslaAuthError

    def reject(tm, token):
        raise TeslaAuthError("Renewing the Tesla access token failed")

    monkeypatch.setattr(TokenManager, "set_refresh_token", reject)
    response = client.post("/api/auth/token", json={"refresh_token": "eyJbogus"})
    assert response.status_code == 502


def test_index_and_static_assets_are_served(client):
    assert client.get("/").status_code == 200
    for path in (
        "/static/css/style.css",
        "/static/js/i18n.js",
        "/static/js/app.js",
        "/static/i18n/languages.json",
        "/static/i18n/en.json",
    ):
        assert client.get(path).status_code == 200, path


def test_analytics_reports_invoice_type_switches(client):
    summary = client.get("/api/analytics").json()["summary"]
    assert summary["charging_invoice_enabled"] is True
    assert summary["subscription_invoice_enabled"] is True


def test_pwa_manifest_and_service_worker_served_from_root(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    body = manifest.json()
    # Relative start_url/scope keep the PWA inside the HA ingress prefix
    assert body["display"] == "standalone"
    assert body["start_url"] == "."
    assert body["scope"] == "."

    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert "javascript" in sw.headers["content-type"]

    assert client.get("/favicon.ico").status_code == 200


# ---------- several Tesla accounts ----------


def connect(client, monkeypatch, sub, **target):
    monkeypatch.setattr(TokenManager, "set_refresh_token", lambda tm, token: fake_tesla_login(tm, sub))
    return client.post("/api/auth/token", json={"refresh_token": "eyJx", **target})


@pytest.fixture
def no_sync(monkeypatch):
    started = []

    async def fake_run_sync(*args, **kwargs):
        started.append(kwargs.get("account_ids"))

    monkeypatch.setattr(server, "_run_sync", fake_run_sync)
    return started


def test_add_second_account_and_list_it(client, monkeypatch, no_sync):
    assert connect(client, monkeypatch, "user-1").json()["account"] == "default"
    response = connect(client, monkeypatch, "user-2", name="Ana")
    assert response.status_code == 200
    ana = response.json()["account"]
    assert ana != "default"
    assert no_sync[-1] == [ana]  # the first sync after login is for that account only

    accounts = client.get("/api/analytics").json()["summary"]["accounts"]
    assert [(a["id"], a["name"], a["connected"]) for a in accounts] == [
        ("default", "", True),
        (ana, "Ana", True),
    ]
    assert "user-2" not in client.get("/api/analytics").text  # the Tesla user id never leaves


def test_same_tesla_login_twice_is_refused(client, monkeypatch, no_sync):
    connect(client, monkeypatch, "user-1")
    client.patch("/api/accounts/default", json={"name": "Pedro"})

    response = connect(client, monkeypatch, "user-1", name="Ana")

    assert response.status_code == 409
    assert "Pedro" in response.json()["detail"]
    assert [a.id for a in server.registry.accounts()] == ["default"]
    assert not list((server.config.refresh_token_path.parent / "accounts").glob("*"))


def test_failed_login_for_a_new_account_leaves_nothing(client, monkeypatch, no_sync):
    from app.api import TeslaAuthError

    def reject(tm, token):
        tm._persist_token(tm.account.refresh_token_path, "half-written")
        raise TeslaAuthError("nope")

    monkeypatch.setattr(TokenManager, "set_refresh_token", reject)
    response = client.post("/api/auth/token", json={"refresh_token": "eyJx", "name": "Ana"})

    assert response.status_code == 502
    assert [a.id for a in server.registry.accounts()] == ["default"]
    assert not list((server.config.refresh_token_path.parent / "accounts").glob("*"))
    assert no_sync == []


def test_login_target_validation(client):
    assert client.post("/api/auth/login/start", json={"account": "nope"}).status_code == 404
    assert client.post("/api/auth/login/start", json={"name": "  "}).status_code == 422
    assert client.post("/api/auth/token", json={"refresh_token": "x", "account": "nope"}).status_code == 404


def test_browser_login_for_a_new_account(client, monkeypatch, no_sync):
    client.post("/api/auth/login/start", json={"name": "Ana"})
    state = server._pending_login["state"]
    monkeypatch.setattr(
        TokenManager, "exchange_authorization_code", lambda tm, code, verifier: fake_tesla_login(tm, "user-2")
    )
    response = client.post(
        "/api/auth/login/complete", json={"callback_url": f"tesla://auth/callback?code=C&state={state}"}
    )
    assert response.status_code == 200
    assert response.json()["account"] != "default"
    assert [a.name for a in server.registry.accounts()] == ["", "Ana"]


def test_rename_and_remove_account(client, monkeypatch, no_sync):
    ana = connect(client, monkeypatch, "user-2", name="Ana").json()["account"]

    assert client.patch(f"/api/accounts/{ana}", json={"name": "Ana M."}).status_code == 200
    assert client.patch("/api/accounts/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/accounts/default").status_code == 409
    assert client.delete(f"/api/accounts/{ana}").status_code == 200
    assert client.delete(f"/api/accounts/{ana}").status_code == 404
    assert [a["id"] for a in client.get("/api/analytics").json()["summary"]["accounts"]] == ["default"]


def test_one_failing_account_does_not_stop_the_others(client, monkeypatch, no_sync):
    import asyncio

    connect(client, monkeypatch, "user-1")
    ana = connect(client, monkeypatch, "user-2", name="Ana").json()["account"]
    monkeypatch.setattr(server.emailer, "send_pending", lambda *_: None)

    synced = []

    def fake_download(self, months):
        if self.account_id == "default":
            raise RuntimeError("Tesla said no")
        synced.append(self.account_id)

    monkeypatch.setattr(server.InvoiceDownloader, "download_invoices", fake_download)
    server._sync_state["last_success"] = None
    asyncio.run(REAL_RUN_SYNC(None, kind="test"))

    assert synced == [ana]
    assert server._sync_state["accounts"] == {"default": "Tesla said no", ana: "ok"}
    assert server._sync_state["last_success"]  # one account synced
    assert "Tesla said no" in server._sync_state["last_result"]


def test_single_account_sync_state_reads_as_before(client, monkeypatch):
    import asyncio

    monkeypatch.setattr(server.emailer, "send_pending", lambda *_: None)
    # No token anywhere: the default account still runs and reports why it failed
    asyncio.run(server._run_sync(None, kind="test"))
    assert server._sync_state["last_result"].startswith("No Tesla token configured")


def test_accounts_without_a_token_are_not_synced(client, monkeypatch, no_sync):
    import asyncio

    ana = connect(client, monkeypatch, "user-2", name="Ana").json()["account"]  # the default has no token
    monkeypatch.setattr(server.emailer, "send_pending", lambda *_: None)
    synced = []
    monkeypatch.setattr(
        server.InvoiceDownloader, "download_invoices", lambda self, months: synced.append(self.account_id)
    )

    asyncio.run(REAL_RUN_SYNC(None, kind="test"))

    assert synced == [ana]
    assert server._sync_state["last_result"] == "ok"


def test_new_account_name_can_come_with_the_pasted_address(client, monkeypatch, no_sync):
    client.post("/api/auth/login/start")  # the dashboard starts the login before the name is typed
    state = server._pending_login["state"]
    monkeypatch.setattr(
        TokenManager, "exchange_authorization_code", lambda tm, code, verifier: fake_tesla_login(tm, "user-2")
    )
    response = client.post(
        "/api/auth/login/complete",
        json={"callback_url": f"tesla://auth/callback?code=C&state={state}", "name": "Ana"},
    )
    assert response.status_code == 200
    assert [a.name for a in server.registry.accounts()] == ["", "Ana"]
    assert not server.downloader.client.token_manager.has_token()  # the main account was not touched


def test_pasting_a_token_already_in_use_is_refused_before_tesla_sees_it(client, monkeypatch, no_sync):
    connect(client, monkeypatch, "user-1")
    monkeypatch.setattr(
        TokenManager,
        "set_refresh_token",
        lambda tm, token: (_ for _ in ()).throw(AssertionError("verifying would rotate the main account's session")),
    )
    response = client.post(
        "/api/auth/token", json={"refresh_token": make_jwt({"sub": "user-1", "iat": 2}), "name": "Ana"}
    )
    assert response.status_code == 409


def test_added_account_cannot_be_renamed_to_blank(client, monkeypatch, no_sync):
    ana = connect(client, monkeypatch, "user-2", name="Ana").json()["account"]
    assert client.patch(f"/api/accounts/{ana}", json={"name": "  "}).status_code == 422
    assert client.patch("/api/accounts/default", json={"name": ""}).status_code == 200


def test_no_email_export_when_every_account_failed(client, monkeypatch):
    import asyncio

    sent = []
    monkeypatch.setattr(server.emailer, "send_pending", lambda *_: sent.append(1))
    asyncio.run(REAL_RUN_SYNC(None, kind="test"))  # no token anywhere: the only account fails
    assert sent == []


def test_reconnect_waits_for_a_running_token_refresh(client, monkeypatch, no_sync):
    import threading
    import time

    connect(client, monkeypatch, "user-1")
    tm = server.downloader.client.token_manager
    old = make_jwt({"sub": "user-1", "exp": 1})
    refreshing = threading.Event()

    def slow_refresh():
        # What a sync's refresh does: hold the lock, then persist its token.
        with tm._lock:
            refreshing.set()
            time.sleep(0.3)
            tm._persist_token(tm.account.access_token_path, old)

    worker = threading.Thread(target=slow_refresh)
    worker.start()
    refreshing.wait()
    new = make_jwt({"sub": "user-1", "exp": 9999999999, "iat": 5})

    def fresh_login(staged_tm, token):
        staged_tm._persist_token(staged_tm.account.refresh_token_path, new)
        staged_tm._persist_token(staged_tm.account.access_token_path, new)

    monkeypatch.setattr(TokenManager, "set_refresh_token", fresh_login)
    response = client.post("/api/auth/token", json={"refresh_token": "opaque", "account": "default"})
    worker.join()

    assert response.status_code == 200
    assert server.config.access_token_path.read_text() == new


def test_dashboard_assets_are_served_under_content_hashes(client):
    import hashlib
    import re

    html = client.get("/").text
    assert client.get("/").headers["cache-control"] == "no-cache"
    expected = hashlib.sha256((server.STATIC_DIR / "js" / "app.js").read_bytes()).hexdigest()[:12]
    assert f'src="static/js/app.js?v={expected}"' in html
    # Every static reference in the page carries a hash, so none can go stale
    assert not re.findall(r'(?:href|src)="static/[^"?]+"', html)

    current = client.get(f"/static/js/app.js?v={expected}")
    assert current.headers["cache-control"] == "public, max-age=31536000, immutable"
    # An outdated hash, or a file fetched without one (translations), is revalidated
    assert client.get("/static/js/app.js?v=000000000000").headers["cache-control"] == "no-cache"
    assert client.get("/static/i18n/en.json").headers["cache-control"] == "no-cache"
    assert client.get("/sw.js").headers["cache-control"] == "no-cache"


def test_api_json_is_compressed_but_pdfs_are_not(client):
    import json as jsonlib

    for i in range(30):
        meta = {"type": "charging", "date": f"2026-07-{i % 28 + 1:02d}T10:00:00", "filename": f"{i}.pdf"}
        (server.config.invoice_path / f"{i}.json").write_text(jsonlib.dumps(meta))
    (server.config.invoice_path / "big.pdf").write_bytes(b"%PDF-1.4" + b"x" * 5000)
    gzip = {"Accept-Encoding": "gzip"}

    assert client.get("/api/analytics", headers=gzip).headers.get("content-encoding") == "gzip"
    assert "content-encoding" not in client.get("/api/download/big.pdf", headers=gzip).headers
