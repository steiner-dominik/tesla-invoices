import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import TeslaAPIClient, TokenManager
from app.config import Config
from app.downloader import METADATA_VERSION, InvoiceDownloader
from app.emailer import EmailExporter


def _safe_file_entry(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}

    suffix = path.suffix.lower()
    if suffix == ".json":
        file_type = "json"
    elif suffix == ".pdf":
        file_type = "pdf"
    else:
        file_type = "other"

    preview = ""
    try:
        if file_type == "json":
            # metadata sidecars are small; 2000 chars shows them completely
            preview = path.read_text(encoding="utf-8")[:2000]
        elif file_type == "pdf":
            preview = f"PDF ({stat.st_size} bytes)"
        else:
            preview = path.read_text(encoding="utf-8", errors="ignore")[:400]
    except Exception:
        preview = ""

    return {
        "name": path.name,
        "type": file_type,
        "size": stat.st_size,
        # With UTC offset, so browsers in another time zone parse it correctly
        "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "preview": preview,
    }

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Fail fast: a broken configuration should crash the process so the
# supervisor/container runtime surfaces the error instead of running a zombie.
config = Config.load()

downloader = InvoiceDownloader(config, TeslaAPIClient(config, TokenManager(config)))
emailer = EmailExporter(config)

# Serializes scheduled and manually triggered downloads.
_sync_lock = asyncio.Lock()
# The event loop only keeps weak references to tasks, so fire-and-forget
# tasks could be garbage-collected mid-run; keep strong references here.
_loop_task: asyncio.Task | None = None
_manual_sync_task: asyncio.Task | None = None
_sync_state: dict[str, Any] = {
    "running": False,
    "last_kind": None,
    "last_finished": None,
    "last_result": None,
    "last_success": None,
}


def _current_and_previous_month() -> list[datetime]:
    cur = datetime.combine(date.today().replace(day=1), datetime.min.time())
    prev = (cur - timedelta(days=1)).replace(day=1)
    # Also fetch the previous month so invoices appearing shortly after a
    # month boundary are not missed.
    return [cur, prev]


def _now_iso() -> str:
    # With UTC offset, so browsers in another time zone parse it correctly
    return datetime.now().astimezone().isoformat(timespec="seconds")


async def _run_sync(months: list[datetime] | None, kind: str, skip_email: bool = False) -> None:
    async with _sync_lock:
        _sync_state.update({"running": True, "last_kind": kind})
        try:
            await asyncio.to_thread(downloader.download_invoices, months)
            await asyncio.to_thread(emailer.send_pending, skip_email)
            _sync_state["last_result"] = "ok"
            _sync_state["last_success"] = _now_iso()
        except Exception as e:
            logger.error(f"Invoice sync ({kind}) failed: {e}")
            _sync_state["last_result"] = str(e)
        finally:
            _sync_state.update({"running": False, "last_finished": _now_iso()})


async def _download_loop() -> None:
    logger.info(f"Automatic sync started: checking for new invoices every {config.polling_interval} minutes")
    while True:
        await _run_sync(_current_and_previous_month(), kind="scheduled")
        # Jitter breaks the metronomic request cadence, which bot-mitigation
        # systems (Akamai fronts the Tesla APIs) score on.
        await asyncio.sleep(config.polling_interval * 60 + random.uniform(0, 60))


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _loop_task
    config.invoice_path.mkdir(parents=True, exist_ok=True)
    _loop_task = asyncio.create_task(_download_loop())
    try:
        yield
    finally:
        _loop_task.cancel()
        with suppress(asyncio.CancelledError):
            await _loop_task
        _loop_task = None


