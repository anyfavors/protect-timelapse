"""
Notification system.
Always writes to the notifications table first, then fires an optional webhook.
The webhook POST is fire-and-forget with a 10-second timeout.
"""

import ipaddress
import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.database import get_connection

log = logging.getLogger("app.notifications")


async def notify(
    event: str,
    level: str,
    message: str,
    project_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Write notification to DB and fire webhook (if configured).
    Never raises — errors are logged and suppressed.
    """
    now = datetime.now(UTC).isoformat()

    # 0. Check per-project mute list
    if project_id is not None:
        try:
            with get_connection() as conn:
                mute_row = conn.execute(
                    "SELECT muted_project_ids FROM settings WHERE id = 1"
                ).fetchone()
            muted: list[int] = json.loads(
                (mute_row["muted_project_ids"] or "[]") if mute_row else "[]"
            )
            if project_id in muted:
                log.debug("Notification suppressed for muted project %d", project_id)
                return
        except Exception:
            pass

    # 1. Persist to notifications table (always)
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO notifications (event, level, project_id, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event, level, project_id, message, now),
            )
            conn.commit()
    except Exception as exc:
        log.error("Failed to write notification to DB: %s", exc)

    # 2. Broadcast WebSocket event
    try:
        from app.websocket import broadcast

        await broadcast(
            "notification",
            {
                "event": event,
                "level": level,
                "message": message,
                "project_id": project_id,
                "timestamp": now,
            },
        )
    except Exception as exc:
        log.warning("Failed to broadcast notification via WS: %s", exc)

    # 3. Fire external webhook (fire-and-forget)
    webhook_url = _get_webhook_url()
    if not webhook_url:
        return

    payload: dict[str, Any] = {
        "event": event,
        "level": level,
        "message": message,
        "timestamp_utc": now,
    }
    if project_id is not None:
        payload["project_id"] = project_id
    if details:
        payload["details"] = details

    if not _is_safe_webhook_url(webhook_url):
        log.warning("Webhook URL rejected (SSRF guard) for event %s: %r", event, webhook_url)
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code >= 400:
                log.warning("Webhook returned %d for event %s", resp.status_code, event)
    except (httpx.TimeoutException, TimeoutError):
        log.warning("Webhook timed out for event %s", event)
    except Exception as exc:
        log.warning("Webhook delivery failed for event %s: %s", event, exc)


def _get_webhook_url() -> str | None:
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT webhook_url FROM settings WHERE id = 1").fetchone()
        return row["webhook_url"] if row and row["webhook_url"] else None
    except Exception:
        return None


def _is_safe_webhook_url(url: str) -> bool:
    """Validate webhook URL to prevent SSRF attacks (#5)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        if not host:
            return False
        # Reject obvious local hostnames
        if host.lower() in ("localhost", "127.0.0.1", "::1"):
            return False
        # Reject IP addresses in private/loopback/link-local ranges
        try:
            addr = ipaddress.ip_address(host)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                return False
        except ValueError:
            pass  # hostname, not IP — allow it
        return True
    except Exception:
        return False
