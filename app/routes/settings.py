"""Global settings routes (single row, id=1)."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

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
    for key, val in data.items():
        if key in _bool_as_int and val is not None:
            set_parts.append(f"{key} = ?")
            values.append(int(val))
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
        import contextlib

        from app.protect import protect_manager

        with contextlib.suppress(Exception):
            await protect_manager.reconnect()

    return _get_settings_row()
