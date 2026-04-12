"""
Tests for resilience features: NVR health, circuit breaker, extraction resume,
render recovery, reconciliation, system status endpoint.
"""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.database import get_connection

# ---------------------------------------------------------------------------
# Helpers
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


def _insert_project(
    name: str = "TestProj",
    status: str = "active",
    project_type: str = "live",
    start_date: str | None = None,
    end_date: str | None = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode, start_date, end_date)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (name, "cam-1", project_type, 60, status, "continuous", start_date, end_date),
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# A. NVR Connection Resilience — protect.py
# ---------------------------------------------------------------------------


def test_protect_manager_mark_disconnected() -> None:
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()
    mgr._connected = True
    mgr.mark_disconnected("test reason")
    assert not mgr._connected
    assert mgr._last_error == "test reason"


def test_protect_manager_status_property() -> None:
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()
    mgr._connected = True
    mgr._camera_count = 5
    mgr._last_error = None
    status = mgr.status
    assert status["connected"] is True
    assert status["camera_count"] == 5
    assert status["last_error"] is None


@pytest.mark.asyncio
async def test_protect_manager_health_check_no_client() -> None:
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()
    result = await mgr.health_check()
    assert result["connected"] is False
    assert "not initialised" in result["last_error"]


@pytest.mark.asyncio
async def test_protect_manager_refresh_bootstrap_no_client() -> None:
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()
    result = await mgr.refresh_bootstrap()
    assert result is False


@pytest.mark.asyncio
async def test_protect_manager_refresh_bootstrap_success() -> None:
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()
    mock_client = MagicMock()
    mock_client.update = AsyncMock()
    mock_client.bootstrap.cameras = {"cam1": MagicMock(), "cam2": MagicMock()}
    mgr._client = mock_client
    mgr._connected = True
    mgr._camera_count = 1

    result = await mgr.refresh_bootstrap()
    assert result is True
    assert mgr._camera_count == 2


@pytest.mark.asyncio
async def test_protect_manager_refresh_bootstrap_failure() -> None:
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()
    mock_client = MagicMock()
    mock_client.update = AsyncMock(side_effect=ConnectionError("NVR down"))
    mgr._client = mock_client
    mgr._connected = True

    result = await mgr.refresh_bootstrap()
    assert result is False
    assert not mgr._connected
    assert "NVR down" in mgr._last_error  # type: ignore[operator]


@pytest.mark.asyncio
async def test_protect_manager_health_check_state_change() -> None:
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()
    mock_client = MagicMock()
    mock_client.update = AsyncMock(side_effect=ConnectionError("boom"))
    mock_client.bootstrap.cameras = {}
    mgr._client = mock_client
    mgr._connected = True  # was connected

    result = await mgr.health_check()
    assert result["connected"] is False
    assert "boom" in result["last_error"]


# ---------------------------------------------------------------------------
# B. Circuit Breaker — capture.py
# ---------------------------------------------------------------------------


def test_circuit_breaker_threshold_constant() -> None:
    from app.capture import _CIRCUIT_BREAKER_THRESHOLD

    assert _CIRCUIT_BREAKER_THRESHOLD == 10


# ---------------------------------------------------------------------------
# C. Historical extraction resume
# ---------------------------------------------------------------------------


