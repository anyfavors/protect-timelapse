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
import tempfile
import time
from datetime import UTC, datetime

from app.config import get_settings
from app.database import get_connection

log = logging.getLogger("app.render")

_stop_event: asyncio.Event | None = None
_worker_task: asyncio.Task | None = None  # type: ignore[type-arg]

# Track the currently-running render so it can be cancelled (F1)
_active_render_id: int | None = None
_active_proc: asyncio.subprocess.Process | None = None  # type: ignore[type-arg]


def get_active_render_id() -> int | None:
    return _active_render_id


async def cancel_active_render(render_id: int) -> bool:
    """Kill the ffmpeg process if render_id is currently rendering. Returns True if killed."""
    global _active_proc, _active_render_id
    if _active_render_id != render_id or _active_proc is None:
        return False
    try:
        _active_proc.kill()
        log.info("Render id=%d cancelled by user request", render_id)
        return True
    except (ProcessLookupError, OSError):
        return False


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
            # Update heartbeat so liveness probe knows worker is alive (B2)
            from app.routes.health import update_render_worker_heartbeat

            update_render_worker_heartbeat()
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
            # Higher priority (larger number) runs first; ties broken by created_at (F5)
            "SELECT * FROM renders WHERE status = 'pending' ORDER BY COALESCE(priority,5) DESC, created_at ASC LIMIT 1"
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

    global _active_render_id
    _active_render_id = render_id

    log.info(
        "Starting render id=%d project=%d type=%s", render_id, project_id, render["render_type"]
    )

    # Use unpredictable temp files to prevent symlink/collision attacks (#12)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix=f"render_{render_id}_", delete=False
    ) as _tf:
        concat_file = _tf.name
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

        # Stabilization pre-pass (vidstabdetect)
        transforms_file: str | None = None
        if render.get("stabilize"):
            with tempfile.NamedTemporaryFile(
                suffix=".trf", prefix=f"transforms_{render_id}_", delete=False
            ) as _tf2:
                transforms_file = _tf2.name
            stab_cmd = [
                "ffmpeg",
                "-y",
                "-threads",
                str(getattr(settings, "ffmpeg_threads", 4)),
                "-r",
                str(render["framerate"]),
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file,
                "-vf",
                f"vidstabdetect=shakiness=5:accuracy=15:result={transforms_file}",
                "-f",
                "null",
                "-",
            ]
            stab_proc = await asyncio.create_subprocess_exec(
                *stab_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stab_err = await asyncio.wait_for(
                stab_proc.communicate(), timeout=getattr(settings, "ffmpeg_timeout_seconds", 3600)
            )
            if stab_proc.returncode != 0:
                log.warning(
                    "Render id=%d: vidstabdetect failed (%s) — continuing without stabilization",
                    render_id,
                    stab_err.decode(errors="replace")[:200],
                )
                transforms_file = None

        # Build ffmpeg command
        cmd = _build_ffmpeg_cmd(
            render, concat_file, output_path, total_frames, settings, transforms_file
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        global _active_proc
        _active_proc = proc  # expose for cancellation (F1)

        # Adaptive timeout: base + 2s per frame, capped at configured max (B8)
        base_timeout = getattr(settings, "ffmpeg_timeout_seconds", 7200)
        adaptive_timeout = min(base_timeout, max(300, total_frames * 2))
        log.debug(
            "Render id=%d: adaptive timeout=%ds for %d frames",
            render_id,
            adaptive_timeout,
            total_frames,
        )

        # Progress monitoring
        await _monitor_progress(proc, render_id, total_frames, adaptive_timeout)

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
        # Clean up partial output file on failure
        with contextlib.suppress(FileNotFoundError):
            os.remove(output_path)
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
        _active_render_id = None
        _active_proc = None
        with contextlib.suppress(FileNotFoundError):
            os.remove(concat_file)
        if "transforms_file" in locals() and transforms_file:
            with contextlib.suppress(FileNotFoundError):
                os.remove(transforms_file)


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
                WHERE project_id = ? AND is_dark = 0 AND (is_blurry IS NULL OR is_blurry = 0)
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
                WHERE project_id = ? AND is_dark = 0 AND (is_blurry IS NULL OR is_blurry = 0)
                ORDER BY captured_at ASC
                """,
                (project_id,),
            ).fetchall()

    paths = [r["file_path"] for r in rows if os.path.exists(r["file_path"])]

    # Cap frame list to prevent memory exhaustion on huge projects (#21)
    _MAX_FRAMES_PER_RENDER = 50_000
    if len(paths) > _MAX_FRAMES_PER_RENDER:
        log.warning(
            "Render project=%d: truncating frame list from %d to %d",
            render["project_id"],
            len(paths),
            _MAX_FRAMES_PER_RENDER,
        )
        paths = paths[:_MAX_FRAMES_PER_RENDER]

    return paths


_QUALITY_MAP = {
    "draft": ("libx264", "veryfast", "28"),
    "standard": ("libx264", "medium", "23"),
    "high": ("libx264", "slow", "18"),
    "archive": ("libx265", "medium", "22"),
}

_LUT_DIR = os.path.join(os.path.dirname(__file__), "luts")


def _build_ffmpeg_cmd(
    render: dict,
    concat_file: str,
    output_path: str,
    total_frames: int,
    settings: object,
    transforms_file: str | None = None,
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

    # Preview render: low-res proxy, max 300 evenly-sampled frames, ultrafast
    is_preview = render_type == "preview"
    if is_preview:
        target_frames = 300
        framerate = min(framerate, 30)
    else:
        # Check if frame skipping is needed (target max 1800 frames @ 30fps = 60s)
        target_frames = 1800

    skip_factor = (
        max(1, math.ceil(total_frames / target_frames)) if total_frames > target_frames else 1
    )

    # ── Filter chain ─────────────────────────────────────────────────────────
    filters: list[str] = []

    # Frame skip (select filter)
    if skip_factor > 1:
        filters += [
            f"select='not(mod(n\\,{skip_factor}))'",
            "setpts=N/FRAME_RATE/TB",
        ]

    # Frame blend — cinematic motion smoothness (needs ≥3 frames)
    if render.get("frame_blend") and total_frames >= 3:
        filters.append("tmix=frames=3:weights='1 2 1'")

    # Deflicker — needs at least `size` frames or ffmpeg aborts (exit -6 / SIGABRT)
    flicker = render.get("flicker_reduction") or "standard"
    if flicker == "standard" and total_frames >= 10:
        filters.append("deflicker=mode=pm:size=10")
    elif flicker in ("strong", "holy_grail") and total_frames >= 50:
        filters.append("deflicker=mode=pm:size=50")
    elif flicker in ("strong", "holy_grail") and total_frames >= 10:
        # Fall back to smaller window when not enough frames for size=50
        filters.append("deflicker=mode=pm:size=10")
    # "off" or too few frames → no deflicker filter added

    # Resolution / scale
    resolution = render.get("resolution") or "1920x1080"
    if is_preview:
        resolution = "854x480"  # 480p proxy

    out_w, out_h = (int(x) for x in resolution.split("x"))

    # Detect source resolution from first frame for lanczos guard
    src_w, src_h = _get_source_resolution(render["project_id"])
    if src_w and src_h and (out_w < src_w or out_h < src_h):
        filters.append(f"scale={out_w}:{out_h}:flags=lanczos")
    elif is_preview:
        # Always scale for preview even if no source info
        filters.append(f"scale={out_w}:{out_h}:flags=lanczos")

    # Color grade LUT — validate path stays inside _LUT_DIR to prevent traversal (#2)
    grade = render.get("color_grade") or "none"
    if grade != "none":
        lut_path = os.path.realpath(os.path.join(_LUT_DIR, f"{grade}.cube"))
        lut_dir_real = os.path.realpath(_LUT_DIR)
        if lut_path.startswith(lut_dir_real + os.sep) and os.path.exists(lut_path):
            filters.append(f"lut3d={lut_path}")
        else:
            log.warning(
                "Render id=%d: rejected LUT path traversal attempt: %r", render.get("id"), grade
            )

    # Stabilization transform pass (requires pre-pass transforms file)
    # unsharp is a separate filter — embedding it after a comma inside vidstabtransform
    # breaks filter chain parsing when other filters are present.
    if transforms_file and os.path.exists(transforms_file):
        filters.append(f"vidstabtransform=input={transforms_file}:zoom=1:smoothing=30")
        filters.append("unsharp=5:5:0.8:3:3:0.4")

    # Timestamp burn-in
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT timestamp_burn_in FROM settings WHERE id = 1").fetchone()
        burn_in = row["timestamp_burn_in"] if row else 0
    except Exception:
        burn_in = 0

    if burn_in:
        epoch = _get_first_frame_epoch(render["project_id"])
        filters.append(
            f"drawtext=text='%{{pts\\:localtime\\:{epoch}\\:%Y-%m-%d %H\\\\:%M}}':"
            "x=w-tw-20:y=h-th-20:fontcolor=white:fontsize=32:box=1:boxcolor=black@0.6"
        )

    # Watermark overlay (movie filter + overlay) — validate path to prevent traversal (#3)
    watermark_path: str | None = None
    try:
        with get_connection() as conn:
            wm_row = conn.execute("SELECT watermark_path FROM settings WHERE id = 1").fetchone()
        if wm_row and wm_row["watermark_path"]:
            wm_real = os.path.realpath(wm_row["watermark_path"])
            # Only allow paths inside /data/ to prevent arbitrary file inclusion
            if wm_real.startswith("/data/") and os.path.exists(wm_real):
                watermark_path = wm_real
            else:
                log.warning(
                    "Render: rejected watermark path outside /data/: %r", wm_row["watermark_path"]
                )
    except Exception:
        pass

    # Build final vf / filter_complex
    filter_complex: str | None = None
    vf: str | None = None
    if watermark_path:
        chain = ",".join(filters) if filters else "null"
        filter_complex = (
            f"[0:v]{chain}[main];movie={watermark_path}[wm];[main][wm]overlay=W-w-10:H-h-10[out]"
        )
    elif filters:
        vf = ",".join(filters)
    # No filters and no watermark → omit -vf entirely (passing -vf null is invalid)

    # ── Encode settings ───────────────────────────────────────────────────────
    if is_preview:
        codec, preset, crf = "libx264", "ultrafast", "32"
    else:
        quality = render.get("quality") or "standard"
        codec, preset, crf = _QUALITY_MAP.get(quality, _QUALITY_MAP["standard"])

    cmd = [
        "ffmpeg",
        "-y",
        "-threads",
        str(ffmpeg_threads),
        # Increase demuxer queue to avoid SIGABRT on large frame counts with filters
        "-thread_queue_size",
        "512",
        "-r",
        str(framerate),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_file,
    ]
    if filter_complex:
        cmd += ["-filter_complex", filter_complex, "-map", "[out]"]
    elif vf:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v",
        codec,
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
        # Avoid muxer queue overflow on variable-framerate filter outputs
        "-max_muxing_queue_size",
        "1024",
        output_path,
    ]
    return cmd


def _get_source_resolution(project_id: int) -> tuple[int, int]:
    """Return (width, height) of the first non-dark frame, or (0, 0) if unavailable."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT file_path FROM frames WHERE project_id = ? AND is_dark = 0 ORDER BY captured_at ASC LIMIT 1",
            (project_id,),
        ).fetchone()
    if row and os.path.exists(row["file_path"]):
        try:
            from PIL import Image

            with Image.open(row["file_path"]) as img:
                return img.width, img.height
        except Exception:
            pass
    return 0, 0


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
