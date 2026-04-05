"""Frame listing, serving, bookmarks, stats, and export routes."""

import asyncio
import io
import logging
import os
import shutil
import zipfile
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
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
    after_id: int | None = Query(
        default=None, description="Cursor-based pagination: return frames with id > after_id"
    ),
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

    if after_id is not None:
        # Cursor-based: avoids full-table scan on large projects
        op = ">" if direction == "ASC" else "<"
        with get_connection() as conn:
            rows = conn.execute(
                f"SELECT {select_cols} FROM frames WHERE project_id = ? AND id {op} ? ORDER BY id {direction} LIMIT ?",
                (project_id, after_id, limit),
            ).fetchall()
    else:
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
def serve_thumbnail(project_id: int, frame_id: int, request: Request) -> Response:
    frame = _get_frame_or_404(project_id, frame_id)
    path = frame.get("thumbnail_path") or frame["file_path"]
    # Avoid TOCTOU: skip existence check, handle FileNotFoundError on open (#20)
    try:
        etag = f'"{int(os.path.getmtime(path) * 1000)}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304)
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Thumbnail file not found on disk")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": etag,
        },
    )


@router.get("/projects/{project_id}/frames/{frame_id}/full")
def serve_full(project_id: int, frame_id: int) -> Response:
    frame = _get_frame_or_404(project_id, frame_id)
    path = frame["file_path"]
    # Avoid TOCTOU: skip existence check, handle FileNotFoundError on open (#20)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Frame file not found on disk")
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
# Frame deletion
# -------------------------------------------------------------------------


@router.delete("/projects/{project_id}/frames/{frame_id}", status_code=204)
def delete_frame(project_id: int, frame_id: int) -> None:
    import contextlib

    frame = _get_frame_or_404(project_id, frame_id)

    # Remove files from disk before DB row (safe to retry if DB fails)
    for path_key in ("file_path", "thumbnail_path"):
        path = frame.get(path_key)
        if path:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)

    with get_connection() as conn:
        conn.execute("DELETE FROM frames WHERE id = ?", (frame_id,))
        conn.execute(
            "UPDATE projects SET frame_count = MAX(0, frame_count - 1) WHERE id = ?",
            (project_id,),
        )
        conn.commit()


# -------------------------------------------------------------------------
# Blurry frames gallery
# -------------------------------------------------------------------------


@router.get("/projects/{project_id}/frames/blurry")
def list_blurry_frames(
    project_id: int, limit: int = Query(default=100, ge=1, le=500)
) -> list[dict]:
    _get_project_or_404(project_id)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, captured_at, thumbnail_path, file_size, sharpness_score
            FROM frames
            WHERE project_id = ? AND is_blurry = 1
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

    from collections.abc import Iterator

    def _generate() -> Iterator[bytes]:
        # Stream-write ZIP entries using ZipFile.open() to avoid buffering entire
        # files in RAM — fixes OOM crash on projects with thousands of frames (#4).
        # ZIP_STORED skips compression (JPEGs already compressed).
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for row in rows:
                path = row["file_path"]
                arcname = os.path.basename(path)
                try:
                    with zf.open(arcname, "w") as zentry, open(path, "rb") as src:
                        shutil.copyfileobj(src, zentry, length=65536)
                except (FileNotFoundError, OSError):
                    continue
                # Yield whatever Central Directory + local headers have accumulated
                chunk = buf.getvalue()
                if chunk:
                    yield chunk
                    buf.seek(0)
                    buf.truncate(0)
        tail = buf.getvalue()
        if tail:
            yield tail

    return StreamingResponse(
        _generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=project_{project_id}_frames.zip"},
    )


# -------------------------------------------------------------------------
# GIF export
# -------------------------------------------------------------------------

# In-memory GIF job registry: {project_id: {"status": ..., "path": ..., "error": ...}}
_gif_jobs: dict[int, dict] = {}


