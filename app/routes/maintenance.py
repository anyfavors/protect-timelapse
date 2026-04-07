"""Maintenance trigger and database backup endpoints."""

import contextlib
import logging
import os
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks

router = APIRouter(prefix="/api", tags=["maintenance"])
log = logging.getLogger("app.routes.maintenance")


@router.post("/maintenance/run", status_code=202)
async def trigger_maintenance(background_tasks: BackgroundTasks) -> dict:
    """Manually trigger the maintenance run (pruning, reconciliation, etc.)."""
    from app.maintenance import run_maintenance

    background_tasks.add_task(run_maintenance)
    log.info("Manual maintenance run triggered via API")
    return {"status": "started", "message": "Maintenance run queued as background task"}


@router.post("/backup", status_code=202)
async def trigger_backup(background_tasks: BackgroundTasks) -> dict:
    """Trigger a safe SQLite backup to /data/backups/ using the sqlite3 backup API."""
    background_tasks.add_task(_do_backup)
    return {"status": "started", "message": "Database backup queued as background task"}


async def _do_backup() -> None:
    from app.config import get_settings

    settings = get_settings()
    src_path = settings.database_path
    if not os.path.exists(src_path):
        log.error("Backup: source database not found at %s", src_path)
        return

    backup_dir = os.path.join(os.path.dirname(src_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dst_path = os.path.join(backup_dir, f"timelapse_{ts}.db")

    try:
        src_conn = sqlite3.connect(src_path)
        dst_conn = sqlite3.connect(dst_path)
        try:
            src_conn.backup(dst_conn)
            log.info("Database backed up to %s", dst_path)
        finally:
            dst_conn.close()
            src_conn.close()

        # Prune old backups — keep last 7
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith("timelapse_") and f.endswith(".db")]
        )
        for old in backups[:-7]:
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(backup_dir, old))
    except Exception as exc:
        log.error("Database backup failed: %s", exc)
