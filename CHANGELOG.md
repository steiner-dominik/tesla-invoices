<!-- https://developers.home-assistant.io/docs/apps/presentation#keeping-a-changelog -->
## 2026.07.01

First public release. Tesla Invoices started as an interactive CLI script,
grew into a Home Assistant add-on
([aSauerwein/tesla-invoices](https://github.com/aSauerwein/tesla-invoices)),
and has been completely refactored into a single application that runs
standalone via Docker or as a [Home Assistant app](https://github.com/steiner-dominik/home-assistant-apps).
Everything below ships in this first release.

### Invoice downloads

- Automatically downloads **all Supercharging and subscription invoices**
  (e.g. Premium Connectivity; subscription downloads can be disabled) on a
  configurable polling interval (1–1440 minutes, default 15).
- Every cycle covers the current **and** previous month, so invoices appearing
  right after a month boundary are never missed; the complete history can be
  fetched via **"Sync all history"** or `POST /api/sync?month=all|cur|prev|YYYY-MM`.
- Follows Tesla's paginated charging-history GraphQL API (which replaced the
  old REST endpoint) with the app-like headers it requires, aggregating
  energy, tier usage and cost from the per-fee-type records.
- One broken invoice never aborts a sync: download errors are logged per
  invoice and retried next cycle; non-PDF responses are rejected; two
  same-day invoices for one vehicle get unique file names.
- Credit notes are stored with negative amounts, so refunds correctly reduce
  all totals; subscription costs are extracted from the localized grand-total
  line of the PDF.

### Resilient Tesla authentication & API access

- **A refresh token alone is enough** — access tokens are bootstrapped,
  rotated and persisted automatically; rotated refresh tokens are saved, and
  freshly pasted tokens win over stored ones only when newer.
- Works around Tesla's bot mitigation: all requests are pinned to **TLS 1.3**,
  and the token refresh is sent with a **browser TLS fingerprint**
  (`curl_cffi`) — Tesla silently issues down-scoped tokens to refreshes made
  from vanilla Python TLS stacks (same root cause as
  [teslamate#5399](https://github.com/teslamate-org/teslamate/issues/5399)).
- A 401/403 from any endpoint forces one token refresh and a retry instead of
  trusting a poisoned access token until expiry; connection resets and
  HTTP 429 are retried with backoff (honoring `Retry-After`); the polling
  schedule adds random jitter so its cadence is not metronomic.

### Analytics dashboard

- Web dashboard (Home Assistant ingress or standalone): summary cards,
  monthly **energy and cost charts** with running totals, per-session
  **price per kWh**, filtering by year/vehicle/type, free-text search and
  sortable columns — following the active filters.
- **Multi-currency aware**: totals are grouped per currency, never blindly
  converted; the preferred display currency is configurable and auto-detected
  by default.
- **Built-in PDF viewer** (no bounce out of the Home Assistant mobile app),
  **CSV export** for expense reports (`GET /api/export.csv`, escaped against
  spreadsheet formula injection), and a **Files tab** with view, download and
  delete actions.
- **Maintenance tab** with detailed sync status, a failure banner when the
  last sync failed, "Sync all history" and "Re-scan PDFs" (re-extracts
  cost/currency from stored PDFs with the current parser).

### Email export

- Optionally sends every new invoice as an email attachment, **exactly once**
  (tracked in the metadata sidecar files).
- Individual invoices can be mailed on demand to any recipient from the
  dashboard.
- SMTP with STARTTLS (587) or implicit TLS (465); Gmail app passwords
  documented.

### Security & robustness

- Token files are stored with mode 600; the download endpoint is hardened
  against path traversal.
- HTTP timeouts everywhere, plus a watchdog health check that reports
  unhealthy if the download loop ever dies (the Supervisor restarts the app).
- PDF parsing and file scanning run off the event loop, so long re-scans
  never block the health check or the dashboard.
- Metadata sidecar files carry a `meta_version`; values produced by older
  extraction logic are re-derived automatically on the next sync or re-scan.

### Packaging & deployment

- One `python:3.14-alpine` image serves both deployments — standalone Docker
  (environment variables / `docker.env`) and the Home Assistant app
  (`/data/options.json`, auto-detected). No s6/bashio, direct Python
  execution.
- Published as a prebuilt multi-arch image to
  `ghcr.io/steiner-dominik/tesla-invoices` (amd64, aarch64) — the Home
  Assistant app pulls it instead of building locally.
- CI runs lint (ruff) and the test suite on every push and pull request.
- Calendar versioning (`YYYY.MM.patch`, zero-padded so versions sort lexically), matching Home Assistant conventions.
