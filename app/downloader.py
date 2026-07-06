import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.api import TeslaAPIClient
from app.config import Config

logger = logging.getLogger(__name__)

# Bump whenever the metadata/PDF-extraction logic changes: sidecar JSON files
# with an older (or missing) meta_version are considered stale and re-derived
# on the next sync or rescan.
METADATA_VERSION = 3

# Currency stays case-sensitive ("EUR", not "due"); keywords are matched
# case-insensitively via the scoped (?i:...) group below.
_CURRENCY_RE = r"(?:\b[A-Z]{3}\b|US\$|€|\$|£)"
_AMOUNT_RE = r"-?\d+(?:[.,]\d{3})*(?:[.,]\d{1,2})?"

# Grand-total lines on Tesla invoices, localized, e.g.
# "Gesamtbetrag (EUR) 9.99", "Total amount (EUR) 4.30", "Total due: EUR 1.234,56"
_TOTAL_RE = re.compile(
    rf"(?i:gesamtbetrag|total\s+amount|amount\s+due|total\s+due|grand\s+total"
    rf"|importe\s+total|montant\s+total|totaal|totale|total)"
    rf"\s*:?\s*\(?\s*(?P<currency>{_CURRENCY_RE})\s*\)?\s*(?P<amount>{_AMOUNT_RE})"
)

# Fallback: any amount directly next to a currency marker.
_CURRENCY_AMOUNT_RE = re.compile(
    rf"(?:(?P<currency1>{_CURRENCY_RE})\s*(?P<amount1>{_AMOUNT_RE}))"
    rf"|(?:(?P<amount2>{_AMOUNT_RE})\s*(?P<currency2>{_CURRENCY_RE}))"
)

# Three-letter words the fallback must not mistake for a currency code
# (e.g. "VAT 20.00" on English invoices).
_NON_CURRENCY_WORDS = {"VAT", "TAX", "QTY", "NET"}


