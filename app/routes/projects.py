"""
Project CRUD routes.
Also responsible for mutating APScheduler when projects are created/updated/deleted.
"""

import logging
import shutil
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
    camera_id: str
    project_type: str = Field(pattern="^(live|historical)$")
    interval_seconds: int = Field(ge=1)
    width: int | None = None
    height: int | None = None
    max_frames: int | None = None
    # Historical only
    start_date: str | None = None
    end_date: str | None = None
    # Capture mode
    capture_mode: str = Field(default="continuous", pattern="^(continuous|daylight_only|schedule)$")
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
    # Template linkage
    template_id: int | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    interval_seconds: int | None = Field(default=None, ge=1)
    capture_mode: str | None = Field(default=None, pattern="^(continuous|daylight_only|schedule)$")
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
    status: str | None = Field(
        default=None,
        pattern="^(active|paused|paused_error|completed|error)$",
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
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
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
                status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                "active" if payload.project_type == "live" else "active",
            ),
        )
        project_id = cur.lastrowid
        conn.commit()

    # Create frame / thumbnail directories on disk
    import asyncio
    import os

    for base in (settings.frames_path, settings.thumbnails_path):
        os.makedirs(f"{base}/{project_id}", exist_ok=True)

    # Register APScheduler job for live projects
    if payload.project_type == "live":
        await add_project_job(project_id, payload.interval_seconds)

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

    # Convert booleans to ints for SQLite
    for bool_field in (
        "use_luminance_check",
        "auto_render_daily",
        "auto_render_weekly",
        "auto_render_monthly",
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
