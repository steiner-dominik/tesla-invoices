FROM python:3.14-alpine

# tzdata makes time zones work: TZ env var (standalone) and the Home
# Assistant time zone (fetched from the Supervisor at startup).
# su-exec lets the entrypoint drop root after fixing volume ownership.
RUN apk add --no-cache tzdata su-exec \
    && addgroup -S tesla && adduser -S -G tesla tesla

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/tesla-invoices

WORKDIR /opt/tesla-invoices

# requirements.txt is generated from uv.lock (fully pinned, transitive
# dependencies included) so image builds are reproducible.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
# Pre-create the writable dirs so the app can run without mounted volumes
RUN chmod +x /entrypoint.sh \
    && mkdir -p invoices secrets \
    && chown -R tesla:tesla /opt/tesla-invoices

EXPOSE 9000

# For standalone deployments; the Home Assistant Supervisor uses its own
# watchdog against the same endpoint (config.yaml).
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD wget -q -O /dev/null "http://127.0.0.1:${PORT:-9000}/health" || exit 1

# The same image serves both deployments: standalone Docker (configured via
# environment variables) and the Home Assistant app (the Supervisor mounts
# /data with options.json, which Config.load() picks up automatically).
# The entrypoint chowns the writable volumes and drops to the unprivileged
# "tesla" user before starting the app.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
