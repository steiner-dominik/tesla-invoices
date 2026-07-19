<!-- https://developers.home-assistant.io/docs/apps/presentation#keeping-a-changelog -->
## 2026.07.19.1

- **New versioning scheme**: releases are now fully date-based —
  `YYYY.MM.DD.N` (this release: `2026.07.19.1`), where `N` counts releases
  published on the same day. The old `YYYY.MM.patch` tags stay valid.
- **Installable as an app (PWA)**: open the dashboard in a browser and use
  *Install app* (Chrome/Edge) or *Add to Home Screen* (iOS/Android) to get a
  standalone app window with its own icon. Nothing is cached offline — the
  dashboard always shows live data.
- **New app icon**, redrawn as vector art in the familiar style: the favicon
  now switches automatically between a light and a dark tile with the OS
  theme, and the same artwork is used for the PWA, the Home Assistant app
  store, and Apple touch icons
  (regenerate anytime with `scripts/make_icons.py`).
- **README refresh**: build/release/license badges and dashboard screenshots
  (mock data only) that follow GitHub's light/dark theme.
- **SBOM with every release**: each GitHub release now carries a CycloneDX
  `sbom.cdx.json` asset, and the Docker images embed an SBOM attestation and
  build provenance. The README's new *Dependencies, SBOM & continuity*
  section documents how to update dependencies and keep the project alive
  should it ever become unmaintained.

Fixes from an external security & architecture audit:

- **Privacy: the full VIN no longer leaks into logs.** Local file names
  embed the VIN, and several log lines printed those names verbatim —
  undoing the deliberate "VIN ending in XXXX" log policy. All such log
  lines now redact the VIN down to its last four characters.
- **Hardened the container entrypoint**: volume ownership is now fixed
  without ever following symlinks, so a malicious link planted inside a
  mounted volume can no longer redirect the ownership change to a host file.
- ⚠️ **`GET /api/export.csv` and `GET /api/export.zip` now require the
  `X-Requested-With` header** (any value), like every mutating request.
  This stops malicious websites from hot-linking these expensive endpoints
  cross-origin to burn CPU and disk I/O. Scripts calling them must add the
  header; the dashboard buttons were adapted.
- **Faster dashboard with a large history**: invoice metadata is now served
  from an always-fresh in-memory cache (invalidated by file modification
  time), so `/api/analytics` and the CSV export no longer re-read and
  re-parse every sidecar file on each request — easier on SD cards, too.
- **`/health` stays responsive during a PDF re-scan** (the extraction loop
  now yields between files), so the HA watchdog cannot misfire during a
  long re-scan.
- **Token reads are now lock-protected**, closing a theoretical race where
  a request could observe a half-rotated access/refresh token pair.

## 2026.07.08

