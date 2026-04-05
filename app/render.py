"""
Render worker — sequential ffmpeg queue.
Picks up 'pending' renders one at a time to protect the host CPU.
Full implementation in Phase 4.
"""

import asyncio
import contextlib
import logging
import math
import os
import re
import time
from datetime import UTC, datetime

from app.config import get_settings
from app.database import get_connection

log = logging.getLogger("app.render")

_stop_event: asyncio.Event | None = None
_worker_task: asyncio.Task | None = None  # type: ignore[type-arg]


async def start_render_worker() -> "asyncio.Task[None]":
    global _stop_event, _worker_task
    _stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(_render_loop(_stop_event))
    log.info("Render worker started")
    return _worker_task


async def stop_render_worker(task: "asyncio.Task[None]") -> None:
    global _stop_event
    if _stop_event:
        _stop_event.set()
    if task and not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=35.0)
    log.info("Render worker stopped")


# -------------------------------------------------------------------------
# Main loop
# -------------------------------------------------------------------------


async def _render_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await _process_next_render()
        except Exception as exc:
            log.error("Render loop error: %s", exc)
        # Poll interval from settings
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT render_poll_interval_seconds FROM settings WHERE id = 1"
                ).fetchone()
            poll = row["render_poll_interval_seconds"] if row else 5
        except Exception:
            poll = 5
        await asyncio.sleep(poll)


async def _process_next_render() -> None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM renders WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    if row is None:
        return

    render = dict(row)
    render_id = render["id"]
    project_id = render["project_id"]
    settings = get_settings()

    # Lock the row
    with get_connection() as conn:
        conn.execute("UPDATE renders SET status = 'rendering' WHERE id = ?", (render_id,))
        conn.commit()

    log.info(
        "Starting render id=%d project=%d type=%s", render_id, project_id, render["render_type"]
    )

    concat_file = f"/tmp/render_{render_id}.txt"
    output_path = os.path.join(settings.renders_path, str(project_id), f"{render_id}.mp4")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        # Build frame list
        frame_paths = _get_frame_paths(render)
        if not frame_paths:
            raise ValueError("No frames found for render")

        total_frames = len(frame_paths)

        # Write concat demuxer file
        with open(concat_file, "w") as f:
            for path in frame_paths:
                f.write(f"file '{path}'\n")

        # Build ffmpeg command
        cmd = _build_ffmpeg_cmd(render, concat_file, output_path, total_frames, settings)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Progress monitoring
        await _monitor_progress(proc, render_id, total_frames, settings.ffmpeg_timeout_seconds)

        if proc.returncode != 0:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""  # type: ignore[union-attr]
            raise RuntimeError(
                f"ffmpeg exited {proc.returncode}: {stderr_bytes.decode(errors='replace')[:500]}"
            )

        file_size = os.path.getsize(output_path)
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE renders
                SET status='done', completed_at=?, file_size=?, progress_pct=100, output_path=?
                WHERE id=?
                """,
                (datetime.now(UTC).isoformat(), file_size, output_path, render_id),
            )
            conn.commit()

        log.info("Render id=%d complete: %s (%.1f MB)", render_id, output_path, file_size / 1024**2)

        from app.websocket import broadcast

        await broadcast(
            "render_complete",
            {
                "render_id": render_id,
                "project_id": project_id,
                "status": "done",
                "file_size": file_size,
            },
        )

        from app.notifications import notify

        await notify(
            event="render_complete",
            level="info",
            message=f"Render #{render_id} completed successfully ({file_size // 1024**2} MB).",
            project_id=project_id,
        )

    except Exception as exc:
        error_msg = str(exc)[:1000]
        log.error("Render id=%d failed: %s", render_id, error_msg)
        with get_connection() as conn:
            conn.execute(
                "UPDATE renders SET status='error', error_msg=? WHERE id=?",
                (error_msg, render_id),
            )
            conn.commit()

        from app.websocket import broadcast

        await broadcast(
            "render_complete",
            {
                "render_id": render_id,
                "project_id": project_id,
                "status": "error",
                "error_msg": error_msg,
            },
        )

        from app.notifications import notify

        await notify(
            event="render_error",
            level="error",
            message=f"Render #{render_id} failed: {error_msg[:200]}",
            project_id=project_id,
        )

    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(concat_file)


def _get_frame_paths(render: dict) -> list[str]:
    project_id = render["project_id"]
    render_type = render["render_type"]

    if render_type in ("auto_weekly", "auto_monthly"):
        # Rollup: use existing daily MP4s
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT output_path FROM renders
                WHERE project_id = ? AND render_type = 'auto_daily'
                AND status = 'done' AND output_path IS NOT NULL
                ORDER BY completed_at ASC
                """,
                (project_id,),
            ).fetchall()
        return [r["output_path"] for r in rows if os.path.exists(r["output_path"])]

    # Standard / range / manual — JPEG frames
    if render_type == "range" and render.get("range_start") and render.get("range_end"):
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT file_path FROM frames
                WHERE project_id = ? AND is_dark = 0
                AND captured_at BETWEEN ? AND ?
                ORDER BY captured_at ASC
                """,
                (project_id, render["range_start"], render["range_end"]),
            ).fetchall()
    else:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT file_path FROM frames
                WHERE project_id = ? AND is_dark = 0
                ORDER BY captured_at ASC
                """,
                (project_id,),
            ).fetchall()

    return [r["file_path"] for r in rows if os.path.exists(r["file_path"])]