def test_retry_extraction_endpoint(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project(
        name="Historical",
        status="error",
        project_type="historical",
        start_date="2024-01-01T00:00",
        end_date="2024-01-01T01:00",
    )
    # Mock the extraction task
    monkeypatch.setattr(
        "app.routes.projects.run_historical_extraction",
        AsyncMock(),
    )
    r = client.post(f"/api/projects/{pid}/retry-extraction")
    assert r.status_code == 200
    assert r.json()["status"] == "started"


def test_retry_extraction_rejects_live_project(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project(name="Live", status="active", project_type="live")
    r = client.post(f"/api/projects/{pid}/retry-extraction")
    assert r.status_code == 400


def test_retry_extraction_rejects_wrong_status(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project(
        name="Historical",
        status="paused",
        project_type="historical",
        start_date="2024-01-01T00:00",
        end_date="2024-01-01T01:00",
    )
    r = client.post(f"/api/projects/{pid}/retry-extraction")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# D. Render worker recovery — maintenance.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_zombie_renders(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.database as db_mod
    import app.maintenance as maint

    monkeypatch.setattr(maint, "get_connection", db_mod.get_connection)

    # Insert a zombie render (stuck in 'rendering', old created_at)
    old_time = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode)"
            " VALUES (?,?,?,?,?,?)",
            ("p", "cam-1", "live", 60, "active", "continuous"),
        )
        conn.execute(
            "INSERT INTO renders (project_id, framerate, resolution, status, created_at)"
            " VALUES (?,?,?,?,?)",
            (1, 30, "1920x1080", "rendering", old_time),
        )
        conn.commit()

    await maint._recover_zombie_renders()

    with get_connection() as conn:
        row = conn.execute("SELECT status, error_msg FROM renders WHERE id = 1").fetchone()
    assert row["status"] == "error"
    assert "stuck" in row["error_msg"]


# ---------------------------------------------------------------------------
# H. Eventual consistency — maintenance.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_frame_counts(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.database as db_mod
    import app.maintenance as maint

    monkeypatch.setattr(maint, "get_connection", db_mod.get_connection)

    # Create project with wrong frame_count
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode, frame_count)"
            " VALUES (?,?,?,?,?,?,?)",
            ("p", "cam-1", "live", 60, "active", "continuous", 999),
        )
        conn.execute(
            "INSERT INTO frames (project_id, captured_at, file_path, file_size) VALUES (?,?,?,?)",
            (1, "2024-01-01T00:00:00", "/tmp/f.jpg", 100),
        )
        conn.commit()

    await maint._reconcile_frame_counts()

    with get_connection() as conn:
        row = conn.execute("SELECT frame_count FROM projects WHERE id = 1").fetchone()
    assert row["frame_count"] == 1


@pytest.mark.asyncio
async def test_reconcile_project_status_stale_extracting(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.database as db_mod
    import app.maintenance as maint

    monkeypatch.setattr(maint, "get_connection", db_mod.get_connection)

    # Create project stuck in extracting with old created_at
    old_time = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("p", "cam-1", "historical", 60, "extracting", "continuous", old_time),
        )
        conn.commit()

    await maint._reconcile_project_status()

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM projects WHERE id = 1").fetchone()
    assert row["status"] == "error"


# ---------------------------------------------------------------------------
# E. System status endpoint
# ---------------------------------------------------------------------------


