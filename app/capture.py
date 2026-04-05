"""
Capture worker — APScheduler-based snapshot ingestion.

Each active project maps to one APScheduler job (id = 'project_{id}').
The snapshot_worker() runs the full visibility pipeline before saving a frame.
"""

import asyncio
import contextlib
import io
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from PIL import Image, ImageFilter, ImageStat

from app.config import get_settings
from app.database import get_connection, get_db_overrides
from app.protect import protect_manager
from app.thumbnails import generate_thumbnail, generate_thumbnail_from_pillow
from app.websocket import broadcast

log = logging.getLogger("app.capture")

scheduler = AsyncIOScheduler()

# Default Laplacian variance threshold below which a frame is flagged as blurry.
_BLUR_THRESHOLD = 20.0

# Per-camera semaphore: at most 2 concurrent snapshots per camera to avoid NVR overload.
_camera_semaphores: dict[str, asyncio.Semaphore] = {}

# Disk check interval: every N captures we run a full disk check.
# APScheduler calls snapshot_worker per project; disk check is cheap so we
# run it every time rather than tracking a separate timer.
_DISK_CHECK_EVERY_N = 1  # check every capture (matches spec: every 5-min loop)

# -------------------------------------------------------------------------
# Scheduler lifecycle
# -------------------------------------------------------------------------