def _build_ffmpeg_cmd(
    render: dict,
    concat_file: str,
    output_path: str,
    total_frames: int,
    settings: object,
) -> list[str]:
    framerate = render["framerate"]
    render_type = render["render_type"]
    ffmpeg_threads = getattr(settings, "ffmpeg_threads", 4)

    # Rollup: stream-copy (no re-encode needed for 1:1 concatenation)
    if render_type in ("auto_weekly", "auto_monthly"):
        return [
            "ffmpeg",
            "-y",
            "-threads",
            str(ffmpeg_threads),
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file,
            "-c",
            "copy",
            output_path,
        ]

    # Check if frame skipping is needed (target max 1800 frames @ 30fps = 60s)
    target_frames = 1800
    skip_factor = (
        max(1, math.ceil(total_frames / target_frames)) if total_frames > target_frames else 1
    )

    # Build filter graph
    filters: list[str] = ["deflicker=mode=pm:size=10"]

    if skip_factor > 1:
        filters = [
            f"select='not(mod(n\\,{skip_factor}))'",
            "setpts=N/FRAME_RATE/TB",
            "deflicker=mode=pm:size=10",
        ]

    # Timestamp burn-in
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT timestamp_burn_in FROM settings WHERE id = 1").fetchone()
        burn_in = row["timestamp_burn_in"] if row else 0
    except Exception:
        burn_in = 0

    if burn_in:
        # Get epoch of first frame for drawtext pts localtime calculation
        epoch = _get_first_frame_epoch(render["project_id"])
        filters.append(
            f"drawtext=text='%{{pts\\:localtime\\:{epoch}\\:%Y-%m-%d %H\\\\:%M}}':"
            "x=w-tw-20:y=h-th-20:fontcolor=white:fontsize=32:box=1:boxcolor=black@0.6"
        )

    vf = ",".join(filters)

    cmd = [
        "ffmpeg",
        "-y",
        "-threads",
        str(ffmpeg_threads),
        "-r",
        str(framerate),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_file,
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        output_path,
    ]
    return cmd


def _get_first_frame_epoch(project_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT captured_at FROM frames WHERE project_id = ? AND is_dark = 0 ORDER BY captured_at ASC LIMIT 1",
            (project_id,),
        ).fetchone()
    if row:
        try:
            dt = datetime.fromisoformat(row["captured_at"]).replace(tzinfo=UTC)
            return int(dt.timestamp())
        except Exception:
            pass
    return 0


async def _monitor_progress(
    proc: asyncio.subprocess.Process,
    render_id: int,
    total_frames: int,
    timeout_seconds: int,
) -> None:
    """Read stderr line by line, update progress_pct at most once per second."""
    last_update = 0.0
    frame_re = re.compile(r"frame=\s*(\d+)")

    async def _read_stderr() -> None:
        nonlocal last_update
        if proc.stderr is None:
            return
        async for line in proc.stderr:
            decoded = line.decode(errors="replace")
            m = frame_re.search(decoded)
            if m and total_frames > 0:
                current = int(m.group(1))
                pct = min(100, int(current / total_frames * 100))
                now = time.monotonic()
                if now - last_update >= 1.0:
                    last_update = now
                    with contextlib.suppress(Exception), get_connection() as conn:
                        conn.execute(
                            "UPDATE renders SET progress_pct = ? WHERE id = ?",
                            (pct, render_id),
                        )
                        conn.commit()
                    from app.websocket import broadcast

                    with contextlib.suppress(Exception):
                        await broadcast(
                            "render_progress",
                            {
                                "render_id": render_id,
                                "progress_pct": pct,
                            },
                        )

    try:
        await asyncio.wait_for(
            asyncio.gather(_read_stderr(), proc.wait()),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        log.error("Render id=%d timed out after %ds — killing ffmpeg", render_id, timeout_seconds)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise


# -------------------------------------------------------------------------
# Render estimate (used by POST /api/renders)
# -------------------------------------------------------------------------


def estimate_render(project_id: int, framerate: int, render_type: str = "manual") -> dict:
    """Return estimated render duration and output file size."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt, AVG(file_size) as avg_size FROM frames WHERE project_id = ? AND is_dark = 0",
            (project_id,),
        ).fetchone()

    frame_count = row["cnt"] if row else 0
    avg_frame_size = row["avg_size"] or 200_000  # default 200 KB

    duration_seconds = frame_count / max(framerate, 1)
    # Empirical baseline: ~0.02s render time per frame at -preset fast
    render_time = int(frame_count * 0.02)
    # CRF 23 H.264 typically ~15% of source JPEG size
    file_size_bytes = int(frame_count * avg_frame_size * 0.15)

    return {
        "frame_count": frame_count,
        "estimated_duration_seconds": int(duration_seconds),
        "estimated_render_time_seconds": render_time,
        "estimated_file_size_bytes": file_size_bytes,
    }
