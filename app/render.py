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
from app.database import VALID_FRAME_SQL, get_connection

log = logging.getLogger("app.render")

_stop_event: asyncio.Event | None = None
_worker_task: asyncio.Task | None = None  # type: ignore[type-arg]

# Track the currently-running render so it can be cancelled (F1)
_active_render_id: int | None = None
_active_proc: asyncio.subprocess.Process | None = None  # type: ignore[type-arg]
_active_render_lock: asyncio.Lock | None = None  # lazily initialised on first use

# Pre-compiled regex for ffmpeg progress lines — avoids re-compiling on every stderr line
_FRAME_RE = re.compile(r"frame=\s*(\d+)")

# Stall detection: if render progress doesn't advance for this long, mark stalled
_STALL_TIMEOUT_SECONDS = 600


def get_active_render_id() -> int | None:
    return _active_render_id


async def cancel_active_render(render_id: int) -> bool:
    """Kill the ffmpeg process if render_id is currently rendering. Returns True if killed."""
    global _active_proc, _active_render_id
    lock = _active_render_lock
    async with lock if lock else asyncio.Lock():
        if _active_render_id != render_id or _active_proc is None:
            return False
        try:
            _active_proc.kill()
            log.info("Render id=%d cancelled by user request", render_id)
            return True
        except (ProcessLookupError, OSError):
            return False


async def pause_active_render(render_id: int) -> bool:
    """Pause a rendering job: kill ffmpeg and set status to 'paused'. Returns True if paused."""
    global _active_proc, _active_render_id
    lock = _active_render_lock
    async with lock if lock else asyncio.Lock():
        if _active_render_id != render_id or _active_proc is None:
            return False
        try:
            _active_proc.kill()
            log.info("Render id=%d paused by user request", render_id)
            return True
        except (ProcessLookupError, OSError):
            return False


async def start_render_worker() -> "asyncio.Task[None]":
    global _stop_event, _worker_task, _active_render_lock
    _stop_event = asyncio.Event()
    _active_render_lock = asyncio.Lock()
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
    _error_backoff = 0  # exponential backoff on repeated errors (seconds)
    while not stop.is_set():
        try:
            # Update heartbeat so liveness probe knows worker is alive (B2)
            from app.routes.health import update_render_worker_heartbeat

            update_render_worker_heartbeat()
            poll = await _process_next_render()
            _error_backoff = 0  # reset on success
        except Exception as exc:
            _error_backoff = min(_error_backoff * 2 + 5, 120)  # cap at 2 minutes
            log.error("Render loop error (backoff=%ds): %s", _error_backoff, exc)
            await asyncio.sleep(_error_backoff)
            continue
        await asyncio.sleep(poll or 5)


