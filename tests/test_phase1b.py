"""
Integration tests for Phase 1b correctness fixes:
- Historical project status / scheduler isolation
- snapshot_worker skips historical projects
- Resolution validation on POST /api/renders
- Notification delete routes
"""

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.database import get_connection
from app.routes import frames, notifications, projects, renders, settings, templates

# ---------------------------------------------------------------------------
# Shared API fixture (same pattern as test_projects.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from contextlib import asynccontextmanager

    import app.config as config_mod

    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))
    monkeypatch.setattr(config_mod, "_settings", None)

    @asynccontextmanager
    async def _noop(app):  # type: ignore[no-untyped-def]
        yield

    app_obj = FastAPI(lifespan=_noop)
    app_obj.include_router(projects.router)
    app_obj.include_router(templates.router)
    app_obj.include_router(frames.router)
    app_obj.include_router(renders.router)
    app_obj.include_router(notifications.router)
    app_obj.include_router(settings.router)

    monkeypatch.setattr("app.routes.projects.add_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.remove_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.reschedule_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.pause_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.resume_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.run_historical_extraction", AsyncMock())

    return TestClient(app_obj)


def _live_payload(**kwargs) -> dict:
    return {
        "name": "Live test",
        "camera_id": "cam-abc",
        "project_type": "live",
        "interval_seconds": 30,
        **kwargs,
    }


def _historical_payload(**kwargs) -> dict:
    return {
        "name": "Hist test",
        "camera_id": "cam-abc",
        "project_type": "historical",
        "interval_seconds": 60,
        "start_date": "2024-01-01T00:00:00",
        "end_date": "2024-01-02T00:00:00",
        **kwargs,
    }


# ---------------------------------------------------------------------------
# Historical project status
# ---------------------------------------------------------------------------


def test_live_project_status_is_active(api: TestClient) -> None:
    r = api.post("/api/projects", json=_live_payload())
    assert r.status_code == 201
    assert r.json()["status"] == "active"


def test_historical_project_status_is_extracting(api: TestClient) -> None:
    r = api.post("/api/projects", json=_historical_payload())
    assert r.status_code == 201
    assert r.json()["status"] == "extracting"


def test_live_project_registers_scheduler_job(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_job = AsyncMock()
    monkeypatch.setattr("app.routes.projects.add_project_job", add_job)
    api.post("/api/projects", json=_live_payload())
    add_job.assert_awaited_once()


def test_historical_project_does_not_register_scheduler_job(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_job = AsyncMock()
    monkeypatch.setattr("app.routes.projects.add_project_job", add_job)
    r = api.post("/api/projects", json=_historical_payload())
    assert r.status_code == 201
    add_job.assert_not_awaited()


# ---------------------------------------------------------------------------
# snapshot_worker skips historical projects
# ---------------------------------------------------------------------------


def _make_jpeg(brightness: int = 200) -> bytes:
    img = Image.new("RGB", (64, 64), color=(brightness, brightness, brightness))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture()
def historical_project_id(tmp_db: Path) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (name, camera_id, project_type, interval_seconds,
                                  start_date, end_date, status)
            VALUES ('Hist', 'cam-1', 'historical', 60,
                    '2024-01-01T00:00:00', '2024-01-02T00:00:00', 'active')
            """
        )
        conn.commit()
    return cur.lastrowid


@pytest.mark.asyncio
async def test_snapshot_worker_skips_historical_project(
    tmp_db: Path,
    historical_project_id: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import shutil

    import app.capture as capture_mod
    import app.config as config_mod
    import app.protect as protect_mod

    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))

    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: MagicMock(free=50 * 1024**3, total=100 * 1024**3)
    )

    mock_cam = AsyncMock()
    mock_cam.get_snapshot = AsyncMock(return_value=_make_jpeg())
    mock_client = MagicMock()
    mock_client.bootstrap.cameras = {"cam-1": mock_cam}
    monkeypatch.setattr(
        protect_mod.protect_manager, "get_client", AsyncMock(return_value=mock_client)
    )
    monkeypatch.setattr(capture_mod, "broadcast", AsyncMock())
    remove_job = AsyncMock()
    monkeypatch.setattr(capture_mod, "remove_project_job", remove_job)

    from app.capture import snapshot_worker

    await snapshot_worker(historical_project_id)

    # No snapshot should have been taken
    mock_cam.get_snapshot.assert_not_awaited()
    # The job should be removed
    remove_job.assert_awaited_once_with(historical_project_id)

    # No frames should be in the DB
    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM frames WHERE project_id = ?", (historical_project_id,)
        ).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------------
# Resolution validation on POST /api/renders
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_id_for_renders(tmp_db: Path) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds) VALUES (?,?,?,?)",
            ("Test", "cam-1", "live", 30),
        )
        conn.commit()
    return cur.lastrowid


def test_render_valid_resolution(api: TestClient, project_id_for_renders: int) -> None:
    for res in ("1920x1080", "3840x2160", "1280x720", "2560x1440"):
        r = api.post(
            "/api/renders",
            json={"project_id": project_id_for_renders, "framerate": 30, "resolution": res},
        )
        assert r.status_code == 201, f"Expected 201 for resolution {res!r}, got {r.status_code}"


def test_render_invalid_resolution_rejected(api: TestClient, project_id_for_renders: int) -> None:
    for bad in ("1920-1080", "fullhd", "1920x1080x720", "x1080", "1920x"):
        r = api.post(
            "/api/renders",
            json={"project_id": project_id_for_renders, "framerate": 30, "resolution": bad},
        )
        assert r.status_code == 422, f"Expected 422 for resolution {bad!r}, got {r.status_code}"


# ---------------------------------------------------------------------------
# Notification delete routes
# ---------------------------------------------------------------------------


def _insert_notification(msg: str = "test") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO notifications (event, level, message) VALUES (?,?,?)",
            ("test_event", "info", msg),
        )
        conn.commit()
    return cur.lastrowid


def test_delete_single_notification(api: TestClient, tmp_db: Path) -> None:
    nid = _insert_notification("delete me")
    r = api.delete(f"/api/notifications/{nid}")
    assert r.status_code == 204

    with get_connection() as conn:
        row = conn.execute("SELECT id FROM notifications WHERE id = ?", (nid,)).fetchone()
    assert row is None


def test_delete_notification_not_found(api: TestClient, tmp_db: Path) -> None:
    r = api.delete("/api/notifications/99999")
    assert r.status_code == 404


def test_clear_all_notifications(api: TestClient, tmp_db: Path) -> None:
    _insert_notification("a")
    _insert_notification("b")
    _insert_notification("c")

    r = api.delete("/api/notifications")
    assert r.status_code == 204

    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert count == 0


def test_clear_read_only_notifications(api: TestClient, tmp_db: Path) -> None:
    nid_read = _insert_notification("read one")
    nid_unread = _insert_notification("unread one")

    # Mark the first as read
    with get_connection() as conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (nid_read,))
        conn.commit()

    r = api.delete("/api/notifications?read_only=true")
    assert r.status_code == 204

    with get_connection() as conn:
        remaining = [row[0] for row in conn.execute("SELECT id FROM notifications").fetchall()]
    assert nid_read not in remaining
    assert nid_unread in remaining
