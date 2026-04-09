"""
Project template CRUD routes.
Templates store reusable project configurations.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.database import get_connection, row_to_dict

router = APIRouter(prefix="/api", tags=["templates"])


class TemplateCreate(BaseModel):
    name: str
    interval_seconds: int = Field(ge=1)
    width: int | None = None
    height: int | None = None
    max_frames: int | None = None
    capture_mode: str = Field(
        default="continuous", pattern="^(continuous|daylight_only|schedule|solar_noon)$"
    )
    use_luminance_check: bool = False
    luminance_threshold: int = Field(default=15, ge=0, le=255)
    schedule_start_time: str | None = None
    schedule_end_time: str | None = None
    schedule_days: str | None = None
    auto_render_daily: bool = False
    auto_render_weekly: bool = False
    auto_render_monthly: bool = False
    retention_days: int = 0
    solar_noon_window_minutes: int = Field(default=30, ge=5, le=120)


class TemplateApply(BaseModel):
    name: str
    camera_id: str


def _get_template_or_404(template_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM project_templates WHERE id = ?", (template_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
    return row_to_dict(row)


@router.get("/templates")
def list_templates() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM project_templates ORDER BY name ASC").fetchall()
    return [row_to_dict(r) for r in rows]


@router.post("/templates", status_code=201)
def create_template(payload: TemplateCreate) -> dict:
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO project_templates (
                    name, interval_seconds, width, height, max_frames,
                    capture_mode, use_luminance_check, luminance_threshold,
                    schedule_start_time, schedule_end_time, schedule_days,
                    auto_render_daily, auto_render_weekly, auto_render_monthly,
                    retention_days, solar_noon_window_minutes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    payload.name,
                    payload.interval_seconds,
                    payload.width,
                    payload.height,
                    payload.max_frames,
                    payload.capture_mode,
                    int(payload.use_luminance_check),
                    payload.luminance_threshold,
                    payload.schedule_start_time,
                    payload.schedule_end_time,
                    payload.schedule_days,
                    int(payload.auto_render_daily),
                    int(payload.auto_render_weekly),
                    int(payload.auto_render_monthly),
                    payload.retention_days,
                    payload.solar_noon_window_minutes,
                ),
            )
            conn.commit()
        except Exception as exc:
            if "UNIQUE constraint" in str(exc):
                raise HTTPException(
                    status_code=409, detail=f"Template named {payload.name!r} already exists"
                ) from exc
            raise

    assert cur.lastrowid is not None
    return _get_template_or_404(cur.lastrowid)


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: int) -> None:
    _get_template_or_404(template_id)
    with get_connection() as conn:
        conn.execute("DELETE FROM project_templates WHERE id = ?", (template_id,))
        conn.commit()


@router.post("/templates/{template_id}/apply", status_code=201)
async def apply_template(template_id: int, payload: TemplateApply) -> dict:
    """Create a new project pre-filled from the template. Camera and name are required."""
    tmpl = _get_template_or_404(template_id)

    from app.routes.projects import ProjectCreate, create_project

    project_payload = ProjectCreate(
        name=payload.name,
        camera_id=payload.camera_id,
        project_type="live",
        interval_seconds=tmpl["interval_seconds"],
        width=tmpl["width"],
        height=tmpl["height"],
        max_frames=tmpl["max_frames"],
        capture_mode=tmpl["capture_mode"],
        use_luminance_check=bool(tmpl["use_luminance_check"]),
        luminance_threshold=tmpl["luminance_threshold"],
        schedule_start_time=tmpl["schedule_start_time"],
        schedule_end_time=tmpl["schedule_end_time"],
        schedule_days=tmpl["schedule_days"],
        auto_render_daily=bool(tmpl["auto_render_daily"]),
        auto_render_weekly=bool(tmpl["auto_render_weekly"]),
        auto_render_monthly=bool(tmpl["auto_render_monthly"]),
        retention_days=tmpl["retention_days"],
        solar_noon_window_minutes=tmpl.get("solar_noon_window_minutes") or 30,
        template_id=template_id,
    )
    return await create_project(project_payload)
