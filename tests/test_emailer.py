import json

from app.emailer import EmailExporter

from .helpers import make_config


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

        def starttls(self):
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

        def starttls(self):
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

        def starttls(self):
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
    pdf = config.invoice_path / "sent.pdf"
    pdf.write_bytes(b"%PDF")
    pdf.with_suffix(".json").write_text(json.dumps({"email_sent": 1}))

    def fail(*args, **kwargs):
        raise AssertionError("SMTP must not be contacted when nothing is pending")

    monkeypatch.setattr("smtplib.SMTP", fail)
    EmailExporter(config).send_pending()
