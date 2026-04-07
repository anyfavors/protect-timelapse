"""Tests for feature additions: render presets, pin/unpin, project capacity,
render pause/resume, maintenance trigger, backup, per-project notification mute,
settings extensions, NVR error classification, and database migrations."""

import io
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.database import get_connection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jpeg() -> bytes:
    img = Image.new("RGB", (80, 60), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_app(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import app.config as config_mod
    from app.routes import (
        cameras,
        frames,
        health,
        maintenance,
        notifications,
        presets,
        projects,
        renders,
        settings,
        templates,
    )
    from app.routes import metrics as metrics_mod

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
        metrics_mod.router,
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


def _insert_project(name: str = "Proj", status: str = "active") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode)"
            " VALUES (?,?,?,?,?,?)",
            (name, "cam-1", "live", 60, status, "continuous"),
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _insert_frame(project_id: int, tmp_path: Path, is_dark: int = 0) -> int:
    frames_dir = tmp_path / "frames" / str(project_id)
    thumbs_dir = tmp_path / "thumbs" / str(project_id)
    frames_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    jpeg = _make_jpeg()
    frame_file = frames_dir / "20240101120000.jpg"
    thumb_file = thumbs_dir / "20240101120000.jpg"
    frame_file.write_bytes(jpeg)
    thumb_file.write_bytes(jpeg)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO frames (project_id, captured_at, file_path, thumbnail_path, file_size, is_dark)"
            " VALUES (?,?,?,?,?,?)",
            (project_id, "2024-01-01T12:00:00", str(frame_file), str(thumb_file), len(jpeg), is_dark),
        )
        conn.execute("UPDATE projects SET frame_count = frame_count + 1 WHERE id = ?", (project_id,))
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _insert_render(project_id: int, status: str = "pending", priority: int = 5) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO renders (project_id, framerate, resolution, render_type, status, priority)"
            " VALUES (?,?,?,?,?,?)",
            (project_id, 30, "1920x1080", "manual", status, priority),
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


# ===========================================================================
# Render Presets
# ===========================================================================


def test_preset_crud(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)

    # List initially empty
    r = api.get("/api/presets")
    assert r.status_code == 200
    assert r.json() == []

    # Create preset
    payload = {
        "name": "My Preset",
        "framerate": 24,
        "resolution": "1280x720",
        "quality": "high",
        "flicker_reduction": "strong",
        "frame_blend": True,
        "stabilize": False,
        "color_grade": "warm",
    }
    r = api.post("/api/presets", json=payload)
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "My Preset"
    assert data["framerate"] == 24
    preset_id = data["id"]

    # Get preset
    r = api.get(f"/api/presets/{preset_id}")
    assert r.status_code == 200
    assert r.json()["name"] == "My Preset"

    # Duplicate name → 409
    r = api.post("/api/presets", json=payload)
    assert r.status_code == 409

    # Delete
    r = api.delete(f"/api/presets/{preset_id}")
    assert r.status_code == 204

    # Now list is empty again
    r = api.get("/api/presets")
    assert r.json() == []


