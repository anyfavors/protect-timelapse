"""Tests for projects CRUD and template routes."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_connection
from app.routes import frames, notifications, projects, renders, settings, templates

# ---------------------------------------------------------------------------
# Shared test app fixture that includes all routes under test
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from contextlib import asynccontextmanager

    import app.config as config_mod

    # Redirect storage paths so create_project doesn't try to mkdir /data
    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))
    monkeypatch.setattr(config_mod, "_settings", None)

    @asynccontextmanager
    async def _noop(app):  # type: ignore[no-untyped-def]
        yield

    app = FastAPI(lifespan=_noop)
    app.include_router(projects.router)
    app.include_router(templates.router)
    app.include_router(frames.router)
    app.include_router(renders.router)
    app.include_router(notifications.router)
    app.include_router(settings.router)

    # Stub scheduler so tests don't need APScheduler running
    monkeypatch.setattr("app.routes.projects.add_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.remove_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.reschedule_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.pause_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.resume_project_job", AsyncMock())
    monkeypatch.setattr("app.routes.projects.run_historical_extraction", AsyncMock())

    return TestClient(app)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_list_projects_empty(api: TestClient) -> None:
    r = api.get("/api/projects")
    assert r.status_code == 200
    assert r.json() == []


def test_create_project(api: TestClient) -> None:
    r = api.post(
        "/api/projects",
        json={
            "name": "Test Timelapse",
            "camera_id": "cam-abc",
            "project_type": "live",
            "interval_seconds": 10,
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Test Timelapse"
    assert data["status"] == "active"
    assert data["frame_count"] == 0


def test_get_project(api: TestClient) -> None:
    r = api.post(
        "/api/projects",
        json={"name": "Proj A", "camera_id": "x", "project_type": "live", "interval_seconds": 5},
    )
    pid = r.json()["id"]
    r2 = api.get(f"/api/projects/{pid}")
    assert r2.status_code == 200
    assert r2.json()["id"] == pid


def test_get_project_not_found(api: TestClient) -> None:
    r = api.get("/api/projects/9999")
    assert r.status_code == 404


def test_update_project(api: TestClient) -> None:
    r = api.post(
        "/api/projects",
        json={"name": "X", "camera_id": "c", "project_type": "live", "interval_seconds": 30},
    )
    pid = r.json()["id"]
    r2 = api.put(f"/api/projects/{pid}", json={"status": "paused"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "paused"


def test_delete_project(api: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as cfg_mod

    # Point paths at tmp so shutil.rmtree doesn't error
    monkeypatch.setattr(cfg_mod, "_settings", None)
    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))

    r = api.post(
        "/api/projects",
        json={"name": "Del", "camera_id": "c", "project_type": "live", "interval_seconds": 5},
    )
    pid = r.json()["id"]
    r2 = api.delete(f"/api/projects/{pid}")
    assert r2.status_code == 204
    r3 = api.get(f"/api/projects/{pid}")
    assert r3.status_code == 404


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_create_and_list_template(api: TestClient) -> None:
    r = api.post("/api/templates", json={"name": "Construction", "interval_seconds": 60})
    assert r.status_code == 201
    r2 = api.get("/api/templates")
    assert len(r2.json()) == 1


def test_template_duplicate_name(api: TestClient) -> None:
    api.post("/api/templates", json={"name": "Dup", "interval_seconds": 10})
    r = api.post("/api/templates", json={"name": "Dup", "interval_seconds": 20})
    assert r.status_code == 409


def test_delete_template(api: TestClient) -> None:
    r = api.post("/api/templates", json={"name": "TDel", "interval_seconds": 5})
    tid = r.json()["id"]
    r2 = api.delete(f"/api/templates/{tid}")
    assert r2.status_code == 204


def test_apply_template(api: TestClient) -> None:
    r = api.post(
        "/api/templates",
        json={"name": "Weather", "interval_seconds": 5, "capture_mode": "daylight_only"},
    )
    tid = r.json()["id"]
    r2 = api.post(
        f"/api/templates/{tid}/apply", json={"name": "New from template", "camera_id": "cam-1"}
    )
    assert r2.status_code == 201
    data = r2.json()
    assert data["interval_seconds"] == 5
    assert data["capture_mode"] == "daylight_only"
    assert data["template_id"] == tid


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_get_settings(api: TestClient) -> None:
    r = api.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["disk_warning_threshold_gb"] == 5


def test_update_settings(api: TestClient) -> None:
    r = api.put("/api/settings", json={"disk_warning_threshold_gb": 10})
    assert r.status_code == 200
    assert r.json()["disk_warning_threshold_gb"] == 10


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_notifications_empty(api: TestClient) -> None:
    r = api.get("/api/notifications")
    assert r.status_code == 200
    assert r.json() == []


def test_mark_notifications_read(api: TestClient, tmp_db: Path) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO notifications (event, level, message) VALUES ('test','info','hello')"
        )
        conn.commit()

    r = api.get("/api/notifications?unread_only=true")
    assert len(r.json()) == 1

    nid = r.json()[0]["id"]
    r2 = api.put("/api/notifications/read", json={"ids": [nid]})
    assert r2.status_code == 204

    r3 = api.get("/api/notifications?unread_only=true")
    assert r3.json() == []


# ---------------------------------------------------------------------------
# Renders
# ---------------------------------------------------------------------------


def _make_project(api: TestClient) -> int:
    r = api.post(
        "/api/projects",
        json={
            "name": "RProj",
            "camera_id": "cam-1",
            "project_type": "live",
            "interval_seconds": 10,
        },
    )
    return r.json()["id"]


def test_create_render(api: TestClient) -> None:
    pid = _make_project(api)
    r = api.post("/api/renders", json={"project_id": pid, "framerate": 30})
    assert r.status_code == 201
    data = r.json()
    assert data["project_id"] == pid
    assert data["status"] == "pending"
    assert "estimated_render_time_seconds" in data


def test_create_render_project_not_found(api: TestClient) -> None:
    r = api.post("/api/renders", json={"project_id": 9999, "framerate": 30})
    assert r.status_code == 404


def test_render_status(api: TestClient) -> None:
    pid = _make_project(api)
    r = api.post("/api/renders", json={"project_id": pid})
    rid = r.json()["id"]
    r2 = api.get(f"/api/renders/{rid}/status")
    assert r2.status_code == 200
    assert r2.json()["status"] == "pending"


def test_list_renders_for_project(api: TestClient) -> None:
    pid = _make_project(api)
    api.post("/api/renders", json={"project_id": pid})
    api.post("/api/renders", json={"project_id": pid})
    r = api.get(f"/api/projects/{pid}/renders")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_duplicate_auto_render_rejected(api: TestClient) -> None:
    pid = _make_project(api)
    api.post("/api/renders", json={"project_id": pid, "render_type": "auto_daily"})
    r2 = api.post("/api/renders", json={"project_id": pid, "render_type": "auto_daily"})
    assert r2.status_code == 409


def test_delete_render(api: TestClient) -> None:
    pid = _make_project(api)
    r = api.post("/api/renders", json={"project_id": pid})
    rid = r.json()["id"]
    r2 = api.delete(f"/api/renders/{rid}")
    assert r2.status_code == 204
    r3 = api.get(f"/api/renders/{rid}/status")
    assert r3.status_code == 404