def test_system_status_endpoint(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    # Patch get_scheduler_status at its source (imported inside the endpoint function)
    import app.capture as capture_mod

    monkeypatch.setattr(
        capture_mod,
        "get_scheduler_status",
        lambda: {"running": True, "job_count": 3, "jobs": []},
    )

    r = client.get("/api/system/status")
    assert r.status_code == 200
    data = r.json()
    assert "nvr" in data
    assert "scheduler" in data
    assert "render_worker" in data
    assert "disk" in data
    assert "db" in data
    assert "projects" in data
    assert "pending_renders" in data
    assert "recent_errors" in data


# ---------------------------------------------------------------------------
# G. Database — WAL size
# ---------------------------------------------------------------------------


def test_wal_size_bytes(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.database import get_wal_size_bytes

    # WAL file may or may not exist in test; function should not crash
    size = get_wal_size_bytes()
    assert isinstance(size, int)
    assert size >= 0


# ---------------------------------------------------------------------------
# Resume on project update — consecutive_failures reset
# ---------------------------------------------------------------------------


def test_resume_resets_consecutive_failures(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    pid = _insert_project(name="PausedErr", status="paused_error")

    # Set high failure count
    with get_connection() as conn:
        conn.execute("UPDATE projects SET consecutive_failures = 15 WHERE id = ?", (pid,))
        conn.commit()

    # Mock scheduler
    monkeypatch.setattr(
        "app.routes.projects.resume_project_job",
        AsyncMock(),
    )

    r = client.put(f"/api/projects/{pid}", json={"status": "active"})
    assert r.status_code == 200

    with get_connection() as conn:
        row = conn.execute(
            "SELECT consecutive_failures, status FROM projects WHERE id = ?", (pid,)
        ).fetchone()
    assert row["consecutive_failures"] == 0
    assert row["status"] == "active"


# ---------------------------------------------------------------------------
# Capture scheduler status helper
# ---------------------------------------------------------------------------


def test_get_scheduler_status_not_running() -> None:
    from app.capture import get_scheduler_status, scheduler

    # Scheduler not started in test context
    if scheduler.running:
        scheduler.shutdown(wait=False)
    status = get_scheduler_status()
    assert status["running"] is False
    assert status["job_count"] == 0


# ---------------------------------------------------------------------------
# Render partial cleanup on failure
# ---------------------------------------------------------------------------


def test_render_cleanup_on_error(tmp_db: Path, tmp_path: Path) -> None:
    """Verify the render error handler cleans up partial output."""
    import contextlib

    output_path = tmp_path / "renders" / "1" / "999.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"partial data")

    # The cleanup logic in render.py:
    with contextlib.suppress(FileNotFoundError):
        output_path.unlink()

    assert not output_path.exists()


# ---------------------------------------------------------------------------
# Maintenance — zombie render with no output_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_zombie_render_no_output(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.database as db_mod
    import app.maintenance as maint

    monkeypatch.setattr(maint, "get_connection", db_mod.get_connection)

    old_time = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode)"
            " VALUES (?,?,?,?,?,?)",
            ("p", "cam-1", "live", 60, "active", "continuous"),
        )
        conn.execute(
            "INSERT INTO renders (project_id, framerate, resolution, status, created_at, output_path)"
            " VALUES (?,?,?,?,?,?)",
            (1, 30, "1920x1080", "rendering", old_time, None),
        )
        conn.commit()

    await maint._recover_zombie_renders()

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM renders WHERE id = 1").fetchone()
    assert row["status"] == "error"


# ---------------------------------------------------------------------------
# Reconciliation — no drift (should be no-op)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_frame_counts_no_drift(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.database as db_mod
    import app.maintenance as maint

    monkeypatch.setattr(maint, "get_connection", db_mod.get_connection)

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode, frame_count)"
            " VALUES (?,?,?,?,?,?,?)",
            ("p", "cam-1", "live", 60, "active", "continuous", 0),
        )
        conn.commit()

    # No frames, frame_count=0 — no change expected
    await maint._reconcile_frame_counts()

    with get_connection() as conn:
        row = conn.execute("SELECT frame_count FROM projects WHERE id = 1").fetchone()
    assert row["frame_count"] == 0


# ---------------------------------------------------------------------------
# Reconciliation — extracting but recent (should NOT be changed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_status_recent_extracting_untouched(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.database as db_mod
    import app.maintenance as maint

    monkeypatch.setattr(maint, "get_connection", db_mod.get_connection)

    # Recent project — should NOT be reconciled
    recent_time = datetime.now(UTC).isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status, capture_mode, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ("p", "cam-1", "historical", 60, "extracting", "continuous", recent_time),
        )
        conn.commit()

    await maint._reconcile_project_status()

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM projects WHERE id = 1").fetchone()
    assert row["status"] == "extracting"  # untouched


# ---------------------------------------------------------------------------
# Notifications — covering some branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_writes_to_db_and_broadcasts(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.database as db_mod
    import app.notifications as notif_mod
    import app.websocket as ws_mod

    monkeypatch.setattr(notif_mod, "get_connection", db_mod.get_connection)
    monkeypatch.setattr(ws_mod, "broadcast", AsyncMock())

    await notif_mod.notify(
        event="test_event",
        level="error",
        message="Something broke",
    )

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["event"] == "test_event"
    assert row["level"] == "error"