async def _process_next_render() -> int:
    """Process the next pending render. Returns the poll interval (seconds) to sleep."""
    # Read settings once — used both for render config and poll interval
    try:
        with get_connection() as conn:
            _settings_row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        render_settings: dict = dict(_settings_row) if _settings_row else {}
    except Exception:
        render_settings = {}
    poll: int = int(render_settings.get("render_poll_interval_seconds") or 5)

    with get_connection() as conn:
        row = conn.execute(
            # Higher priority (larger number) runs first; ties broken by created_at (F5)
            "SELECT * FROM renders WHERE status = 'pending' ORDER BY COALESCE(priority,5) DESC, created_at ASC LIMIT 1"
        ).fetchone()
    if row is None:
        return poll

    render = dict(row)
    render_id = render["id"]
    project_id = render["project_id"]
    settings = get_settings()

    # Validate resolution format before ffmpeg sees it (B7)
    resolution = render.get("resolution") or "1920x1080"
    if not re.fullmatch(r"\d{1,5}x\d{1,5}", resolution):
        log.error("Render id=%d: invalid resolution %r — skipping", render_id, resolution)
        with get_connection() as conn:
            conn.execute(
                "UPDATE renders SET status='error', error_msg='Invalid resolution format' WHERE id=?",
                (render_id,),
            )
            conn.commit()
        return poll

    # Lock the row and stamp start time (for ETA calculation in UI)
    with get_connection() as conn:
        conn.execute(
            "UPDATE renders SET status = 'rendering', started_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), render_id),
        )
        conn.commit()

    # Disk space pre-check: abort early rather than failing mid-render
    try:
        import shutil as _shutil

        usage = _shutil.disk_usage(settings.renders_path)
        free_gb = usage.free / 1024**3
        if free_gb < 0.5:  # require at least 500 MB free
            raise RuntimeError(f"Insufficient disk space: only {free_gb:.2f} GB free")
    except OSError as exc:
        log.warning("Render id=%d: disk check failed: %s", render_id, exc)

    lock = _active_render_lock
    async with lock if lock else asyncio.Lock():
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
        frame_paths, truncated_from = _get_frame_paths(render)
        if not frame_paths:
            raise ValueError("No frames found for render")

        if truncated_from:
            warn_msg = (
                f"WARNING: project has {truncated_from:,} frames but only the first "
                f"{len(frame_paths):,} were rendered (50 000 frame limit)."
            )
            with get_connection() as _conn:
                _conn.execute(
                    "UPDATE renders SET error_msg = ? WHERE id = ?",
                    (warn_msg, render_id),
                )
                _conn.commit()

        total_frames = len(frame_paths)

        # Write concat demuxer file
        with open(concat_file, "w") as f:
            for path in frame_paths:
                f.write(f"file '{path}'\n")

        # Stabilization pre-pass (vidstabdetect)
        transforms_file: str | None = None
        if render.get("stabilize"):  # pragma: no cover
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

        # Build ffmpeg command (pass snapshotted settings to avoid mid-render staleness)
        cmd = _build_ffmpeg_cmd(
            render,
            concat_file,
            output_path,
            total_frames,
            settings,
            transforms_file,
            render_settings=render_settings,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        global _active_proc
        _active_proc = proc  # expose for cancellation (F1)

        # Adaptive timeout: at least base_timeout, grows by 2s/frame (so large renders don't timeout)
        base_timeout = getattr(settings, "ffmpeg_timeout_seconds", 7200)
        adaptive_timeout = max(base_timeout, total_frames * 2)
        log.debug(
            "Render id=%d: adaptive timeout=%ds for %d frames",
            render_id,
            adaptive_timeout,
            total_frames,
        )

        # Progress monitoring (pass monotonic start time for ETA calculation)
        _render_monotonic_start = time.monotonic()
        await _monitor_progress(
            proc, render_id, total_frames, adaptive_timeout, started_at=_render_monotonic_start
        )

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
        raw_error = str(exc)[:1000]
        log.error("Render id=%d failed: %s", render_id, raw_error)
        # Sanitise error message: strip internal file paths before storing/broadcasting (S9/S17)
        error_msg = re.sub(r"(/data|/tmp|/app|/home)\S+", "<path>", raw_error)
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

        async def _cleanup() -> None:
            global _active_render_id, _active_proc
            lock = _active_render_lock
            async with lock if lock else asyncio.Lock():
                _active_render_id = None
                _active_proc = None
            with contextlib.suppress(FileNotFoundError):
                os.remove(concat_file)
            if "transforms_file" in locals() and transforms_file:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(transforms_file)

        await asyncio.shield(_cleanup())

    return poll


def _get_frame_paths(render: dict) -> tuple[list[str], int]:
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
        return [r["output_path"] for r in rows if os.path.exists(r["output_path"])], 0

    # Standard / range / manual — JPEG frames
    if render_type == "range" and render.get("range_start") and render.get("range_end"):
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT file_path FROM frames
                WHERE project_id = ? AND {VALID_FRAME_SQL}
                AND captured_at BETWEEN ? AND ?
                ORDER BY captured_at ASC
                """,
                (project_id, render["range_start"], render["range_end"]),
            ).fetchall()
    else:
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT file_path FROM frames
                WHERE project_id = ? AND {VALID_FRAME_SQL}
                ORDER BY captured_at ASC
                """,
                (project_id,),
            ).fetchall()

    paths = [r["file_path"] for r in rows if os.path.exists(r["file_path"])]

    # Cap frame list to prevent memory exhaustion on huge projects (#21)
    _MAX_FRAMES_PER_RENDER = 50_000
    truncated_from = 0
    if len(paths) > _MAX_FRAMES_PER_RENDER:
        truncated_from = len(paths)
        log.warning(
            "Render project=%d: truncating frame list from %d to %d",
            render["project_id"],
            len(paths),
            _MAX_FRAMES_PER_RENDER,
        )
        paths = paths[:_MAX_FRAMES_PER_RENDER]

    return paths, truncated_from


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
    render_settings: dict | None = None,
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

    # Deflicker — needs at least `size` frames after skip_factor reduction or ffmpeg
    # aborts with a boundary error (exit -6 / SIGABRT).
    # Use effective_frames (post-skip) to guard the window size.
    effective_frames = math.ceil(total_frames / skip_factor) if skip_factor > 1 else total_frames
    flicker = render.get("flicker_reduction") or "standard"
    if flicker != "off":
        if effective_frames >= 50 and flicker in ("strong", "holy_grail"):
            filters.append("deflicker=mode=pm:size=50")
        elif effective_frames >= 10:
            filters.append("deflicker=mode=pm:size=10")
        # too few effective frames → skip deflicker silently

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

    # Use snapshotted settings if provided, otherwise fall back to live DB read
    _rs = render_settings or {}

    # Timestamp burn-in — prefer snapshotted value; fall back to live DB read if no snapshot
    if render_settings is not None:
        burn_in = _rs.get("timestamp_burn_in") or 0
    else:
        try:
            with get_connection() as conn:
                _bi_row = conn.execute(
                    "SELECT timestamp_burn_in FROM settings WHERE id = 1"
                ).fetchone()
            burn_in = _bi_row["timestamp_burn_in"] if _bi_row else 0
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
    _wm_path_raw = _rs.get("watermark_path")
    if not _wm_path_raw and not render_settings:
        try:
            with get_connection() as conn:
                wm_row = conn.execute("SELECT watermark_path FROM settings WHERE id = 1").fetchone()
            _wm_path_raw = wm_row["watermark_path"] if wm_row else None
        except Exception:
            pass
    if _wm_path_raw:
        wm_real = os.path.realpath(_wm_path_raw)
        # Only allow paths inside /data/ to prevent arbitrary file inclusion
        if wm_real.startswith("/data/") and os.path.exists(wm_real):
            watermark_path = wm_real
        else:
            log.warning("Render: rejected watermark path outside /data/: %r", _wm_path_raw)

    # Build final vf / filter_complex
    filter_complex: str | None = None
    vf: str | None = None
    if watermark_path:
        chain = ",".join(filters) if filters else "null"
        # Escape watermark path for ffmpeg filter syntax to prevent injection (S4)
        wm_escaped = watermark_path.replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:")
        filter_complex = (
            f"[0:v]{chain}[main];movie='{wm_escaped}'[wm];[main][wm]overlay=W-w-10:H-h-10[out]"
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


async def _monitor_progress(  # pragma: no cover
    proc: asyncio.subprocess.Process,
    render_id: int,
    total_frames: int,
    timeout_seconds: int,
    started_at: float | None = None,
) -> None:
    """Read stderr line by line, update progress_pct at most once per second.

    Also detects stalls (no progress for _STALL_TIMEOUT_SECONDS) and kills ffmpeg.
    """
    last_update = 0.0
    last_pct = -1
    last_progress_change = time.monotonic()
    stalled = False

    async def _read_stderr() -> None:
        nonlocal last_update, last_pct, last_progress_change, stalled
        if proc.stderr is None:
            return
        async for line in proc.stderr:
            decoded = line.decode(errors="replace")
            m = _FRAME_RE.search(decoded)
            if m and total_frames > 0:
                current = int(m.group(1))
                pct = min(100, int(current / total_frames * 100))
                now = time.monotonic()

                # Stall detection
                if pct != last_pct:
                    last_pct = pct
                    last_progress_change = now
                elif now - last_progress_change > _STALL_TIMEOUT_SECONDS:
                    stalled = True
                    log.error(
                        "Render id=%d stalled (no progress for %ds) — killing ffmpeg",
                        render_id,
                        _STALL_TIMEOUT_SECONDS,
                    )
                    with contextlib.suppress(Exception), get_connection() as conn:
                        conn.execute(
                            "UPDATE renders SET status = 'stalled' WHERE id = ?",
                            (render_id,),
                        )
                        conn.commit()
                    from app.notifications import notify

                    try:
                        await notify(
                            event="render_stalled",
                            level="error",
                            message=f"Render #{render_id} stalled — no progress for {_STALL_TIMEOUT_SECONDS // 60} min.",
                        )
                    except Exception as _ne:
                        log.warning("render_stalled notify failed: %s", _ne)
                    with contextlib.suppress(ProcessLookupError, OSError):
                        proc.kill()
                    return

                if now - last_update >= 5.0:  # throttle DB writes to every 5s
                    last_update = now
                    # Compute ETA from elapsed time and progress
                    eta_seconds: int | None = None
                    if started_at is not None and pct > 0:
                        elapsed = now - started_at
                        eta_seconds = int(elapsed / (pct / 100) * (1 - pct / 100))
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
                                "eta_seconds": eta_seconds,
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

    if stalled:
        raise RuntimeError(f"Render stalled: no ffmpeg progress for {_STALL_TIMEOUT_SECONDS}s")


# -------------------------------------------------------------------------
# Render estimate (used by POST /api/renders)
# -------------------------------------------------------------------------


def estimate_render(project_id: int, framerate: int, render_type: str = "manual") -> dict:
    """Return estimated render duration and output file size.

    Applies the same is_dark + is_blurry filters as the actual render so
    the estimate reflects what ffmpeg will actually process.
    """
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) as cnt, AVG(file_size) as avg_size FROM frames "
            f"WHERE project_id = ? AND {VALID_FRAME_SQL}",
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