app = FastAPI(title="Tesla Invoices", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    # The Supervisor watchdog polls this endpoint: report unhealthy when the
    # download loop has died, so the app gets restarted instead of idling.
    if _loop_task is not None and _loop_task.done():
        raise HTTPException(status_code=500, detail="Download loop is not running")
    return {"status": "ok"}


@app.get("/api/analytics")
async def get_analytics() -> dict[str, Any]:
    # File scanning happens in a thread so the event loop (and the watchdog's
    # /health endpoint) stays responsive.
    return await asyncio.to_thread(_collect_analytics)


def _collect_analytics() -> dict[str, Any]:
    if not config.invoice_path.exists():
        return {"data": [], "summary": {}, "sync": _sync_state}

    data = []
    # Costs are grouped per currency and never converted: summing EUR and USD
    # invoices into one number would be meaningless.
    cost_by_currency: dict[str, float] = {}
    total_kwh = 0.0
    vehicles = set()
    email_skipped_count = 0

    for json_file in config.invoice_path.glob("*.json"):
        if json_file.name.startswith("."):
            continue  # internal state files (e.g. the email export state)
        try:
            with open(json_file) as f:
                meta = json.load(f)
                data.append(meta)
                cost = float(meta.get("total_cost", 0) or 0)
                if cost:
                    currency = meta.get("currency") or ""
                    cost_by_currency[currency] = cost_by_currency.get(currency, 0.0) + cost
                if meta.get("type") == "charging":
                    total_kwh += float(meta.get("energy_kwh", 0) or 0)
                vehicles.add(meta.get("vehicle_name") or meta.get("vin"))
                if "email_skipped" in meta and "email_sent" not in meta:
                    email_skipped_count += 1
        except Exception as e:
            logger.error(f"Failed to read {json_file}: {e}")

    data.sort(key=lambda x: x.get("date", ""), reverse=True)

    if config.default_currency and config.default_currency in cost_by_currency:
        primary_currency = config.default_currency
    else:
        # Auto-detect: the currency carrying the largest share of the cost
        # (by magnitude, so a currency holding only credit notes cannot win).
        primary_currency = max(cost_by_currency, key=lambda c: abs(cost_by_currency[c]), default="")

    summary = {
        "cost_by_currency": {c: round(v, 2) for c, v in sorted(cost_by_currency.items())},
        "primary_currency": primary_currency,
        "total_kwh": round(total_kwh, 2),
        "vehicles": sorted(v for v in vehicles if v),
        "invoice_count": len(data),
        # Manual sending only needs mailserver+from, not the auto-export switch
        "email_configured": emailer.is_configured,
        # Pre-populates the recipient prompt of the manual Email button
        "email_default_to": config.email_to,
        # The UI only offers "also email during this sync" when auto-export is on
        "email_export_enabled": config.enable_email_export,
        # Invoices flagged email_skipped, sendable via the combined export
        "email_skipped_count": email_skipped_count,
    }

    return {"summary": summary, "data": data, "sync": _sync_state}


@app.get("/api/files")
async def list_debug_files() -> dict[str, Any]:
    return await asyncio.to_thread(_scan_files)


def _invoice_date(path: Path) -> str:
    """Invoice date from the metadata sidecar (the PDF's mtime is just the
    download time, which says nothing about when the invoice is from)."""
    sidecar = path if path.suffix == ".json" else path.with_suffix(".json")
    try:
        return str(json.loads(sidecar.read_text()).get("date") or "")
    except (OSError, ValueError):
        return ""


def _scan_files() -> dict[str, Any]:
    invoice_dir = config.invoice_path.resolve()
    invoice_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for path in invoice_dir.iterdir():
        # Dotfiles are internal state (e.g. the email export state), not invoices
        if path.is_file() and not path.name.startswith("."):
            entry = _safe_file_entry(path)
            if entry:
                entry["date"] = _invoice_date(path)
                files.append(entry)

    # Newest invoice first (fall back to mtime for files without metadata),
    # so recent invoices are visible without scrolling.
    files.sort(key=lambda e: e["date"] or e["modified"], reverse=True)

    return {"path": str(invoice_dir), "files": files}


@app.post("/api/files/rescan", status_code=200)
async def rescan_pdfs() -> dict[str, Any]:
    """Re-extract cost/currency from stored subscription PDFs with the current
    parser logic. Charging figures come from the Tesla API, not the PDFs, so
    those sidecars are refreshed by a normal sync instead."""
    # PDF parsing is CPU-heavy; run it in a thread so the event loop (and the
    # watchdog's /health endpoint) stays responsive during a long rescan.
    return await asyncio.to_thread(_rescan_pdfs)


def _rescan_pdfs() -> dict[str, Any]:
    updated = 0
    skipped = 0
    for pdf_path in sorted(config.invoice_path.glob("*.pdf")):
        json_path = pdf_path.with_suffix(".json")
        try:
            existing = json.loads(json_path.read_text()) if json_path.exists() else {}
        except Exception:
            existing = {}

        if existing.get("type") == "charging":
            skipped += 1
            continue

        try:
            total_cost, currency = downloader.extract_cost_from_pdf(pdf_path.read_bytes())
        except Exception as exc:
            logger.warning(f"Failed to rescan {pdf_path.name}: {exc}")
            continue

        if total_cost == 0 and not currency:
            logger.warning(f"Rescan could not extract a cost from {pdf_path.name}")
            continue

        if existing.get("is_credit_note"):
            # Credit notes must reduce the totals, whatever sign the PDF prints.
            total_cost = -abs(total_cost)

        metadata = {
            **existing,
            "total_cost": total_cost,
            "currency": currency,
            "meta_version": METADATA_VERSION,
        }
        if metadata != existing:
            json_path.write_text(json.dumps(metadata, indent=4, sort_keys=True))
            updated += 1

    logger.info(f"PDF rescan finished: {updated} metadata file(s) updated, {skipped} charging file(s) skipped")
    return {"status": "ok", "updated": updated, "skipped_charging": skipped}


@app.post("/api/sync", status_code=202)
async def trigger_sync(month: str = "all", skip_email: bool = False) -> dict[str, str]:
    """Manually trigger a download: month=all|cur|prev|YYYY-MM.

    ``skip_email=true`` marks the invoices found by this sync as skipped
    instead of emailing each one — meant for bulk/history syncs, where one
    mail per invoice would flood the recipient."""
    global _manual_sync_task
    # Also check the pending manual task: two rapid clicks would both pass the
    # lock check (the first task may not have acquired the lock yet) and queue
    # the same full sync twice.
    if _sync_lock.locked() or (_manual_sync_task is not None and not _manual_sync_task.done()):
        raise HTTPException(status_code=409, detail="A sync is already running")

    if month == "all":
        months = None
    elif month in ("cur", "prev"):
        months = [_current_and_previous_month()[0 if month == "cur" else 1]]
    else:
        try:
            months = [datetime.strptime(month, "%Y-%m")]
        except ValueError:
            raise HTTPException(status_code=422, detail="month must be all, cur, prev or YYYY-MM") from None

    _manual_sync_task = asyncio.create_task(_run_sync(months, kind=f"manual ({month})", skip_email=skip_email))
    return {"status": "started", "month": month}


def _resolve_invoice_file(filename: str, suffixes: tuple[str, ...]) -> Path:
    file_path = (config.invoice_path / filename).resolve()
    # Never serve anything outside the invoice directory (e.g. token files in /data)
    if not file_path.is_relative_to(config.invoice_path.resolve()) or file_path.suffix not in suffixes:
        raise HTTPException(status_code=404, detail="File not found")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return file_path


@app.get("/api/download/{filename}")
async def download_invoice(filename: str, inline: bool = False):
    """Serve an invoice; ``inline=true`` opens it in the browser instead of downloading."""
    file_path = _resolve_invoice_file(filename, (".pdf", ".json"))
    media_type = "application/pdf" if file_path.suffix == ".pdf" else "application/json"
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type=media_type,
        content_disposition_type="inline" if inline else "attachment",
    )


