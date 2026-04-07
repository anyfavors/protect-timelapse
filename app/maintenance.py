"""
Maintenance worker — daily cron at 02:00 local time.
Handles frame retention pruning, render rolling-window pruning,
and auto-render scheduling.
"""

import contextlib
import logging
import os
import shutil as _shutil
from datetime import UTC, datetime, timedelta

from apscheduler.triggers.cron import CronTrigger

from app.database import get_connection

log = logging.getLogger("app.maintenance")


def register_maintenance_job(scheduler) -> None:  # type: ignore[no-untyped-def]
    """Register the daily maintenance cron in the given APScheduler instance.

    The hour/minute are read from the settings table so users can configure them
    without restarting. Call this again with replace_existing=True after a settings change.
    """
    hour = 2
    minute = 0
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT maintenance_hour, maintenance_minute FROM settings WHERE id = 1"
            ).fetchone()
        if row:
            if row["maintenance_hour"] is not None:
                hour = int(row["maintenance_hour"])
            if row["maintenance_minute"] is not None:
                minute = int(row["maintenance_minute"])
    except Exception:
        pass

    scheduler.add_job(
        run_maintenance,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="maintenance_daily",
        replace_existing=True,
        max_instances=1,
    )
    log.info("Maintenance job registered (daily at %02d:%02d)", hour, minute)


async def run_maintenance() -> None:
    log.info("Maintenance run started")
    await _prune_old_frames()
    await _prune_old_renders()
    await _recover_zombie_renders()
    await _recover_stalled_renders()
    await _reconcile_frame_counts()
    await _reconcile_project_status()
    await _schedule_auto_renders()
    await _backup_database()
    await _maybe_vacuum_database()
    log.info("Maintenance run complete")


# -------------------------------------------------------------------------
# Frame retention pruning
# -------------------------------------------------------------------------


async def _prune_old_frames() -> None:
    with get_connection() as conn:
        projects = conn.execute(
            "SELECT id, name, retention_days FROM projects WHERE retention_days > 0"
        ).fetchall()

    for project in projects:
        project_id = project["id"]
        cutoff = datetime.now(UTC) - timedelta(days=project["retention_days"])
        cutoff_iso = cutoff.isoformat()

        with get_connection() as conn:
            old_frames = conn.execute(
                "SELECT id, file_path, thumbnail_path FROM frames WHERE project_id = ? AND captured_at < ?",
                (project_id, cutoff_iso),
            ).fetchall()

        if old_frames:
            # Delete DB rows first, then files — prevents a new frame with the same
            # path from being orphaned if it arrives between fetch and disk delete (#10)
            frame_ids = [f["id"] for f in old_frames]
            placeholders = ",".join("?" * len(frame_ids))
            with get_connection() as conn:
                conn.execute(f"DELETE FROM frames WHERE id IN ({placeholders})", frame_ids)
                count_row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM frames WHERE project_id = ?", (project_id,)
                ).fetchone()
                conn.execute(
                    "UPDATE projects SET frame_count = ? WHERE id = ?",
                    (count_row["cnt"], project_id),
                )
                conn.commit()

            deleted = 0
            for frame in old_frames:
                with contextlib.suppress(FileNotFoundError):
                    if frame["file_path"]:
                        os.remove(frame["file_path"])
                with contextlib.suppress(FileNotFoundError):
                    if frame["thumbnail_path"]:
                        os.remove(frame["thumbnail_path"])
                deleted += 1

            log.info(
                "Project %d: pruned %d frames older than %d days",
                project_id,
                deleted,
                project["retention_days"],
            )


# -------------------------------------------------------------------------
# Auto-render rolling window pruning
# -------------------------------------------------------------------------


