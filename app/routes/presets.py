"""Render preset CRUD routes."""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.database import get_connection

router = APIRouter(prefix="/api", tags=["presets"])
log = logging.getLogger("app.routes.presets")


class PresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    framerate: int = Field(default=30, ge=1, le=120)
    resolution: str = Field(default="1920x1080", pattern=r"^\d{1,5}x\d{1,5}$")
    quality: str = Field(default="standard", pattern="^(draft|standard|high|archive)$")
    flicker_reduction: str = Field(default="standard", pattern="^(off|standard|strong|holy_grail)$")
    frame_blend: bool = False
    stabilize: bool = False
    color_grade: str = Field(default="none", pattern="^(none|neutral|warm|cool|cinematic)$")


def _row_to_dict(row: Any) -> dict:
    return dict(row)


def _get_preset_or_404(preset_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM render_presets WHERE id = ?", (preset_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Preset {preset_id} not found")
    return _row_to_dict(row)


@router.get("/presets")
def list_presets() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM render_presets ORDER BY name ASC").fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("/presets", status_code=201)
def create_preset(payload: PresetCreate) -> dict:
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO render_presets
                    (name, framerate, resolution, quality, flicker_reduction,
                     frame_blend, stabilize, color_grade)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    payload.name,
                    payload.framerate,
                    payload.resolution,
                    payload.quality,
                    payload.flicker_reduction,
                    int(payload.frame_blend),
                    int(payload.stabilize),
                    payload.color_grade,
                ),
            )
            conn.commit()
        except Exception as exc:
            if "UNIQUE constraint" in str(exc):
                raise HTTPException(
                    status_code=409, detail=f"Preset named {payload.name!r} already exists"
                ) from exc
            raise

    if cur.lastrowid is None:
        raise HTTPException(status_code=500, detail="Failed to create preset")
    return _get_preset_or_404(cur.lastrowid)


@router.delete("/presets/{preset_id}", status_code=204)
def delete_preset(preset_id: int) -> None:
    _get_preset_or_404(preset_id)
    with get_connection() as conn:
        conn.execute("DELETE FROM render_presets WHERE id = ?", (preset_id,))
        conn.commit()


@router.get("/presets/{preset_id}")
def get_preset(preset_id: int) -> dict:
    return _get_preset_or_404(preset_id)
