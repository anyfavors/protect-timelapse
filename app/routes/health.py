import os
import shutil
import time

from fastapi import APIRouter
from fastapi.responses import Response

from app.config import get_settings
from app.database import _POOL_SIZE, _pool, get_connection
from app.protect import protect_manager

router = APIRouter(tags=["health"])

# Cache disk breakdown for 60s to avoid O(n*m) dir walk on every request (#19)
_disk_cache: dict | None = None
_disk_cache_ts: float = 0.0
_DISK_CACHE_TTL = 60.0

# Render worker heartbeat tracking (B2)
_last_render_worker_heartbeat: float = 0.0


def update_render_worker_heartbeat() -> None:
    global _last_render_worker_heartbeat
    _last_render_worker_heartbeat = time.monotonic()


def _dir_size_gb(path: str) -> float:
    """Return total size of *path* directory in gigabytes."""
    total = 0
    if not os.path.isdir(path):
        return 0.0
    for entry in os.scandir(path):
        if entry.is_file(follow_symlinks=False):
            total += entry.stat().st_size
        elif entry.is_dir(follow_symlinks=False):
            total += int(_dir_size_gb(entry.path) * 1024**3)
    return total / 1024**3


@router.get("/api/health")
async def health() -> dict:
    try:
        usage = shutil.disk_usage("/data")
        free_gb = round(usage.free / 1024**3, 2)
        total_gb = round(usage.total / 1024**3, 2)
    except FileNotFoundError:
        # /data may not exist outside Docker
        free_gb = -1.0
        total_gb = -1.0

    return {
        "status": "ok",
        "nvr_connected": protect_manager.is_connected,
        "disk_free_gb": free_gb,
        "disk_total_gb": total_gb,
    }


@router.get("/api/health/live")
async def liveness() -> Response:
    """Liveness probe: 200 if render worker polled in last 120s (B2)."""
    now = time.monotonic()
    # First 120s after startup — always healthy (worker may not have polled yet)
    if _last_render_worker_heartbeat == 0.0 or (now - _last_render_worker_heartbeat) < 120.0:
        return Response(content='{"status":"ok"}', media_type="application/json", status_code=200)
    return Response(
        content='{"status":"unhealthy","reason":"render_worker_stalled"}',
        media_type="application/json",
        status_code=503,
    )


@router.get("/api/health/ready")
async def readiness() -> Response:
    """Readiness probe: 200 if NVR is connected (B2)."""
    if protect_manager.is_connected:
        return Response(content='{"status":"ok"}', media_type="application/json", status_code=200)
    return Response(
        content='{"status":"not_ready","reason":"nvr_disconnected"}',
        media_type="application/json",
        status_code=503,
    )


@router.get("/api/admin/pool-stats")
def pool_stats() -> dict:
    """Connection pool diagnostics (B10)."""
    idle = _pool.qsize()
    return {
        "pool_size": _POOL_SIZE,
        "idle_connections": idle,
        "active_connections": _POOL_SIZE - idle,
    }


@router.get("/api/system/status")
def system_status() -> dict:
    """Aggregated system status for the dashboard UI."""
    from app.capture import get_scheduler_status
    from app.database import get_wal_size_bytes

    settings = get_settings()

    # Disk
    try:
        usage = shutil.disk_usage(settings.frames_path)
        disk = {
            "free_gb": round(usage.free / 1024**3, 2),
            "total_gb": round(usage.total / 1024**3, 2),
        }
    except FileNotFoundError:
        disk = {"free_gb": -1, "total_gb": -1}

    # Render worker
    now = time.monotonic()
    if _last_render_worker_heartbeat == 0.0:
        render_worker = {"alive": True, "last_heartbeat_age_s": 0}
    else:
        age = now - _last_render_worker_heartbeat
        render_worker = {"alive": age < 120, "last_heartbeat_age_s": int(age)}

    # DB
    wal_size = get_wal_size_bytes()

    # Project summary
    with get_connection() as conn:
        project_counts = conn.execute(
            """
            SELECT status, COUNT(*) as cnt FROM projects GROUP BY status
            """
        ).fetchall()
        recent_errors = conn.execute(
            """
            SELECT id, event, level, message, created_at, project_id
            FROM notifications WHERE level IN ('error', 'warning')
            ORDER BY created_at DESC LIMIT 10
            """
        ).fetchall()
        pending_renders = conn.execute(
            "SELECT COUNT(*) as cnt FROM renders WHERE status IN ('pending', 'rendering')"
        ).fetchone()

    return {
        "nvr": protect_manager.status,
        "scheduler": get_scheduler_status(),
        "render_worker": render_worker,
        "disk": disk,
        "db": {"wal_size_bytes": wal_size, "wal_size_mb": round(wal_size / 1024 / 1024, 2)},
        "projects": {row["status"]: row["cnt"] for row in project_counts},
        "pending_renders": pending_renders["cnt"] if pending_renders else 0,
        "recent_errors": [dict(r) for r in recent_errors],
    }


@router.get("/api/disk")
def disk_breakdown() -> dict:
    """Per-project disk usage breakdown across frames, renders, and thumbs."""
    global _disk_cache, _disk_cache_ts
    now = time.monotonic()
    if _disk_cache is not None and (now - _disk_cache_ts) < _DISK_CACHE_TTL:
        return _disk_cache

    cfg = get_settings()
    try:
        usage = shutil.disk_usage("/data")
        total_gb = round(usage.total / 1024**3, 2)
        used_gb = round((usage.total - usage.free) / 1024**3, 2)
        free_gb = round(usage.free / 1024**3, 2)
    except FileNotFoundError:
        total_gb = used_gb = free_gb = 0.0

    with get_connection() as conn:
        projects = conn.execute("SELECT id, name FROM projects").fetchall()

    breakdown = []
    for proj in projects:
        pid = proj["id"]
        frames_gb = _dir_size_gb(os.path.join(cfg.frames_path, str(pid)))
        renders_gb = _dir_size_gb(os.path.join(cfg.renders_path, str(pid)))
        thumbs_gb = _dir_size_gb(os.path.join(cfg.thumbnails_path, str(pid)))
        breakdown.append(
            {
                "id": pid,
                "name": proj["name"],
                "frames_gb": round(frames_gb, 3),
                "renders_gb": round(renders_gb, 3),
                "thumbs_gb": round(thumbs_gb, 3),
                "total_gb": round(frames_gb + renders_gb + thumbs_gb, 3),
            }
        )

    breakdown.sort(key=lambda x: x["total_gb"], reverse=True)
    result = {
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "projects": breakdown,
    }
    _disk_cache = result
    _disk_cache_ts = now
    return result
