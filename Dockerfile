FROM python:3.14-alpine

# tzdata makes time zones work: TZ env var (standalone) and the Home
# Assistant time zone (fetched from the Supervisor at startup).
RUN apk add --no-cache tzdata

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/tesla-invoices

WORKDIR /opt/tesla-invoices

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 9000

# For standalone deployments; the Home Assistant Supervisor uses its own
# watchdog against the same endpoint (config.yaml).
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD wget -q -O /dev/null "http://127.0.0.1:${PORT:-9000}/health" || exit 1

# The same image serves both deployments: standalone Docker (configured via
# environment variables) and the Home Assistant app (the Supervisor mounts
# /data with options.json, which Config.load() picks up automatically).
CMD ["python", "-m", "app.main"]
