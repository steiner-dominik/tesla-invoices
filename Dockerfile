# uv only exports the lockfile during the build; it never ends up in the
# final image. A named stage (rather than an inline --mount image) so
# Dependabot sees and bumps the pinned uv version.
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM python:3.14-alpine

# tzdata makes time zones work: TZ env var (standalone) and the Home
# Assistant time zone (fetched from the Supervisor at startup).
# su-exec lets the entrypoint drop root after fixing volume ownership.
RUN apk add --no-cache tzdata su-exec \
    && addgroup -S tesla && adduser -S -G tesla tesla

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/tesla-invoices

WORKDIR /opt/tesla-invoices

# Installed straight from uv.lock (fully pinned, transitive dependencies
# included) so image builds are reproducible, and with --require-hashes so
# pip refuses any file whose hash differs from the one locked — a tampered
# or substituted package fails the build instead of landing in the image.
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv export --frozen --no-dev --no-emit-project --output-file /tmp/requirements.txt \
    && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

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
