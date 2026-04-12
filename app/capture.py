"""
Capture worker — APScheduler-based snapshot ingestion.

Each active project maps to one APScheduler job (id = 'project_{id}').
The snapshot_worker() runs the full visibility pipeline before saving a frame.
"""

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import shutil
import time
import zoneinfo
from datetime import UTC, datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from PIL import Image, ImageFilter, ImageStat

from app.config import get_settings
from app.database import get_connection, get_db_overrides
from app.protect import protect_manager
from app.thumbnails import generate_thumbnail_from_pillow
from app.websocket import broadcast

log = logging.getLogger("app.capture")

# Cached LocationInfo — rebuilt lazily when location settings change
_location_info_cache: tuple[str, float, float, object] | None = None  # (tz, lat, lon, LocationInfo)

scheduler = AsyncIOScheduler()

# Default Laplacian variance threshold below which a frame is flagged as blurry.
_BLUR_THRESHOLD = 20.0

# Circuit breaker: auto-pause a project after this many consecutive capture failures.
_CIRCUIT_BREAKER_THRESHOLD = 10

# Per-camera semaphore: at most 2 concurrent snapshots per camera to avoid NVR overload.
_camera_semaphores: dict[str, asyncio.Semaphore] = {}

# Disk check throttle: at most one shutil.disk_usage() call per minute across all projects.
_DISK_CHECK_INTERVAL = 60.0  # seconds
_disk_last_checked: float = 0.0
_disk_last_result: tuple[float, float] | None = None  # (free_gb, threshold_gb as floats)

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

    # Periodic NVR health check — detects disconnect/reconnect, broadcasts status
    scheduler.add_job(
        _nvr_health_check_job,
        trigger=IntervalTrigger(seconds=60),
        id="nvr_health_check",
        replace_existing=True,
        max_instances=1,
    )

    # Periodic bootstrap refresh — picks up new/removed cameras
    scheduler.add_job(
        _nvr_bootstrap_refresh_job,
        trigger=IntervalTrigger(seconds=300),
        id="nvr_bootstrap_refresh",
        replace_existing=True,
        max_instances=1,
    )

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

    # --- 1. Disk failsafe (throttled: at most once per minute) ---------------
    try:
        global _disk_last_checked, _disk_last_result
        now_mono = time.monotonic()
        if now_mono - _disk_last_checked >= _DISK_CHECK_INTERVAL:
            loop = asyncio.get_running_loop()
            usage = await loop.run_in_executor(None, shutil.disk_usage, settings.frames_path)
            free_gb = usage.free / 1024**3
            threshold_gb = float(_get_disk_threshold())
            _disk_last_checked = now_mono
            _disk_last_result = (free_gb, threshold_gb)
        elif _disk_last_result is not None:
            free_gb, threshold_gb = _disk_last_result
        else:
            # First call ever — force a synchronous check rather than blindly allowing capture (B5)
            try:
                usage = shutil.disk_usage(settings.frames_path)
                free_gb = usage.free / 1024**3
                threshold_gb = float(_get_disk_threshold())
                _disk_last_checked = time.monotonic()
                _disk_last_result = (free_gb, threshold_gb)
            except OSError:
                free_gb, threshold_gb = float("inf"), 0.0
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
        client = await asyncio.wait_for(protect_manager.get_client(), timeout=30)
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
        sem = _camera_semaphores.setdefault(camera_id, asyncio.Semaphore(2))
        async with sem:
            raw = await cam.get_snapshot(**kwargs)
        if raw is None:
            raise RuntimeError("NVR returned empty snapshot")
        snapshot_bytes: bytes = raw
        _reset_failures(project_id)

    except httpx.ReadTimeout as exc:
        # Retry once after a short delay for transient timeouts
        log.debug("Project %d: transient timeout, retrying once in 2s", project_id)
        try:
            await asyncio.sleep(2)
            client_retry = await protect_manager.get_client()
            cam_retry = client_retry.bootstrap.cameras.get(project["camera_id"])
            if cam_retry is None:
                raise RuntimeError("Camera not found on retry") from exc
            async with _camera_semaphores.get(project["camera_id"], asyncio.Semaphore(2)):
                raw_retry = await cam_retry.get_snapshot(**kwargs)
            if not raw_retry:
                raise RuntimeError("Empty snapshot on retry") from exc
            snapshot_bytes = raw_retry
            _reset_failures(project_id)
        except Exception as retry_exc:
            log.warning("Project %d: NVR snapshot failed after retry — %s", project_id, retry_exc)
            protect_manager.mark_disconnected(str(retry_exc))
            failures = _increment_failures(project_id)
            if failures >= 3:
                await _notify_nvr_offline(project_id, project["name"], failures)
            if failures >= _CIRCUIT_BREAKER_THRESHOLD:
                log.error(
                    "Project %d: circuit breaker tripped after %d failures — pausing",
                    project_id,
                    failures,
                )
                _set_project_status(project_id, "paused_error")
                await remove_project_job(project_id)
            return

    except (httpx.ConnectError, Exception) as exc:
        log.warning("Project %d: NVR snapshot failed — %s", project_id, exc)
        # Signal NVR disconnect so next get_client() attempts reconnect
        if isinstance(exc, httpx.ReadTimeout | httpx.ConnectError | RuntimeError):
            protect_manager.mark_disconnected(str(exc))
        failures = _increment_failures(project_id)
        if failures >= 3:
            await _notify_nvr_offline(project_id, project["name"], failures)
        # Circuit breaker: auto-pause after too many consecutive failures
        if failures >= _CIRCUIT_BREAKER_THRESHOLD:
            log.error(
                "Project %d: circuit breaker tripped after %d failures — pausing",
                project_id,
                failures,
            )
            _set_project_status(project_id, "paused_error")
            await remove_project_job(project_id)
            from app.notifications import notify as _notify

            await _notify(
                event="project_paused",
                level="error",
                message=(
                    f"Project '{project['name']}' auto-paused after {failures} "
                    f"consecutive capture failures. Last error: {exc}"
                ),
                project_id=project_id,
            )
            await broadcast(
                "project_status_change",
                {
                    "project_id": project_id,
                    "status": "paused_error",
                    "reason": f"Auto-paused after {failures} consecutive failures",
                },
            )
        return

    # --- 6. Luminance filter ------------------------------------------------
    is_dark = 0
    pil_img: Image.Image | None = None

    if project["use_luminance_check"]:
        pil_img = Image.open(io.BytesIO(snapshot_bytes))
        pil_img.load()  # force decode so BytesIO can be released
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
                with Image.open(last_row["file_path"]) as _last_raw:
                    last_img = _last_raw.convert("L").resize((64, 64))
                curr_gray = pil_img.convert("L").resize((64, 64))
                diff = (
                    sum(
                        abs(a - b)
                        for a, b in zip(last_img.tobytes(), curr_gray.tobytes(), strict=False)
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

    # Frame deduplication: check hash in same connection as insert to avoid TOCTOU race
    frame_hash = hashlib.sha256(snapshot_bytes).hexdigest()

    # Atomic frame write: write to .tmp then rename (crash-safe)
    # Abort early if frame file can't be written — don't insert orphaned DB record (#7)
    tmp_frame_path = frame_path + ".tmp"
    try:
        with open(tmp_frame_path, "wb") as f:
            f.write(snapshot_bytes)
        os.replace(tmp_frame_path, frame_path)
    except OSError as exc:
        log.error("Project %d: failed to write frame file %s: %s", project_id, frame_path, exc)
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_frame_path)
        return

    # Reuse the already-decoded PIL image if available, otherwise decode now
    if pil_img is None:
        pil_img = Image.open(io.BytesIO(snapshot_bytes))
    loop = asyncio.get_running_loop()
    thumb_bytes = await loop.run_in_executor(None, generate_thumbnail_from_pillow, pil_img)
    tmp_thumb_path = thumb_path + ".tmp"
    try:
        with open(tmp_thumb_path, "wb") as f:
            f.write(thumb_bytes)
        os.replace(tmp_thumb_path, thumb_path)
    except OSError:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_thumb_path)
        thumb_path = frame_path  # fall back to original

    file_size = len(snapshot_bytes)

    # --- 8. DB insert + frame_count (single atomic transaction) --------------
    now_utc = datetime.now(UTC).isoformat()
    frame_date = now_utc[:10]  # YYYY-MM-DD
    frame_hour = int(now_utc[11:13])
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # INSERT OR IGNORE handles dedup atomically — if file_hash already exists for this
        # project, the insert is skipped and rowcount == 0 (no separate SELECT needed).
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO frames
                (project_id, captured_at, file_path, thumbnail_path, file_size,
                 is_dark, sharpness_score, is_blurry, file_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                frame_hash,
            ),
        )
        if cur.rowcount == 0:
            conn.rollback()
            log.debug("Project %d: duplicate frame skipped (hash=%s)", project_id, frame_hash[:16])
            return
        conn.execute(
            "UPDATE projects SET frame_count = frame_count + 1 WHERE id = ?",
            (project_id,),
        )
        # Upsert into pre-aggregated frame_stats
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
# NVR periodic jobs
# -------------------------------------------------------------------------


