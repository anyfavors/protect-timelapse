"""Render queue routes."""

import contextlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.database import get_connection
from app.limiter import limiter
from app.render import cancel_active_render, estimate_render, pause_active_render

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
    priority: int = Field(default=5, ge=1, le=10, description="Queue priority 1 (low) to 10 (high)")


def _row_to_dict(row: Any) -> dict:
    return dict(row)


def _get_render_or_404(render_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM renders WHERE id = ?", (render_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Render {render_id} not found")
    return _row_to_dict(row)


@router.post("/renders", status_code=201)
@limiter.limit("30/minute")
def create_render(request: Request, payload: RenderCreate) -> dict:
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
                quality, flicker_reduction, frame_blend, stabilize, color_grade, priority
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                payload.priority,
            ),
        )
        conn.commit()

    if cur.lastrowid is None:
        raise HTTPException(status_code=500, detail="Failed to create render: no row ID returned")
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


def _enrich_render(r: dict) -> dict:
    """Add ETA field to a render dict if it is currently rendering."""
    if r.get("status") == "rendering" and r.get("started_at") and r.get("progress_pct", 0) > 0:
        try:
            started = datetime.fromisoformat(r["started_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            elapsed = (datetime.now(UTC) - started).total_seconds()
            pct = r["progress_pct"]
            r["eta_seconds"] = int(elapsed / (pct / 100) * (1 - pct / 100))
        except Exception:
            r["eta_seconds"] = None
    else:
        r["eta_seconds"] = None
    return r


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
    return [_enrich_render(_row_to_dict(r)) for r in rows]


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


@router.put("/renders/{render_id}/priority", status_code=200)
def set_render_priority(render_id: int, priority: int = Query(ge=1, le=10)) -> dict:
    """Update render queue priority 1 (low) to 10 (high) (F5)."""
    render = _get_render_or_404(render_id)
    if render["status"] not in ("pending",):
        raise HTTPException(status_code=409, detail="Can only reprioritize pending renders")
    with get_connection() as conn:
        conn.execute("UPDATE renders SET priority = ? WHERE id = ?", (priority, render_id))
        conn.commit()
    return {"render_id": render_id, "priority": priority}


@router.post("/renders/{render_id}/cancel", status_code=200)
async def cancel_render(render_id: int) -> dict:
    """Cancel a pending or in-progress render (F1)."""
    render = _get_render_or_404(render_id)
    if render["status"] not in ("pending", "rendering"):
        raise HTTPException(
            status_code=409,
            detail=f"Render {render_id} is not cancellable (status={render['status']})",
        )

    # Kill ffmpeg if this render is currently running
    killed = await cancel_active_render(render_id)

    with get_connection() as conn:
        conn.execute(
            "UPDATE renders SET status='error', error_msg='Cancelled by user' WHERE id = ? AND status IN ('pending','rendering')",
            (render_id,),
        )
        conn.commit()

    log.info("Render id=%d cancelled (ffmpeg_killed=%s)", render_id, killed)
    return {"render_id": render_id, "cancelled": True, "ffmpeg_killed": killed}


@router.post("/renders/{render_id}/pause", status_code=200)
async def pause_render(render_id: int) -> dict:
    """Pause a pending or in-progress render (sets status to 'paused')."""
    render = _get_render_or_404(render_id)
    if render["status"] not in ("pending", "rendering"):
        raise HTTPException(
            status_code=409,
            detail=f"Render {render_id} cannot be paused (status={render['status']})",
        )

    killed = False
    if render["status"] == "rendering":
        killed = await pause_active_render(render_id)

    with get_connection() as conn:
        conn.execute(
            "UPDATE renders SET status = 'paused' WHERE id = ? AND status IN ('pending', 'rendering')",
            (render_id,),
        )
        conn.commit()

    log.info("Render id=%d paused (ffmpeg_killed=%s)", render_id, killed)
    return {"render_id": render_id, "paused": True, "ffmpeg_killed": killed}


@router.post("/renders/{render_id}/resume", status_code=200)
def resume_render(render_id: int) -> dict:
    """Resume a paused render by setting its status back to 'pending'."""
    render = _get_render_or_404(render_id)
    if render["status"] != "paused":
        raise HTTPException(
            status_code=409,
            detail=f"Render {render_id} is not paused (status={render['status']})",
        )

    with get_connection() as conn:
        conn.execute(
            "UPDATE renders SET status = 'pending', progress_pct = 0 WHERE id = ?",
            (render_id,),
        )
        conn.commit()

    log.info("Render id=%d resumed", render_id)
    return {"render_id": render_id, "resumed": True}


@router.get("/renders/{render_id_a}/compare/{render_id_b}")
def compare_renders(render_id_a: int, render_id_b: int) -> dict:
    """Return metadata diff between two renders for side-by-side comparison (F9)."""
    a = _get_render_or_404(render_id_a)
    b = _get_render_or_404(render_id_b)

    def _summary(r: dict) -> dict:
        return {
            "id": r["id"],
            "label": r.get("label"),
            "status": r["status"],
            "resolution": r.get("resolution"),
            "framerate": r.get("framerate"),
            "quality": r.get("quality"),
            "flicker_reduction": r.get("flicker_reduction"),
            "color_grade": r.get("color_grade"),
            "frame_blend": bool(r.get("frame_blend")),
            "stabilize": bool(r.get("stabilize")),
            "file_size_bytes": r.get("file_size"),
            "estimated_duration_seconds": r.get("estimated_duration_seconds"),
            "completed_at": r.get("completed_at"),
            "priority": r.get("priority"),
        }

    return {"a": _summary(a), "b": _summary(b)}


@router.delete("/renders/{render_id}", status_code=204)
def delete_render(render_id: int) -> None:
    render = _get_render_or_404(render_id)
    if render["output_path"]:
        with contextlib.suppress(FileNotFoundError):
            os.remove(render["output_path"])
    with get_connection() as conn:
        conn.execute("DELETE FROM renders WHERE id = ?", (render_id,))
        conn.commit()