class InvoiceDownloader:
    def __init__(self, config: Config, client: TeslaAPIClient):
        self.config = config
        self.client = client

    @staticmethod
    def _month_set(months: Iterable[datetime] | None) -> set[tuple[int, int]] | None:
        if months is None:
            return None
        return {(m.year, m.month) for m in months}

    @staticmethod
    def _is_desired_date(item_datetime: datetime, months: set[tuple[int, int]] | None) -> bool:
        if months is None:  # None means: all invoices
            return True
        return (item_datetime.year, item_datetime.month) in months

    @staticmethod
    def _write_metadata(path: Path, metadata: dict) -> None:
        """Merge metadata into an existing sidecar file instead of overwriting.

        Keys written by others (e.g. the email exporter's ``email_sent``) must
        survive, and unchanged files are not rewritten to spare flash storage.
        """
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except (ValueError, OSError):
                logger.warning(f"Could not parse existing metadata {path}, rewriting it")
        merged = {**existing, **metadata}
        if merged != existing:
            path.write_text(json.dumps(merged, indent=4, sort_keys=True))

    def download_invoices(self, months: list[datetime] | None = None) -> None:
        """Download invoices for the given months, or all invoices if ``months`` is None."""
        month_set = self._month_set(months)
        date_str = "all" if month_set is None else ", ".join(sorted(f"{y}-{m:02d}" for y, m in month_set))
        logger.info(f"Desired invoice month(s): {date_str}")

        self.config.invoice_path.mkdir(parents=True, exist_ok=True)
        vehicles = self.client.get_vehicles()

        failures = 0
        for vin, vehicle in vehicles.items():
            display_name = vehicle.get("display_name", "")
            name_suffix = f"- {display_name}" if display_name else ""
            logger.info(f"Processing vehicle {vin} {name_suffix}...")

            # Charging Invoices
            charging_data = self.client.get_charging_history(vin)
            charging_sessions = self._extract_charging_sessions(charging_data)
            failures += self._save_charging_invoices(charging_sessions, month_set, vin, display_name)

            # Subscription Invoices
            if self.config.enable_subscription_invoice:
                logger.info("Subscription Invoice Enabled -> starting to download subscription invoices")
                sub_data = self.client.get_subscription_invoices(vin)
                failures += self._save_subscription_invoices(sub_data.get("data", []), month_set, vin, display_name)

        if failures:
            logger.warning(f"DONE downloading invoices, but {failures} invoice(s) failed and will be retried")
        else:
            logger.info("DONE downloading invoices")

    @staticmethod
    def _extract_charging_sessions(charging_payload: Any) -> list[dict]:
        # The API client already returns a plain list of sessions for the
        # GraphQL endpoint; the dict handling below covers raw payloads
        # (old REST shape or an unparsed GraphQL response).
        if isinstance(charging_payload, list):
            return charging_payload

        if not isinstance(charging_payload, dict):
            return []

        data_payload = charging_payload.get("data")
        if isinstance(data_payload, list):  # old REST shape: {"data": [...]}
            return data_payload
        if isinstance(data_payload, dict):
            # GraphQL response: data.me.charging.historyV2.data
            history_v2 = (((data_payload.get("me") or {}).get("charging") or {}).get("historyV2") or {})
            if isinstance(history_v2.get("data"), list):
                return history_v2["data"]

        return []

    def _save_charging_invoices(
        self,
        charging_sessions: list[dict],
        months: set[tuple[int, int]] | None,
        vin: str,
        vehicle_name: str,
    ) -> int:
        """Returns the number of invoices that failed; one bad session or
        invoice must not abort the sync for everything else."""
        failures = 0
        for session in charging_sessions:
            try:
                session_date = (
                    session.get("unlatchDateTime")
                    or session.get("chargeStopDateTime")
                    or session.get("chargeStartDateTime")
                )
                if not session_date:
                    logger.warning(f"Charging session without any date, skipping: {session.get('chargeSessionId')}")
                    continue
                session_dt = datetime.fromisoformat(session_date)
                if not self._is_desired_date(session_dt, months):
                    continue
                invoices = session.get("invoices") or []
            except Exception as e:
                logger.error(f"Failed to process charging session {session.get('chargeSessionId')}: {e}")
                failures += 1
                continue

            for invoice in invoices:
                try:
                    self._save_one_charging_invoice(session, invoice, session_dt, vin, vehicle_name)
                except Exception as e:
                    logger.error(
                        f"Failed to save charging invoice {invoice.get('fileName')} "
                        f"(session {session.get('chargeSessionId')}): {e}"
                    )
                    failures += 1
        return failures

    def _save_one_charging_invoice(
        self, session: dict, invoice: dict, session_dt: datetime, vin: str, vehicle_name: str
    ) -> None:
        invoice_id = invoice["contentId"]
        filename = invoice["fileName"]
        country_code = session.get("countryCode", "UN")

        base_name = f"tesla_charging_invoice_{vin}_{session_dt.strftime('%Y-%m-%d')}_{country_code}_{filename}"
        local_pdf_path = self.config.invoice_path / base_name
        local_json_path = local_pdf_path.with_suffix(".json")

        # Save session data even if the PDF exists, to keep analytics populated
        metadata = {
            "type": "charging",
            "meta_version": METADATA_VERSION,
            "vin": vin,
            "vehicle_name": vehicle_name,
            "date": session_dt.isoformat(),
            "filename": base_name,
            "country": country_code,
            **self._charging_fee_metadata(session),
            "site_name": session.get("siteLocationName", ""),
        }
        self._write_metadata(local_json_path, metadata)

        if local_pdf_path.exists():
            logger.debug(f"Invoice {filename} already saved")
            return

        logger.info(f"Downloading {filename}")
        content = self.client.get_charging_invoice(invoice_id, vin)
        local_pdf_path.write_bytes(content)
        logger.info(f"File '{local_pdf_path}' saved.")

    @staticmethod
    def _charging_fee_metadata(session: dict) -> dict:
        """Aggregate cost/energy figures from the GraphQL fee list.

        A session carries one fee entry per fee type (CHARGING, PARKING, ...),
        each with its own usage and totals. Energy figures only make sense for
        the CHARGING fee (uom kWh); costs are summed over all fee types.
        """
        fees = session.get("fees") or []
        if isinstance(fees, dict):  # old REST shape, kept as fallback
            return {
                "energy_kwh": fees.get("energyDelivered", 0) or session.get("kwhDelivered", 0),
                "total_tier1_kwh": fees.get("totalTier1Kwh", 0),
                "total_tier2_kwh": fees.get("totalTier2Kwh", 0),
                "total_base_cost": fees.get("totalBaseCost", 0),
                "total_cost": fees.get("totalCost", 0) or session.get("totalCost", 0),
                "currency": session.get("currencyCode", ""),
            }

        charging_fees = [f for f in fees if f.get("feeType") == "CHARGING"]
        return {
            "energy_kwh": sum(f.get("usageBase") or 0 for f in charging_fees),
            "total_tier1_kwh": sum(f.get("usageTier1") or 0 for f in charging_fees),
            "total_tier2_kwh": sum(f.get("usageTier2") or 0 for f in charging_fees),
            "total_base_cost": sum(f.get("totalBase") or 0 for f in charging_fees),
            "total_cost": sum(f.get("totalDue") or 0 for f in fees),
            "currency": next((f.get("currencyCode") for f in fees if f.get("currencyCode")), ""),
        }

    @staticmethod
    def _parse_currency_amount(amount_text: str) -> float:
        cleaned = amount_text.replace(" ", "")
        if not cleaned:
            return 0.0

        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        elif "." in cleaned:
            if cleaned.count(".") > 1:
                cleaned = cleaned.replace(".", "")

        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    @staticmethod
    def _normalize_currency(currency: str) -> str:
        normalized = (currency or "").strip().upper()
        mapping = {"€": "EUR", "$": "USD", "US$": "USD", "£": "GBP"}
        return mapping.get(normalized, normalized)

    @staticmethod
    def extract_cost_from_pdf(pdf_bytes: bytes) -> tuple[float, str]:
        """Returns (amount, currency); the amount keeps its sign so credit
        notes (negative totals) reduce the analytics instead of vanishing."""
        try:
            import io

            import pypdf

            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)

            # An invoice may carry several total lines (net, tax, gross);
            # the gross grand total is the largest of them (by magnitude).
            amounts: list[tuple[float, str]] = []
            for match in _TOTAL_RE.finditer(text):
                amount = InvoiceDownloader._parse_currency_amount(match.group("amount"))
                if amount:
                    amounts.append((amount, InvoiceDownloader._normalize_currency(match.group("currency"))))

            if not amounts:
                # No recognizable total line: fall back to the largest
                # amount that sits directly next to a currency marker.
                for match in _CURRENCY_AMOUNT_RE.finditer(text):
                    currency = InvoiceDownloader._normalize_currency(
                        match.group("currency1") or match.group("currency2") or ""
                    )
                    if currency in _NON_CURRENCY_WORDS:
                        continue
                    amount = InvoiceDownloader._parse_currency_amount(
                        match.group("amount1") or match.group("amount2") or ""
                    )
                    if amount:
                        amounts.append((amount, currency))

            if amounts:
                return max(amounts, key=lambda x: abs(x[0]))
        except Exception as e:
            logger.error(f"Failed to extract cost from PDF: {e}")
        return 0.0, ""

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            return {}

    def _subscription_paths(self, stem: str, invoice_id: str) -> tuple[Path, Path, dict]:
        """Pick the file name for a subscription invoice.

        Legacy names carry no invoice id, so the first invoice of a day keeps
        the plain name. A *different* invoice on the same date (same VIN) gets
        the invoice id appended instead of being silently skipped and having
        its metadata overwrite the first one's.
        """
        pdf_path = self.config.invoice_path / f"{stem}.pdf"
        json_path = pdf_path.with_suffix(".json")
        existing = self._read_json(json_path)
        claimed_id = str(existing.get("invoice_id") or "")
        if invoice_id and claimed_id and claimed_id != invoice_id:
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "", invoice_id)[:24]
            pdf_path = self.config.invoice_path / f"{stem}_{safe_id}.pdf"
            json_path = pdf_path.with_suffix(".json")
            existing = self._read_json(json_path)
        return pdf_path, json_path, existing

    def _save_subscription_invoices(
        self,
        subscription_invoices: list[dict],
        months: set[tuple[int, int]] | None,
        vin: str,
        vehicle_name: str,
    ) -> int:
        """Returns the number of invoices that failed; one bad invoice must
        not abort the sync for everything else."""
        failures = 0
        for invoice in subscription_invoices:
            try:
                self._save_one_subscription_invoice(invoice, months, vin, vehicle_name)
            except Exception as e:
                logger.error(f"Failed to save subscription invoice {invoice.get('InvoiceId')}: {e}")
                failures += 1
        return failures

    def _save_one_subscription_invoice(
        self,
        invoice: dict,
        months: set[tuple[int, int]] | None,
        vin: str,
        vehicle_name: str,
    ) -> None:
        invoice_dt = datetime.fromisoformat(invoice["InvoiceDate"])
        if not self._is_desired_date(invoice_dt, months):
            return

        invoice_id = str(invoice.get("InvoiceId") or "")
        is_credit_note = bool(invoice.get("IsCreditNote"))
        # Credit notes share the month with the invoice they correct,
        # so they need their own file name.
        credit_suffix = "_creditnote" if is_credit_note else ""
        stem = f"tesla_subscription_invoice_{vin}_{invoice_dt.strftime('%Y-%m-%d')}{credit_suffix}"
        local_pdf_path, local_json_path, existing_meta = self._subscription_paths(stem, invoice_id)

        # Reuse the stored cost only if it was extracted by the current
        # logic; otherwise re-parse the PDF (stale/garbage values). A stored
        # 0.0 at the current version means "parsed, nothing found" — don't
        # re-parse the same PDF on every cycle.
        total_cost = 0.0
        currency = ""
        have_cost = existing_meta.get("meta_version") == METADATA_VERSION and "total_cost" in existing_meta
        if have_cost:
            total_cost = float(existing_meta.get("total_cost") or 0.0)
            currency = existing_meta.get("currency") or ""

        if not local_pdf_path.exists():
            logger.info(f"Downloading {invoice['InvoiceFileName']}")
            content = self.client.get_subscription_invoice(invoice["InvoiceId"], vin)
            local_pdf_path.write_bytes(content)
            logger.info(f"File '{local_pdf_path}' saved.")

        if not have_cost and local_pdf_path.exists():
            logger.debug(f"Parsing PDF {local_pdf_path.name} to extract cost")
            total_cost, currency = self.extract_cost_from_pdf(local_pdf_path.read_bytes())
            if is_credit_note:
                # Some credit-note layouts print the refunded amount as
                # positive; analytics must subtract it either way.
                total_cost = -abs(total_cost)

        metadata = {
            "type": "subscription",
            "meta_version": METADATA_VERSION,
            "vin": vin,
            "vehicle_name": vehicle_name,
            "date": invoice_dt.isoformat(),
            "filename": local_pdf_path.name,
            "total_cost": total_cost,
            "currency": currency,
            # The subscriptions/invoices response carries no description
            # field, only SubscriptionId/InvoiceType/IsCreditNote.
            "description": "Subscription credit note" if is_credit_note else "Subscription",
            "subscription_id": invoice.get("SubscriptionId", ""),
            "invoice_id": invoice_id,
            "is_credit_note": is_credit_note,
        }
        self._write_metadata(local_json_path, metadata)