async def _nvr_health_check_job() -> None:
    """Periodic health check — broadcast NVR status changes via WS."""
    status = await protect_manager.health_check()
    await broadcast("nvr_status", status)


async def _nvr_bootstrap_refresh_job() -> None:
    """Periodic bootstrap refresh — picks up added/removed cameras.
    Also prunes semaphores for cameras no longer in the NVR bootstrap."""
    await protect_manager.refresh_bootstrap()
    # Remove semaphores for cameras that no longer exist to prevent unbounded growth
    try:
        client = await protect_manager.get_client()
        known_ids = set(client.bootstrap.cameras.keys())
        for stale_id in list(_camera_semaphores.keys()):
            if stale_id not in known_ids:
                _camera_semaphores.pop(stale_id, None)
                log.debug("Pruned semaphore for removed camera %s", stale_id)
    except Exception:
        pass  # Non-fatal — semaphores will be pruned on next successful refresh


def get_scheduler_status() -> dict:
    """Return scheduler state for the system status endpoint."""
    jobs = []
    if scheduler.running:
        for job in scheduler.get_jobs():
            jobs.append({"id": job.id, "next_run": str(job.next_run_time)})
    return {
        "running": scheduler.running,
        "job_count": len(jobs),
        "jobs": jobs,
    }


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _get_location_info() -> "tuple[str, object]":
    """Return (tz_name, LocationInfo) using a lazy cache invalidated when settings change."""
    global _location_info_cache
    from astral import LocationInfo

    tz, lat, lon = _get_location()
    if (
        _location_info_cache is None
        or _location_info_cache[0] != tz
        or _location_info_cache[1] != lat
        or _location_info_cache[2] != lon
    ):
        city = LocationInfo(
            name="custom", region="custom", timezone=tz, latitude=lat, longitude=lon
        )
        _location_info_cache = (tz, lat, lon, city)
    return _location_info_cache[0], _location_info_cache[3]