@router.post("/projects/{project_id}/gif", status_code=202)
async def start_gif_export(project_id: int, background_tasks: BackgroundTasks) -> dict:
    _get_project_or_404(project_id)
    _gif_jobs[project_id] = {"status": "pending", "path": None, "error": None}
    background_tasks.add_task(_run_gif_export, project_id)
    return {"status": "pending", "project_id": project_id}


@router.get("/projects/{project_id}/gif/status")
def gif_status(project_id: int) -> dict:
    _get_project_or_404(project_id)
    job = _gif_jobs.get(project_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No GIF job for this project")
    return {"project_id": project_id, **job}


@router.get("/projects/{project_id}/gif/download")
def download_gif(project_id: int) -> Response:
    _get_project_or_404(project_id)
    job = _gif_jobs.get(project_id)
    if not job or job["status"] != "done" or not job["path"]:
        raise HTTPException(status_code=404, detail="GIF not ready")
    if not os.path.exists(job["path"]):
        raise HTTPException(status_code=404, detail="GIF file not found on disk")
    with open(job["path"], "rb") as f:
        data = f.read()
    return Response(
        content=data,
        media_type="image/gif",
        headers={"Content-Disposition": f"attachment; filename=project_{project_id}.gif"},
    )


async def _run_gif_export(project_id: int) -> None:
    _gif_jobs[project_id]["status"] = "rendering"
    try:
        from app.config import get_settings

        settings = get_settings()

        # Pick up to 60 evenly-spaced non-dark non-blurry frames
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT file_path FROM frames
                WHERE project_id = ? AND is_dark = 0 AND (is_blurry IS NULL OR is_blurry = 0)
                ORDER BY captured_at ASC
                """,
                (project_id,),
            ).fetchall()

        all_paths = [r["file_path"] for r in rows if os.path.exists(r["file_path"])]
        max_frames = 60
        if len(all_paths) > max_frames:
            step = len(all_paths) / max_frames
            all_paths = [all_paths[int(i * step)] for i in range(max_frames)]

        if not all_paths:
            raise ValueError("No frames available for GIF export")

        concat_file = f"/tmp/gif_{project_id}.txt"
        with open(concat_file, "w") as f:
            for p in all_paths:
                f.write(f"file '{p}'\n")

        output_path = os.path.join(
            settings.renders_path, str(project_id), f"export_{project_id}.gif"
        )
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        cmd = [
            "ffmpeg",
            "-y",
            "-r",
            "10",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file,
            "-vf",
            "fps=10,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            output_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        import contextlib

        with contextlib.suppress(FileNotFoundError):
            os.remove(concat_file)

        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace")[:500])

        _gif_jobs[project_id] = {"status": "done", "path": output_path, "error": None}
        log.info("GIF export complete for project %d: %s", project_id, output_path)

    except Exception as exc:
        _gif_jobs[project_id] = {"status": "error", "path": None, "error": str(exc)[:500]}
        log.error("GIF export failed for project %d: %s", project_id, exc)


# -------------------------------------------------------------------------
# Stats
# -------------------------------------------------------------------------


@router.get("/projects/{project_id}/stats/daily")
def daily_stats(project_id: int) -> list[dict]:
    """Returns daily frame counts for the capture heatmap (from pre-aggregated frame_stats)."""
    _get_project_or_404(project_id)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT date, SUM(captured) as count
            FROM frame_stats
            WHERE project_id = ?
            GROUP BY date
            ORDER BY date ASC
            """,
            (project_id,),
        ).fetchall()
    if not rows:
        # Fall back to live aggregation (pre-existing data before migration)
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
    """Returns hourly captured/dark counts for the timeline view (from pre-aggregated frame_stats)."""
    _get_project_or_404(project_id)
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                date || 'T' || PRINTF('%02d', hour) || ':00:00' as hour,
                captured,
                dark
            FROM frame_stats
            WHERE project_id = ?
            ORDER BY date ASC, hour ASC
            """,
            (project_id,),
        ).fetchall()
    if not rows:
        # Fall back to live aggregation (pre-existing data before migration)
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
