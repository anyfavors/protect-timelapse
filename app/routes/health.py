import shutil

from fastapi import APIRouter

from app.protect import protect_manager

router = APIRouter(tags=["health"])


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
