import logging
import smtplib
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from app import storage
from app.config import Config

logger = logging.getLogger(__name__)

SMTP_TIMEOUT = 20

# Tracks whether email export was enabled the last time a sync ran, so the
# transition disabled -> enabled can be detected (the backlog accumulated
# while disabled must not be flooded out at that moment). Lives inside the
# invoice directory because that is the persistent volume in both the HA and
# the standalone deployment; the leading dot hides it from the file browser.
STATE_FILENAME = ".email_export_state.json"

# A combined backlog send is batched so a large history does not end up as
# one gigantic mail (or hundreds of single ones). Tesla PDFs are ~100 KB,
# so these limits keep each mail comfortably below common size caps.
MAX_ATTACHMENTS_PER_MAIL = 20
MAX_BYTES_PER_MAIL = 15 * 1024 * 1024

FOOTER = "This message was sent automatically by Tesla Invoices."

# Serializes every send path (auto-export after a sync, manual single send,
# combined backlog send): they all run in worker threads and share the
# "check email_sent flag → send → set email_sent flag" cycle, so two of them
# processing the same invoice concurrently would email it twice.
_SEND_LOCK = threading.Lock()


class EmailExporter:
    """Sends each downloaded invoice PDF as an email attachment exactly once.

    Per-invoice state lives in the JSON metadata sidecar:
      * ``email_sent`` (timestamp) — successfully emailed; never sent again.
      * ``email_skipped`` (timestamp) — deliberately not auto-sent (synced
        while export was disabled, or during a sync with sending skipped).
        Skipped invoices can later be sent combined via ``send_skipped``.

    ``email_sent`` is only written after a successful send, so a failed send
    is retried on the next cycle (issue #6 semantics).
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

    # ---------- metadata / state ----------

    @staticmethod
    def _read_metadata(path: Path) -> dict:
        return storage.read_json(path)

    @property
    def _state_path(self) -> Path:
        return self.config.invoice_path / STATE_FILENAME

    def _load_state(self) -> dict:
        return storage.read_json(self._state_path)

    def _save_state(self, state: dict) -> None:
        try:
            storage.write_json_atomic(self._state_path, state)
        except OSError as e:
            logger.warning(f"Could not persist email export state: {e}")

    def _pending_invoices(self) -> list[Path]:
        """PDFs that were neither emailed nor deliberately skipped."""
        pending = []
        for pdf in sorted(self.config.invoice_path.glob("*.pdf")):
            metadata = self._read_metadata(pdf.with_suffix(".json"))
            if "email_sent" not in metadata and "email_skipped" not in metadata:
                pending.append(pdf)
        return pending

    def _skipped_invoices(self) -> list[Path]:
        skipped = []
        for pdf in sorted(self.config.invoice_path.glob("*.pdf")):
            metadata = self._read_metadata(pdf.with_suffix(".json"))
            if "email_skipped" in metadata and "email_sent" not in metadata:
                skipped.append(pdf)
        return skipped

    def _mark_pending_skipped(self) -> int:
        """Flag all pending invoices as skipped so they are never auto-sent.
        They remain sendable through ``send_skipped`` (combined export)."""
        now = int(time.time())
        marked = 0
        for pdf in self._pending_invoices():
            storage.update_json(pdf.with_suffix(".json"), {"email_skipped": now})
            marked += 1
        return marked

    # ---------- message building ----------

    @staticmethod
    def _fmt_date(value: object) -> str:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return str(value or "")
        if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
            return dt.strftime("%Y-%m-%d")
        stamp = dt.strftime("%Y-%m-%d %H:%M")
        if dt.tzinfo is not None:
            stamp += dt.strftime(" UTC%z")
        return stamp

    @staticmethod
    def _fmt_amount(metadata: dict) -> str:
        try:
            cost = float(metadata.get("total_cost") or 0)
        except (TypeError, ValueError):
            return ""
        if not cost:
            return ""
        return f"{cost:.2f} {metadata.get('currency') or ''}".strip()

    def _subject_for(self, pdf: Path, metadata: dict) -> str:
        date = self._fmt_date(metadata.get("date"))[:10]
        parts = [p for p in (
            date,
            (metadata.get("type") or "").capitalize(),
            self._fmt_amount(metadata),
        ) if p]
        if parts:
            return "Tesla invoice - " + " - ".join(parts)
        return f"Tesla invoice - {pdf.name}"

    def _describe_invoice(self, pdf: Path, metadata: dict) -> list[str]:
        lines = [f"Invoice:   {pdf.name}"]
        if metadata.get("date"):
            lines.append(f"Date:      {self._fmt_date(metadata['date'])}")
        if metadata.get("type"):
            lines.append(f"Type:      {metadata['type'].capitalize()}")
        vehicle = metadata.get("vehicle_name") or metadata.get("vin")
        if vehicle:
            lines.append(f"Vehicle:   {vehicle}")
        if metadata.get("site_name"):
            lines.append(f"Location:  {metadata['site_name']}")
        try:
            energy = float(metadata.get("energy_kwh") or 0)
        except (TypeError, ValueError):
            energy = 0.0
        if energy:
            lines.append(f"Energy:    {energy:.2f} kWh")
        amount = self._fmt_amount(metadata)
        if amount:
            lines.append(f"Amount:    {amount}")
        return lines

    def _single_body(self, pdf: Path, metadata: dict) -> str:
        details = "\n".join(f"    {line}" for line in self._describe_invoice(pdf, metadata))
        return (
            "Hello,\n\n"
            "attached is a Tesla invoice.\n\n"
            f"{details}\n\n"
            f"{FOOTER}\n"
        )

    def _combined_body(self, batch: list[Path], part: int, parts: int) -> str:
        lines = []
        totals: dict[str, float] = {}
        for pdf in batch:
            metadata = self._read_metadata(pdf.with_suffix(".json"))
            date = self._fmt_date(metadata.get("date"))[:10] or "unknown"
            kind = (metadata.get("type") or "invoice").ljust(12)
            amount = self._fmt_amount(metadata)
            vehicle = metadata.get("vehicle_name") or metadata.get("vin") or ""
            lines.append(f"    {date}  {kind}  {amount or '-':>14}  {vehicle}  ({pdf.name})")
            try:
                cost = float(metadata.get("total_cost") or 0)
            except (TypeError, ValueError):
                cost = 0.0
            if cost:
                currency = metadata.get("currency") or ""
                totals[currency] = totals.get(currency, 0.0) + cost

        total_lines = "".join(
            f"    Total: {value:.2f} {currency}".rstrip() + "\n" for currency, value in sorted(totals.items())
        )
        part_note = f" (part {part} of {parts})" if parts > 1 else ""
        return (
            "Hello,\n\n"
            f"attached is a combined export of {len(batch)} Tesla invoice(s) that were "
            f"not sent individually by the automatic email export{part_note}.\n\n"
            + "\n".join(lines)
            + ("\n\n" + total_lines if total_lines else "\n")
            + f"\n{FOOTER}\n"
        )

    # ---------- sending ----------

    def _connect(self) -> smtplib.SMTP:
        # Explicit context: create_default_context() verifies the server
        # certificate and hostname; smtplib's implicit default has not always
        # done so, and this channel carries credentials and invoices.
        context = ssl.create_default_context()
        if self.config.email_server_port == 465:
            # Port 465 speaks implicit TLS from the first byte; STARTTLS
            # would hang against it.
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                self.config.email_server, self.config.email_server_port, timeout=SMTP_TIMEOUT, context=context
            )
        else:
            smtp = smtplib.SMTP(self.config.email_server, self.config.email_server_port, timeout=SMTP_TIMEOUT)
            smtp.ehlo()
            smtp.starttls(context=context)
        if self.config.email_user:
            smtp.login(self.config.email_user, self.config.email_pass)
        return smtp

    def _send_one(self, smtp: smtplib.SMTP, pdf: Path, to: str | None = None) -> None:
        recipient = to or self.config.email_to
        metadata_path = pdf.with_suffix(".json")
        metadata = self._read_metadata(metadata_path)

        email = EmailMessage()
        email["From"] = self.config.email_from
        email["To"] = recipient
        email["Subject"] = self._subject_for(pdf, metadata)
        email.set_content(self._single_body(pdf, metadata))
        email.add_attachment(
            pdf.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=pdf.name,
        )
        smtp.send_message(email)

        # No recipient address in the log — logs may end up in bug reports.
        logger.info(f"Sent invoice {pdf.name} by email")
        # A manual send of a previously skipped invoice resolves the skip.
        storage.update_json(metadata_path, {"email_sent": int(time.time())}, remove=("email_skipped",))

    def send_pending(self, skip: bool = False) -> None:
        """Auto-export after a sync. ``skip=True`` (bulk sync with sending
        disabled by the user) marks the new invoices as skipped instead of
        mailing each one."""
        state = self._load_state()
        was_enabled = bool(state.get("export_enabled", False))
        enabled = self.config.enable_email_export

        if not enabled:
            # Flag everything now, so enabling export later only mails
            # invoices that are genuinely new from that point on.
            marked = self._mark_pending_skipped()
            if marked:
                logger.info(
                    f"Email export is disabled: marked {marked} invoice(s) as skipped "
                    "so they will not be auto-sent when export is enabled later"
                )
            if was_enabled or "export_enabled" not in state:
                self._save_state({"export_enabled": False})
            return

        if not was_enabled:
            # Export was just switched on (or this is the first run of a
            # version that tracks it): do not flood the recipient with the
            # entire backlog. It stays available via the combined export.
            marked = self._mark_pending_skipped()
            logger.info(
                f"Email export was enabled: {marked} existing invoice(s) marked as skipped; "
                "only invoices downloaded from now on are emailed automatically "
                "(use 'Send skipped invoices' in Maintenance for the backlog)"
            )
            self._save_state({"export_enabled": True})
            return

        if skip:
            marked = self._mark_pending_skipped()
            if marked:
                logger.info(f"Email sending skipped for this sync: marked {marked} invoice(s) as skipped")
            return

        with _SEND_LOCK:
            pending = self._pending_invoices()
            if not pending:
                logger.debug("Email export: nothing to send")
                return

            logger.info(f"Email export: sending {len(pending)} new invoice(s)")
            try:
                with self._connect() as smtp:
                    for pdf in pending:
                        try:
                            self._send_one(smtp, pdf)
                        except (smtplib.SMTPException, OSError) as e:
                            # OSError too: one unreadable PDF must not abort
                            # the loop and silently skip everything after it.
                            logger.error(f"Failed to send mail for {pdf.name}: {e}")
            except (smtplib.SMTPException, OSError) as e:
                logger.error(f"Email export via {self.config.email_server} failed: {e}")

    def send_single(self, pdf: Path, to: str | None = None) -> None:
        """Send one invoice on demand, optionally to a custom recipient.
        Unlike ``send_pending``, errors propagate to the caller so the UI
        can show them."""
        with _SEND_LOCK, self._connect() as smtp:
            self._send_one(smtp, pdf, to=to)

    def send_skipped(self, to: str | None = None) -> dict:
        """Send all skipped invoices combined, batched into as few emails as
        the size limits allow. Errors propagate to the caller (UI-triggered).
        Returns ``{"sent": <invoices>, "emails": <messages>}``."""
        recipient = (to or self.config.email_to).strip()
        with _SEND_LOCK:
            skipped = self._skipped_invoices()
            if not skipped:
                return {"sent": 0, "emails": 0}

            batches: list[list[Path]] = []
            batch: list[Path] = []
            batch_bytes = 0
            for pdf in skipped:
                size = pdf.stat().st_size
                if batch and (len(batch) >= MAX_ATTACHMENTS_PER_MAIL or batch_bytes + size > MAX_BYTES_PER_MAIL):
                    batches.append(batch)
                    batch, batch_bytes = [], 0
                batch.append(pdf)
                batch_bytes += size
            batches.append(batch)

            sent = 0
            with self._connect() as smtp:
                for index, batch in enumerate(batches, start=1):
                    email = EmailMessage()
                    email["From"] = self.config.email_from
                    email["To"] = recipient
                    part = f", part {index} of {len(batches)}" if len(batches) > 1 else ""
                    email["Subject"] = f"Tesla invoices - combined export of {len(batch)} invoice(s){part}"
                    email.set_content(self._combined_body(batch, index, len(batches)))
                    for pdf in batch:
                        email.add_attachment(
                            pdf.read_bytes(),
                            maintype="application",
                            subtype="pdf",
                            filename=pdf.name,
                        )
                    smtp.send_message(email)

                    now = int(time.time())
                    for pdf in batch:
                        storage.update_json(pdf.with_suffix(".json"), {"email_sent": now}, remove=("email_skipped",))
                    sent += len(batch)
                    logger.info(f"Sent combined export email {index}/{len(batches)} with {len(batch)} invoice(s)")

            return {"sent": sent, "emails": len(batches)}
