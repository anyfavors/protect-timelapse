#!/bin/sh
# Ensure /data is owned by appuser even when mounted from a root-owned host dir.
# Runs as root (set via USER root before ENTRYPOINT), then drops privileges.
chown -R appuser:appuser /data 2>/dev/null || true
exec gosu appuser uvicorn app:app --host 0.0.0.0 --port 8080 "$@"
