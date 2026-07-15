import pytest
from fastapi.testclient import TestClient

import app.server as server


@pytest.fixture
def client(tmp_path, monkeypatch):
    invoice_dir = tmp_path / "invoices"
    invoice_dir.mkdir()
    monkeypatch.setattr(server.config, "invoice_path", invoice_dir)
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
    # Reads stay open — forms cannot exfiltrate GET responses cross-origin
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
    monkeypatch.setattr(
        server.downloader.client.token_manager,
        "exchange_authorization_code",
        lambda code, verifier: calls.append((code, verifier)),
    )
    started = []

    async def fake_run_sync(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr(server, "_run_sync", fake_run_sync)

    response = client.post("/api/auth/login/complete", json={
        "callback_url": f"tesla://auth/callback?code=THECODE&state={state}",
    })
    assert response.status_code == 200
    assert response.json() == {"status": "connected"}
    assert calls and calls[0][0] == "THECODE"
    assert server._pending_login == {}  # cleared after use


def test_auth_login_complete_rejects_state_mismatch(client, monkeypatch):
    client.post("/api/auth/login/start")
    monkeypatch.setattr(
        server.downloader.client.token_manager,
        "exchange_authorization_code",
        lambda code, verifier: (_ for _ in ()).throw(AssertionError("must not exchange on mismatch")),
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
    monkeypatch.setattr(
        server.downloader.client.token_manager,
        "set_refresh_token",
        lambda token: stored.append(token),
    )

    async def fake_run_sync(*args, **kwargs):
        pass

    monkeypatch.setattr(server, "_run_sync", fake_run_sync)

    response = client.post("/api/auth/token", json={"refresh_token": "  eyJx.token  "})
    assert response.status_code == 200
    assert response.json() == {"status": "connected"}
    assert stored == ["eyJx.token"]  # whitespace from copy-paste stripped


def test_auth_token_rejects_empty(client, monkeypatch):
    monkeypatch.setattr(
        server.downloader.client.token_manager,
        "set_refresh_token",
        lambda token: (_ for _ in ()).throw(AssertionError("must not be called for empty input")),
    )
    assert client.post("/api/auth/token", json={"refresh_token": "   "}).status_code == 422


def test_auth_token_invalid_token_is_502(client, monkeypatch):
    from app.api import TeslaAuthError

    def reject(token):
        raise TeslaAuthError("Renewing the Tesla access token failed")

    monkeypatch.setattr(server.downloader.client.token_manager, "set_refresh_token", reject)
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
