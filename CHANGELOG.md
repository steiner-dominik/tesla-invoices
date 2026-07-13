<!-- https://developers.home-assistant.io/docs/apps/presentation#keeping-a-changelog -->
## 2026.07.04

- **Aligned chart timelines**: the *Energy per month* and *Cost per month*
  charts now share one x-axis range (first to last month of the filtered
  data), so the two timelines line up instead of each chart starting at its
  own first data point. Months without data show a thin faded **0 bar**
  (tappable, like any other bar) instead of an invisible gap.
- **Bulk ZIP download**: a new **Download all (ZIP)** button next to
  *Export CSV* downloads every stored invoice PDF as one ZIP archive
  (API: `GET /api/export.zip`).
- **Sync all history asks about emails**: instead of a separate checkbox on
  the Maintenance tab, starting a full history sync now asks directly
  whether each new invoice should also be emailed (default: no — invoices
  are marked as *skipped* and can be sent later via *Email backlog*). The
  question only appears when the automatic email export is enabled.
- **Quieter logs**: HTTP requests are no longer logged one line each — the
  Supervisor watchdog and the Docker healthcheck poll `/health` constantly
  and drowned the log. Syncs, downloads and emails are still logged.
- **Dependency updates**: `pypdf` 5.1.0 → 6.14.2 (used for extracting
  subscription invoice totals) and `uvicorn` 0.50.0 → 0.51.0.

## 2026.07.03

- **Dark mode**: the dashboard now follows your system / Home Assistant
  appearance automatically, and a new **Auto / Light / Dark** switch in the
  top-right corner lets you override it (the choice is remembered).
- **Unified timestamps**: all timestamps in the dashboard (invoice table,
  file browser, sync status, header chip) are now shown as
  `YYYY-MM-DD HH:MM` (24-hour clock) including the time zone.
- **Email export no longer floods your inbox**:
  - Invoices synced while email export was disabled are marked as *skipped*
    and are never auto-sent later — enabling the export only emails invoices
    that are new from that point on.
  - **Sync all history** now skips email sending by default; a new checkbox
    on the Maintenance tab lets you opt in explicitly.
  - New **Maintenance → Email backlog** section: sends all skipped invoices
    as a **combined export**, batched into a few emails (max. 20 attachments
    / 15 MB each) instead of one mail per invoice.
    (API: `POST /api/email/send-skipped`, `POST /api/sync?skip_email=true`.)
- **Nicer emails**: exported invoices now have a proper text body with an
  invoice summary (date, type, vehicle, location, energy, amount) and a
  meaningful subject line instead of a bare attachment.

## 2026.07.02

- **Home Assistant time zone support**: the app now automatically adopts the
  time zone configured in Home Assistant (read from the Supervisor at
  startup), so log timestamps and the current/previous-month sync window
  match your local time instead of UTC. Standalone deployments can set the
  `TZ` environment variable (which always takes precedence); `tzdata` is now
  included in the image.
- **Clearer logs, no sensitive data**: log messages have been rewritten in
  plain language ("Starting invoice sync…", "Invoice sync finished", …).
  Email addresses, full VINs and raw Tesla API responses are no longer
  written to the log at any level, so logs can be shared in bug reports
  safely. Tokens were never logged.
- **`access_token` option removed from the Home Assistant app**: it was never
  needed — the refresh token is sufficient, and access tokens are obtained,
  rotated and persisted automatically. If the app reports an invalid
  `access_token` option after updating, remove that line via
  *Configuration → three-dot menu → Edit in YAML*.
- **Access-token-only mode for the standalone app**: users who prefer not to
  store a long-lived credential can now supply just `ACCESS_TOKEN` (no
  refresh token). Syncing works until the token expires and then stops with a
  clear error; renewal stays automatic when a refresh token is configured.
- **Working token generator links**: the documentation now points to
  [tesla_auth](https://github.com/adriankumpf/tesla_auth) (Windows/macOS/
  Linux) and [Auth app for Tesla](https://apps.apple.com/us/app/auth-app-for-tesla/id1552058613)
  (iOS); the previously listed tools are outdated or gone.
- Clarified in the README, docs and dashboard footer that this project is
  **not affiliated with Tesla, Inc.**
- Docker image: added a container `HEALTHCHECK` for standalone deployments;
  the README now documents all API endpoints.

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
