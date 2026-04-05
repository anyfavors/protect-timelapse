"""In-app notification routes."""

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.database import get_connection

router = APIRouter(prefix="/api", tags=["notifications"])


def _row_to_dict(row: Any) -> dict:
    return dict(row)


@router.get("/notifications")
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    with get_connection() as conn:
        if unread_only:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE is_read = 0 ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


class MarkReadPayload(BaseModel):
    ids: list[int] | None = None
    all: bool = False


@router.put("/notifications/read", status_code=204)
def mark_read(payload: MarkReadPayload) -> None:
    with get_connection() as conn:
        if payload.all:
            conn.execute("UPDATE notifications SET is_read = 1")
        elif payload.ids:
            placeholders = ",".join("?" * len(payload.ids))
            conn.execute(
                f"UPDATE notifications SET is_read = 1 WHERE id IN ({placeholders})",
                payload.ids,
            )
        conn.commit()
