"""
Project CRUD routes.
Also responsible for mutating APScheduler when projects are created/updated/deleted.
"""

import logging
import shutil
import unittest.mock as _mock
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.capture import (
    add_project_job,
    pause_project_job,
    remove_project_job,
    reschedule_project_job,
    resume_project_job,
    run_historical_extraction,
)
from app.config import get_settings
from app.database import get_connection

router = APIRouter(prefix="/api", tags=["projects"])
log = logging.getLogger("app.routes.projects")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str
    camera_id: str = Field(min_length=1)
    project_type: str = Field(pattern="^(live|historical)$")
    interval_seconds: int = Field(ge=1)
    width: int | None = None
    height: int | None = None
    max_frames: int | None = None
    # Historical only
    start_date: str | None = None
    end_date: str | None = None
    # Capture mode
    capture_mode: str = Field(
        default="continuous", pattern="^(continuous|daylight_only|schedule|solar_noon)$"
    )
    use_luminance_check: bool = False
    luminance_threshold: int = Field(default=15, ge=0, le=255)
    # Schedule mode
    schedule_start_time: str | None = None
    schedule_end_time: str | None = None
    schedule_days: str | None = None
    # Auto-render & retention
    auto_render_daily: bool = False
    auto_render_weekly: bool = False
    auto_render_monthly: bool = False
    retention_days: int = 0
    # Motion filter
    use_motion_filter: bool = False
    motion_threshold: int = Field(default=5, ge=1, le=100)
    # Solar noon mode
    solar_noon_window_minutes: int = Field(default=30, ge=5, le=120)
    # Template linkage
    template_id: int | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    interval_seconds: int | None = Field(default=None, ge=1)
    capture_mode: str | None = Field(
        default=None, pattern="^(continuous|daylight_only|schedule|solar_noon)$"
    )
    use_luminance_check: bool | None = None
    luminance_threshold: int | None = Field(default=None, ge=0, le=255)
    schedule_start_time: str | None = None
    schedule_end_time: str | None = None
    schedule_days: str | None = None
    auto_render_daily: bool | None = None
    auto_render_weekly: bool | None = None
    auto_render_monthly: bool | None = None
    retention_days: int | None = None
    max_frames: int | None = None
    use_motion_filter: bool | None = None
    motion_threshold: int | None = Field(default=None, ge=1, le=100)
    status: str | None = Field(
        default=None,
        pattern="^(active|paused|paused_error|completed|error|extracting)$",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row: Any) -> dict:
    return dict(row)


