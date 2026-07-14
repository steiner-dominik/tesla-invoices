# ⚡ Tesla Invoices

[![GitHub Sponsors](https://img.shields.io/badge/GitHub%20Sponsors-%E2%9D%A4-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/steiner-dominik)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-donate-FF5E5B?logo=kofi&logoColor=white)](https://ko-fi.com/dominik_steiner)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/dominik.st)

**Automatically download all your Tesla charging & subscription invoices — and actually understand them.**

> ⚠️ **This is an independent community project. It is not affiliated with, endorsed by, or
> supported by Tesla, Inc. in any way.** "Tesla" is used here solely to describe which
> vehicles and accounts the software works with.

Tesla Invoices fetches every Supercharging and subscription invoice from your Tesla account,
stores the PDFs locally, and serves a clean analytics dashboard: monthly energy and cost
charts, price per kWh, multi-vehicle support, CSV export for your expense report, and
optional automatic email forwarding of every new invoice.

Runs anywhere Docker runs — or as a
[Home Assistant app](https://github.com/steiner-dominik/home-assistant-apps) with one click.

---

## ✨ Features

- **Automatic downloads** — checks for new invoices on a configurable interval and keeps
  the full history in sync (nothing is missed at month boundaries).
- **Analytics dashboard** — totals, monthly kWh/cost charts, per-session price per kWh,
  filtering, search, and sorting across your whole fleet.
- **Built-in PDF viewer** — view any invoice right in the dashboard, no extra login.
- **CSV export** — one click gets you a spreadsheet-ready export of all invoice data.
- **Email export** — optionally sends every new invoice as an email attachment, exactly
  once; individual invoices can also be mailed on demand to any recipient.
- **Multi-currency aware** — costs are grouped per currency (never blindly converted),
  with credit notes correctly reducing your totals.
- **English & German** — the dashboard follows the Home Assistant language (or the
  `LANGUAGE` env var / browser language when standalone) and can be switched
  anytime via the EN/DE toggle in the header.
- **Sign in from the dashboard** — no token wrangling: start the app, click
  **Connect Tesla account**, log in on Tesla's own page, and you're done. The
  token is obtained, rotated, and stored automatically with strict file permissions.

## 🚀 Quick start (Docker)

**No configuration needed to start.** Just run it:

```bash
docker run -d --name tesla-invoices \
  -p 9000:9000 \
  -v ./invoices:/opt/tesla-invoices/invoices \
  -v ./secrets:/opt/tesla-invoices/secrets \
  ghcr.io/steiner-dominik/tesla-invoices:latest
```

Then open **<http://localhost:9000>** and click **“Connect Tesla account”** —
you log in on Tesla's own website (nothing but the resulting token is ever
stored), and the first sync starts automatically. To fetch your complete
history afterwards, click **“Sync all history”** on the Maintenance tab.

That's the whole setup. Everything else — email export, polling interval,
preferred currency, a login for the web UI — is optional (see
[Configuration](#️-configuration)); with `docker compose` add an
`env_file` for those. A ready-made [docker-compose.yml](docker-compose.yml)
is included:

```bash
docker compose up -d
```

> 💡 **Prefer to bring your own token?** You still can: mount
> `secrets/refresh_token.txt` or set `REFRESH_TOKEN` (or a short-lived
> `ACCESS_TOKEN`) — see [Configuration](#️-configuration). The in-app login
> is just the easy path.

> ⚠️ The web UI has **no authentication by default** — only expose port 9000 on
> a trusted network, set `BASIC_AUTH_USER`/`BASIC_AUTH_PASS` to require a
> login, or put it behind your reverse proxy's auth.

## 🏠 Home Assistant app

Want this inside Home Assistant, with ingress and the app store handling
updates? Use the companion app repository:

👉 **[steiner-dominik/home-assistant-apps](https://github.com/steiner-dominik/home-assistant-apps)**

The HA app uses the exact same image built from this repository — same
features, same dashboard, zero extra configuration files.

## ⚙️ Configuration

All settings are environment variables (see [docker.env.example](docker.env.example)):

**Everything here is optional** — the app starts with no configuration and you
sign in from the dashboard.

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `REFRESH_TOKEN` | – | Optional Tesla refresh token (or mount `secrets/refresh_token.txt`); usually not needed — sign in from the dashboard instead |
| `ACCESS_TOKEN` | – | Optional; obtained automatically from the refresh token if omitted (see note below) |
| `TZ` | UTC | Time zone for timestamps and month boundaries, e.g. `Europe/Vienna` |
| `POLLING_INTERVAL` | `15` | Minutes between checks for new invoices |
| `ENABLE_SUBSCRIPTION_INVOICE` | `True` | Also download subscription (e.g. Premium Connectivity) invoices |
| `DEFAULT_CURRENCY` | auto | Preferred dashboard currency (e.g. `EUR`); auto-detected when empty |
| `LANGUAGE` | browser | Default dashboard language (`en`, `de`); the EN/DE toggle in the dashboard overrides it per user |
| `ENABLE_EMAIL_EXPORT` | `False` | Email every new invoice, exactly once |
| `EMAIL_FROM` / `EMAIL_TO` | – | Sender / recipient for the email export |
| `EMAIL_SERVER` / `EMAIL_SERVER_PORT` | – / `587` | SMTP server; port 587 = STARTTLS, 465 = implicit TLS |
| `EMAIL_USER` / `EMAIL_PASS` | – | SMTP credentials (leave empty for no login) |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` | – | Optional web UI / API login (HTTP Basic Auth); set both or neither. `/health` stays open for the container healthcheck |
| `PORT` | `9000` | Web UI port |
| `INVOICE_PATH` | `/opt/tesla-invoices/invoices` | Where PDFs and metadata are stored |
| `ACCESS_TOKEN_PATH` / `REFRESH_TOKEN_PATH` | `/opt/tesla-invoices/secrets/…` | Token file locations |

**Signing in:** the easiest way is the **Connect Tesla account** button in the
dashboard — it runs Tesla's normal login (on Tesla's own site) and stores the
resulting refresh token for you. Supplying `REFRESH_TOKEN` / a mounted token
file still works and simply pre-fills it. Tokens generated with
[tesla_auth](https://github.com/adriankumpf/tesla_auth) or the iOS
[Auth app for Tesla](https://apps.apple.com/us/app/auth-app-for-tesla/id1552058613)
are accepted too.

**Access token only:** if you prefer not to store a long-lived, account-wide
credential, you can supply just an `ACCESS_TOKEN` and leave `REFRESH_TOKEN`
empty. The app then works until that token expires (typically a few hours) and
stops syncing with a clear error until you provide a fresh one — with a refresh
token this renewal happens automatically.

**Gmail tip:** use an [App Password](https://myaccount.google.com/apppasswords)
(requires 2-step verification) — your normal account password will not work.

## 🔌 API

The dashboard is a thin client over a small REST API you can use directly:

| Endpoint | Description |
| -------- | ----------- |
| `POST /api/sync?month=all\|cur\|prev\|YYYY-MM&skip_email=true` | Trigger a sync for a month range; `skip_email` marks new invoices as skipped instead of emailing each one |
| `GET /api/analytics` | All invoice metadata + summary + sync status |
| `GET /api/export.csv` | CSV export of all invoices |
| `GET /api/export.zip` | All invoice PDFs bundled into one ZIP |
| `GET /api/download/{filename}?inline=true` | Download / view an invoice PDF |
| `POST /api/email/{filename}` | Email one invoice; JSON body `{"to": "…"}` overrides the configured recipient |
| `POST /api/email/send-skipped` | Email all skipped invoices as a combined, batched export (JSON body `{"to": "…"}` optional) |
| `GET /api/files` | List stored files with previews (Files tab) |
| `DELETE /api/files/{filename}` | Delete one stored PDF (its metadata file is deleted too) |
| `POST /api/files/rescan` | Re-extract cost/currency from stored PDFs (409 while a sync runs) |
| `POST /api/auth/login/start` | Begin an interactive Tesla login; returns the URL to open |
| `POST /api/auth/login/complete` | Finish the login (JSON body `{"callback_url": "…"}`) and store the token |
| `GET /health` | Health check (used by the HA watchdog and Docker) |

> 🔐 **CSRF protection:** every `POST`/`DELETE` request must carry an
> `X-Requested-With` header (any value). Without it the API answers 403 —
> this blocks cross-site request forgery from malicious websites. Example:
>
> ```bash
> curl -X POST -H 'X-Requested-With: cli' 'http://localhost:9000/api/sync?month=2026-06'
> ```

## 🛠️ Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/); `uv.lock`
pins everything, and `requirements.txt` (used by the Docker image) is
exported from it — regenerate both together when changing dependencies:

```bash
uv sync            # creates .venv with all (dev) dependencies
uv run ruff check .
uv run pytest

# after editing dependencies in pyproject.toml:
uv lock && uv export --no-dev --no-hashes --no-emit-project --output-file requirements.txt
```

The layout is deliberately small: [app/api.py](app/api.py) (Tesla API client + auth),
[app/downloader.py](app/downloader.py) (invoice download + PDF cost extraction),
[app/server.py](app/server.py) (FastAPI backend), [app/emailer.py](app/emailer.py)
(SMTP export), [app/static/index.html](app/static/index.html) (dependency-free dashboard).

## ❤️ Support the project

If Tesla Invoices is useful to you, you can support its development:

- [GitHub Sponsors](https://github.com/sponsors/steiner-dominik)
- [Ko-fi](https://ko-fi.com/dominik_steiner)
- [Buy Me a Coffee](https://buymeacoffee.com/dominik.st)

## 🙏 Credits

Originally developed by [Dominik Steiner](https://dominik.st/einer) and Andreas
Sauerwein-Schlosser ([aSauerwein/tesla-invoices](https://github.com/aSauerwein/tesla-invoices)).

### Timeline

- 2024: Initial standalone interactive CLI script to download Tesla charging invoices. (Dominik Steiner)
- 2024: Re-Packaged into a container environment and a Home Assistant add-on. (Andreas Sauerwein-Schlosser)
- 2024: Added Tesla subscription invoices download to the add-on. (Andreas Sauerwein-Schlosser)
- 2026: Refactored the complete codebase to improve maintainability and scalability. Added dashboard feature. (Dominik Steiner w/ Claude Fable 5)
- 2026: Fixed broken Tesla Owner-API endpoints. (Dominik Steiner)


## 📄 License

[MIT](LICENSE)

## ⚖️ Disclaimer

**This project is not affiliated with, endorsed by, sponsored by, or in any
way officially connected to Tesla, Inc.** or any of its subsidiaries. All
product names, trademarks and registered trademarks are property of their
respective owners.

This software is provided “as is” and without any warranty. Use at your own
risk.
