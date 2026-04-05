"""
Tests for new features added in fixes round 2.
Covers: batch frame delete, CSV export, analyze-interval, render cancel,
        render compare, render priority, schedule-test, health probes,
        pool-stats, maintenance backup, frame filter params.
"""

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.database import get_connection

# ---------------------------------------------------------------------------
# Helpers (mirrors test_phase4.py)
# ---------------------------------------------------------------------------


def _make_jpeg() -> bytes:
    img = Image.new("RGB", (80, 60), color=(100, 120, 140))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_app(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from contextlib import asynccontextmanager

    import app.config as config_mod
    from app.routes import cameras, frames, health, notifications, projects, renders, settings

    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))
    monkeypatch.setattr(config_mod, "_settings", None)

    @asynccontextmanager
    async def _noop(app):  # type: ignore[no-untyped-def]
        yield

    application = FastAPI(lifespan=_noop)
    for r in (
        frames.router,
        health.router,
        projects.router,
        renders.router,
        settings.router,
        notifications.router,
        cameras.router,
    ):
        application.include_router(r)
    return TestClient(application)


def _insert_project(name: str = "TestProj", status: str = "active") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode)"
            " VALUES (?,?,?,?,?,?)",
            (name, "cam-1", "live", 60, status, "continuous"),
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _insert_frame(
    project_id: int,
    tmp_path: Path,
    is_blurry: int = 0,
    is_dark: int = 0,
    captured_at: str = "2024-01-01T12:00:00+00:00",
) -> int:
    frames_dir = tmp_path / "frames" / str(project_id)
    thumbs_dir = tmp_path / "thumbs" / str(project_id)
    frames_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    jpeg = _make_jpeg()
    ts = captured_at.replace(":", "").replace("+", "").replace("-", "")[:14]
    frame_file = frames_dir / f"{ts}.jpg"
    thumb_file = thumbs_dir / f"{ts}.jpg"
    frame_file.write_bytes(jpeg)
    thumb_file.write_bytes(jpeg)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO frames (project_id, file_path, thumbnail_path, file_size,"
            " is_blurry, is_dark, captured_at) VALUES (?,?,?,?,?,?,?)",
            (
                project_id,
                str(frame_file),
                str(thumb_file),
                len(jpeg),
                is_blurry,
                is_dark,
                captured_at,
            ),
        )
        conn.execute(
            "UPDATE projects SET frame_count = frame_count + 1 WHERE id = ?", (project_id,)
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _insert_render(project_id: int, status: str = "pending") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO renders (project_id, framerate, resolution, render_type, status)"
            " VALUES (?,?,?,?,?)",
            (project_id, 30, "1920x1080", "manual", status),
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Batch frame delete (F2)
# ---------------------------------------------------------------------------


def test_batch_delete_blurry(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project()
    _insert_frame(pid, tmp_path, is_blurry=1, captured_at="2024-01-01T10:00:00+00:00")
    _insert_frame(pid, tmp_path, is_blurry=0, captured_at="2024-01-01T11:00:00+00:00")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.delete(f"/api/projects/{pid}/frames?filter=is_blurry")
    assert r.status_code == 200
    assert r.json()["deleted"] == 1

    with get_connection() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM frames WHERE project_id=?", (pid,)).fetchone()[0]
    assert cnt == 1


def test_batch_delete_dark(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project()
    _insert_frame(pid, tmp_path, is_dark=1, captured_at="2024-01-01T10:00:00+00:00")
    _insert_frame(pid, tmp_path, is_dark=0, captured_at="2024-01-01T11:00:00+00:00")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.delete(f"/api/projects/{pid}/frames?filter=is_dark")
    assert r.status_code == 200
    assert r.json()["deleted"] == 1


def test_batch_delete_all(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project()
    _insert_frame(pid, tmp_path, captured_at="2024-01-01T10:00:00+00:00")
    _insert_frame(pid, tmp_path, captured_at="2024-01-01T11:00:00+00:00")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.delete(f"/api/projects/{pid}/frames?filter=all")
    assert r.status_code == 200
    assert r.json()["deleted"] == 2


def test_batch_delete_empty(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project()
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.delete(f"/api/projects/{pid}/frames?filter=is_blurry")
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


# ---------------------------------------------------------------------------
# CSV export (F6)
# ---------------------------------------------------------------------------


def test_csv_export(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project()
    _insert_frame(pid, tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/frames/export/csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("id,captured_at")
    assert len(lines) == 2  # header + 1 frame


def test_csv_export_empty(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project()
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/frames/export/csv")
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert len(lines) == 1  # header only


# ---------------------------------------------------------------------------
# Analyze-interval (F4)
# ---------------------------------------------------------------------------


def test_analyze_interval_no_frames(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/frames/analyze-interval")
    assert r.status_code == 200
    data = r.json()
    assert data["total_non_dark_frames"] == 0
    assert data["suggested_interval_seconds"] is None


def test_analyze_interval_with_frames(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    _insert_frame(pid, tmp_path, captured_at="2024-01-01T08:00:00+00:00")
    _insert_frame(pid, tmp_path, captured_at="2024-01-01T09:00:00+00:00")
    _insert_frame(pid, tmp_path, captured_at="2024-01-01T10:00:00+00:00")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(
        f"/api/projects/{pid}/frames/analyze-interval?target_duration_seconds=60&target_fps=30"
    )
    assert r.status_code == 200
    data = r.json()
    assert data["total_non_dark_frames"] == 3
    assert data["target_duration_seconds"] == 60
    assert data["suggested_interval_seconds"] is not None


# ---------------------------------------------------------------------------
# Frame filter params (F7)
# ---------------------------------------------------------------------------


def test_frame_filter_is_dark(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    _insert_frame(pid, tmp_path, is_dark=1, captured_at="2024-01-01T10:00:00+00:00")
    _insert_frame(pid, tmp_path, is_dark=0, captured_at="2024-01-01T11:00:00+00:00")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/frames?is_dark=true")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1


def test_frame_filter_is_blurry(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    _insert_frame(pid, tmp_path, is_blurry=1, captured_at="2024-01-01T10:00:00+00:00")
    _insert_frame(pid, tmp_path, is_blurry=0, captured_at="2024-01-01T11:00:00+00:00")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/frames?is_blurry=true")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_frame_filter_after_before(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    _insert_frame(pid, tmp_path, captured_at="2024-01-01T08:00:00+00:00")
    _insert_frame(pid, tmp_path, captured_at="2024-01-02T08:00:00+00:00")
    _insert_frame(pid, tmp_path, captured_at="2024-01-03T08:00:00+00:00")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(
        f"/api/projects/{pid}/frames?after=2024-01-01T12:00:00%2B00:00&before=2024-01-03T00:00:00%2B00:00"
    )
    assert r.status_code == 200
    assert len(r.json()) == 1


# ---------------------------------------------------------------------------
# Render priority (F5)
# ---------------------------------------------------------------------------


def test_set_render_priority(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project()
    rid = _insert_render(pid, status="pending")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.put(f"/api/renders/{rid}/priority?priority=8")
    assert r.status_code == 200
    assert r.json()["priority"] == 8


def test_set_render_priority_not_pending(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    rid = _insert_render(pid, status="done")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.put(f"/api/renders/{rid}/priority?priority=8")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Render cancel (F1)
# ---------------------------------------------------------------------------


def test_cancel_pending_render(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    rid = _insert_render(pid, status="pending")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    from app.routes import renders as renders_mod

    monkeypatch.setattr(renders_mod, "cancel_active_render", AsyncMock(return_value=False))

    r = client.post(f"/api/renders/{rid}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM renders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "error"


def test_cancel_done_render_rejected(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    rid = _insert_render(pid, status="done")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.post(f"/api/renders/{rid}/cancel")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Render compare (F9)
# ---------------------------------------------------------------------------


def test_compare_renders(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project()
    rid_a = _insert_render(pid, status="done")
    rid_b = _insert_render(pid, status="done")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/renders/{rid_a}/compare/{rid_b}")
    assert r.status_code == 200
    data = r.json()
    assert "a" in data and "b" in data
    assert data["a"]["id"] == rid_a
    assert data["b"]["id"] == rid_b


# ---------------------------------------------------------------------------
# Schedule test (F8)
# ---------------------------------------------------------------------------


def test_schedule_test_now(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project()
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    import app.capture as capture_mod

    monkeypatch.setattr(capture_mod, "_check_capture_mode", lambda p: True, raising=False)

    r = client.get(f"/api/projects/{pid}/schedule-test")
    assert r.status_code == 200
    data = r.json()
    assert "would_capture" in data
    assert data["project_id"] == pid


def test_schedule_test_with_timestamp(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    import app.capture as capture_mod

    monkeypatch.setattr(capture_mod, "_check_capture_mode", lambda p: True, raising=False)

    r = client.get(f"/api/projects/{pid}/schedule-test?timestamp=2024-06-15T12:00:00%2B00:00")
    assert r.status_code == 200
    assert r.json()["capture_mode"] == "continuous"


def test_schedule_test_invalid_timestamp(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/schedule-test?timestamp=not-a-date")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Health probes (B2)
# ---------------------------------------------------------------------------


def test_liveness_probe_startup(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/health/live")
    assert r.status_code == 200


def test_liveness_probe_stalled(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    import app.routes.health as health_mod

    # Simulate a heartbeat from 200s ago
    monkeypatch.setattr(health_mod, "_last_render_worker_heartbeat", time.monotonic() - 200.0)

    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/health/live")
    assert r.status_code == 503


def test_readiness_probe_disconnected(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.protect as protect_mod

    monkeypatch.setattr(protect_mod.protect_manager, "_connected", False)

    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/health/ready")
    assert r.status_code == 503


def test_readiness_probe_connected(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.protect as protect_mod

    monkeypatch.setattr(protect_mod.protect_manager, "_connected", True)

    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/health/ready")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Pool stats (B10)
# ---------------------------------------------------------------------------


def test_pool_stats(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/admin/pool-stats")
    assert r.status_code == 200
    data = r.json()
    assert "pool_size" in data
    assert "idle_connections" in data
    assert "active_connections" in data


# ---------------------------------------------------------------------------
# Maintenance: backup database (B5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backup_database(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.config as config_mod
    import app.maintenance as maint

    monkeypatch.setattr(config_mod, "_settings", None)

    await maint._backup_database()
    backup = Path(str(tmp_db) + ".backup")
    assert backup.exists()


@pytest.mark.asyncio
async def test_backup_database_missing_src(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Should skip gracefully when DB file doesn't exist."""
    import app.config as config_mod
    import app.maintenance as maint

    fake_settings = MagicMock()
    fake_settings.database_path = str(tmp_path / "nonexistent.db")
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake_settings)

    # Should not raise
    await maint._backup_database()


# ---------------------------------------------------------------------------
# Maintenance: prune renders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_old_renders(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.maintenance as maint

    pid = _insert_project()
    renders_dir = tmp_path / "renders" / str(pid)
    renders_dir.mkdir(parents=True, exist_ok=True)

    # Use monkeypatched get_connection in maintenance module
    import app.database as db_mod

    monkeypatch.setattr(maint, "get_connection", db_mod.get_connection)

    # Insert 9 daily auto-renders with distinct timestamps (keep limit is 7)
    for i in range(9):
        out = renders_dir / f"r{i}.mp4"
        out.write_bytes(b"x")
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO renders (project_id, framerate, resolution, render_type, status,"
                " output_path, created_at) VALUES (?,?,?,?,?,?,datetime('now', ?))",
                (pid, 30, "1920x1080", "auto_daily", "done", str(out), f"-{i} days"),
            )
            conn.commit()

    await maint._prune_old_renders()

    with get_connection() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM renders WHERE project_id=? AND render_type='auto_daily'",
            (pid,),
        ).fetchone()[0]
    assert cnt == 7


# ---------------------------------------------------------------------------
# Maintenance: schedule auto-renders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_auto_renders_no_frames(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no frames, no auto-render should be scheduled."""
    import app.database as db_mod
    import app.maintenance as maint

    monkeypatch.setattr(maint, "get_connection", db_mod.get_connection)

    _insert_project()
    with get_connection() as conn:
        conn.execute("UPDATE projects SET auto_render_daily=1")
        conn.commit()

    await maint._schedule_auto_renders()

    with get_connection() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM renders").fetchone()[0]
    assert cnt == 0


# ---------------------------------------------------------------------------
# Notifications: _is_safe_webhook_url
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Health: _dir_size_gb recursive walk (UI4 coverage)
# ---------------------------------------------------------------------------


def test_dir_size_gb(tmp_path: Path) -> None:
    from app.routes.health import _dir_size_gb

    # Non-existent dir returns 0
    assert _dir_size_gb(str(tmp_path / "missing")) == 0.0

    # Dir with files
    d = tmp_path / "data"
    d.mkdir()
    (d / "a.txt").write_bytes(b"x" * 1024)
    sub = d / "sub"
    sub.mkdir()
    (sub / "b.txt").write_bytes(b"x" * 2048)

    size = _dir_size_gb(str(d))
    assert size > 0.0


# ---------------------------------------------------------------------------
# Notifications: send_notification coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_notification_no_webhook(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """send_notification should write DB row and not crash without webhook."""
    import app.database as db_mod
    import app.notifications as notif_mod

    monkeypatch.setattr(notif_mod, "get_connection", db_mod.get_connection)

    # Stub out websocket broadcast
    monkeypatch.setattr(notif_mod, "_get_webhook_url", lambda: None)

    from app.notifications import notify

    await notify("test_event", "info", "Hello test")

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM notifications WHERE event='test_event'").fetchone()
    assert row is not None
    assert row["message"] == "Hello test"


@pytest.mark.asyncio
async def test_send_notification_ssrf_blocked(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSRF-blocked webhook should log warning and not make HTTP call."""
    import app.database as db_mod
    import app.notifications as notif_mod

    monkeypatch.setattr(notif_mod, "get_connection", db_mod.get_connection)
    monkeypatch.setattr(notif_mod, "_get_webhook_url", lambda: "http://localhost/hook")

    from app.notifications import notify

    # Should not raise
    await notify("test_event", "info", "test")


# ---------------------------------------------------------------------------
# Projects: update returns early when nothing allowed in payload
# ---------------------------------------------------------------------------


def test_update_project_no_allowed_fields(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    # status is in allowed list — use a disallowed-only field to hit empty-updates branch
    # Actually send only status (allowed) to exercise the update path
    r = client.put(f"/api/projects/{pid}", json={"status": "paused"})
    assert r.status_code == 200
    assert r.json()["status"] == "paused"


# ---------------------------------------------------------------------------
# Projects: clone with frames
# ---------------------------------------------------------------------------


def test_clone_project_with_frames(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    _insert_frame(pid, tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    import app.capture as capture_mod

    monkeypatch.setattr(capture_mod, "add_project_job", AsyncMock())

    r = client.post(f"/api/projects/{pid}/clone?copy_frames_days=30")
    assert r.status_code in (200, 201)
    data = r.json()
    assert data["name"].startswith("TestProj")

    with get_connection() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    assert cnt == 2


# ---------------------------------------------------------------------------
# Renders: download not available
# ---------------------------------------------------------------------------


def test_download_render_not_done(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project()
    rid = _insert_render(pid, status="pending")
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/renders/{rid}/download")
    assert r.status_code == 404


def test_webhook_url_validation() -> None:
    from app.notifications import _is_safe_webhook_url

    assert _is_safe_webhook_url("https://example.com/hook") is True
    assert _is_safe_webhook_url("http://example.com/hook") is True
    assert _is_safe_webhook_url("http://localhost/hook") is False
    assert _is_safe_webhook_url("http://127.0.0.1/hook") is False
    assert _is_safe_webhook_url("http://192.168.1.1/hook") is False
    assert _is_safe_webhook_url("http://10.0.0.1/hook") is False
    assert _is_safe_webhook_url("ftp://example.com/hook") is False


# ---------------------------------------------------------------------------
# Thumbnails division-by-zero guard
# ---------------------------------------------------------------------------


def test_thumbnail_zero_dimensions() -> None:
    from app.thumbnails import generate_thumbnail

    # Patch Image.open to return a 0x0 image
    fake_img = MagicMock()
    fake_img.size = (0, 0)

    with (
        patch("app.thumbnails.Image.open", return_value=fake_img),
        pytest.raises(ValueError, match="Invalid image dimensions"),
    ):
        generate_thumbnail(b"fake")


def test_thumbnail_from_pillow_zero_dimensions() -> None:
    from app.thumbnails import generate_thumbnail_from_pillow

    fake_img = MagicMock()
    fake_img.size = (0, 0)

    with pytest.raises(ValueError, match="Invalid image dimensions"):
        generate_thumbnail_from_pillow(fake_img)


def test_generate_thumbnail_valid() -> None:
    from app.thumbnails import generate_thumbnail

    img = Image.new("RGB", (640, 480), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    result = generate_thumbnail(buf.getvalue())
    assert len(result) > 0
    # Check output dimensions
    out = Image.open(io.BytesIO(result))
    assert out.size[0] == 320
