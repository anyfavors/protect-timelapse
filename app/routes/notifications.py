"""In-app notification routes."""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.database import get_connection, row_to_dict

router = APIRouter(prefix="/api", tags=["notifications"])


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
    return [row_to_dict(r) for r in rows]


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


@router.delete("/notifications/{notification_id}", status_code=204)
def delete_notification(notification_id: int) -> None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM notifications WHERE id = ?", (notification_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Notification {notification_id} not found")
        conn.execute("DELETE FROM notifications WHERE id = ?", (notification_id,))
        conn.commit()


@router.delete("/notifications", status_code=204)
def clear_notifications(read_only: bool = Query(default=False)) -> None:
    with get_connection() as conn:
        if read_only:
            conn.execute("DELETE FROM notifications WHERE is_read = 1")
        else:
            conn.execute("DELETE FROM notifications")
        conn.commit()
