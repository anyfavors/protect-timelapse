"""
WebSocket connection manager.
broadcast() is called by background workers to push real-time events to all
connected browser clients.

capture_event messages are coalesced over a 250 ms window to avoid flooding
clients when many projects fire simultaneously at the same interval.
"""

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("app.websocket")

router = APIRouter(tags=["websocket"])

_COALESCE_WINDOW = 0.25  # seconds


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        # Coalescing buffer: event_type -> list of payloads
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._flush_task: asyncio.Task[None] | None = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        log.debug("WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        log.debug("WS client disconnected (%d total)", len(self._clients))

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        # Coalesce capture_event messages
        # Note: the check+assign below is race-free in single-threaded asyncio —
        # no two coroutines run concurrently without an await, so this is atomic (#18)
        if event == "capture_event":
            self._pending.setdefault(event, []).append(payload)
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_after_delay())
            return
        await self._send_all(json.dumps({"event": event, **payload}))

    async def _flush_after_delay(self) -> None:
        await asyncio.sleep(_COALESCE_WINDOW)
        batches = self._pending.copy()
        self._pending.clear()
        for _event, payloads in batches.items():
            msg = json.dumps({"event": "capture_batch", "updates": payloads})
            await self._send_all(msg)

    async def _send_all(self, message: str) -> None:
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def close_all(self) -> None:
        for ws in list(self._clients):
            with contextlib.suppress(Exception):
                await ws.close()
        self._clients.clear()


manager = ConnectionManager()


async def broadcast(event: str, payload: dict[str, Any]) -> None:
    """Module-level helper so workers don't need to import manager directly."""
    await manager.broadcast(event, payload)


@router.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            # Keep the connection alive; clients send pings or we just wait
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)
