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
- **Self-healing auth** — you only need a refresh token; access tokens are obtained and
  rotated automatically and stored with strict file permissions.

## 🚀 Quick start (Docker)

You need a Tesla **refresh token**, generated with one of these apps:

| Platform | App |
| -------- | --- |
| Windows / macOS / Linux | [tesla_auth](https://github.com/adriankumpf/tesla_auth) (recommended) |
| iOS | [Auth app for Tesla](https://apps.apple.com/us/app/auth-app-for-tesla/id1552058613) |

> 🔒 **Treat tokens like passwords.** They grant full access to your Tesla account.
> Never commit them to version control.
>
> Prefer not to hand the app a long-lived credential? Supply only a (short-lived)
> **access token** instead — see the Configuration section below.

```bash
git clone https://github.com/steiner-dominik/tesla-invoices.git
cd tesla-invoices

# 1. Store your token(s)
echo "YOUR_REFRESH_TOKEN" > secrets/refresh_token.txt

# 2. Configure (polling interval, email export, …)
cp docker.env.example docker.env   # then edit to taste

# 3. Run
docker compose up -d
```

Open **<http://localhost:9000>** — the first sync starts automatically.
To fetch your complete invoice history, click **“Sync all history”** on the
Maintenance tab.

Prefer plain `docker run`?

```bash
docker run -d --name tesla-invoices \
  -p 9000:9000 \
  -v ./invoices:/opt/tesla-invoices/invoices \
  -v ./secrets:/opt/tesla-invoices/secrets \
  --env-file docker.env \
  ghcr.io/steiner-dominik/tesla-invoices:latest
```

> ⚠️ The web UI has **no authentication** — only expose port 9000 on a trusted
> network (or put it behind your reverse proxy's auth).

## 🏠 Home Assistant app

Want this inside Home Assistant, with ingress and the app store handling
updates? Use the companion app repository:

👉 **[steiner-dominik/home-assistant-apps](https://github.com/steiner-dominik/home-assistant-apps)**

The HA app uses the exact same image built from this repository — same
features, same dashboard, zero extra configuration files.

## ⚙️ Configuration

All settings are environment variables (see [docker.env.example](docker.env.example)):

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `REFRESH_TOKEN` | – | Tesla refresh token (alternatively mount `secrets/refresh_token.txt`) |
| `ACCESS_TOKEN` | – | Optional; obtained automatically from the refresh token if omitted (see note below) |
| `TZ` | UTC | Time zone for timestamps and month boundaries, e.g. `Europe/Vienna` |
| `POLLING_INTERVAL` | `15` | Minutes between checks for new invoices |
| `ENABLE_SUBSCRIPTION_INVOICE` | `True` | Also download subscription (e.g. Premium Connectivity) invoices |
| `DEFAULT_CURRENCY` | auto | Preferred dashboard currency (e.g. `EUR`); auto-detected when empty |
| `ENABLE_EMAIL_EXPORT` | `False` | Email every new invoice, exactly once |
| `EMAIL_FROM` / `EMAIL_TO` | – | Sender / recipient for the email export |
| `EMAIL_SERVER` / `EMAIL_SERVER_PORT` | – / `587` | SMTP server; port 587 = STARTTLS, 465 = implicit TLS |
| `EMAIL_USER` / `EMAIL_PASS` | – | SMTP credentials (leave empty for no login) |
| `PORT` | `9000` | Web UI port |
| `INVOICE_PATH` | `/opt/tesla-invoices/invoices` | Where PDFs and metadata are stored |
| `ACCESS_TOKEN_PATH` / `REFRESH_TOKEN_PATH` | `/opt/tesla-invoices/secrets/…` | Token file locations |

**Access token only:** if you prefer not to give the app a long-lived,
account-wide credential, you can supply just an `ACCESS_TOKEN` and leave
`REFRESH_TOKEN` empty. The app then works until that token expires (typically
a few hours) and stops syncing with a clear error until you provide a fresh
one — with a refresh token this renewal happens automatically. The Home
Assistant app intentionally has no access-token option; it always uses the
refresh-token flow.

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
| `POST /api/email/{filename}?to=…` | Email one invoice to any recipient |
| `POST /api/email/send-skipped?to=…` | Email all skipped invoices as a combined, batched export |
| `GET /api/files` | List stored files with previews (Files tab) |
| `DELETE /api/files/{filename}` | Delete one stored PDF / metadata file |
| `POST /api/files/rescan` | Re-extract cost/currency from stored PDFs |
| `GET /health` | Health check (used by the HA watchdog and Docker) |

## 🛠️ Development

```bash
python -m venv venv && . venv/bin/activate
pip install -r requirements.txt pytest httpx ruff
ruff check .
pytest
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


## ⚖️ Disclaimer

**This project is not affiliated with, endorsed by, sponsored by, or in any
way officially connected to Tesla, Inc.** or any of its subsidiaries. All
product names, trademarks and registered trademarks are property of their
respective owners.

This software is provided “as is” and without any warranty. Use at your own
risk.