async def start_scheduler() -> None:
    """Boot APScheduler and register jobs for all currently active projects."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, interval_seconds, capture_mode FROM projects WHERE status = 'active' AND project_type = 'live'"
        ).fetchall()

    for row in rows:
        _register_job(row["id"], row["interval_seconds"], row["capture_mode"])

    scheduler.start()
    log.info("Capture scheduler started with %d active project(s)", len(rows))


async def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("Capture scheduler stopped")


async def add_project_job(
    project_id: int, interval_seconds: int, capture_mode: str = "continuous"
) -> None:
    _register_job(project_id, interval_seconds, capture_mode)


async def remove_project_job(project_id: int) -> None:
    job_id = f"project_{project_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


async def reschedule_project_job(
    project_id: int, interval_seconds: int, capture_mode: str = "continuous"
) -> None:
    job_id = f"project_{project_id}"
    effective = 60 if capture_mode == "solar_noon" else interval_seconds
    if scheduler.get_job(job_id):
        scheduler.reschedule_job(job_id, trigger=IntervalTrigger(seconds=effective))
    else:
        _register_job(project_id, interval_seconds, capture_mode)


async def pause_project_job(project_id: int) -> None:
    job_id = f"project_{project_id}"
    if scheduler.get_job(job_id):
        scheduler.pause_job(job_id)


async def resume_project_job(
    project_id: int, interval_seconds: int, capture_mode: str = "continuous"
) -> None:
    job_id = f"project_{project_id}"
    if scheduler.get_job(job_id):
        scheduler.resume_job(job_id)
    else:
        _register_job(project_id, interval_seconds, capture_mode)


def _register_job(project_id: int, interval_seconds: int, capture_mode: str = "continuous") -> None:
    # Solar noon projects poll every 60 s; the worker itself gates on noon proximity.
    effective_interval = 60 if capture_mode == "solar_noon" else interval_seconds
    job_id = f"project_{project_id}"
    scheduler.add_job(
        snapshot_worker,
        trigger=IntervalTrigger(seconds=effective_interval),
        id=job_id,
        args=[project_id],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def _set_project_status(project_id: int, status: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
        conn.commit()


# -------------------------------------------------------------------------
# Snapshot worker — full visibility pipeline
# -------------------------------------------------------------------------


async def snapshot_worker(project_id: int) -> None:
    """
    Full capture pipeline for one project interval:
    1. Disk failsafe
    2. Reload project row (picks up live settings changes)
    3. Astronomical filter (daylight_only)
    4. Schedule window filter
    5. NVR snapshot fetch
    6. Luminance filter
    7. Save JPEG + thumbnail
    8. DB insert + frame_count increment
    9. max_frames check
    """
    settings = get_settings()

    # --- 1. Disk failsafe ---------------------------------------------------
    try:
        usage = shutil.disk_usage(settings.frames_path)
        free_gb = usage.free / 1024**3
        threshold_gb = _get_disk_threshold()
        if free_gb < threshold_gb:
            await _handle_disk_breach(free_gb, threshold_gb)
            return
    except Exception as exc:
        log.warning("Disk check failed: %s", exc)

    # --- 2. Reload project (live settings) ----------------------------------
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND status = 'active'", (project_id,)
        ).fetchone()
    if row is None:
        # Project was paused, deleted, or completed — remove its job
        await remove_project_job(project_id)
        return

    project = dict(row)

    # Safety net: historical projects must never get snapshot jobs
    if project["project_type"] != "live":
        await remove_project_job(project_id)
        return

    # --- 3. Astronomical filter ---------------------------------------------
    if project["capture_mode"] == "daylight_only" and not _is_daylight():
        log.debug("Project %d: outside daylight window, skipping", project_id)
        return

    # --- 3b. Solar noon filter ----------------------------------------------
    # Solar noon mode: scheduler runs every minute; only capture within
    # ±window of solar noon AND only once per calendar day.
    if project["capture_mode"] == "solar_noon":
        if not _is_solar_noon_window(project):
            return
        # Check if already captured today (UTC date)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        with get_connection() as conn:
            already = conn.execute(
                "SELECT 1 FROM frames WHERE project_id = ? AND captured_at >= ? LIMIT 1",
                (project_id, today),
            ).fetchone()
        if already:
            log.debug("Project %d: solar noon frame already captured today", project_id)
            return

    # --- 4. Schedule window filter ------------------------------------------
    tz_name: str = get_db_overrides().get("tz") or settings.tz  # type: ignore[assignment]
    if project["capture_mode"] == "schedule" and not _is_in_schedule(project, tz_name):
        log.debug("Project %d: outside schedule window, skipping", project_id)
        return

    # --- 5. NVR snapshot ----------------------------------------------------
    try:
        client = await protect_manager.get_client()
        cam = client.bootstrap.cameras.get(project["camera_id"])
        if cam is None:
            log.warning("Project %d: camera %s not found on NVR", project_id, project["camera_id"])
            _increment_failures(project_id)
            return

        kwargs: dict = {}
        if project["width"]:
            kwargs["width"] = project["width"]
        if project["height"]:
            kwargs["height"] = project["height"]

        camera_id = project["camera_id"]
        if camera_id not in _camera_semaphores:
            _camera_semaphores[camera_id] = asyncio.Semaphore(2)
        async with _camera_semaphores[camera_id]:
            raw = await cam.get_snapshot(**kwargs)
        if raw is None:
            raise RuntimeError("NVR returned empty snapshot")
        snapshot_bytes: bytes = raw
        _reset_failures(project_id)

    except (httpx.ReadTimeout, httpx.ConnectError, Exception) as exc:
        log.warning("Project %d: NVR snapshot failed — %s", project_id, exc)
        failures = _increment_failures(project_id)
        if failures >= 3:
            await _notify_nvr_offline(project_id, project["name"], failures)
        return

    # --- 6. Luminance filter ------------------------------------------------
    is_dark = 0
    pil_img: Image.Image | None = None

    if project["use_luminance_check"]:
        pil_img = Image.open(io.BytesIO(snapshot_bytes))
        gray = pil_img.convert("L")
        brightness = ImageStat.Stat(gray).mean[0]
        if brightness < project["luminance_threshold"]:
            is_dark = 1
            log.debug(
                "Project %d: frame is dark (brightness=%.1f < threshold=%d)",
                project_id,
                brightness,
                project["luminance_threshold"],
            )

    # --- 6b. Motion filter (skip if scene hasn't changed enough) -----------
    if project.get("use_motion_filter"):
        if pil_img is None:
            pil_img = Image.open(io.BytesIO(snapshot_bytes))
        with get_connection() as conn:
            last_row = conn.execute(
                "SELECT file_path FROM frames WHERE project_id = ? ORDER BY captured_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if last_row:
            try:
                # No existence pre-check: handle FileNotFoundError directly to avoid TOCTOU
                # (maintenance worker may delete the file between check and open) (#6)
                last_img = Image.open(last_row["file_path"]).convert("L").resize((64, 64))
                curr_gray = pil_img.convert("L").resize((64, 64))
                diff = (
                    sum(
                        abs(a - b)
                        for a, b in zip(last_img.getdata(), curr_gray.getdata(), strict=False)
                    )
                    / (64 * 64 * 255)
                    * 100
                )
                threshold = project.get("motion_threshold") or 5
                if diff < threshold:
                    log.debug(
                        "Project %d: motion filter skip (diff=%.1f%% < threshold=%d%%)",
                        project_id,
                        diff,
                        threshold,
                    )
                    return
            except Exception as exc:
                log.debug("Project %d: motion filter error, allowing frame: %s", project_id, exc)

    if pil_img is None:
        pil_img = Image.open(io.BytesIO(snapshot_bytes))
    gray_for_sharp = pil_img.convert("L")
    edges = gray_for_sharp.filter(ImageFilter.FIND_EDGES)
    sharpness_score = float(ImageStat.Stat(edges).var[0])
    is_blurry = 1 if sharpness_score < _BLUR_THRESHOLD else 0

    # --- 7. Save JPEG + thumbnail ------------------------------------------
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    frame_dir = os.path.join(settings.frames_path, str(project_id))
    thumb_dir = os.path.join(settings.thumbnails_path, str(project_id))
    os.makedirs(frame_dir, exist_ok=True)
    os.makedirs(thumb_dir, exist_ok=True)

    frame_path = os.path.join(frame_dir, f"{timestamp}.jpg")
    thumb_path = os.path.join(thumb_dir, f"{timestamp}.jpg")

    # Abort early if frame file can't be written — don't insert orphaned DB record (#7)
    try:
        with open(frame_path, "wb") as f:
            f.write(snapshot_bytes)
    except OSError as exc:
        log.error("Project %d: failed to write frame file %s: %s", project_id, frame_path, exc)
        return

    # Reuse the already-decoded PIL image if available, otherwise decode now
    if pil_img is None:
        pil_img = Image.open(io.BytesIO(snapshot_bytes))
    loop = asyncio.get_event_loop()
    thumb_bytes = await loop.run_in_executor(None, generate_thumbnail_from_pillow, pil_img)
    with open(thumb_path, "wb") as f:
        f.write(thumb_bytes)

    file_size = len(snapshot_bytes)

    # --- 8. DB insert + frame_count -----------------------------------------
    now_utc = datetime.now(UTC).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO frames (project_id, captured_at, file_path, thumbnail_path, file_size,
                                is_dark, sharpness_score, is_blurry)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                now_utc,
                frame_path,
                thumb_path,
                file_size,
                is_dark,
                sharpness_score,
                is_blurry,
            ),
        )
        conn.execute(
            "UPDATE projects SET frame_count = frame_count + 1 WHERE id = ?",
            (project_id,),
        )
        # Upsert into pre-aggregated frame_stats
        frame_date = now_utc[:10]  # YYYY-MM-DD
        frame_hour = int(now_utc[11:13])
        conn.execute(
            """
            INSERT INTO frame_stats (project_id, date, hour, captured, dark)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(project_id, date, hour) DO UPDATE SET
                captured = captured + 1,
                dark = dark + excluded.dark
            """,
            (project_id, frame_date, frame_hour, is_dark),
        )
        new_count_row = conn.execute(
            "SELECT frame_count FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        conn.commit()

    new_count = new_count_row["frame_count"] if new_count_row else 0
    log.debug(
        "Project %d: frame saved (%s, is_dark=%d, count=%d)",
        project_id,
        timestamp,
        is_dark,
        new_count,
    )

    # Broadcast WebSocket event
    await broadcast(
        "capture_event",
        {
            "project_id": project_id,
            "frame_count": new_count,
            "is_dark": bool(is_dark),
            "timestamp": now_utc,
        },
    )

    # Broadcast disk update
    try:
        usage = shutil.disk_usage(settings.frames_path)
        await broadcast(
            "disk_update",
            {
                "free_gb": round(usage.free / 1024**3, 2),
                "total_gb": round(usage.total / 1024**3, 2),
            },
        )
    except Exception:
        pass

    # --- 9. max_frames check ------------------------------------------------
    if project["max_frames"] and new_count >= project["max_frames"]:
        log.info(
            "Project %d reached max_frames=%d, marking completed", project_id, project["max_frames"]
        )
        with get_connection() as conn:
            conn.execute("UPDATE projects SET status = 'completed' WHERE id = ?", (project_id,))
            conn.commit()
        await remove_project_job(project_id)
        from app.notifications import notify as _notify

        await _notify(
            event="project_completed",
            level="info",
            message=f"Project '{project['name']}' reached {project['max_frames']} frames and was completed.",
            project_id=project_id,
        )


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _is_daylight() -> bool:
    from astral import LocationInfo
    from astral.sun import sun

    tz, lat, lon = _get_location()
    city = LocationInfo(name="custom", region="custom", timezone=tz, latitude=lat, longitude=lon)
    now = datetime.now(UTC)
    try:
        s = sun(city.observer, date=now.date(), tzinfo=tz)
        return s["sunrise"] <= now <= s["sunset"]
    except Exception:
        # If astral fails (e.g. polar night), allow capture
        return True


def _get_location() -> tuple[str, float, float]:
    """Return (tz, lat, lon) merged from DB overrides and env settings."""
    settings = get_settings()
    overrides = get_db_overrides()
    tz: str = overrides.get("tz") or settings.tz  # type: ignore[assignment]
    lat: float = overrides.get("latitude") or settings.latitude  # type: ignore[assignment]
    lon: float = overrides.get("longitude") or settings.longitude  # type: ignore[assignment]
    return tz, lat, lon


def _is_solar_noon_window(project: dict) -> bool:
    """Return True if now is within ±window minutes of today's solar noon.

    Solar noon mode is designed to capture exactly one frame per day at the
    moment of peak sun, giving consistent light conditions across all frames
    regardless of season. The scheduler runs every minute; this gate fires
    only during the configured window so exactly one capture occurs per day.
    """
    from astral import LocationInfo
    from astral.sun import sun

    tz_name, lat, lon = _get_location()
    window = int(project.get("solar_noon_window_minutes") or 30)

    try:
        city = LocationInfo(
            name="custom", region="custom", timezone=tz_name, latitude=lat, longitude=lon
        )
        now = datetime.now(UTC)
        s = sun(city.observer, date=now.date(), tzinfo=tz_name)
        noon = s["noon"]
        diff_minutes = abs((now - noon).total_seconds()) / 60
        return diff_minutes <= window
    except Exception:
        return False


def _is_in_schedule(project: dict, tz_name: str) -> bool:
    import zoneinfo

    tz = zoneinfo.ZoneInfo(tz_name)
    local_now = datetime.now(tz)
    weekday = local_now.isoweekday()  # 1=Mon … 7=Sun

    allowed_days = {
        int(d) for d in (project.get("schedule_days") or "1,2,3,4,5").split(",") if d.strip()
    }
    if weekday not in allowed_days:
        return False

    start_str = project.get("schedule_start_time") or "00:00"
    end_str = project.get("schedule_end_time") or "23:59"
    # Validate time format to prevent ValueError crash on malformed values (#16)
    try:
        start_parts = start_str.split(":")
        end_parts = end_str.split(":")
        start_h, start_m = int(start_parts[0]), int(start_parts[1])
        end_h, end_m = int(end_parts[0]), int(end_parts[1])
        if not (0 <= start_h <= 23 and 0 <= start_m <= 59):
            raise ValueError(f"Invalid start time: {start_str!r}")
        if not (0 <= end_h <= 23 and 0 <= end_m <= 59):
            raise ValueError(f"Invalid end time: {end_str!r}")
    except (ValueError, IndexError) as exc:
        log.warning(
            "Project %d: invalid schedule time (%s), allowing capture", project.get("id"), exc
        )
        return True  # fail open — allow capture rather than crash

    current_minutes = local_now.hour * 60 + local_now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    return start_minutes <= current_minutes <= end_minutes


def _check_capture_mode(project: dict) -> bool:
    """Return True if the project's capture mode would allow a capture right now.

    Used by the schedule-test endpoint (F8) to preview capture behaviour.
    """
    mode = project.get("capture_mode", "continuous")
    if mode == "daylight_only":
        return _is_daylight()
    if mode == "solar_noon":
        return _is_solar_noon_window(project)
    if mode == "schedule":
        tz_name, _, _ = _get_location()
        return _is_in_schedule(project, tz_name)
    # continuous or unknown — always capture
    return True


def _increment_failures(project_id: int) -> int:
    with get_connection() as conn:
        conn.execute(
            "UPDATE projects SET consecutive_failures = consecutive_failures + 1 WHERE id = ?",
            (project_id,),
        )
        row = conn.execute(
            "SELECT consecutive_failures FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        conn.commit()
    return row["consecutive_failures"] if row else 0


def _reset_failures(project_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE projects SET consecutive_failures = 0 WHERE id = ?", (project_id,))
        conn.commit()


async def _handle_disk_breach(free_gb: float, threshold_gb: int) -> None:
    log.error(
        "Disk space critical: %.2f GB free (threshold: %d GB) — pausing all projects",
        free_gb,
        threshold_gb,
    )
    with get_connection() as conn:
        conn.execute("UPDATE projects SET status = 'paused_error' WHERE status = 'active'")
        conn.commit()
    if scheduler.running:
        scheduler.pause()
    from app.notifications import notify as _notify

    await _notify(
        event="storage_critical",
        level="error",
        message=f"Capture paused: Less than {threshold_gb}GB free on /data (free: {free_gb:.2f}GB).",
        details={"free_gb": round(free_gb, 2)},
    )
    await broadcast("disk_update", {"free_gb": round(free_gb, 2), "critical": True})


async def _notify_nvr_offline(project_id: int, project_name: str, failures: int) -> None:
    from app.notifications import notify as _notify

    await _notify(
        event="nvr_offline",
        level="warning",
        message=f"Camera for project '{project_name}' has failed {failures} consecutive times.",
        project_id=project_id,
    )


# -------------------------------------------------------------------------
# Historical extraction
# -------------------------------------------------------------------------


async def run_historical_extraction(project_id: int) -> None:
    """
    Background task: download 1-hour NVR video chunks, extract frames via
    ffmpeg, rename to UTC timestamps, generate thumbnails, then mark done.
    """
    try:
        await _run_historical_extraction_inner(project_id)
    except Exception as exc:
        log.exception("Historical extraction project %d crashed: %s", project_id, exc)
        _set_project_status(project_id, "error")


async def _run_historical_extraction_inner(project_id: int) -> None:
    settings = get_settings()

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        log.error("Historical extraction: project %d not found", project_id)
        return

    project = dict(row)

    if not project.get("start_date") or not project.get("end_date"):
        log.error("Historical extraction: project %d missing start_date or end_date", project_id)
        _set_project_status(project_id, "error")
        return

    _set_project_status(project_id, "extracting")

    start_dt = datetime.fromisoformat(project["start_date"]).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(project["end_date"]).replace(tzinfo=UTC)

    if end_dt <= start_dt:
        log.error("Historical extraction: project %d end_date is not after start_date", project_id)
        _set_project_status(project_id, "error")
        return

    interval = project["interval_seconds"]

    frame_dir = os.path.join(settings.frames_path, str(project_id))
    thumb_dir = os.path.join(settings.thumbnails_path, str(project_id))
    os.makedirs(frame_dir, exist_ok=True)
    os.makedirs(thumb_dir, exist_ok=True)

    try:
        client = await protect_manager.get_client()
        cam = client.bootstrap.cameras.get(project["camera_id"])
        if cam is None:
            log.error("Historical extraction: camera %s not found", project["camera_id"])
            _set_project_status(project_id, "error")
            return
    except RuntimeError as exc:
        log.error("Historical extraction: NVR unavailable — %s", exc)
        _set_project_status(project_id, "error")
        return

    chunk_start = start_dt
    chunk_index = 0
    total_frames = 0
    total_duration_h = max(1, (end_dt - start_dt).total_seconds() / 3600)

    while chunk_start < end_dt:
        chunk_end = min(chunk_start + timedelta(hours=1), end_dt)
        elapsed_h = (chunk_start - start_dt).total_seconds() / 3600
        progress_pct = int(elapsed_h / total_duration_h * 100)
        await broadcast(
            "extraction_progress",
            {"project_id": project_id, "progress_pct": progress_pct, "frames": total_frames},
        )

        with tempfile.NamedTemporaryFile(
            suffix=".mp4", prefix=f"hist_{project_id}_{chunk_index}_", delete=False
        ) as tmp:
            tmp_path = tmp.name

        try:
            video_bytes = await cam.get_video(chunk_start, chunk_end)
            if video_bytes is None:
                raise RuntimeError("NVR returned empty video chunk")
            with open(tmp_path, "wb") as f:
                f.write(video_bytes)

            # Extract frames via ffmpeg
            extract_dir = os.path.join(frame_dir, f"_tmp_chunk_{chunk_index}")
            os.makedirs(extract_dir, exist_ok=True)

            cmd = [
                "ffmpeg",
                "-y",
                "-threads",
                str(settings.ffmpeg_threads),
                "-i",
                tmp_path,
                "-vf",
                f"fps=1/{interval}",
                "-q:v",
                "2",
                os.path.join(extract_dir, "%014d.jpg"),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=settings.ffmpeg_timeout_seconds
            )
            if proc.returncode != 0:
                log.error(
                    "Historical extraction ffmpeg failed: %s", stderr.decode(errors="replace")
                )
                continue

            # Rename extracted files to UTC timestamps and generate thumbnails
            extracted = sorted(os.listdir(extract_dir))
            for frame_index, filename in enumerate(extracted):
                src = os.path.join(extract_dir, filename)
                frame_utc = chunk_start + timedelta(seconds=frame_index * interval)
                ts = frame_utc.strftime("%Y%m%d%H%M%S")
                dst = os.path.join(frame_dir, f"{ts}.jpg")
                try:
                    os.rename(src, dst)
                except OSError as exc:
                    log.warning("Historical extraction: rename failed %s → %s: %s", src, dst, exc)
                    continue  # skip frame rather than crashing the chunk (#9)

                with open(dst, "rb") as fh:
                    img_bytes = fh.read()
                loop = asyncio.get_event_loop()
                thumb_bytes = await loop.run_in_executor(None, generate_thumbnail, img_bytes)
                thumb_path = os.path.join(thumb_dir, f"{ts}.jpg")
                with open(thumb_path, "wb") as fh:
                    fh.write(thumb_bytes)

                file_size = os.path.getsize(dst)
                captured_at = frame_utc.isoformat()
                with get_connection() as conn:
                    conn.execute(
                        "INSERT INTO frames (project_id, captured_at, file_path, thumbnail_path, file_size) VALUES (?,?,?,?,?)",
                        (project_id, captured_at, dst, thumb_path, file_size),
                    )
                    conn.execute(
                        "UPDATE projects SET frame_count = frame_count + 1 WHERE id = ?",
                        (project_id,),
                    )
                    conn.commit()
                total_frames += 1

            with contextlib.suppress(Exception):
                shutil.rmtree(extract_dir)

        except TimeoutError:
            log.error("Historical extraction: ffmpeg timed out on chunk %d", chunk_index)
        except Exception as exc:
            log.error("Historical extraction error on chunk %d: %s", chunk_index, exc)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)

        chunk_start = chunk_end
        chunk_index += 1

    _set_project_status(project_id, "completed")
    await broadcast(
        "extraction_progress",
        {"project_id": project_id, "progress_pct": 100, "frames": total_frames},
    )
    log.info("Historical extraction complete: project %d, %d frames", project_id, total_frames)


# Config shorthand used in helpers (avoid circular import at module level)
def _get_disk_threshold() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT disk_warning_threshold_gb FROM settings WHERE id = 1").fetchone()
    return row["disk_warning_threshold_gb"] if row else 5
