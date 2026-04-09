"""
Shared pytest fixtures.

tmp_db  — temporary SQLite file with the full schema applied.
          Monkeypatches app.database.get_connection so all code under test
          uses an isolated database without touching /data.

client  — FastAPI TestClient with the lifespan stubbed out (no real NVR,
          no background workers started).
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# tmp_db fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_capture_disk_cache() -> Generator[None, None, None]:
    """Reset the disk-check throttle cache between tests to prevent cross-test pollution."""
    import app.capture as cap_mod

    cap_mod._disk_last_checked = 0.0
    cap_mod._disk_last_result = None
    yield
    cap_mod._disk_last_checked = 0.0
    cap_mod._disk_last_result = None


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Create an isolated SQLite database in a temp directory, run init_database()
    against it, and monkeypatch get_connection() so all code uses it.
    """
    db_path = tmp_path / "test.db"

    # Point config at the temp file
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # Reset the cached settings so the new DATABASE_PATH is picked up
    import app.config as config_module

    monkeypatch.setattr(config_module, "_settings", None)

    # Monkeypatch get_connection to use the tmp path directly
    @contextmanager
    def _get_connection() -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    import app.database as db_module

    monkeypatch.setattr(db_module, "get_connection", _get_connection)

    # Initialise schema
    from app.database import init_database

    init_database()

    return db_path


# ---------------------------------------------------------------------------
# client fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """
    TestClient with the FastAPI lifespan stubbed:
    - No NVR connection attempted
    - No APScheduler started
    - No render worker started
    Uses the tmp_db fixture for database isolation.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    import app.protect as protect_module

    # Stub NVR manager so it doesn't try to connect
    monkeypatch.setattr(protect_module.protect_manager, "setup", AsyncMock())
    monkeypatch.setattr(protect_module.protect_manager, "teardown", AsyncMock())
    monkeypatch.setattr(protect_module.protect_manager, "_connected", False)

    # Import the app *after* patching so the lifespan sees the stubs
    from fastapi import FastAPI

    @asynccontextmanager
    async def _stub_lifespan(app: FastAPI):  # type: ignore[type-arg]
        yield  # no startup/shutdown side-effects in tests

    # Rebuild the app with the stub lifespan
    test_app = FastAPI(lifespan=_stub_lifespan)

    # Mount the same routes as the real app
    from app.routes import health

    test_app.include_router(health.router)

    with TestClient(test_app) as c:
        yield c