async def _prune_old_renders() -> None:
    """Keep at most 7 daily, 4 weekly, 3 monthly auto-renders per project."""
    query = """
    WITH RankedRenders AS (
        SELECT id, output_path, render_type,
               ROW_NUMBER() OVER (
                   PARTITION BY project_id, render_type
                   ORDER BY created_at DESC
               ) AS rn
        FROM renders
        WHERE render_type IN ('auto_daily', 'auto_weekly', 'auto_monthly')
        AND status = 'done'
    )
    SELECT id, output_path FROM RankedRenders
    WHERE (render_type = 'auto_daily'   AND rn > 7)
       OR (render_type = 'auto_weekly'  AND rn > 4)
       OR (render_type = 'auto_monthly' AND rn > 3)
    """
    with get_connection() as conn:
        stale = conn.execute(query).fetchall()

    for render in stale:
        # Delete DB row before file — prevents inconsistency if process crashes mid-cleanup (#23)
        with get_connection() as conn:
            conn.execute("DELETE FROM renders WHERE id = ?", (render["id"],))
            conn.commit()
        with contextlib.suppress(FileNotFoundError):
            if render["output_path"]:
                os.remove(render["output_path"])

    if stale:
        log.info("Pruned %d stale auto-renders", len(stale))


# -------------------------------------------------------------------------
# Zombie render recovery
# -------------------------------------------------------------------------


async def _recover_zombie_renders() -> None:
    """Detect renders stuck in 'rendering' for >2 hours and mark them failed."""
    cutoff = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with get_connection() as conn:
        zombies = conn.execute(
            "SELECT id, output_path FROM renders WHERE status = 'rendering' AND created_at < ?",
            (cutoff,),
        ).fetchall()

    for render in zombies:
        # Clean up partial output file
        if render["output_path"]:
            with contextlib.suppress(FileNotFoundError):
                os.remove(render["output_path"])
        with get_connection() as conn:
            conn.execute(
                "UPDATE renders SET status = 'error', error_msg = 'Recovered: stuck in rendering for >2h' WHERE id = ?",
                (render["id"],),
            )
            conn.commit()
        log.warning("Recovered zombie render id=%d", render["id"])

    if zombies:
        log.info("Recovered %d zombie render(s)", len(zombies))


# -------------------------------------------------------------------------
# Eventual consistency reconciliation
# -------------------------------------------------------------------------


async def _reconcile_frame_counts() -> None:
    """Fix drifted frame_count values by comparing with actual COUNT(*)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.frame_count, COUNT(f.id) AS actual_count
            FROM projects p
            LEFT JOIN frames f ON f.project_id = p.id
            GROUP BY p.id
            HAVING p.frame_count != COUNT(f.id)
            """
        ).fetchall()

    for row in rows:
        with get_connection() as conn:
            conn.execute(
                "UPDATE projects SET frame_count = ? WHERE id = ?",
                (row["actual_count"], row["id"]),
            )
            conn.commit()
        log.info(
            "Reconciled frame_count for project %d: %d → %d",
            row["id"],
            row["frame_count"],
            row["actual_count"],
        )


async def _reconcile_project_status() -> None:
    """Fix stale project statuses that indicate a crashed worker."""
    # Projects stuck in 'extracting' for >4 hours → error
    cutoff = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    with get_connection() as conn:
        stale = conn.execute(
            "SELECT id, name FROM projects WHERE status = 'extracting' AND created_at < ?",
            (cutoff,),
        ).fetchall()

    for project in stale:
        with get_connection() as conn:
            conn.execute(
                "UPDATE projects SET status = 'error' WHERE id = ? AND status = 'extracting'",
                (project["id"],),
            )
            conn.commit()
        log.warning(
            "Reconciled stale extraction: project %d '%s' → error",
            project["id"],
            project["name"],
        )


# -------------------------------------------------------------------------
# Auto-render scheduling
# -------------------------------------------------------------------------


