import json
import logging
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

from app.config import Config

logger = logging.getLogger(__name__)

SMTP_TIMEOUT = 20


class EmailExporter:
    """Sends each downloaded invoice PDF as an email attachment exactly once.

    Sent-state is tracked as an ``email_sent`` timestamp in the invoice's JSON
    metadata sidecar. The flag is only written after a successful send, so a
    failed send is retried on the next cycle (issue #6 semantics).
    """

    def __init__(self, config: Config):
        self.config = config

    @property
    def is_configured(self) -> bool:
        """SMTP settings are present. Manual sends only need this — the
        recipient can be entered ad hoc (the configured ``to`` is just the
        default), and enable_email_export merely controls the automatic
        export (which does require ``to``, validated in Config)."""
        return bool(self.config.email_server and self.config.email_from)

    @staticmethod
    def _read_metadata(path: Path) -> dict:
        if path.exists() and path.stat().st_size > 0:
            try:
                return json.loads(path.read_text())
            except ValueError:
                logger.warning(f"Could not parse metadata {path}, treating invoice as not sent")
        return {}

    def _pending_invoices(self) -> list[Path]:
        pending = []
        for pdf in sorted(self.config.invoice_path.glob("*.pdf")):
            metadata = self._read_metadata(pdf.with_suffix(".json"))
            if "email_sent" not in metadata:
                pending.append(pdf)
        return pending

    def _connect(self) -> smtplib.SMTP:
        if self.config.email_server_port == 465:
            # Port 465 speaks implicit TLS from the first byte; STARTTLS
            # would hang against it.
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                self.config.email_server, self.config.email_server_port, timeout=SMTP_TIMEOUT
            )
        else:
            smtp = smtplib.SMTP(self.config.email_server, self.config.email_server_port, timeout=SMTP_TIMEOUT)
            smtp.ehlo()
            smtp.starttls()
        if self.config.email_user:
            smtp.login(self.config.email_user, self.config.email_pass)
        return smtp

    def _send_one(self, smtp: smtplib.SMTP, pdf: Path, to: str | None = None) -> None:
        recipient = to or self.config.email_to
        email = EmailMessage()
        email["From"] = self.config.email_from
        email["To"] = recipient
        email["Subject"] = f"Tesla Invoice Export - {pdf.name}"
        email.add_attachment(
            pdf.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=pdf.name,
        )
        smtp.send_message(email)

        logger.info(f"Sent mail to {recipient} for invoice {pdf.name}")
        metadata_path = pdf.with_suffix(".json")
        metadata = self._read_metadata(metadata_path)
        metadata["email_sent"] = int(time.time())
        metadata_path.write_text(json.dumps(metadata, indent=4, sort_keys=True))

    def send_pending(self) -> None:
        if not self.config.enable_email_export:
            return

        pending = self._pending_invoices()
        if not pending:
            logger.debug("Email export: nothing to send")
            return

        logger.info(f"Email export: sending {len(pending)} invoice(s) to {self.config.email_to}")
        try:
            with self._connect() as smtp:
                for pdf in pending:
                    try:
                        self._send_one(smtp, pdf)
                    except smtplib.SMTPException as e:
                        logger.error(f"Failed to send mail for {pdf.name}: {e}")
        except (smtplib.SMTPException, OSError) as e:
            # Not necessarily a connect failure: an OSError can also come
            # from reading a PDF mid-send.
            logger.error(f"Email export via {self.config.email_server} failed: {e}")

    def send_single(self, pdf: Path, to: str | None = None) -> None:
        """Send one invoice on demand, optionally to a custom recipient.
        Unlike ``send_pending``, errors propagate to the caller so the UI
        can show them."""
        with self._connect() as smtp:
            self._send_one(smtp, pdf, to=to)