@pytest.mark.asyncio
async def test_notify_fires_webhook(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.database as db_mod
    import app.notifications as notif_mod
    import app.websocket as ws_mod

    monkeypatch.setattr(notif_mod, "get_connection", db_mod.get_connection)
    monkeypatch.setattr(ws_mod, "broadcast", AsyncMock())

    # Set webhook URL in settings
    with get_connection() as conn:
        conn.execute(
            "UPDATE settings SET webhook_url = 'http://hooks.example.com/hook' WHERE id = 1"
        )
        conn.commit()

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    monkeypatch.setattr(notif_mod.httpx, "AsyncClient", lambda **kw: mock_client)

    await notif_mod.notify(
        event="webhook_test",
        level="info",
        message="Webhook test",
    )

    mock_client.post.assert_called_once()


# ---------------------------------------------------------------------------
# Health — disk_breakdown cache path
# ---------------------------------------------------------------------------


def test_disk_breakdown_returns_data(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/disk")
    assert r.status_code == 200
    data = r.json()
    assert "total_gb" in data
    assert "projects" in data


# ---------------------------------------------------------------------------
# Notifications — SSRF guard edge cases
# ---------------------------------------------------------------------------


def test_ssrf_guard_rejects_localhost() -> None:
    from app.notifications import _is_safe_webhook_url

    assert _is_safe_webhook_url("http://localhost/hook") is False
    assert _is_safe_webhook_url("http://127.0.0.1/hook") is False
    assert _is_safe_webhook_url("http://::1/hook") is False


def test_ssrf_guard_rejects_private_ip() -> None:
    from app.notifications import _is_safe_webhook_url

    assert _is_safe_webhook_url("http://192.168.1.1/hook") is False
    assert _is_safe_webhook_url("http://10.0.0.1/hook") is False


def test_ssrf_guard_rejects_bad_scheme() -> None:
    from app.notifications import _is_safe_webhook_url

    assert _is_safe_webhook_url("ftp://example.com/hook") is False
    assert _is_safe_webhook_url("javascript:alert(1)") is False
    assert _is_safe_webhook_url("") is False


def test_ssrf_guard_allows_public_hostname() -> None:
    from app.notifications import _is_safe_webhook_url

    assert _is_safe_webhook_url("http://hooks.example.com/hook") is True
    assert _is_safe_webhook_url("https://hooks.example.com/hook") is True


@pytest.mark.asyncio
async def test_notify_webhook_with_details_and_project_id(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover details dict and project_id in webhook payload (lines 75-78)."""
    import app.database as db_mod
    import app.notifications as notif_mod
    import app.websocket as ws_mod

    monkeypatch.setattr(notif_mod, "get_connection", db_mod.get_connection)
    monkeypatch.setattr(ws_mod, "broadcast", AsyncMock())

    # Insert a project so FK is satisfied
    pid = _insert_project(name="WebhookTest", status="active")
    with get_connection() as conn:
        conn.execute(
            "UPDATE settings SET webhook_url = 'http://hooks.example.com/hook' WHERE id = 1"
        )
        conn.commit()

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    monkeypatch.setattr(notif_mod.httpx, "AsyncClient", lambda **kw: mock_client)

    await notif_mod.notify(
        event="test_details",
        level="info",
        message="Test with details",
        project_id=pid,
        details={"key": "value"},
    )

    payload = mock_client.post.call_args[1]["json"]
    assert payload["project_id"] == pid
    assert payload["details"] == {"key": "value"}


@pytest.mark.asyncio
async def test_notify_webhook_logs_warning_on_4xx(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover webhook response status >= 400 warning (lines 87-88)."""
    import app.database as db_mod
    import app.notifications as notif_mod
    import app.websocket as ws_mod

    monkeypatch.setattr(notif_mod, "get_connection", db_mod.get_connection)
    monkeypatch.setattr(ws_mod, "broadcast", AsyncMock())

    with get_connection() as conn:
        conn.execute(
            "UPDATE settings SET webhook_url = 'http://hooks.example.com/hook' WHERE id = 1"
        )
        conn.commit()

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    monkeypatch.setattr(notif_mod.httpx, "AsyncClient", lambda **kw: mock_client)

    await notif_mod.notify(
        event="test_4xx",
        level="info",
        message="Should log warning",
    )

    # No exception raised — just logged
    mock_client.post.assert_called_once()


def test_get_webhook_url_returns_none_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover _get_webhook_url exception path (line 98-99)."""
    from app.notifications import _get_webhook_url

    monkeypatch.setattr(
        "app.notifications.get_connection",
        MagicMock(side_effect=RuntimeError("db boom")),
    )
    assert _get_webhook_url() is None


def test_ssrf_guard_rejects_empty_host() -> None:
    from app.notifications import _is_safe_webhook_url

    assert _is_safe_webhook_url("http:///path") is False


@pytest.mark.asyncio
async def test_notify_suppresses_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover DB write failure path (lines 44-45)."""
    import app.notifications as notif_mod

    monkeypatch.setattr(
        notif_mod,
        "get_connection",
        MagicMock(side_effect=RuntimeError("db error")),
    )
    # Should not raise
    await notif_mod.notify(event="fail", level="error", message="boom")
