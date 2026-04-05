"""Render queue routes."""

import contextlib
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.database import get_connection
from app.render import estimate_render

router = APIRouter(prefix="/api", tags=["renders"])
log = logging.getLogger("app.routes.renders")


class RenderCreate(BaseModel):
    project_id: int
    framerate: int = Field(default=30, ge=1, le=120)
    resolution: str = Field(default="1920x1080", pattern=r"^\d{1,5}x\d{1,5}$")
    render_type: str = Field(
        default="manual", pattern="^(manual|range|auto_daily|auto_weekly|auto_monthly|preview)$"
    )
    label: str | None = None
    range_start: str | None = None
    range_end: str | None = None
    quality: str = Field(default="standard", pattern="^(draft|standard|high|archive)$")
    flicker_reduction: str = Field(default="standard", pattern="^(off|standard|strong|holy_grail)$")
    frame_blend: bool = False
    stabilize: bool = False
    color_grade: str = Field(default="none", pattern="^(none|neutral|warm|cool|cinematic)$")


def _row_to_dict(row: Any) -> dict:
    return dict(row)


def _get_render_or_404(render_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM renders WHERE id = ?", (render_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Render {render_id} not found")
    return _row_to_dict(row)


@router.post("/renders", status_code=201)
def create_render(payload: RenderCreate) -> dict:
    # Validate project exists
    with get_connection() as conn:
        proj = conn.execute(
            "SELECT id FROM projects WHERE id = ?", (payload.project_id,)
        ).fetchone()
    if proj is None:
        raise HTTPException(status_code=404, detail=f"Project {payload.project_id} not found")

    # Check for duplicate pending render of same type (non-range)
    if payload.render_type not in ("manual", "range"):
        with get_connection() as conn:
            dup = conn.execute(
                "SELECT id FROM renders WHERE project_id=? AND render_type=? AND status IN ('pending','rendering')",
                (payload.project_id, payload.render_type),
            ).fetchone()
        if dup:
            raise HTTPException(
                status_code=409,
                detail=f"A {payload.render_type} render is already pending for this project",
            )

    estimate = estimate_render(payload.project_id, payload.framerate, payload.render_type)

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO renders (
                project_id, framerate, resolution, render_type, label,
                range_start, range_end,
                estimated_duration_seconds, estimated_file_size_bytes,
                quality, flicker_reduction, frame_blend, stabilize, color_grade
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.project_id,
                payload.framerate,
                payload.resolution,
                payload.render_type,
                payload.label,
                payload.range_start,
                payload.range_end,
                estimate["estimated_duration_seconds"],
                estimate["estimated_file_size_bytes"],
                payload.quality,
                payload.flicker_reduction,
                int(payload.frame_blend),
                int(payload.stabilize),
                payload.color_grade,
            ),
        )
        conn.commit()

    assert cur.lastrowid is not None
    result = _get_render_or_404(cur.lastrowid)
    result.update(
        {
            "estimated_render_time_seconds": estimate["estimated_render_time_seconds"],
            "frame_count": estimate["frame_count"],
        }
    )
    return result


@router.get("/renders/{render_id}/status")
def render_status(render_id: int) -> dict:
    render = _get_render_or_404(render_id)
    return {
        "status": render["status"],
        "progress_pct": render["progress_pct"],
        "error_msg": render["error_msg"],
    }


@router.get("/renders")
def list_all_renders() -> list[dict]:
    """Global render queue — all projects, newest first, with project name joined."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*, p.name AS project_name
            FROM renders r
            LEFT JOIN projects p ON p.id = r.project_id
            ORDER BY r.created_at DESC
            LIMIT 200
            """,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get("/projects/{project_id}/renders")
def list_renders(project_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM renders WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get("/renders/{render_id}/download")
def download_render(render_id: int) -> FileResponse:
    render = _get_render_or_404(render_id)
    if render["status"] != "done" or not render["output_path"]:
        raise HTTPException(status_code=404, detail="Render output not available")
    if not os.path.exists(render["output_path"]):
        raise HTTPException(status_code=404, detail="Render file not found on disk")
    filename = f"render_{render_id}.mp4"
    return FileResponse(
        render["output_path"],
        media_type="video/mp4",
        filename=filename,
    )


@router.delete("/renders/{render_id}", status_code=204)
def delete_render(render_id: int) -> None:
    render = _get_render_or_404(render_id)
    if render["output_path"]:
        with contextlib.suppress(FileNotFoundError):
            os.remove(render["output_path"])
    with get_connection() as conn:
        conn.execute("DELETE FROM renders WHERE id = ?", (render_id,))
        conn.commit()
