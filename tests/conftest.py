"""
Shared pytest fixtures.

tmp_db  — temporary SQLite file with the full schema applied.
          Monkeypatches app.database.get_connection so all code under test
          uses an isolated database without touching /data.

client  — FastAPI TestClient with the lifespan stubbed out (no real NVR,
          no background workers started).

full_api — Full TestClient with ALL routers mounted and scheduler stubs.

Factory fixtures: make_project, make_frame, make_render, make_jpeg
"""

import io
import sqlite3
import time
from collections.abc import Generator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.database import get_connection

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


@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> Generator[None, None, None]:
    """Disable slowapi rate limiting during tests to avoid spurious 429 errors."""
    from app.limiter import limiter

    limiter.enabled = False
    yield
    limiter.enabled = True


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
    from contextlib import contextmanager

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
# client fixture (minimal — only health router)
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
    import app.protect as protect_module

    # Stub NVR manager so it doesn't try to connect
    monkeypatch.setattr(protect_module.protect_manager, "setup", AsyncMock())
    monkeypatch.setattr(protect_module.protect_manager, "teardown", AsyncMock())
    monkeypatch.setattr(protect_module.protect_manager, "_connected", False)

    # Import the app *after* patching so the lifespan sees the stubs
    from app.routes import health

    @asynccontextmanager
    async def _stub_lifespan(app: FastAPI):  # type: ignore[type-arg]
        yield  # no startup/shutdown side-effects in tests

    # Rebuild the app with the stub lifespan
    test_app = FastAPI(lifespan=_stub_lifespan)

    # Mount the same routes as the real app
    test_app.include_router(health.router)

    with TestClient(test_app) as c:
        yield c


# ---------------------------------------------------------------------------
# full_api fixture — mounts ALL routers
# ---------------------------------------------------------------------------


@pytest.fixture()
def full_api(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Full API client with all routers and scheduler/capture stubs."""
    import app.config as config_mod
    from app.routes import (
        cameras,
        frames,
        health,
        maintenance,
        metrics,
        notifications,
        presets,
        projects,
        renders,
        settings,
        templates,
    )

    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))
    monkeypatch.setattr(config_mod, "_settings", None)

    @asynccontextmanager
    async def _noop(app):  # type: ignore[no-untyped-def]
        yield

    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    application = FastAPI(lifespan=_noop)
    _limiter = Limiter(key_func=get_remote_address)
    application.state.limiter = _limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    for r in (
        frames.router,
        health.router,
        projects.router,
        renders.router,
        settings.router,
        notifications.router,
        cameras.router,
        presets.router,
        maintenance.router,
        metrics.router,
        templates.router,
    ):
        application.include_router(r)

    monkeypatch.setattr("app.routes.projects.add_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.remove_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.reschedule_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.pause_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.resume_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.run_historical_extraction", AsyncMock())

    return TestClient(application)


# ---------------------------------------------------------------------------
# Factory helpers — shared across all test modules
# ---------------------------------------------------------------------------


def make_jpeg(width: int = 100, height: int = 80, brightness: int = 128) -> bytes:
    """Create a minimal JPEG in memory."""
    img = Image.new("RGB", (width, height), color=(brightness, brightness, brightness))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def make_project(
    name: str = "TestProj",
    camera_id: str = "cam-1",
    project_type: str = "live",
    interval_seconds: int = 60,
    status: str = "active",
    capture_mode: str = "continuous",
    **extra_fields: object,
) -> int:
    """Insert a project into the DB and return its ID."""
    cols = [
        "name",
        "camera_id",
        "project_type",
        "interval_seconds",
        "status",
        "capture_mode",
    ]
    vals: list[object] = [name, camera_id, project_type, interval_seconds, status, capture_mode]
    for k, v in extra_fields.items():
        cols.append(k)
        vals.append(v)
    placeholders = ",".join("?" * len(cols))
    col_str = ",".join(cols)
    with get_connection() as conn:
        cur = conn.execute(
            f"INSERT INTO projects ({col_str}) VALUES ({placeholders})",
            vals,
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def make_frame(
    project_id: int,
    tmp_path: Path,
    *,
    is_blurry: int = 0,
    is_dark: int = 0,
    captured_at: str | None = None,
) -> int:
    """Insert a frame with real JPEG files on disk and return its ID."""
    if captured_at is None:
        # Use monotonic-based unique timestamp to avoid collisions
        ns = time.monotonic_ns()
        captured_at = f"2024-01-01T12:{(ns // 10**9) % 60:02d}:{ns % 60:02d}"

    frames_dir = tmp_path / "frames" / str(project_id)
    thumbs_dir = tmp_path / "thumbs" / str(project_id)
    frames_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    jpeg = make_jpeg()
    ts_clean = captured_at.replace(":", "").replace("+", "").replace("-", "").replace("T", "")[:14]
    # Append nanoseconds to guarantee unique filenames
    ts_clean += str(time.monotonic_ns())[-6:]
    frame_file = frames_dir / f"{ts_clean}.jpg"
    thumb_file = thumbs_dir / f"{ts_clean}.jpg"
    frame_file.write_bytes(jpeg)
    thumb_file.write_bytes(jpeg)

    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO frames (project_id, captured_at, file_path, thumbnail_path, file_size, is_blurry, is_dark)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                project_id,
                captured_at,
                str(frame_file),
                str(thumb_file),
                len(jpeg),
                is_blurry,
                is_dark,
            ),
        )
        conn.execute(
            "UPDATE projects SET frame_count = frame_count + 1 WHERE id = ?", (project_id,)
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def make_render(
    project_id: int,
    *,
    status: str = "pending",
    render_type: str = "manual",
    output_path: str | None = None,
    priority: int = 5,
) -> int:
    """Insert a render row and return its ID."""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO renders (project_id, framerate, resolution, render_type, status, output_path, priority)"
            " VALUES (?,?,?,?,?,?,?)",
            (project_id, 30, "1920x1080", render_type, status, output_path, priority),
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]
