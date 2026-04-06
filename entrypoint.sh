#!/bin/sh
set -e
# Ensure /data top-level dir is owned by appuser. NOT recursive — recursive
# chown on millions of frames would stall startup for minutes.
chown appuser:appuser /data 2>/dev/null || true
exec setpriv --reuid=appuser --regid=appuser --init-groups \
    uvicorn app:app --host 0.0.0.0 --port 8080 "$@"
