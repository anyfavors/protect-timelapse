#!/bin/sh
set -e

export HOME=/home/appuser

# If already running as appuser (UID 1000), skip chown/setpriv — Dockerfile
# USER directive already dropped privileges.
if [ "$(id -u)" = "1000" ]; then
    # Single worker: APScheduler and the render queue run inside the process.
    # Multiple workers would spawn duplicate background workers and concurrent renders.
    # Concurrency is handled by asyncio within the single process. (D5)
    exec uvicorn app:app \
        --host 0.0.0.0 \
        --port 8080 \
        --loop uvloop \
        --timeout-graceful-shutdown 30 \
        "$@"
fi

# Running as root (e.g. docker run --user root or no USER directive):
# fix ownership then drop to appuser.
chown appuser:appuser /data 2>/dev/null || echo "[entrypoint] WARN: could not chown /data (may be read-only or wrong UID)"
chown -R appuser:appuser /home/appuser/.config 2>/dev/null || echo "[entrypoint] WARN: could not chown .config"

exec setpriv --reuid=appuser --regid=appuser --init-groups \
    uvicorn app:app \
        --host 0.0.0.0 \
        --port 8080 \
        --loop uvloop \
        --timeout-graceful-shutdown 30 \
        "$@"
