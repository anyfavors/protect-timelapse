#!/bin/sh
set -e
# Ensure /data top-level dir is owned by appuser. NOT recursive — recursive
# chown on millions of frames would stall startup for minutes.
chown appuser:appuser /data 2>/dev/null || echo "[entrypoint] WARN: could not chown /data (may be read-only or wrong UID)"
# uiprotect writes config to $HOME/.config/ufp/
chown -R appuser:appuser /home/appuser/.config 2>/dev/null || echo "[entrypoint] WARN: could not chown .config"
export HOME=/home/appuser

# Single worker: APScheduler and the render queue run inside the process.
# Multiple workers would spawn duplicate background workers and concurrent renders.
# Concurrency is handled by asyncio within the single process. (D5)
exec setpriv --reuid=appuser --regid=appuser --init-groups \
    uvicorn app:app \
        --host 0.0.0.0 \
        --port 8080 \
        --loop uvloop \
        --timeout-graceful-shutdown 30 \
        "$@"