@app.delete("/api/files/{filename}")
async def delete_file(filename: str) -> dict[str, str]:
    """Delete a single stored file (PDF or metadata sidecar). A deleted PDF
    is re-downloaded on the next sync as long as its invoice exists at Tesla."""
    file_path = _resolve_invoice_file(filename, (".pdf", ".json"))
    file_path.unlink()
    logger.info(f"Deleted {file_path.name} via file browser")
    return {"status": "deleted", "file": file_path.name}


# NOTE: must be registered before /api/email/{filename}, or "send-skipped"
# would be interpreted as a file name by the route below.
@app.post("/api/email/send-skipped")
async def email_skipped_invoices(to: str | None = None) -> dict[str, Any]:
    """Send all invoices flagged ``email_skipped`` as a combined export
    (batched into a few emails instead of one per invoice)."""
    if not emailer.is_configured:
        raise HTTPException(status_code=400, detail="Email is not configured (from/mailserver missing)")
    recipient = (to or config.email_to).strip()
    if "@" not in recipient:
        raise HTTPException(status_code=422, detail="Recipient must be a valid email address")
    try:
        result = await asyncio.to_thread(emailer.send_skipped, recipient)
    except Exception as e:
        logger.error(f"Combined email export failed: {e}")
        raise HTTPException(status_code=502, detail=f"Sending failed: {e}") from e
    return {"status": "sent", "to": recipient, **result}


@app.post("/api/email/{filename}")
async def email_invoice(filename: str, to: str | None = None) -> dict[str, str]:
    """Manually send one invoice PDF; ``to`` overrides the configured
    recipient (the UI pre-populates it with the config default)."""
    file_path = _resolve_invoice_file(filename, (".pdf",))
    if not emailer.is_configured:
        raise HTTPException(status_code=400, detail="Email is not configured (from/mailserver missing)")
    recipient = (to or config.email_to).strip()
    if "@" not in recipient:
        raise HTTPException(status_code=422, detail="Recipient must be a valid email address")
    try:
        await asyncio.to_thread(emailer.send_single, file_path, recipient)
    except Exception as e:
        logger.error(f"Manual email send failed for {filename}: {e}")
        raise HTTPException(status_code=502, detail=f"Sending failed: {e}") from e
    return {"status": "sent", "to": recipient}


def _csv_safe(value: Any) -> Any:
    """Neutralize spreadsheet formula injection: text from the Tesla API
    (e.g. site names) must not execute when the CSV is opened in Excel.
    Numbers stay untouched, so negative costs are unaffected."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


@app.get("/api/export.csv")
async def export_csv() -> Response:
    """All invoice metadata as CSV, for expense reports and spreadsheets."""
    return await asyncio.to_thread(_build_csv)


def _build_csv() -> Response:
    import csv
    import io

    fields = [
        "date",
        "type",
        "vehicle_name",
        "vin",
        "site_name",
        "description",
        "country",
        "energy_kwh",
        "total_cost",
        "currency",
        "filename",
    ]
    rows = []
    for json_file in config.invoice_path.glob("*.json"):
        if json_file.name.startswith("."):
            continue  # internal state files (e.g. the email export state)
        try:
            meta = json.loads(json_file.read_text())
            rows.append({field: _csv_safe(meta.get(field, "")) for field in fields})
        except Exception as e:
            logger.error(f"Failed to read {json_file}: {e}")
    rows.sort(key=lambda r: r.get("date", ""), reverse=True)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="tesla_invoices.csv"'},
    )
