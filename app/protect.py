"""
Singleton ProtectApiClient manager.
All NVR access must go through ProtectClientManager.get_client().
"""

import asyncio
import logging

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

    async def setup(self) -> None:
        """Authenticate with the NVR and load the bootstrap data."""
        from app.database import get_db_overrides

        settings = get_settings()
        overrides = get_db_overrides()
        host = overrides.get("protect_host") or settings.protect_host
        port = int(overrides.get("protect_port") or settings.protect_port)
        verify_ssl = bool(overrides.get("protect_verify_ssl", settings.protect_verify_ssl))

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
                log.info(
                    "Connected to UniFi Protect NVR at %s:%d — %d camera(s)",
                    settings.protect_host,
                    settings.protect_port,
                    len(self._client.bootstrap.cameras),
                )
            except Exception as exc:
                self._connected = False
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
                    log.info("Reconnected to NVR")
                except Exception as exc:
                    raise RuntimeError(f"NVR offline: {exc}") from exc
            return self._client

    @property
    def is_connected(self) -> bool:
        return self._connected


# Module-level singleton — imported by routes and workers
protect_manager = ProtectClientManager()
