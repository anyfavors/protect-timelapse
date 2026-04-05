import os
import shutil

from fastapi import APIRouter

from app.config import get_settings
from app.database import get_connection
from app.protect import protect_manager

router = APIRouter(tags=["health"])


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


@router.get("/api/disk")
def disk_breakdown() -> dict:
    """Per-project disk usage breakdown across frames, renders, and thumbs."""
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
    return {
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "projects": breakdown,
    }
