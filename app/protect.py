"""
Singleton ProtectApiClient manager.
All NVR access must go through ProtectClientManager.get_client().
"""

import asyncio
import logging
from datetime import UTC, datetime

from uiprotect import ProtectApiClient

from app.config import get_settings

log = logging.getLogger("app.protect")


class ProtectClientManager:
    """
    Lazy-initialising, lock-protected singleton for the uiprotect client.
    Call setup() at startup and teardown() at shutdown.
    """

    def __init__(self) -> None:
        self._client: ProtectApiClient | None = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._camera_count = 0
        self._last_health_check: datetime | None = None
        self._last_error: str | None = None

    async def setup(self) -> None:
        """Authenticate with the NVR and load the bootstrap data."""
        from app.database import get_db_overrides

        settings = get_settings()
        overrides = get_db_overrides()
        host = overrides.get("protect_host") or settings.protect_host
        port = int(overrides.get("protect_port") or settings.protect_port)
        # Explicit int() before bool() — prevents bool("0") == True when DB stores string (#11)
        raw_ssl = overrides.get("protect_verify_ssl")
        verify_ssl = bool(int(raw_ssl)) if raw_ssl is not None else settings.protect_verify_ssl

        async with self._lock:
            if self._client is not None:
                return
            self._client = ProtectApiClient(
                host=host,
                port=port,
                username=settings.protect_username,
                password=settings.protect_password,
                verify_ssl=verify_ssl,
            )
            try:
                await self._client.update()
                self._connected = True
                self._camera_count = len(self._client.bootstrap.cameras)
                self._last_error = None
                log.info(
                    "Connected to UniFi Protect NVR at %s:%d — %d camera(s)",
                    settings.protect_host,
                    settings.protect_port,
                    self._camera_count,
                )
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc)
                log.warning("Could not connect to NVR at startup: %s", exc)

    async def reconnect(self) -> None:
        """Tear down and re-setup the client (e.g. after NVR settings change)."""
        await self.teardown()
        await self.setup()

    async def teardown(self) -> None:
        """Close the NVR websocket session."""
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.close_session()
                except Exception as exc:
                    log.warning("Error closing NVR session: %s", exc)
                finally:
                    self._client = None
                    self._connected = False

    async def get_client(self) -> ProtectApiClient:
        """
        Return the authenticated client, attempting reconnect if stale.
        Raises RuntimeError if the NVR is unreachable.
        """
        async with self._lock:
            if self._client is None:
                raise RuntimeError("NVR client not initialised — call setup() first")
            if not self._connected:
                # Attempt reconnect
                try:
                    await self._client.update()
                    self._connected = True
                    self._camera_count = len(self._client.bootstrap.cameras)
                    self._last_error = None
                    log.info("Reconnected to NVR")
                except Exception as exc:
                    self._last_error = str(exc)
                    raise RuntimeError(f"NVR offline: {exc}") from exc
            return self._client

    def mark_disconnected(self, reason: str = "") -> None:
        """Signal that the NVR connection has failed.

        Callers (e.g. snapshot_worker) invoke this on connection/auth errors
        so that the next get_client() call attempts a reconnect.
        """
        if self._connected:
            log.warning("NVR marked disconnected: %s", reason or "unknown")
        self._connected = False
        self._last_error = reason or self._last_error

    async def refresh_bootstrap(self) -> bool:
        """Re-fetch bootstrap data (camera list, NVR info).

        Returns True on success, False on failure.
        """
        async with self._lock:
            if self._client is None:
                return False
            try:
                await self._client.update()
                old_count = self._camera_count
                self._camera_count = len(self._client.bootstrap.cameras)
                self._connected = True
                self._last_error = None
                if self._camera_count != old_count:
                    log.info(
                        "Bootstrap refreshed: camera count %d → %d",
                        old_count,
                        self._camera_count,
                    )
                return True
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc)
                log.warning("Bootstrap refresh failed: %s", exc)
                return False

    async def health_check(self) -> dict:
        """Lightweight NVR health probe. Returns status dict."""
        self._last_health_check = datetime.now(UTC)
        was_connected = self._connected

        if self._client is None:
            return {
                "connected": False,
                "camera_count": 0,
                "last_error": self._last_error or "Client not initialised",
            }

        try:
            async with self._lock:
                await self._client.update()
                self._connected = True
                self._camera_count = len(self._client.bootstrap.cameras)
                self._last_error = None
        except Exception as exc:
            self._connected = False
            self._last_error = str(exc)

        if was_connected != self._connected:
            state = "connected" if self._connected else "disconnected"
            log.info("NVR state changed → %s", state)

        return {
            "connected": self._connected,
            "camera_count": self._camera_count,
            "last_error": self._last_error,
            "last_check": self._last_health_check.isoformat() if self._last_health_check else None,
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def status(self) -> dict:
        """Current NVR status snapshot (no I/O)."""
        return {
            "connected": self._connected,
            "camera_count": self._camera_count,
            "last_error": self._last_error,
            "last_check": self._last_health_check.isoformat() if self._last_health_check else None,
        }


# Module-level singleton — imported by routes and workers
protect_manager = ProtectClientManager()