- **Easier Tesla login**: the *Connect Tesla account* dialog now walks you
  through the sign-in with clear numbered steps, including a hint for desktop
  browsers where the `tesla://auth/callback` address never reaches the address
  bar (find it via right-click → *Inspect* → *Console*/*Network*). New
  fallback: **paste a refresh token directly** (e.g. from
  [tesla_auth](https://github.com/adriankumpf/tesla_auth) or the iOS
  [Auth app for Tesla](https://apps.apple.com/us/app/auth-app-for-tesla/id1552058613))
  — the token is verified with Tesla before it is stored, so a bad paste never
  replaces a working credential. New endpoint: `POST /api/auth/token`.
- **Charging invoices can now be disabled** (`ENABLE_CHARGING_INVOICE=False` /
  HA option `enable_charging_invoice`) for users who only want subscription
  invoices — the mirror of the existing subscription switch. Enable none, one,
  or both: with **both disabled, downloads are paused** — syncs only verify
  the Tesla connection (keeping the token fresh), and the dashboard shows a
  banner that nothing will be downloaded.
- **Translations are now easy to contribute**: the dashboard texts moved out
  of the code into plain JSON files (`app/static/i18n/<code>.json` plus a
  `languages.json` manifest). Adding a language needs no code changes — see
  the README ("Contributing a translation"). The language toggle is built
  from the manifest automatically.
- **Correct month assignment across timezone boundaries**: charging sessions
  are now bucketed (and their files named) by the *local* date instead of
  UTC — an invoice from 23:30 UTC on June 30 is a July invoice in Vienna.
  Previously such invoices could be missed by the current/previous-month
  sync window or grouped into the wrong month.
- **Email export robustness**: one unreadable PDF no longer aborts the export
  loop and silently skips the remaining invoices; concurrent send paths
  (auto-export, manual send, combined backlog) are serialized so an invoice
  can never be emailed twice by two overlapping operations.
- **Fixed a race between login and a running sync**: token refreshes and
  interactive logins are now serialized, so a sync can no longer read a
  half-rotated token pair.
- **Invoices readable on shared volumes**: downloaded PDFs and metadata files
  are now written with `0644` permissions instead of `0600`, so they are
  readable by other users/apps when the invoice directory is shared (e.g. an
  SMB mount). Token files stay `0600`.
- **Hardened against unexpected Tesla API responses**: a non-object value in
  the charging-history GraphQL response (e.g. `"me": "Not Found"`) no longer
  crashes the sync.
- The dashboard was split into separate HTML/CSS/JS files (served under
  `static/`); no functional change beyond the above.

## 2026.07.07

Two fixes for the in-app Tesla login introduced in 2026.07.06:

- **"Open Tesla login" works again**: Tesla deregistered the
  `https://auth.tesla.com/void/callback` redirect that the login relied on,
  so it failed with *"The 'redirect_uri' supplied is not registered for this
  'client_id'"*. The login now uses the Tesla mobile app's
  `tesla://auth/callback` deep link instead — the same fix as
  [tesla_auth v0.13.0](https://github.com/adriankumpf/tesla_auth). After
  signing in, the browser shows an error or an empty page (it cannot open
  the Tesla app) — copy the `tesla://auth/callback?code=…` address from the
  address bar and paste it into the app, as the updated dialog explains.
- **Setup banner no longer sticks around**: the "Welcome! Connect your Tesla
  account" banner was shown even when a token was already configured (e.g.
  via the `refresh_token` option or `REFRESH_TOKEN`), because its stylesheet
  overrode the attribute that hides it. It now disappears as soon as a Tesla
  account is connected.

## 2026.07.06

- **Sign in from the dashboard — no token needed to start**: a new **Connect
  Tesla account** button runs Tesla's normal login (on Tesla's own website)
  and stores the resulting token automatically. You no longer have to
  generate a refresh token with a separate tool first; the app now starts
  with no configuration at all. Providing `REFRESH_TOKEN` / a mounted token
  file (or a token from
  [tesla_auth](https://github.com/adriankumpf/tesla_auth)) still works and
  simply pre-fills the credential.
  - The login happens entirely on Tesla's site: you open it in a new tab,
    sign in (any security check is Tesla's), and paste the resulting address
    back into the app, which exchanges it for a token. Your password is
    never seen by this app.
  - A **Tesla account** section on the Maintenance tab shows the connection
    status and lets you reconnect (e.g. to switch accounts).
  - New endpoints: `POST /api/auth/login/start`, `POST /api/auth/login/complete`.
- **German translation**: the dashboard is now available in English and
  German, including all dialogs, statuses and number/month formatting. A new
  **EN / DE** switch sits in the top-right corner next to the theme toggle
  (the choice is remembered per browser).
- **Follows your language automatically**: as a Home Assistant app, the
  dashboard defaults to the language configured in Home Assistant (read from
  the Core API at startup; the app now requests `homeassistant_api`
  permission for this). Standalone deployments can set the optional
  `LANGUAGE` environment variable (`en`/`de`); otherwise the browser
  language decides.

## 2026.07.05

Security- and robustness-focused release, following an external code review.

### Security

- **Cross-site request forgery blocked**: all `POST`/`DELETE` API endpoints
  now require an `X-Requested-With` header (the dashboard sends it
  automatically). Without this, any website open in a browser on the same
  network could trigger syncs, delete files, or **email the stored invoices
  to an arbitrary address**. API scripts must add the header — see the
  README's API section.
- **Email recipients moved into the request body** (`{"to": "…"}` instead of
  `?to=…`), keeping addresses out of proxy and access logs.
- **Optional login for standalone deployments**: set
  `BASIC_AUTH_USER`/`BASIC_AUTH_PASS` to protect the web UI and API with
  HTTP Basic Auth (`/health` stays open for the container healthcheck). The
  Home Assistant app is unaffected — ingress already authenticates.
- **SMTP certificate verification is now explicit**
  (`ssl.create_default_context()`), instead of relying on library defaults.
- **The container no longer runs as root**: the entrypoint fixes volume
  ownership and drops to an unprivileged user before starting the app. If
  you bind-mount `invoices/`/`secrets/`, their owner changes to the
  container user on first start.
- Downloaded files are verified to actually be PDFs (`%PDF` signature), not
  just to carry a PDF Content-Type.

### Fixed

- **Multi-vehicle accounts**: the charging history request now passes the
  vehicle's VIN as a GraphQL variable and ignores sessions of other
  vehicles. Previously, accounts with several vehicles could download every
  invoice once per vehicle, filed under the wrong VIN.
- **Amount parsing**: English-format totals without decimals (`1,234`) were
  read as `1.234`. A single separator followed by exactly three digits is
  now treated as a thousands separator.
- **No more duplicate emails after crashes or concurrent writes**: all
  metadata files are written atomically (temp file + rename) under a lock;
  the PDF re-scan no longer runs concurrently with a sync (it answers
  HTTP 409 while one is running).
- Deleting a PDF in the Files tab now deletes its metadata entry too, so no
  ghost rows remain in the dashboard and CSV export.
- The "Send skipped invoices" counter no longer counts metadata files whose
  PDF is missing.
- Date-only invoices (subscriptions) no longer show a made-up "00:00" time.
- Retry give-up errors now report the actual last error instead of always
  claiming "connection errors".

### Changed

- **In-page dialogs** replace browser `prompt()`/`confirm()` popups, which
  can silently fail inside embedded webviews (e.g. the Home Assistant
  companion apps) and make buttons appear dead.
- **ZIP export streams from disk** instead of assembling the whole archive
  in memory — large archives no longer risk out-of-memory on small boxes.
- The dashboard pauses its background polling while its tab is hidden.
- The project is now licensed under the **MIT License**.
- Dependencies are fully pinned via a committed `uv.lock`;
  `requirements.txt` (used for the Docker image) is exported from it and
  CI verifies they stay in sync.

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
