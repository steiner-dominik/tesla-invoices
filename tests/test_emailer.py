import json

from app.emailer import STATE_FILENAME, EmailExporter

from .helpers import make_config


def _install_dummy_smtp(monkeypatch, sent_messages):
    class DummySMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, *args):
            pass

        def send_message(self, msg):
            sent_messages.append(msg)

    monkeypatch.setattr("smtplib.SMTP", DummySMTP)


def _forbid_smtp(monkeypatch, reason):
    def fail(*args, **kwargs):
        raise AssertionError(reason)

    monkeypatch.setattr("smtplib.SMTP", fail)
    monkeypatch.setattr("smtplib.SMTP_SSL", fail)


def _mark_export_enabled(config):
    """Prime the state file as if export had already been enabled earlier,
    so send_pending() is in its steady sending state."""
    (config.invoice_path / STATE_FILENAME).write_text(json.dumps({"export_enabled": True}))


def _email_config(tmp_path):
    config = make_config(
        tmp_path,
        enable_email_export=True,
        email_from="a@example.com",
        email_to="b@example.com",
        email_server="mail.example.com",
    )
    config.invoice_path.mkdir(parents=True)
    return config


def test_pending_skips_already_sent(tmp_path):
    config = _email_config(tmp_path)
    sent = config.invoice_path / "sent.pdf"
    sent.write_bytes(b"%PDF")
    sent.with_suffix(".json").write_text(json.dumps({"email_sent": 1234}))

    unsent = config.invoice_path / "unsent.pdf"
    unsent.write_bytes(b"%PDF")
    unsent.with_suffix(".json").write_text(json.dumps({"type": "charging"}))

    no_metadata = config.invoice_path / "no_metadata.pdf"
    no_metadata.write_bytes(b"%PDF")

    pending = EmailExporter(config)._pending_invoices()
    assert pending == [no_metadata, unsent]


def test_send_pending_noop_when_disabled(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.invoice_path.mkdir(parents=True)

    def fail(*args, **kwargs):
        raise AssertionError("SMTP must not be contacted when export is disabled")

    monkeypatch.setattr("smtplib.SMTP", fail)
    EmailExporter(config).send_pending()


def test_send_single_sends_and_marks_metadata(tmp_path, monkeypatch):
    config = _email_config(tmp_path)
    pdf = config.invoice_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF")

    sent_messages = []

    class DummySMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, *args):
            pass

        def send_message(self, msg):
            sent_messages.append(msg)

    monkeypatch.setattr("smtplib.SMTP", DummySMTP)
    EmailExporter(config).send_single(pdf)

    assert len(sent_messages) == 1
    assert sent_messages[0]["To"] == "b@example.com"
    metadata = json.loads(pdf.with_suffix(".json").read_text())
    assert "email_sent" in metadata


def test_port_465_uses_implicit_tls(tmp_path, monkeypatch):
    config = _email_config(tmp_path)
    config.email_server_port = 465
    pdf = config.invoice_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF")

    sent_messages = []

    class DummySMTPSSL:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            raise AssertionError("STARTTLS must not be used on port 465")

        def login(self, *args):
            pass

        def send_message(self, msg):
            sent_messages.append(msg)

    def no_plain_smtp(*args, **kwargs):
        raise AssertionError("plain SMTP must not be used on port 465")

    monkeypatch.setattr("smtplib.SMTP_SSL", DummySMTPSSL)
    monkeypatch.setattr("smtplib.SMTP", no_plain_smtp)
    EmailExporter(config).send_single(pdf)

    assert len(sent_messages) == 1


def test_send_single_custom_recipient(tmp_path, monkeypatch):
    config = _email_config(tmp_path)
    pdf = config.invoice_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF")

    sent_messages = []

    class DummySMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, *args):
            pass

        def send_message(self, msg):
            sent_messages.append(msg)

    monkeypatch.setattr("smtplib.SMTP", DummySMTP)
    EmailExporter(config).send_single(pdf, to="custom@example.com")

    assert sent_messages[0]["To"] == "custom@example.com"


def test_is_configured(tmp_path):
    assert EmailExporter(_email_config(tmp_path)).is_configured
    assert not EmailExporter(make_config(tmp_path)).is_configured
    # "to" is not required for manual sends: the recipient is entered ad hoc
    no_to = make_config(tmp_path, email_server="mail.example.com", email_from="a@example.com")
    assert EmailExporter(no_to).is_configured


def test_send_pending_no_connection_without_pending(tmp_path, monkeypatch):
    config = _email_config(tmp_path)
    _mark_export_enabled(config)
    pdf = config.invoice_path / "sent.pdf"
    pdf.write_bytes(b"%PDF")
    pdf.with_suffix(".json").write_text(json.dumps({"email_sent": 1}))

    _forbid_smtp(monkeypatch, "SMTP must not be contacted when nothing is pending")
    EmailExporter(config).send_pending()