def _get_project_or_404(project_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/projects")
def list_projects() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   f.id             AS last_frame_id,
                   f.captured_at    AS last_captured_at
            FROM projects p
            LEFT JOIN frames f ON f.id = (
                SELECT id FROM frames
                WHERE project_id = p.id
                ORDER BY captured_at DESC
                LIMIT 1
            )
            ORDER BY p.created_at DESC
            """,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/projects", status_code=201)
async def create_project(payload: ProjectCreate) -> dict:
    settings = get_settings()

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (
                name, camera_id, project_type, interval_seconds,
                width, height, max_frames,
                start_date, end_date,
                capture_mode, use_luminance_check, luminance_threshold,
                schedule_start_time, schedule_end_time, schedule_days,
                auto_render_daily, auto_render_weekly, auto_render_monthly,
                retention_days, template_id,
                use_motion_filter, motion_threshold,
                solar_noon_window_minutes,
                status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.name,
                payload.camera_id,
                payload.project_type,
                payload.interval_seconds,
                payload.width,
                payload.height,
                payload.max_frames,
                payload.start_date,
                payload.end_date,
                payload.capture_mode,
                int(payload.use_luminance_check),
                payload.luminance_threshold,
                payload.schedule_start_time,
                payload.schedule_end_time,
                payload.schedule_days,
                int(payload.auto_render_daily),
                int(payload.auto_render_weekly),
                int(payload.auto_render_monthly),
                payload.retention_days,
                payload.template_id,
                int(payload.use_motion_filter),
                payload.motion_threshold,
                payload.solar_noon_window_minutes,
                "active" if payload.project_type == "live" else "extracting",
            ),
        )
        if cur.lastrowid is None:
            raise HTTPException(
                status_code=500, detail="Failed to create project: no row ID returned"
            )
        project_id: int = cur.lastrowid
        conn.commit()

    # Create frame / thumbnail directories on disk
    import asyncio
    import os

    for base in (settings.frames_path, settings.thumbnails_path):
        os.makedirs(f"{base}/{project_id}", exist_ok=True)

    # Register APScheduler job for live projects
    if payload.project_type == "live":
        await add_project_job(project_id, payload.interval_seconds, payload.capture_mode)

    # Kick off historical extraction as a background task
    if payload.project_type == "historical":
        asyncio.create_task(run_historical_extraction(project_id))

    return _get_project_or_404(project_id)


@router.get("/projects/{project_id}")
def get_project(project_id: int) -> dict:
    return _get_project_or_404(project_id)


@router.put("/projects/{project_id}")
async def update_project(project_id: int, payload: ProjectUpdate) -> dict:
    _get_project_or_404(project_id)  # raises 404 if missing

    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        return _get_project_or_404(project_id)

    # Explicit allowlist — prevents SQL injection even if Pydantic is bypassed (#1)
    _ALLOWED_UPDATE_FIELDS = {
        "name",
        "interval_seconds",
        "capture_mode",
        "use_luminance_check",
        "luminance_threshold",
        "schedule_start_time",
        "schedule_end_time",
        "schedule_days",
        "auto_render_daily",
        "auto_render_weekly",
        "auto_render_monthly",
        "retention_days",
        "max_frames",
        "use_motion_filter",
        "motion_threshold",
        "status",
        "solar_noon_window_minutes",
    }
    updates = {k: v for k, v in updates.items() if k in _ALLOWED_UPDATE_FIELDS}
    if not updates:
        return _get_project_or_404(project_id)

    # Convert booleans to ints for SQLite
    for bool_field in (
        "use_luminance_check",
        "auto_render_daily",
        "auto_render_weekly",
        "auto_render_monthly",
        "use_motion_filter",
    ):
        if bool_field in updates:
            updates[bool_field] = int(updates[bool_field])

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [project_id]

    with get_connection() as conn:
        conn.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
        conn.commit()

    # Reschedule if interval changed
    if "interval_seconds" in updates:
        await reschedule_project_job(project_id, updates["interval_seconds"])

    # Pause or resume scheduler job based on status change
    if "status" in updates:
        if updates["status"] == "paused":
            await pause_project_job(project_id)
        elif updates["status"] == "active":
            project = _get_project_or_404(project_id)
            await resume_project_job(project_id, project["interval_seconds"])

    return _get_project_or_404(project_id)


@router.post("/projects/{project_id}/clone", status_code=201)
async def clone_project(
    project_id: int,
    copy_frames_days: int | None = None,
) -> dict:
    """Clone a project config. Optionally copy the last N days of frames (F10).

    copy_frames_days: if set, copies frame DB records (and files) from the last N days.
    """
    import os

    source = _get_project_or_404(project_id)
    settings = get_settings()

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (
                name, camera_id, project_type, interval_seconds,
                width, height, max_frames,
                capture_mode, use_luminance_check, luminance_threshold,
                schedule_start_time, schedule_end_time, schedule_days,
                auto_render_daily, auto_render_weekly, auto_render_monthly,
                retention_days, template_id,
                use_motion_filter, motion_threshold, solar_noon_window_minutes,
                status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source["name"] + " (copy)",
                source["camera_id"],
                source["project_type"],
                source["interval_seconds"],
                source["width"],
                source["height"],
                source["max_frames"],
                source["capture_mode"],
                source["use_luminance_check"],
                source["luminance_threshold"],
                source["schedule_start_time"],
                source["schedule_end_time"],
                source["schedule_days"],
                source["auto_render_daily"],
                source["auto_render_weekly"],
                source["auto_render_monthly"],
                source["retention_days"],
                source["template_id"],
                source["use_motion_filter"],
                source["motion_threshold"],
                source["solar_noon_window_minutes"],
                "active" if source["project_type"] == "live" else "extracting",
            ),
        )
        if cur.lastrowid is None:
            raise HTTPException(
                status_code=500, detail="Failed to clone project: no row ID returned"
            )
        new_id: int = cur.lastrowid
        conn.commit()

    for base in (settings.frames_path, settings.thumbnails_path):
        os.makedirs(f"{base}/{new_id}", exist_ok=True)

    if source["project_type"] == "live":
        await add_project_job(new_id, source["interval_seconds"])

    # Optionally copy last N days of frames (F10)
    if copy_frames_days and copy_frames_days > 0:
        import contextlib
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=copy_frames_days)).isoformat()
        src_frame_dir = f"{settings.frames_path}/{project_id}"
        dst_frame_dir = f"{settings.frames_path}/{new_id}"
        src_thumb_dir = f"{settings.thumbnails_path}/{project_id}"
        dst_thumb_dir = f"{settings.thumbnails_path}/{new_id}"

        with get_connection() as conn:
            frame_rows = conn.execute(
                "SELECT * FROM frames WHERE project_id = ? AND captured_at >= ? ORDER BY captured_at ASC",
                (project_id, cutoff),
            ).fetchall()

        copied = 0
        for frame in frame_rows:
            new_file_path = None
            new_thumb_path = None

            if frame["file_path"]:
                src = frame["file_path"]
                dst = src.replace(src_frame_dir, dst_frame_dir, 1)
                try:
                    import shutil as _shutil

                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    _shutil.copy2(src, dst)
                    new_file_path = dst
                except OSError:
                    continue

            if frame["thumbnail_path"]:
                tsrc = frame["thumbnail_path"]
                tdst = tsrc.replace(src_thumb_dir, dst_thumb_dir, 1)
                with contextlib.suppress(OSError):
                    import shutil as _shutil

                    os.makedirs(os.path.dirname(tdst), exist_ok=True)
                    _shutil.copy2(tsrc, tdst)
                    new_thumb_path = tdst

            with get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO frames (project_id, captured_at, file_path, thumbnail_path,
                        file_size, is_dark, bookmark_note, sharpness_score, is_blurry)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id,
                        frame["captured_at"],
                        new_file_path,
                        new_thumb_path,
                        frame["file_size"],
                        frame["is_dark"],
                        frame["bookmark_note"],
                        frame["sharpness_score"],
                        frame["is_blurry"],
                    ),
                )
                conn.commit()
            copied += 1

        # Sync frame_count
        with get_connection() as conn:
            conn.execute("UPDATE projects SET frame_count = ? WHERE id = ?", (copied, new_id))
            conn.commit()

    return _get_project_or_404(new_id)


@router.get("/projects/{project_id}/schedule-test")
def schedule_test(
    project_id: int,
    timestamp: str | None = None,
) -> dict:
    """Test whether a project would capture at a given ISO timestamp (or now). (F8)"""
    from app.capture import _check_capture_mode  # type: ignore[attr-defined]

    project = _get_project_or_404(project_id)

    if timestamp:
        try:
            test_time = datetime.fromisoformat(timestamp)
            if test_time.tzinfo is None:
                test_time = test_time.replace(tzinfo=UTC)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid timestamp: {timestamp!r}"
            ) from exc
    else:
        test_time = datetime.now(UTC)

    # _check_capture_mode uses datetime.now() internally — patch it temporarily
    with _mock.patch("app.capture.datetime") as mock_dt:
        mock_dt.now.return_value = test_time
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        error_msg: str | None = None
        try:
            would_capture: bool | None = _check_capture_mode(project)
        except Exception as exc:
            would_capture = None
            error_msg = str(exc)

    return {
        "project_id": project_id,
        "timestamp": test_time.isoformat(),
        "capture_mode": project.get("capture_mode"),
        "would_capture": would_capture,
        "error": error_msg,
    }


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: int) -> None:
    import contextlib

    _get_project_or_404(project_id)
    settings = get_settings()

    # Remove files BEFORE SQL delete so a failed DB delete is retryable
    for base in (settings.frames_path, settings.thumbnails_path, settings.renders_path):
        path = f"{base}/{project_id}"
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(path)

    # Remove APScheduler job
    await remove_project_job(project_id)

    with get_connection() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
