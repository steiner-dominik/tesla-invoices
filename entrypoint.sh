#!/bin/sh
# Drop root before starting the app: it holds a token with full Tesla-account
# access and never needs root itself. Both deployments start the container as
# root (Docker default, HA Supervisor), so fix ownership of the writable
# volumes first, then re-exec as the unprivileged user. When the container is
# started with --user directly, just run.
set -e

if [ "$(id -u)" = "0" ]; then
    for dir in /data /opt/tesla-invoices/invoices /opt/tesla-invoices/secrets; do
        if [ -d "$dir" ]; then
            chown -R tesla:tesla "$dir" 2>/dev/null \
                || echo "WARNING: could not chown $dir (read-only mount?)" >&2
        fi
    done
    exec su-exec tesla:tesla "$@"
fi

exec "$@"
