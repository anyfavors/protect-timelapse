"""
Camera routes — list NVR cameras and proxy live preview snapshots.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.limiter import limiter
from app.protect import protect_manager

router = APIRouter(prefix="/api", tags=["cameras"])
log = logging.getLogger("app.routes.cameras")


@router.get("/cameras")
async def list_cameras() -> list[dict]:
    """Return all cameras visible on the NVR."""
    try:
        client = await protect_manager.get_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    cameras = []
    for cam in client.bootstrap.cameras.values():
        cameras.append(
            {
                "id": cam.id,
                "name": cam.name,
                "type": cam.type,
                "is_online": cam.is_connected,
            }
        )
    return cameras


@router.get("/cameras/{camera_id}/preview")
@limiter.limit("10/minute")
async def camera_preview(request: Request, camera_id: str) -> Response:
    """Proxy a single low-res snapshot from the NVR for live FoV preview."""
    try:
        client = await protect_manager.get_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    cam = client.bootstrap.cameras.get(camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail=f"Camera {camera_id!r} not found")

    try:
        snapshot = await cam.get_snapshot(width=640)
    except Exception as exc:
        log.warning("Preview snapshot failed for camera %s: %s", camera_id, exc)
        raise HTTPException(status_code=503, detail="Could not fetch snapshot from NVR") from exc

    if snapshot is None:
        raise HTTPException(status_code=503, detail="Could not fetch snapshot from NVR")
    return Response(content=snapshot, media_type="image/jpeg")


@router.get("/cameras/{camera_id}/recording-range")
async def camera_recording_range(camera_id: str) -> dict:
    """Return the earliest and latest available recording timestamps for a camera.

    Used by the frontend to snap historical date pickers to valid ranges.
    Falls back gracefully if the NVR doesn't expose recording stats.
    """
    try:
        client = await protect_manager.get_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    cam = client.bootstrap.cameras.get(camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail=f"Camera {camera_id!r} not found")

    # uiprotect exposes recording stats via cam.stats (may not always be populated)
    earliest: str | None = None
    latest: str | None = None
    try:
        if hasattr(cam, "stats") and cam.stats:
            stats = cam.stats
            if hasattr(stats, "video") and stats.video:
                vstat = stats.video
                if hasattr(vstat, "recording_start") and vstat.recording_start:
                    earliest = vstat.recording_start.isoformat()
                if hasattr(vstat, "recording_end") and vstat.recording_end:
                    latest = vstat.recording_end.isoformat()
    except Exception as exc:
        log.debug("Could not read recording stats for camera %s: %s", camera_id, exc)

    return {
        "camera_id": camera_id,
        "earliest": earliest,
        "latest": latest,
        "available": earliest is not None and latest is not None,
    }