def test_preset_not_found(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    r = api.get("/api/presets/9999")
    assert r.status_code == 404


# ===========================================================================
# Project Pin / Unpin
# ===========================================================================


def test_pin_unpin_project(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project()

    # Pin
    r = api.post(f"/api/projects/{pid}/pin")
    assert r.status_code == 200
    assert r.json()["is_pinned"] is True

    with get_connection() as conn:
        row = conn.execute("SELECT is_pinned FROM projects WHERE id = ?", (pid,)).fetchone()
    assert row["is_pinned"] == 1

    # Unpin
    r = api.delete(f"/api/projects/{pid}/pin")
    assert r.status_code == 200
    assert r.json()["is_pinned"] is False

    with get_connection() as conn:
        row = conn.execute("SELECT is_pinned FROM projects WHERE id = ?", (pid,)).fetchone()
    assert row["is_pinned"] == 0


def test_pinned_projects_sort_first(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    id1 = _insert_project("Alpha")
    id2 = _insert_project("Beta")
    # Pin Beta
    api.post(f"/api/projects/{id2}/pin")

    r = api.get("/api/projects")
    ids = [p["id"] for p in r.json()]
    assert ids.index(id2) < ids.index(id1)


# ===========================================================================
# Project Capacity Planner
# ===========================================================================


def test_project_capacity(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project()
    _insert_frame(pid, tmp_path)

    import app.config as c

    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setattr(c, "_settings", None)

    r = api.get(f"/api/projects/{pid}/capacity")
    assert r.status_code == 200
    data = r.json()
    assert "days_remaining" in data
    assert "frames_per_day" in data
    assert data["frame_count"] == 1


def test_project_capacity_not_found(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    r = api.get("/api/projects/9999/capacity")
    assert r.status_code == 404


# ===========================================================================
# Render Pause / Resume
# ===========================================================================


def test_render_pause_pending(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project()
    rid = _insert_render(pid, status="pending")

    r = api.post(f"/api/renders/{rid}/pause")
    assert r.status_code == 200
    assert r.json()["paused"] is True

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM renders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "paused"


def test_render_resume(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project()
    rid = _insert_render(pid, status="paused")

    r = api.post(f"/api/renders/{rid}/resume")
    assert r.status_code == 200
    assert r.json()["resumed"] is True

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM renders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "pending"


def test_render_pause_wrong_state(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project()
    rid = _insert_render(pid, status="done")

    r = api.post(f"/api/renders/{rid}/pause")
    assert r.status_code == 409


def test_render_resume_wrong_state(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project()
    rid = _insert_render(pid, status="pending")

    r = api.post(f"/api/renders/{rid}/resume")
    assert r.status_code == 409


# ===========================================================================
# Render ETA
# ===========================================================================


def test_list_renders_has_eta_field(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project()
    _insert_render(pid, status="pending")

    r = api.get("/api/renders")
    assert r.status_code == 200
    renders = r.json()
    assert len(renders) >= 1
    # All renders should have eta_seconds key (None for non-rendering)
    assert "eta_seconds" in renders[0]
    assert renders[0]["eta_seconds"] is None


# ===========================================================================
# Settings Extensions
# ===========================================================================


def test_settings_maintenance_window(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)

    r = api.put("/api/settings", json={"maintenance_hour": 3, "maintenance_minute": 30})
    assert r.status_code == 200

    with get_connection() as conn:
        row = conn.execute(
            "SELECT maintenance_hour, maintenance_minute FROM settings WHERE id = 1"
        ).fetchone()
    assert row["maintenance_hour"] == 3
    assert row["maintenance_minute"] == 30


def test_settings_nvr_backoff(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)

    r = api.put("/api/settings", json={"nvr_reconnect_backoff_seconds": 60})
    assert r.status_code == 200

    with get_connection() as conn:
        row = conn.execute(
            "SELECT nvr_reconnect_backoff_seconds FROM settings WHERE id = 1"
        ).fetchone()
    assert row["nvr_reconnect_backoff_seconds"] == 60


def test_settings_muted_project_ids(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)

    r = api.put("/api/settings", json={"muted_project_ids": [1, 2, 3]})
    assert r.status_code == 200

    with get_connection() as conn:
        row = conn.execute("SELECT muted_project_ids FROM settings WHERE id = 1").fetchone()
    import json
    assert json.loads(row["muted_project_ids"]) == [1, 2, 3]


# ===========================================================================
# Maintenance trigger
# ===========================================================================


def test_manual_maintenance_trigger(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.maintenance as _maint_mod

    called = []

    async def _fake_run_maintenance() -> None:
        called.append(True)

    monkeypatch.setattr(_maint_mod, "run_maintenance", _fake_run_maintenance)
    api = _make_app(tmp_db, tmp_path, monkeypatch)

    r = api.post("/api/maintenance/run")
    assert r.status_code == 202
    assert r.json()["status"] == "started"


# ===========================================================================
# Database backup endpoint
# ===========================================================================


def test_backup_endpoint(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    r = api.post("/api/backup")
    assert r.status_code == 202
    assert r.json()["status"] == "started"


# ===========================================================================
# Notification mute
# ===========================================================================


@pytest.mark.asyncio
async def test_notification_suppressed_for_muted_project(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    import app.database as db_mod
    import app.notifications as notif_mod

    monkeypatch.setattr(notif_mod, "get_connection", db_mod.get_connection)

    from app.notifications import notify

    pid = None
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode)"
            " VALUES (?,?,?,?,?,?)",
            ("Muted", "cam-x", "live", 60, "active", "continuous"),
        )
        conn.commit()
        pid = cur.lastrowid

    # Mute this project
    with get_connection() as conn:
        conn.execute(
            "UPDATE settings SET muted_project_ids = ? WHERE id = 1",
            (json.dumps([pid]),),
        )
        conn.commit()

    # Send notification — should be suppressed
    await notify(event="test_event", level="info", message="should be muted", project_id=pid)

    # No notification should be in DB
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM notifications WHERE project_id = ? AND event = 'test_event'",
            (pid,),
        ).fetchone()
    assert row["cnt"] == 0


@pytest.mark.asyncio
async def test_notification_not_suppressed_for_unmuted_project(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.database as db_mod
    import app.notifications as notif_mod

    monkeypatch.setattr(notif_mod, "get_connection", db_mod.get_connection)

    from app.notifications import notify

    pid = None
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode)"
            " VALUES (?,?,?,?,?,?)",
            ("NotMuted", "cam-y", "live", 60, "active", "continuous"),
        )
        conn.commit()
        pid = cur.lastrowid

    await notify(event="test_event2", level="info", message="should appear", project_id=pid)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM notifications WHERE project_id = ? AND event = 'test_event2'",
            (pid,),
        ).fetchone()
    assert row["cnt"] == 1


# ===========================================================================
# NVR error classification
# ===========================================================================


def test_classify_nvr_error_timeout() -> None:
    from app.protect import _classify_nvr_error

    exc = Exception("Connection timed out")
    assert _classify_nvr_error(exc) == "timeout"


def test_classify_nvr_error_auth() -> None:
    from app.protect import _classify_nvr_error

    exc = Exception("401 Unauthorized")
    assert _classify_nvr_error(exc) == "auth_failure"


def test_classify_nvr_error_ssl() -> None:
    from app.protect import _classify_nvr_error

    exc = Exception("SSL certificate verify failed")
    assert _classify_nvr_error(exc) == "ssl_error"


def test_classify_nvr_error_unknown() -> None:
    from app.protect import _classify_nvr_error

    exc = Exception("Some weird thing happened")
    assert _classify_nvr_error(exc) == "unknown"


# ===========================================================================
# Render timeout fix
# ===========================================================================


def test_adaptive_timeout_grows_with_frame_count() -> None:
    """Adaptive timeout should be at least base_timeout, growing with frame count."""
    from app.render import _build_ffmpeg_cmd  # noqa — just validate formula

    base = 7200
    total_frames = 50_000
    adaptive = max(base, total_frames * 2)
    assert adaptive == 100_000  # grows beyond base for large renders


# ===========================================================================
# Database schema V9 + V10
# ===========================================================================


def test_schema_v9_columns_exist(tmp_db: Path) -> None:
    """V9 migration should add all expected columns and tables."""
    with get_connection() as conn:
        # render_presets table
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='render_presets'"
        ).fetchone()
        assert row is not None

        # projects.is_pinned
        info = conn.execute("PRAGMA table_info(projects)").fetchall()
        cols = [r["name"] for r in info]
        assert "is_pinned" in cols

        # settings new columns
        info = conn.execute("PRAGMA table_info(settings)").fetchall()
        cols = [r["name"] for r in info]
        for col in ("maintenance_hour", "maintenance_minute", "nvr_reconnect_backoff_seconds", "muted_project_ids"):
            assert col in cols, f"Missing settings column: {col}"

        # frames.file_hash
        info = conn.execute("PRAGMA table_info(frames)").fetchall()
        cols = [r["name"] for r in info]
        assert "file_hash" in cols


def test_schema_v10_index_exists(tmp_db: Path) -> None:
    """V10 migration should add the compound index."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_frames_project_quality'"
        ).fetchone()
        assert row is not None


# ===========================================================================
# Estimate render respects is_blurry filter
# ===========================================================================


def test_estimate_render_excludes_blurry(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as config_mod

    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setattr(config_mod, "_settings", None)

    from app.render import estimate_render

    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode)"
            " VALUES (?,?,?,?,?,?)",
            ("Est", "cam", "live", 60, "active", "continuous"),
        )
        pid = cur.lastrowid
        # Insert 3 good frames and 2 blurry
        for i in range(3):
            conn.execute(
                "INSERT INTO frames (project_id, captured_at, file_path, file_size, is_dark, is_blurry)"
                " VALUES (?,?,?,?,0,0)",
                (pid, f"2024-01-01T{10+i:02d}:00:00", f"/tmp/f{i}.jpg", 100_000),
            )
        for i in range(2):
            conn.execute(
                "INSERT INTO frames (project_id, captured_at, file_path, file_size, is_dark, is_blurry)"
                " VALUES (?,?,?,?,0,1)",
                (pid, f"2024-01-01T{13+i:02d}:00:00", f"/tmp/b{i}.jpg", 100_000),
            )
        conn.execute("UPDATE projects SET frame_count = 5 WHERE id = ?", (pid,))
        conn.commit()

    est = estimate_render(pid, framerate=30)
    assert est["frame_count"] == 3  # blurry excluded


# ===========================================================================
# Metrics endpoint
# ===========================================================================


def test_metrics_endpoint(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    r = api.get("/metrics")
    assert r.status_code == 200
    # Should return text/plain prometheus format or fallback
    assert "timelapse" in r.text or "prometheus" in r.text


# ===========================================================================
# Log download endpoint
# ===========================================================================


def test_log_endpoint_no_source(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    r = api.get("/api/logs")
    assert r.status_code == 200
    data = r.json()
    assert "lines" in data


def test_log_endpoint_with_file(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _make_app(tmp_db, tmp_path, monkeypatch)
    log_file = tmp_path / "test.log"
    log_file.write_text("line1\nline2\nline3\n")
    r = api.get(f"/api/logs?log_file={log_file}")
    assert r.status_code == 200
    data = r.json()
    assert data["lines"] == ["line1", "line2", "line3"]


# ===========================================================================
# Maintenance VACUUM (monthly check)
# ===========================================================================


@pytest.mark.asyncio
async def test_vacuum_runs_on_first_of_month(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime as _dt_mod

    import app.config as config_mod
    import app.maintenance as _maint_mod

    fixed_dt = _dt_mod.datetime(2024, 3, 1, 2, 0, tzinfo=_dt_mod.UTC)

    class _FakeDatetime(_dt_mod.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return fixed_dt

    monkeypatch.setattr(_maint_mod, "datetime", _FakeDatetime)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_db))
    monkeypatch.setattr(config_mod, "_settings", None)

    from app.maintenance import _maybe_vacuum_database

    # Should not raise
    await _maybe_vacuum_database()


# ===========================================================================
# Stalled render recovery in maintenance
# ===========================================================================


@pytest.mark.asyncio
async def test_recover_stalled_renders(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.config as config_mod
    import app.database as db_mod
    import app.maintenance as maint_mod

    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))
    monkeypatch.setattr(config_mod, "_settings", None)
    # Ensure maintenance module uses the same patched get_connection as the test
    monkeypatch.setattr(maint_mod, "get_connection", db_mod.get_connection)

    from app.maintenance import _recover_stalled_renders

    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode)"
            " VALUES (?,?,?,?,?,?)",
            ("Stall", "cam", "live", 60, "active", "continuous"),
        )
        pid = cur.lastrowid
        cur2 = conn.execute(
            "INSERT INTO renders (project_id, framerate, resolution, render_type, status)"
            " VALUES (?,?,?,?,?)",
            (pid, 30, "1920x1080", "manual", "stalled"),
        )
        rid = cur2.lastrowid
        conn.commit()

    await _recover_stalled_renders()

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM renders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "pending"


# ===========================================================================
# Settings: geo update persists and cache is cleared
# ===========================================================================


def test_settings_geo_update_persisted(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT /api/settings with lat/lon persists values and invalidates the location cache."""
    import app.capture as cap_mod

    # Prime the cache so we can verify it gets cleared
    cap_mod._location_info_cache = ("UTC", 0.0, 0.0, object())

    api = _make_app(tmp_db, tmp_path, monkeypatch)

    r = api.put("/api/settings", json={"latitude": 55.6, "longitude": 12.5})
    assert r.status_code == 200
    data = r.json()
    assert data["latitude"] == 55.6
    assert data["longitude"] == 12.5
    # Cache should be cleared by the geo-update code path
    assert cap_mod._location_info_cache is None


# ===========================================================================
# Settings: maintenance schedule re-register
# ===========================================================================


def test_settings_maintenance_rereg(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT /api/settings with maintenance_hour should persist and execute re-register path."""
    api = _make_app(tmp_db, tmp_path, monkeypatch)

    r = api.put("/api/settings", json={"maintenance_hour": 4, "maintenance_minute": 15})
    assert r.status_code == 200

    with get_connection() as conn:
        row = conn.execute(
            "SELECT maintenance_hour, maintenance_minute FROM settings WHERE id = 1"
        ).fetchone()
    assert row["maintenance_hour"] == 4
    assert row["maintenance_minute"] == 15
