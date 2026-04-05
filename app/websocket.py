"""
WebSocket connection manager.
broadcast() is called by background workers to push real-time events to all
connected browser clients.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("app.websocket")

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

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
        message = json.dumps({"event": event, **payload})
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
            import contextlib

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
