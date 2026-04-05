"""
Tests for Phase 4 additions and coverage gaps.

Covers: disk endpoint, settings watermark, NVR tester, frame delete,
        blurry frames, GIF export, cursor pagination, ETag on thumbnails,
        global renders queue, project clone, notifications bulk-delete.
"""

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.database import get_connection

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_jpeg() -> bytes:
    img = Image.new("RGB", (100, 80), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_app(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Full API client with DB pointing at tmp_db."""
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


def _insert_project(tmp_path: Path) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status)"
            " VALUES (?,?,?,?,?)",
            ("TestProj", "cam-1", "live", 60, "active"),
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _insert_frame(project_id: int, tmp_path: Path, is_blurry: int = 0) -> int:
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
            "INSERT INTO frames (project_id, file_path, thumbnail_path, file_size, is_blurry)"
            " VALUES (?,?,?,?,?)",
            (project_id, str(frame_file), str(thumb_file), len(jpeg), is_blurry),
        )
        conn.execute(
            "UPDATE projects SET frame_count = frame_count + 1 WHERE id = ?", (project_id,)
        )
        conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# /api/health and /api/disk
# ---------------------------------------------------------------------------


def test_health_endpoint(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
    assert "disk_free_gb" in data


def test_disk_endpoint(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _insert_project(tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/disk")
    assert r.status_code == 200
    data = r.json()
    assert "total_gb" in data
    assert "projects" in data
    assert isinstance(data["projects"], list)


# ---------------------------------------------------------------------------
# Frame delete
# ---------------------------------------------------------------------------


def test_delete_frame(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project(tmp_path)
    fid = _insert_frame(pid, tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.delete(f"/api/projects/{pid}/frames/{fid}")
    assert r.status_code == 204

    # Row should be gone
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM frames WHERE id = ?", (fid,)).fetchone()
    assert row is None


def test_delete_frame_404(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project(tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.delete(f"/api/projects/{pid}/frames/9999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Blurry frames endpoint
# ---------------------------------------------------------------------------


def test_blurry_frames(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project(tmp_path)
    _insert_frame(pid, tmp_path, is_blurry=0)
    _insert_frame(pid, tmp_path, is_blurry=1)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/frames/blurry")
    assert r.status_code == 200
    data = r.json()
    # Endpoint returns only blurry frames (is_blurry=1), so exactly 1 result
    assert len(data) == 1
    # Each row has sharpness_score field
    assert "sharpness_score" in data[0]


# ---------------------------------------------------------------------------
# Cursor-based pagination
# ---------------------------------------------------------------------------


def test_cursor_pagination(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project(tmp_path)
    [_insert_frame(pid, tmp_path) for _ in range(5)]
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    # First page (no cursor)
    r1 = client.get(f"/api/projects/{pid}/frames?limit=3")
    assert r1.status_code == 200
    page1 = r1.json()
    assert len(page1) == 3

    # Second page using cursor
    last_id = page1[-1]["id"]
    r2 = client.get(f"/api/projects/{pid}/frames?limit=3&after_id={last_id}")
    assert r2.status_code == 200
    page2 = r2.json()
    assert len(page2) == 2
    assert all(f["id"] > last_id for f in page2)


# ---------------------------------------------------------------------------
# ETag on thumbnails
# ---------------------------------------------------------------------------


def test_thumbnail_etag(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project(tmp_path)
    fid = _insert_frame(pid, tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r1 = client.get(f"/api/projects/{pid}/frames/{fid}/thumbnail")
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag is not None

    # Conditional request should get 304
    r2 = client.get(
        f"/api/projects/{pid}/frames/{fid}/thumbnail",
        headers={"if-none-match": etag},
    )
    assert r2.status_code == 304


# ---------------------------------------------------------------------------
# Global renders queue
# ---------------------------------------------------------------------------


def test_global_renders_queue(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project(tmp_path)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO renders (project_id, framerate, resolution, render_type, status)"
            " VALUES (?,?,?,?,?)",
            (pid, 30, "1920x1080", "manual", "pending"),
        )
        conn.commit()

    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/renders")
    assert r.status_code == 200
    data = r.json()
    assert len(data) >= 1
    assert data[0]["project_name"] == "TestProj"


# ---------------------------------------------------------------------------
# Project clone
# ---------------------------------------------------------------------------


def test_clone_project(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.capture as capture_mod

    monkeypatch.setattr(capture_mod, "add_project_job", AsyncMock())

    pid = _insert_project(tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.post(f"/api/projects/{pid}/clone")
    assert r.status_code == 201
    data = r.json()
    assert "(copy)" in data["name"]
    assert data["id"] != pid


# ---------------------------------------------------------------------------
# Settings: watermark upload + clear
# ---------------------------------------------------------------------------


def test_watermark_upload_and_clear(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.config as config_mod

    # Point DATABASE_PATH at the test DB so settings table exists
    monkeypatch.setenv("DATABASE_PATH", str(tmp_db))
    monkeypatch.setattr(config_mod, "_settings", None)

    client = _make_app(tmp_db, tmp_path, monkeypatch)

    jpeg = _make_jpeg()
    r = client.post(
        "/api/settings/watermark",
        files={"file": ("logo.png", jpeg, "image/png")},
    )
    assert r.status_code == 200
    assert r.json()["watermark_path"] is not None

    # Clear it
    r2 = client.delete("/api/settings/watermark")
    assert r2.status_code == 204

    # DB should be cleared
    with get_connection() as conn:
        row = conn.execute("SELECT watermark_path FROM settings WHERE id=1").fetchone()
    assert row is None or row["watermark_path"] is None


# ---------------------------------------------------------------------------
# Settings: NVR test
# ---------------------------------------------------------------------------


def test_nvr_test_connected(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.protect as protect_mod

    cam = MagicMock()
    mock_client = MagicMock()
    mock_client.bootstrap = MagicMock()
    mock_client.bootstrap.cameras = {"c1": cam, "c2": cam}
    monkeypatch.setattr(
        protect_mod.protect_manager, "get_client", AsyncMock(return_value=mock_client)
    )

    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/settings/nvr-test")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["camera_count"] == 2


def test_nvr_test_offline(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.protect as protect_mod

    monkeypatch.setattr(
        protect_mod.protect_manager,
        "get_client",
        AsyncMock(side_effect=RuntimeError("NVR offline")),
    )

    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/settings/nvr-test")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "NVR offline" in data["error"]


# ---------------------------------------------------------------------------
# Notifications bulk delete
# ---------------------------------------------------------------------------


def test_zip_export(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project(tmp_path)
    _insert_frame(pid, tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/frames/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    # Should be valid non-empty ZIP content
    assert len(r.content) > 0


def test_gif_status_no_job(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project(tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/gif/status")
    assert r.status_code == 404


def test_gif_download_not_ready(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project(tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/gif/download")
    assert r.status_code == 404


def test_daily_stats(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project(tmp_path)
    _insert_frame(pid, tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/stats/daily")
    assert r.status_code == 200
    # May be empty or have data; just check it's a list
    assert isinstance(r.json(), list)


def test_timeline_stats(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _insert_project(tmp_path)
    _insert_frame(pid, tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get(f"/api/projects/{pid}/stats/timeline")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_projects_list_includes_last_frame(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _insert_project(tmp_path)
    _insert_frame(pid, tmp_path)
    client = _make_app(tmp_db, tmp_path, monkeypatch)

    r = client.get("/api/projects")
    assert r.status_code == 200
    data = r.json()
    proj = next(p for p in data if p["id"] == pid)
    assert proj["last_frame_id"] is not None
    assert proj["last_captured_at"] is not None


def test_watermark_preview_no_watermark(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _make_app(tmp_db, tmp_path, monkeypatch)
    r = client.get("/api/settings/watermark-preview")
    assert r.status_code == 404


def test_bulk_delete_notifications(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_connection() as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO notifications (event, level, message, is_read) VALUES (?,?,?,?)",
                ("test", "info", f"msg{i}", i % 2),
            )
        conn.commit()

    client = _make_app(tmp_db, tmp_path, monkeypatch)

    # Clear only read notifications
    r = client.delete("/api/notifications?read_only=true")
    assert r.status_code == 204
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM notifications").fetchall()
    assert len(rows) == 2  # one read deleted, two unread remain

    # Clear all
    r2 = client.delete("/api/notifications")
    assert r2.status_code == 204
    with get_connection() as conn:
        rows = conn.execute("SELECT id FROM notifications").fetchall()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# WebSocket: connection manager broadcast and coalescing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_manager_broadcast_sends_message() -> None:
    from app.websocket import ConnectionManager

    mgr = ConnectionManager()

    class _FakeWS:
        def __init__(self):
            self.sent = []
            self.accepted = False

        async def accept(self):
            self.accepted = True

        async def send_text(self, msg: str) -> None:
            self.sent.append(msg)

    ws = _FakeWS()
    await mgr.connect(ws)  # type: ignore[arg-type]
    await mgr.broadcast("render_progress", {"render_id": 1, "progress_pct": 50})
    assert len(ws.sent) == 1
    import json

    data = json.loads(ws.sent[0])
    assert data["event"] == "render_progress"


@pytest.mark.asyncio
async def test_ws_manager_capture_batch_coalescing() -> None:
    """capture_event messages should be coalesced into capture_batch."""
    import asyncio

    from app.websocket import ConnectionManager

    mgr = ConnectionManager()

    class _FakeWS:
        def __init__(self):
            self.sent = []

        async def accept(self): ...

        async def send_text(self, msg: str) -> None:
            self.sent.append(msg)

    ws = _FakeWS()
    await mgr.connect(ws)  # type: ignore[arg-type]

    await mgr.broadcast("capture_event", {"project_id": 1, "frame_count": 10})
    await mgr.broadcast("capture_event", {"project_id": 2, "frame_count": 5})

    # Wait for the coalescing window to flush
    await asyncio.sleep(0.35)

    import json

    events = [json.loads(m)["event"] for m in ws.sent]
    assert "capture_batch" in events
    # Should not have sent individual capture_event messages
    assert "capture_event" not in events


@pytest.mark.asyncio
async def test_ws_manager_dead_client_removed() -> None:
    """Dead clients (those that raise on send) are removed from the pool."""
    from app.websocket import ConnectionManager

    mgr = ConnectionManager()

    class _DeadWS:
        async def accept(self): ...

        async def send_text(self, msg: str) -> None:
            raise RuntimeError("broken pipe")

    ws = _DeadWS()
    await mgr.connect(ws)  # type: ignore[arg-type]
    await mgr.broadcast("render_progress", {"render_id": 99, "progress_pct": 0})
    assert ws not in mgr._clients
