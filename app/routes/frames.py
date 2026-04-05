"""Frame listing, serving, bookmarks, stats, and export routes."""

import io
import logging
import os
import zipfile
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.database import get_connection

router = APIRouter(prefix="/api", tags=["frames"])
log = logging.getLogger("app.routes.frames")


def _get_project_or_404(project_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return dict(row)


def _get_frame_or_404(project_id: int, frame_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM frames WHERE id = ? AND project_id = ?", (frame_id, project_id)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Frame {frame_id} not found")
    return dict(row)


def _row_to_dict(row: Any) -> dict:
    return dict(row)


# -------------------------------------------------------------------------
# Frame listing
# -------------------------------------------------------------------------


@router.get("/projects/{project_id}/frames")
def list_frames(
    project_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    fields: str | None = Query(
        default=None, description="Comma-separated field names (e.g. id,captured_at)"
    ),
) -> list[dict]:
    _get_project_or_404(project_id)

    # Build SELECT — allow field projection for lightweight scrubber index
    allowed_fields = {
        "id",
        "project_id",
        "captured_at",
        "file_path",
        "thumbnail_path",
        "file_size",
        "is_dark",
        "bookmark_note",
    }
    if fields:
        requested = {f.strip() for f in fields.split(",")}
        select_cols = ", ".join(requested & allowed_fields) or "*"
    else:
        select_cols = "*"

    direction = "ASC" if order == "asc" else "DESC"
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {select_cols} FROM frames WHERE project_id = ? ORDER BY captured_at {direction} LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# -------------------------------------------------------------------------
# Image serving
# -------------------------------------------------------------------------


@router.get("/projects/{project_id}/frames/{frame_id}/thumbnail")
def serve_thumbnail(project_id: int, frame_id: int) -> Response:
    frame = _get_frame_or_404(project_id, frame_id)
    path = frame.get("thumbnail_path") or frame["file_path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Thumbnail file not found on disk")
    with open(path, "rb") as f:
        data = f.read()
    return Response(content=data, media_type="image/jpeg")


@router.get("/projects/{project_id}/frames/{frame_id}/full")
def serve_full(project_id: int, frame_id: int) -> Response:
    frame = _get_frame_or_404(project_id, frame_id)
    path = frame["file_path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Frame file not found on disk")
    with open(path, "rb") as f:
        data = f.read()
    return Response(content=data, media_type="image/jpeg")


# -------------------------------------------------------------------------
# Bookmarks
# -------------------------------------------------------------------------


class BookmarkUpdate(BaseModel):
    note: str | None = None


@router.put("/projects/{project_id}/frames/{frame_id}/bookmark")
def set_bookmark(project_id: int, frame_id: int, payload: BookmarkUpdate) -> dict:
    frame = _get_frame_or_404(project_id, frame_id)
    with get_connection() as conn:
        conn.execute(
            "UPDATE frames SET bookmark_note = ? WHERE id = ?",
            (payload.note, frame["id"]),
        )
        conn.commit()
    return _get_frame_or_404(project_id, frame_id)


@router.get("/projects/{project_id}/frames/bookmarks")
def list_bookmarks(project_id: int) -> list[dict]:
    _get_project_or_404(project_id)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, captured_at, thumbnail_path, bookmark_note
            FROM frames
            WHERE project_id = ? AND bookmark_note IS NOT NULL
            ORDER BY captured_at ASC
            """,
            (project_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# -------------------------------------------------------------------------
# Dark frames gallery
# -------------------------------------------------------------------------


@router.get("/projects/{project_id}/frames/dark")
def list_dark_frames(project_id: int, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    _get_project_or_404(project_id)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, captured_at, thumbnail_path, file_size
            FROM frames
            WHERE project_id = ? AND is_dark = 1
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# -------------------------------------------------------------------------
# ZIP export
# -------------------------------------------------------------------------


@router.get("/projects/{project_id}/frames/export")
def export_frames(project_id: int) -> StreamingResponse:
    _get_project_or_404(project_id)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT file_path, captured_at FROM frames WHERE project_id = ? ORDER BY captured_at ASC",
            (project_id,),
        ).fetchall()

    def _generate():  # type: ignore[return]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for row in rows:
                path = row["file_path"]
                if os.path.exists(path):
                    arcname = os.path.basename(path)
                    zf.write(path, arcname)
        buf.seek(0)
        yield buf.read()

    return StreamingResponse(
        _generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=project_{project_id}_frames.zip"},
    )


# -------------------------------------------------------------------------
# Stats
# -------------------------------------------------------------------------


@router.get("/projects/{project_id}/stats/daily")
def daily_stats(project_id: int) -> list[dict]:
    """Returns daily frame counts for the capture heatmap."""
    _get_project_or_404(project_id)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DATE(captured_at) as date, COUNT(*) as count
            FROM frames
            WHERE project_id = ?
            GROUP BY DATE(captured_at)
            ORDER BY date ASC
            """,
            (project_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get("/projects/{project_id}/stats/timeline")
def timeline_stats(project_id: int) -> list[dict]:
    """Returns hourly captured/dark counts for the timeline view."""
    _get_project_or_404(project_id)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                STRFTIME('%Y-%m-%dT%H:00:00', captured_at) as hour,
                COUNT(*) as captured,
                SUM(is_dark) as dark
            FROM frames
            WHERE project_id = ?
            GROUP BY hour
            ORDER BY hour ASC
            """,
            (project_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