async def _schedule_auto_renders() -> None:
    now = datetime.now(UTC)
    yesterday_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_end = yesterday_start + timedelta(days=1)

    with get_connection() as conn:
        projects = conn.execute("SELECT * FROM projects WHERE status != 'error'").fetchall()
        settings_row = conn.execute(
            "SELECT default_framerate FROM settings WHERE id = 1"
        ).fetchone()

    framerate = settings_row["default_framerate"] if settings_row else 30

    for project in projects:
        project_id = project["id"]

        # Daily auto-render
        if project["auto_render_daily"]:
            await _maybe_insert_render(
                project_id, framerate, "auto_daily", yesterday_start, yesterday_end
            )

        # Weekly auto-render (trigger on Mondays)
        if project["auto_render_weekly"] and now.weekday() == 0:
            week_start = yesterday_start - timedelta(days=6)
            await _maybe_insert_render(
                project_id, framerate, "auto_weekly", week_start, yesterday_end
            )

        # Monthly auto-render (trigger on 1st of month)
        if project["auto_render_monthly"] and now.day == 1:
            month_start = (now - timedelta(days=now.day)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            await _maybe_insert_render(
                project_id, framerate, "auto_monthly", month_start, yesterday_end
            )


async def _recover_stalled_renders() -> None:
    """Reset renders stuck in 'stalled' status back to pending so the worker retries."""
    with get_connection() as conn:
        stalled = conn.execute(
            "SELECT id, output_path FROM renders WHERE status = 'stalled'"
        ).fetchall()

    for render in stalled:
        if render["output_path"]:
            with contextlib.suppress(FileNotFoundError):
                os.remove(render["output_path"])
        with get_connection() as conn:
            conn.execute(
                "UPDATE renders SET status = 'pending', progress_pct = 0 WHERE id = ?",
                (render["id"],),
            )
            conn.commit()
        log.warning("Reset stalled render id=%d to pending", render["id"])


async def _backup_database() -> None:
    """Create a daily SQLite backup alongside the main DB (B5)."""
    from app.config import get_settings

    settings = get_settings()
    src = settings.database_path
    if not os.path.exists(src):
        return
    backup_path = src + ".backup"
    try:
        _shutil.copy2(src, backup_path)
        log.info("Database backed up to %s", backup_path)
    except OSError as exc:
        log.error("Database backup failed: %s", exc)


async def _maybe_vacuum_database() -> None:
    """Run VACUUM on the first day of each month to reclaim WAL space."""
    if datetime.now(UTC).day != 1:
        return
    try:
        import sqlite3

        from app.config import get_settings

        settings = get_settings()
        # VACUUM cannot run inside a transaction — use a direct autocommit connection
        conn = sqlite3.connect(settings.database_path, isolation_level=None)
        try:
            conn.execute("VACUUM")
            log.info("Database VACUUM complete")
        finally:
            conn.close()
    except Exception as exc:
        log.error("Database VACUUM failed: %s", exc)


async def _maybe_insert_render(
    project_id: int,
    framerate: int,
    render_type: str,
    range_start: datetime,
    range_end: datetime,
) -> None:
    """Insert a render job only if frames exist and no render already exists for this window."""
    start_iso = range_start.isoformat()
    end_iso = range_end.isoformat()

    with get_connection() as conn:
        # Check frames exist in the window
        frame_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM frames WHERE project_id = ? AND captured_at BETWEEN ? AND ? AND is_dark = 0",
            (project_id, start_iso, end_iso),
        ).fetchone()["cnt"]

        if frame_count == 0:
            return

        # Check no render already exists for this window
        existing = conn.execute(
            """
            SELECT id FROM renders
            WHERE project_id = ? AND render_type = ?
            AND range_start = ? AND status IN ('pending','rendering','done')
            """,
            (project_id, render_type, start_iso),
        ).fetchone()

        if existing:
            return

        conn.execute(
            """
            INSERT INTO renders (project_id, framerate, resolution, render_type, range_start, range_end)
            VALUES (?, ?, '1920x1080', ?, ?, ?)
            """,
            (project_id, framerate, render_type, start_iso, end_iso),
        )
        conn.commit()

    log.info(
        "Auto-render scheduled: project=%d type=%s start=%s", project_id, render_type, start_iso
    )
