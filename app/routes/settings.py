"""Global settings routes (single row, id=1)."""

import contextlib
import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import get_settings as _get_app_settings
from app.database import get_connection

router = APIRouter(prefix="/api", tags=["settings"])

_NVR_FIELDS = {"protect_host", "protect_port", "protect_verify_ssl"}


class SettingsUpdate(BaseModel):
    # Render / capture
    webhook_url: str | None = None
    disk_warning_threshold_gb: int | None = None
    timestamp_burn_in: bool | None = None
    default_framerate: int | None = None
    render_poll_interval_seconds: int | None = None
    # NVR connection overrides (override env vars)
    protect_host: str | None = None
    protect_port: int | None = None
    protect_verify_ssl: bool | None = None
    # Geolocation overrides
    latitude: float | None = None
    longitude: float | None = None
    tz: str | None = None
    # UI
    dark_mode: bool | None = None
    # Maintenance window (hour 0-23, minute 0-59)
    maintenance_hour: int | None = None
    maintenance_minute: int | None = None
    # NVR reconnect backoff
    nvr_reconnect_backoff_seconds: int | None = None
    # Per-project notification mute (JSON list of project IDs)
    muted_project_ids: list[int] | None = None


def _get_settings_row() -> dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return dict(row) if row else {}


@router.get("/settings")
def get_settings_route() -> dict:
    return _get_settings_row()


@router.put("/settings")
async def update_settings(payload: SettingsUpdate) -> dict:
    data = payload.model_dump()

    # Build SET clause — include explicit None values so fields can be cleared
    # (NULL = "use env default" for override fields).
    # For boolean/int fields that have a real meaning when False, include them too.
    set_parts: list[str] = []
    values: list[Any] = []

    _bool_as_int = {"timestamp_burn_in", "protect_verify_ssl", "dark_mode"}
    _json_fields = {"muted_project_ids"}
    for key, val in data.items():
        if key in _bool_as_int and val is not None:
            set_parts.append(f"{key} = ?")
            values.append(int(val))
        elif key in _json_fields and val is not None:
            set_parts.append(f"{key} = ?")
            values.append(json.dumps(val))
        elif val is not None:
            set_parts.append(f"{key} = ?")
            values.append(val)
        else:
            # Explicit None — only clear override fields (not required fields)
            if key in {
                "protect_host",
                "protect_port",
                "protect_verify_ssl",
                "latitude",
                "longitude",
                "tz",
            }:
                set_parts.append(f"{key} = NULL")

    if set_parts:
        with get_connection() as conn:
            conn.execute(
                f"UPDATE settings SET {', '.join(set_parts)} WHERE id = 1",
                values,
            )
            conn.commit()

    # Reconnect NVR if any NVR override fields were touched
    nvr_touched = any(k in _NVR_FIELDS for k in data if data[k] is not None)
    if nvr_touched or any(k in _NVR_FIELDS and data[k] is None for k in _NVR_FIELDS):
        from app.protect import protect_manager

        with contextlib.suppress(Exception):
            await protect_manager.reconnect()

    # Invalidate LocationInfo cache if geolocation fields changed
    _GEO_FIELDS = {"latitude", "longitude", "tz"}
    if any(k in _GEO_FIELDS for k in data):
        with contextlib.suppress(Exception):
            from app.capture import invalidate_location_cache
            invalidate_location_cache()

    # Re-register maintenance job if schedule changed
    _MAINT_FIELDS = {"maintenance_hour", "maintenance_minute"}
    if any(k in _MAINT_FIELDS and data.get(k) is not None for k in _MAINT_FIELDS):
        with contextlib.suppress(Exception):
            from app.capture import scheduler
            from app.maintenance import register_maintenance_job
            register_maintenance_job(scheduler)

    return _get_settings_row()


@router.post("/settings/watermark", status_code=200)
async def upload_watermark(file: UploadFile) -> dict:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=422, detail="Only image files are accepted")
    config = _get_app_settings()
    watermark_path = os.path.join(os.path.dirname(config.database_path), "watermark.png")
    os.makedirs(os.path.dirname(watermark_path), exist_ok=True)
    content = await file.read()
    with open(watermark_path, "wb") as f:
        f.write(content)
    with get_connection() as conn:
        conn.execute("UPDATE settings SET watermark_path = ? WHERE id = 1", (watermark_path,))
        conn.commit()
    return {"watermark_path": watermark_path}


@router.get("/settings/watermark-preview")
def get_watermark_preview() -> FileResponse:
    row = _get_settings_row()
    path = row.get("watermark_path")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No watermark set")
    return FileResponse(path, media_type="image/png")


@router.delete("/settings/watermark", status_code=204)
def delete_watermark() -> None:
    row = _get_settings_row()
    path = row.get("watermark_path")
    if path:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
    with get_connection() as conn:
        conn.execute("UPDATE settings SET watermark_path = NULL WHERE id = 1")
        conn.commit()


@router.get("/settings/nvr-test")
async def test_nvr_connection() -> dict:
    import time

    from app.protect import protect_manager

    t0 = time.monotonic()
    try:
        client = await protect_manager.get_client()
        latency_ms = int((time.monotonic() - t0) * 1000)
        camera_count = len(client.bootstrap.cameras)
        return {"ok": True, "latency_ms": latency_ms, "camera_count": camera_count, "error": None}
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "camera_count": 0, "error": str(exc)[:300]}
