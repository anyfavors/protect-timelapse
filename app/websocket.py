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


_MAX_WS_CONNECTIONS = 50  # total across all IPs
_MAX_WS_PER_IP = 5


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._ip_counts: dict[str, int] = {}
        # Coalescing buffer: event_type -> list of payloads
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._flush_task: asyncio.Task[None] | None = None

    async def connect(self, ws: WebSocket) -> None:
        # Enforce connection limits to prevent DoS (S10)
        if len(self._clients) >= _MAX_WS_CONNECTIONS:
            await ws.close(code=1013)  # Try Again Later
            return
        client_ip = ws.client.host if ws.client else "unknown"
        if self._ip_counts.get(client_ip, 0) >= _MAX_WS_PER_IP:
            await ws.close(code=1008)  # Policy Violation
            return
        await ws.accept()
        self._clients.add(ws)
        self._ip_counts[client_ip] = self._ip_counts.get(client_ip, 0) + 1
        log.debug("WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        client_ip = ws.client.host if ws.client else "unknown"
        cnt = self._ip_counts.get(client_ip, 1) - 1
        if cnt <= 0:
            self._ip_counts.pop(client_ip, None)
        else:
            self._ip_counts[client_ip] = cnt
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


_PING_INTERVAL = 30.0  # seconds between server-side pings


@router.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            try:
                # Wait for a message with a timeout; send ping on timeout to detect dead clients
                await asyncio.wait_for(ws.receive_text(), timeout=_PING_INTERVAL)
            except TimeoutError:
                # Send a lightweight ping frame to detect stale connections
                await ws.send_text('{"event":"ping"}')
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)
