import asyncio
import base64
import binascii
import json
import logging
import os
import random
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app import auth, storage
from app.api import TeslaAPIClient, TeslaAuthError, TokenManager
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

# The exact value is irrelevant — HTML forms cannot set custom headers, and a
# cross-origin script could only add one after a CORS preflight, which this
# app never answers. Its mere presence therefore proves a same-origin caller.
CSRF_HEADER = "x-requested-with"


@app.middleware("http")
async def enforce_csrf_header(request: Request, call_next):
    """Reject cross-site mutations: without this, any website open in a
    browser on the same network could trigger syncs, delete files, or mail
    the stored invoices to an arbitrary address via a simple form POST."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and CSRF_HEADER not in request.headers:
        return JSONResponse(
            status_code=403,
            content={"detail": "Missing X-Requested-With header — cross-site requests are rejected"},
        )
    return await call_next(request)


def _basic_auth_ok(header: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = base64.b64decode(encoded.strip()).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    # compare_digest on both parts: no timing oracle for the user name either
    user_ok = secrets.compare_digest(user, config.basic_auth_user)
    return secrets.compare_digest(password, config.basic_auth_pass) and user_ok


# Registered after (= wrapped around) the CSRF middleware, so unauthenticated
# requests are answered with 401 before anything else runs.
@app.middleware("http")
async def enforce_basic_auth(request: Request, call_next):
    """Optional login for standalone deployments (BASIC_AUTH_USER/_PASS).
    /health stays open: the Docker HEALTHCHECK and the HA watchdog poll it
    without credentials. The HA app never sets these options — ingress
    already authenticates."""
    if config.basic_auth_user and request.url.path != "/health":
        if not _basic_auth_ok(request.headers.get("authorization", "")):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Tesla Invoices"'})
    return await call_next(request)


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
                # Only count sidecars whose PDF still exists — the combined
                # export sends PDFs, so orphans would inflate the button count
                if (
                    "email_skipped" in meta
                    and "email_sent" not in meta
                    and json_file.with_suffix(".pdf").exists()
                ):
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
        # Default dashboard language (HA language or LANGUAGE env var);
        # empty lets the browser decide. The user's toggle choice wins.
        "language": config.language,
        # Whether a Tesla token is configured; the UI shows the login flow
        # (and a setup banner) when this is false.
        "token_configured": downloader.client.token_manager.has_token(),
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
    # Serialized against syncs via the same lock, so the rescan never reads a
    # PDF that is still being downloaded and never fights the downloader over
    # a sidecar. Refuse instead of queueing: a full sync can take minutes.
    if _sync_lock.locked():
        raise HTTPException(status_code=409, detail="A sync is running — retry when it has finished")
    async with _sync_lock:
        # PDF parsing is CPU-heavy; run it in a thread so the event loop (and
        # the watchdog's /health endpoint) stays responsive during a long rescan.
        return await asyncio.to_thread(_rescan_pdfs)


def _rescan_pdfs() -> dict[str, Any]:
    updated = 0
    skipped = 0
    for pdf_path in sorted(config.invoice_path.glob("*.pdf")):
        json_path = pdf_path.with_suffix(".json")
        existing = storage.read_json(json_path)

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

        if storage.update_json(
            json_path,
            {"total_cost": total_cost, "currency": currency, "meta_version": METADATA_VERSION},
        ):
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


# Holds the PKCE verifier + state between login/start and login/complete.
# Single-user app, so one in-flight login at a time is enough; lost on
# restart, in which case the user simply starts the flow again.
_pending_login: dict[str, str] = {}


@app.post("/api/auth/login/start")
async def auth_login_start() -> dict[str, str]:
    """Begin an interactive Tesla login: return the URL the user opens in
    their browser to sign in. No token is needed to call this — it is how a
    fresh install gets its first token."""
    verifier, challenge = auth.generate_pkce()
    state = secrets.token_urlsafe(16)
    _pending_login.clear()
    _pending_login.update({"verifier": verifier, "state": state})
    return {"url": auth.build_authorize_url(challenge, state)}


class CallbackRequest(BaseModel):
    callback_url: str


@app.post("/api/auth/login/complete")
async def auth_login_complete(payload: CallbackRequest) -> dict[str, str]:
    """Finish the login: parse the pasted callback URL, exchange the code for
    tokens, persist them, and kick off a first sync so invoices appear right
    away instead of only on the next poll."""
    global _manual_sync_task
    code, state = auth.parse_callback(payload.callback_url)
    if not code:
        raise HTTPException(status_code=422, detail="No authorization code found in the pasted address")

    verifier = _pending_login.get("verifier")
    expected_state = _pending_login.get("state")
    if not verifier:
        raise HTTPException(status_code=409, detail="No login in progress — start the login again")
    # state is only absent when the user pasted a bare code; when present it
    # must match, which ties the callback to the login this server started.
    if state and expected_state and state != expected_state:
        raise HTTPException(status_code=422, detail="Login state mismatch — start the login again")

    token_manager = downloader.client.token_manager
    try:
        await asyncio.to_thread(token_manager.exchange_authorization_code, code, verifier)
    except TeslaAuthError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    _pending_login.clear()

    # Fetch the current and previous month immediately (unless a sync is
    # already running), so the dashboard is not empty after connecting.
    if not _sync_lock.locked():
        _manual_sync_task = asyncio.create_task(_run_sync(_current_and_previous_month(), kind="after login"))
    return {"status": "connected"}


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
async def delete_file(filename: str) -> dict[str, Any]:
    """Delete a single stored file (PDF or metadata sidecar). A deleted PDF
    is re-downloaded on the next sync as long as its invoice exists at Tesla."""
    file_path = _resolve_invoice_file(filename, (".pdf", ".json"))
    file_path.unlink()
    # Deleting a PDF removes its sidecar too — an orphan sidecar would keep a
    # ghost row in the dashboard and CSV with no file behind it.
    sidecar_deleted = False
    if file_path.suffix == ".pdf":
        sidecar = file_path.with_suffix(".json")
        if sidecar.is_file():
            sidecar.unlink()
            sidecar_deleted = True
    logger.info(f"Deleted {file_path.name} via file browser (sidecar too: {sidecar_deleted})")
    return {"status": "deleted", "file": file_path.name, "sidecar_deleted": sidecar_deleted}


class EmailRequest(BaseModel):
    """Recipient travels in the POST body: addresses in query strings end up
    in proxy and access logs."""

    to: str | None = None


def _validated_recipient(payload: EmailRequest | None) -> str:
    if not emailer.is_configured:
        raise HTTPException(status_code=400, detail="Email is not configured (from/mailserver missing)")
    recipient = ((payload.to if payload else None) or config.email_to).strip()
    if "@" not in recipient:
        raise HTTPException(status_code=422, detail="Recipient must be a valid email address")
    return recipient


# NOTE: must be registered before /api/email/{filename}, or "send-skipped"
# would be interpreted as a file name by the route below.
@app.post("/api/email/send-skipped")
async def email_skipped_invoices(payload: EmailRequest | None = None) -> dict[str, Any]:
    """Send all invoices flagged ``email_skipped`` as a combined export
    (batched into a few emails instead of one per invoice)."""
    recipient = _validated_recipient(payload)
    try:
        result = await asyncio.to_thread(emailer.send_skipped, recipient)
    except Exception as e:
        logger.error(f"Combined email export failed: {e}")
        raise HTTPException(status_code=502, detail=f"Sending failed: {e}") from e
    return {"status": "sent", "to": recipient, **result}


@app.post("/api/email/{filename}")
async def email_invoice(filename: str, payload: EmailRequest | None = None) -> dict[str, str]:
    """Manually send one invoice PDF; body ``{"to": …}`` overrides the
    configured recipient (the UI pre-populates it with the config default)."""
    file_path = _resolve_invoice_file(filename, (".pdf",))
    recipient = _validated_recipient(payload)
    try:
        await asyncio.to_thread(emailer.send_single, file_path, recipient)
    except Exception as e:
        logger.error(f"Manual email send failed for {filename}: {e}")
        raise HTTPException(status_code=502, detail=f"Sending failed: {e}") from e
    return {"status": "sent", "to": recipient}


@app.get("/api/export.zip")
async def export_zip() -> FileResponse:
    """All stored invoice PDFs bundled into a single ZIP for bulk download."""
    zip_path = await asyncio.to_thread(_build_zip)
    # Served from a temp file and deleted after the response: years of
    # invoices assembled in memory could OOM a small Home Assistant box.
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename="tesla_invoices.zip",
        background=BackgroundTask(os.unlink, zip_path),
    )


def _build_zip() -> str:
    import tempfile
    import zipfile

    fd, zip_path = tempfile.mkstemp(prefix="tesla_invoices_", suffix=".zip")
    try:
        # PDFs are already compressed, so store them as-is instead of wasting
        # CPU on deflate for a ~0% gain.
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_STORED) as archive:
            for pdf_file in sorted(config.invoice_path.glob("*.pdf")):
                if pdf_file.name.startswith("."):
                    continue  # internal state files are never invoices
                archive.write(pdf_file, arcname=pdf_file.name)
    except BaseException:
        with suppress(OSError):
            os.unlink(zip_path)
        raise
    return zip_path


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