def test_pending_excludes_skipped(tmp_path):
    config = _email_config(tmp_path)
    skipped = config.invoice_path / "skipped.pdf"
    skipped.write_bytes(b"%PDF")
    skipped.with_suffix(".json").write_text(json.dumps({"email_skipped": 1234}))

    fresh = config.invoice_path / "fresh.pdf"
    fresh.write_bytes(b"%PDF")

    assert EmailExporter(config)._pending_invoices() == [fresh]


def test_sync_with_export_disabled_marks_backlog_skipped(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.invoice_path.mkdir(parents=True)
    pdf = config.invoice_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF")
    pdf.with_suffix(".json").write_text(json.dumps({"type": "charging"}))

    _forbid_smtp(monkeypatch, "SMTP must not be contacted when export is disabled")
    EmailExporter(config).send_pending()

    metadata = json.loads(pdf.with_suffix(".json").read_text())
    assert "email_skipped" in metadata
    assert "email_sent" not in metadata


def test_enabling_export_does_not_flood_backlog(tmp_path, monkeypatch):
    config = _email_config(tmp_path)
    backlog = config.invoice_path / "backlog.pdf"
    backlog.write_bytes(b"%PDF")

    sent = []
    _install_dummy_smtp(monkeypatch, sent)
    exporter = EmailExporter(config)

    # First run after enabling export (no state file yet): the backlog is
    # marked as skipped instead of producing one mail per invoice.
    exporter.send_pending()
    assert sent == []
    assert "email_skipped" in json.loads(backlog.with_suffix(".json").read_text())

    # A genuinely new invoice afterwards is emailed normally.
    new = config.invoice_path / "new.pdf"
    new.write_bytes(b"%PDF")
    exporter.send_pending()
    assert len(sent) == 1
    assert "email_sent" in json.loads(new.with_suffix(".json").read_text())


def test_send_pending_skip_flag_marks_instead_of_sending(tmp_path, monkeypatch):
    config = _email_config(tmp_path)
    _mark_export_enabled(config)
    pdf = config.invoice_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF")

    _forbid_smtp(monkeypatch, "SMTP must not be contacted when sending is skipped")
    EmailExporter(config).send_pending(skip=True)

    assert "email_skipped" in json.loads(pdf.with_suffix(".json").read_text())


def test_send_skipped_combines_and_batches(tmp_path, monkeypatch):
    config = _email_config(tmp_path)
    for index in range(3):
        pdf = config.invoice_path / f"invoice_{index}.pdf"
        pdf.write_bytes(b"%PDF")
        pdf.with_suffix(".json").write_text(json.dumps({
            "email_skipped": 1234,
            "date": "2026-07-01T10:00:00",
            "type": "charging",
            "vehicle_name": "Car",
            "total_cost": 4.2,
            "currency": "EUR",
        }))

    sent = []
    _install_dummy_smtp(monkeypatch, sent)
    monkeypatch.setattr("app.emailer.MAX_ATTACHMENTS_PER_MAIL", 2)

    result = EmailExporter(config).send_skipped()

    assert result == {"sent": 3, "emails": 2}
    assert len(sent) == 2
    assert len(list(sent[0].iter_attachments())) == 2
    assert len(list(sent[1].iter_attachments())) == 1
    assert "part 1 of 2" in sent[0]["Subject"]

    body = sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "combined export" in body
    assert "Total: 8.40 EUR" in body  # per-batch total of the two invoices

    for index in range(3):
        metadata = json.loads((config.invoice_path / f"invoice_{index}.json").read_text())
        assert "email_sent" in metadata
        assert "email_skipped" not in metadata


def test_single_email_has_summary_body_and_subject(tmp_path, monkeypatch):
    config = _email_config(tmp_path)
    pdf = config.invoice_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF")
    pdf.with_suffix(".json").write_text(json.dumps({
        "date": "2026-07-01T10:00:00",
        "type": "charging",
        "vehicle_name": "Car",
        "site_name": "Supercharger Example",
        "energy_kwh": 12.3,
        "total_cost": 4.2,
        "currency": "EUR",
    }))

    sent = []
    _install_dummy_smtp(monkeypatch, sent)
    EmailExporter(config).send_single(pdf)

    message = sent[0]
    assert message["Subject"] == "Tesla invoice - 2026-07-01 - Charging - 4.20 EUR"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Date:      2026-07-01 10:00" in body
    assert "Vehicle:   Car" in body
    assert "Location:  Supercharger Example" in body
    assert "Energy:    12.30 kWh" in body
    assert "Amount:    4.20 EUR" in body