def invalidate_location_cache() -> None:
    """Call this after settings update to force LocationInfo rebuild on next use."""
    global _location_info_cache
    _location_info_cache = None


def _is_daylight(now: datetime | None = None) -> bool:
    from astral.sun import sun

    tz, city = _get_location_info()
    if now is None:
        now = datetime.now(UTC)
    try:
        s = sun(city.observer, date=now.date(), tzinfo=tz)  # type: ignore[union-attr,attr-defined]
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


def _is_solar_noon_window(project: dict, now: datetime | None = None) -> bool:
    """Return True if now is within ±window minutes of today's solar noon.

    Solar noon mode is designed to capture exactly one frame per day at the
    moment of peak sun, giving consistent light conditions across all frames
    regardless of season. The scheduler runs every minute; this gate fires
    only during the configured window so exactly one capture occurs per day.
    """
    from astral.sun import sun

    tz_name, city = _get_location_info()
    window = int(project.get("solar_noon_window_minutes") or 30)

    try:
        if now is None:
            now = datetime.now(UTC)
        s = sun(city.observer, date=now.date(), tzinfo=tz_name)  # type: ignore[union-attr,attr-defined]
        noon = s["noon"]
        diff_minutes = abs((now - noon).total_seconds()) / 60
        return diff_minutes <= window
    except Exception:
        return False


def _is_in_schedule(project: dict, tz_name: str, now: datetime | None = None) -> bool:
    tz = zoneinfo.ZoneInfo(tz_name)
    local_now = now.astimezone(tz) if now is not None else datetime.now(tz)
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


def _check_capture_mode(project: dict, now: datetime | None = None) -> bool:
    """Return True if the project's capture mode would allow a capture right now.

    Used by the schedule-test endpoint (F8) to preview capture behaviour.
    Pass `now` to test a specific point in time without patching datetime.
    """
    mode = project.get("capture_mode", "continuous")
    if mode == "daylight_only":
        return _is_daylight(now=now)
    if mode == "solar_noon":
        return _is_solar_noon_window(project, now=now)
    if mode == "schedule":
        tz_name, _, _ = _get_location()
        return _is_in_schedule(project, tz_name, now=now)
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


