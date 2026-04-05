"""Global settings routes (single row, id=1)."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.database import get_connection

router = APIRouter(prefix="/api", tags=["settings"])


class SettingsUpdate(BaseModel):
    webhook_url: str | None = None
    disk_warning_threshold_gb: int | None = None
    timestamp_burn_in: bool | None = None
    default_framerate: int | None = None
    render_poll_interval_seconds: int | None = None


def _get_settings_row() -> dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return dict(row) if row else {}


@router.get("/settings")
def get_settings_route() -> dict:
    return _get_settings_row()


@router.put("/settings")
def update_settings(payload: SettingsUpdate) -> dict:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        return _get_settings_row()

    if "timestamp_burn_in" in updates:
        updates["timestamp_burn_in"] = int(updates["timestamp_burn_in"])

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values())

    with get_connection() as conn:
        conn.execute(f"UPDATE settings SET {set_clause} WHERE id = 1", values)
        conn.commit()

    return _get_settings_row()