async def _handle_disk_breach(free_gb: float, threshold_gb: float) -> None:
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
        await broadcast(
            "extraction_progress",
            {"project_id": project_id, "progress_pct": -1, "error": str(exc)},
        )


async def _run_historical_extraction_inner(project_id: int) -> None:  # pragma: no cover
    settings = get_settings()

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        log.error("Historical extraction: project %d not found", project_id)
        return

    project = dict(row)

    if not project.get("start_date") or not project.get("end_date"):
        error_msg = "Missing start_date or end_date"
        log.error("Historical extraction: project %d %s", project_id, error_msg)
        _set_project_status(project_id, "error")
        await broadcast(
            "extraction_progress",
            {"project_id": project_id, "progress_pct": -1, "error": error_msg},
        )
        return

    _set_project_status(project_id, "extracting")

    # Dates from the frontend are in the user's local time (datetime-local input).
    # Parse as local time, then convert to UTC for the NVR API.
    tz_name, _, _ = _get_location()
    local_tz = zoneinfo.ZoneInfo(tz_name)

    raw_start = datetime.fromisoformat(project["start_date"])
    raw_end = datetime.fromisoformat(project["end_date"])

    # If the datetime already has tzinfo (e.g. stored with Z suffix), use it as-is.
    # Otherwise treat it as local time and convert to UTC.
    if raw_start.tzinfo is None:
        start_dt = raw_start.replace(tzinfo=local_tz).astimezone(UTC)
    else:
        start_dt = raw_start.astimezone(UTC)

    if raw_end.tzinfo is None:
        end_dt = raw_end.replace(tzinfo=local_tz).astimezone(UTC)
    else:
        end_dt = raw_end.astimezone(UTC)

    if end_dt <= start_dt:
        error_msg = "end_date must be after start_date"
        log.error("Historical extraction: project %d %s", project_id, error_msg)
        _set_project_status(project_id, "error")
        await broadcast(
            "extraction_progress",
            {"project_id": project_id, "progress_pct": -1, "error": error_msg},
        )
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
            error_msg = f"Camera {project['camera_id']} not found on NVR"
            log.error("Historical extraction: %s", error_msg)
            _set_project_status(project_id, "error")
            await broadcast(
                "extraction_progress",
                {"project_id": project_id, "progress_pct": -1, "error": error_msg},
            )
            return
    except RuntimeError as exc:
        log.error("Historical extraction: NVR unavailable — %s", exc)
        _set_project_status(project_id, "error")
        await broadcast(
            "extraction_progress",
            {"project_id": project_id, "progress_pct": -1, "error": f"NVR unavailable: {exc}"},
        )
        return

    # ── Build list of timestamps to extract ──────────────────────────────
    # One snapshot every `interval` seconds from start to end.
    all_timestamps: list[datetime] = []
    cursor = start_dt
    while cursor < end_dt:
        all_timestamps.append(cursor)
        cursor += timedelta(seconds=interval)

    if not all_timestamps:
        error_msg = "Time range too short for the configured interval"
        log.error("Historical extraction project %d: %s", project_id, error_msg)
        _set_project_status(project_id, "error")
        await broadcast(
            "extraction_progress",
            {"project_id": project_id, "progress_pct": -1, "error": error_msg},
        )
        return

    # ── Resume support: skip timestamps that already have frames ──────
    # Use bounded query to avoid full-table scan on large projects (B21)
    range_start_iso = all_timestamps[0].isoformat()
    range_end_iso = all_timestamps[-1].isoformat()
    with get_connection() as conn:
        existing_rows = conn.execute(
            "SELECT captured_at FROM frames WHERE project_id = ? AND captured_at >= ? AND captured_at <= ?",
            (project_id, range_start_iso, range_end_iso),
        ).fetchall()
    existing_ts = {row["captured_at"] for row in existing_rows}

    timestamps = [ts for ts in all_timestamps if ts.isoformat() not in existing_ts]

    # ── Apply daylight filter on resume (fix: don't re-apply time-window filter) ──
    # Only filter timestamps that fall outside daylight if capture_mode requires it.
    # We check by actual sun times, not by current time, so resume is correct.
    if project.get("capture_mode") == "daylight_only":
        from astral.sun import sun as _astral_sun

        tz_name_hist, city_hist = _get_location_info()
        filtered_ts: list[datetime] = []
        for _ts in timestamps:
            with contextlib.suppress(Exception):
                _s = _astral_sun(city_hist.observer, date=_ts.date(), tzinfo=tz_name_hist)  # type: ignore[union-attr,attr-defined]
                if _s["sunrise"] <= _ts <= _s["sunset"]:
                    filtered_ts.append(_ts)
                continue
            filtered_ts.append(_ts)  # fail open
        timestamps = filtered_ts
    already_done = len(all_timestamps) - len(timestamps)
    total_expected = len(timestamps)

    if total_expected == 0:
        log.info(
            "Historical extraction project %d: all %d frames already extracted",
            project_id,
            already_done,
        )
        _set_project_status(project_id, "completed")
        await broadcast(
            "extraction_progress",
            {
                "project_id": project_id,
                "progress_pct": 100,
                "frames": already_done,
            },
        )
        return

    log.info(
        "Historical extraction project %d: %d snapshots to fetch "
        "(%d already done, %s → %s, every %ds)",
        project_id,
        total_expected,
        already_done,
        start_dt.isoformat(),
        end_dt.isoformat(),
        interval,
    )

    # ── Fetch historical snapshots directly from NVR ──────────────────
    # Uses camera.get_snapshot(dt=...) which hits the NVR's
    # "recording-snapshot" endpoint — returns a single JPEG per timestamp.
    # No video download, no ffmpeg, no temp files.
    _SNAPSHOT_TIMEOUT = 30  # seconds per snapshot request
    _BATCH_SIZE = 100  # frames per DB batch insert
    total_frames = 0
    consecutive_errors = 0

    # Accumulate batch rows before DB insert
    _batch: list[tuple] = []

    # Resolution kwargs (same as live capture)
    snap_kwargs: dict = {}
    if project["width"]:
        snap_kwargs["width"] = project["width"]
    if project["height"]:
        snap_kwargs["height"] = project["height"]

    async def _flush_batch() -> None:
        nonlocal total_frames
        if not _batch:
            return
        with get_connection() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO frames "
                "(project_id, captured_at, file_path, thumbnail_path, file_size, "
                "sharpness_score, is_blurry, file_hash) "
                "VALUES (?,?,?,?,?,?,?,?)",
                _batch,
            )
            conn.execute(
                "UPDATE projects SET frame_count = frame_count + ? WHERE id = ?",
                (len(_batch), project_id),
            )
            # Upsert frame_stats for each batch row
            for _b in _batch:
                _cat = _b[1]  # captured_at isostring
                _is_dark_val = 0  # historical frames not luminance-checked
                _fdate = _cat[:10]
                _fhour = int(_cat[11:13]) if len(_cat) >= 13 else 0
                conn.execute(
                    """
                    INSERT INTO frame_stats (project_id, date, hour, captured, dark)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(project_id, date, hour) DO UPDATE SET
                        captured = captured + 1,
                        dark = dark + excluded.dark
                    """,
                    (project_id, _fdate, _fhour, _is_dark_val),
                )
            conn.commit()
        total_frames += len(_batch)
        _batch.clear()

    for idx, frame_dt in enumerate(timestamps):
        progress_pct = int(idx / total_expected * 100)

        # Broadcast progress every 10 frames to avoid WS spam
        if idx % 10 == 0:
            await broadcast(
                "extraction_progress",
                {
                    "project_id": project_id,
                    "progress_pct": progress_pct,
                    "frames": total_frames,
                    "current": idx + 1,
                    "total_expected": total_expected,
                },
            )

        try:
            snapshot_bytes = await asyncio.wait_for(
                cam.get_snapshot(dt=frame_dt, **snap_kwargs),
                timeout=_SNAPSHOT_TIMEOUT,
            )
            if not snapshot_bytes:
                log.debug(
                    "Historical extraction project %d: empty snapshot at %s, skipping",
                    project_id,
                    frame_dt.isoformat(),
                )
                consecutive_errors += 1
                if consecutive_errors >= 20:
                    await _flush_batch()
                    error_msg = (
                        f"Aborted after {consecutive_errors} consecutive empty/failed snapshots. "
                        "NVR may not have recordings for this time range."
                    )
                    log.error("Historical extraction project %d: %s", project_id, error_msg)
                    _set_project_status(project_id, "error")
                    await broadcast(
                        "extraction_progress",
                        {"project_id": project_id, "progress_pct": -1, "error": error_msg},
                    )
                    return
                continue

            consecutive_errors = 0

            ts = frame_dt.strftime("%Y%m%d%H%M%S")
            frame_path = os.path.join(frame_dir, f"{ts}.jpg")
            thumb_path = os.path.join(thumb_dir, f"{ts}.jpg")

            # Atomic write: .tmp → rename
            tmp_frame = frame_path + ".tmp"
            try:
                with open(tmp_frame, "wb") as f:
                    f.write(snapshot_bytes)
                os.replace(tmp_frame, frame_path)
            except OSError as exc:
                log.warning(
                    "Historical extraction project %d: write failed at %s: %s",
                    project_id,
                    frame_dt.isoformat(),
                    exc,
                )
                with contextlib.suppress(FileNotFoundError):
                    os.remove(tmp_frame)
                consecutive_errors += 1
                continue

            # Generate thumbnail (atomic)
            pil_img = Image.open(io.BytesIO(snapshot_bytes))
            loop = asyncio.get_event_loop()
            thumb_bytes = await loop.run_in_executor(None, generate_thumbnail_from_pillow, pil_img)
            tmp_thumb = thumb_path + ".tmp"
            try:
                with open(tmp_thumb, "wb") as f:
                    f.write(thumb_bytes)
                os.replace(tmp_thumb, thumb_path)
            except OSError:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(tmp_thumb)
                thumb_path = frame_path

            # Blur/sharpness detection (same pipeline as live capture)
            gray_for_sharp = pil_img.convert("L")
            edges = gray_for_sharp.filter(ImageFilter.FIND_EDGES)
            sharpness_score = float(ImageStat.Stat(edges).var[0])
            is_blurry = 1 if sharpness_score < _BLUR_THRESHOLD else 0

            file_size = len(snapshot_bytes)
            frame_hash = hashlib.sha256(snapshot_bytes).hexdigest()
            captured_at = frame_dt.isoformat()

            _batch.append(
                (
                    project_id,
                    captured_at,
                    frame_path,
                    thumb_path,
                    file_size,
                    sharpness_score,
                    is_blurry,
                    frame_hash,
                )
            )

            if len(_batch) >= _BATCH_SIZE:
                await _flush_batch()

        except TimeoutError:
            log.warning(
                "Historical extraction project %d: snapshot timed out at %s",
                project_id,
                frame_dt.isoformat(),
            )
            consecutive_errors += 1
        except Exception as exc:
            log.warning(
                "Historical extraction project %d: snapshot failed at %s: %s",
                project_id,
                frame_dt.isoformat(),
                exc,
            )
            consecutive_errors += 1

        if consecutive_errors >= 20:
            await _flush_batch()
            error_msg = (
                f"Aborted after {consecutive_errors} consecutive failures at "
                f"{frame_dt.isoformat()}. Last frame fetched: {total_frames}."
            )
            log.error("Historical extraction project %d: %s", project_id, error_msg)
            _set_project_status(project_id, "error")
            await broadcast(
                "extraction_progress",
                {"project_id": project_id, "progress_pct": -1, "error": error_msg},
            )
            return

        # Yield to event loop periodically to avoid starving other tasks
        if idx % 5 == 0:
            await asyncio.sleep(0)

    # Flush any remaining batch
    await _flush_batch()

    # ── Finalize ──────────────────────────────────────────────────────
    if total_frames == 0:
        error_msg = (
            f"Extracted 0 frames out of {total_expected} attempts. "
            "The NVR may not have recordings for this time range."
        )
        log.error("Historical extraction project %d: %s", project_id, error_msg)
        _set_project_status(project_id, "error")
        await broadcast(
            "extraction_progress",
            {"project_id": project_id, "progress_pct": -1, "error": error_msg},
        )
        return

    _set_project_status(project_id, "completed")
    await broadcast(
        "extraction_progress",
        {"project_id": project_id, "progress_pct": 100, "frames": total_frames},
    )
    log.info(
        "Historical extraction complete: project %d, %d/%d frames",
        project_id,
        total_frames,
        total_expected,
    )


# Config shorthand used in helpers (avoid circular import at module level)
def _get_disk_threshold() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT disk_warning_threshold_gb FROM settings WHERE id = 1").fetchone()
    return row["disk_warning_threshold_gb"] if row else 5
