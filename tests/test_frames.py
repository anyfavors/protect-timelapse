"""Tests for frame routes and notification delivery."""

import io
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.database import get_connection
from app.routes import frames, notifications

# ---------------------------------------------------------------------------
# Fixture
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

    app = FastAPI(lifespan=_noop)
    app.include_router(frames.router)
    app.include_router(notifications.router)

    return TestClient(app)


def _make_jpeg() -> bytes:
    img = Image.new("RGB", (200, 150), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _create_project_with_frames(tmp_path: Path, n: int = 3) -> tuple[int, list[int]]:
    """Insert a project and n frames into the DB, creating actual JPEG files."""
    frames_dir = tmp_path / "frames"
    thumbs_dir = tmp_path / "thumbs"

    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds) VALUES (?,?,?,?)",
            ("FrameProj", "cam-1", "live", 10),
        )
        conn.commit()
        pid = cur.lastrowid

    proj_frames_dir = frames_dir / str(pid)
    proj_thumbs_dir = thumbs_dir / str(pid)
    proj_frames_dir.mkdir(parents=True)
    proj_thumbs_dir.mkdir(parents=True)

    frame_ids = []
    jpeg = _make_jpeg()
    for i in range(n):
        ts = f"2024-01-01T0{i}:00:00"
        fp = str(proj_frames_dir / f"{ts}.jpg")
        tp = str(proj_thumbs_dir / f"{ts}.jpg")
        (proj_frames_dir / f"{ts}.jpg").write_bytes(jpeg)
        (proj_thumbs_dir / f"{ts}.jpg").write_bytes(jpeg)
        with get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO frames (project_id, file_path, thumbnail_path, captured_at, file_size) VALUES (?,?,?,?,?)",
                (pid, fp, tp, ts, len(jpeg)),
            )
            conn.commit()
            frame_ids.append(cur.lastrowid)

    return pid, frame_ids


# ---------------------------------------------------------------------------
# Frame listing tests
# ---------------------------------------------------------------------------


def test_list_frames(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, _ = _create_project_with_frames(tmp_path, n=3)
    r = api.get(f"/api/projects/{pid}/frames")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 3


def test_list_frames_project_not_found(api: TestClient) -> None:
    r = api.get("/api/projects/9999/frames")
    assert r.status_code == 404


def test_list_frames_field_projection(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, _ = _create_project_with_frames(tmp_path, n=2)
    r = api.get(f"/api/projects/{pid}/frames?fields=id,captured_at")
    assert r.status_code == 200
    for item in r.json():
        assert "id" in item
        assert "captured_at" in item
        # file_path should not be present (projected out)
        assert "file_path" not in item


def test_list_frames_pagination(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, _ = _create_project_with_frames(tmp_path, n=3)
    r = api.get(f"/api/projects/{pid}/frames?limit=2&offset=0")
    assert r.status_code == 200
    assert len(r.json()) == 2

    r2 = api.get(f"/api/projects/{pid}/frames?limit=2&offset=2")
    assert r2.status_code == 200
    assert len(r2.json()) == 1


# ---------------------------------------------------------------------------
# Image serving
# ---------------------------------------------------------------------------


def test_serve_thumbnail(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, frame_ids = _create_project_with_frames(tmp_path, n=1)
    fid = frame_ids[0]
    r = api.get(f"/api/projects/{pid}/frames/{fid}/thumbnail")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert len(r.content) > 0


def test_serve_full_frame(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, frame_ids = _create_project_with_frames(tmp_path, n=1)
    fid = frame_ids[0]
    r = api.get(f"/api/projects/{pid}/frames/{fid}/full")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_serve_frame_not_found(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, _ = _create_project_with_frames(tmp_path, n=1)
    r = api.get(f"/api/projects/{pid}/frames/9999/thumbnail")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------


def test_set_and_list_bookmark(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, frame_ids = _create_project_with_frames(tmp_path, n=2)
    fid = frame_ids[0]

    r = api.put(f"/api/projects/{pid}/frames/{fid}/bookmark", json={"note": "Key moment"})
    assert r.status_code == 200
    assert r.json()["bookmark_note"] == "Key moment"

    r2 = api.get(f"/api/projects/{pid}/frames/bookmarks")
    assert r2.status_code == 200
    bookmarks = r2.json()
    assert len(bookmarks) == 1
    assert bookmarks[0]["bookmark_note"] == "Key moment"


def test_clear_bookmark(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, frame_ids = _create_project_with_frames(tmp_path, n=1)
    fid = frame_ids[0]

    api.put(f"/api/projects/{pid}/frames/{fid}/bookmark", json={"note": "temp"})
    api.put(f"/api/projects/{pid}/frames/{fid}/bookmark", json={"note": None})

    r = api.get(f"/api/projects/{pid}/frames/bookmarks")
    assert r.json() == []


# ---------------------------------------------------------------------------
# Dark frames
# ---------------------------------------------------------------------------


def test_list_dark_frames(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, frame_ids = _create_project_with_frames(tmp_path, n=3)

    # Mark one frame as dark
    with get_connection() as conn:
        conn.execute("UPDATE frames SET is_dark = 1 WHERE id = ?", (frame_ids[0],))
        conn.commit()

    r = api.get(f"/api/projects/{pid}/frames/dark")
    assert r.status_code == 200
    assert len(r.json()) == 1


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_daily_stats(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, _ = _create_project_with_frames(tmp_path, n=3)
    r = api.get(f"/api/projects/{pid}/stats/daily")
    assert r.status_code == 200
    # All frames are on 2024-01-01 in our helper
    data = r.json()
    assert len(data) == 1
    assert data[0]["count"] == 3


def test_timeline_stats(api: TestClient, tmp_db: Path, tmp_path: Path) -> None:
    pid, _ = _create_project_with_frames(tmp_path, n=3)
    r = api.get(f"/api/projects/{pid}/stats/timeline")
    assert r.status_code == 200
    data = r.json()
    # 3 different hours (00, 01, 02)
    assert len(data) == 3
    for row in data:
        assert row["captured"] == 1
        assert row["dark"] == 0


# ---------------------------------------------------------------------------
# Notification delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_writes_to_db(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """notify() always writes to notifications table."""
    from unittest.mock import AsyncMock, MagicMock

    import app.database as db_mod
    import app.notifications as notif_mod
    import app.websocket as ws_mod

    # Ensure notify() uses the same patched connection as the test assertions
    monkeypatch.setattr(notif_mod, "get_connection", db_mod.get_connection)
    monkeypatch.setattr(ws_mod, "broadcast", AsyncMock())

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):  # type: ignore[return]
            return MagicMock(status_code=200)

    monkeypatch.setattr(notif_mod.httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    from app.notifications import notify

    await notify(event="disk_full", level="error", message="Disk is full")

    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM notifications WHERE event = 'disk_full'").fetchall()

    assert len(rows) == 1
    assert rows[0]["level"] == "error"
    assert rows[0]["message"] == "Disk is full"


@pytest.mark.asyncio
async def test_notify_fires_webhook(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """notify() posts to webhook_url when set in settings."""
    from unittest.mock import AsyncMock, MagicMock

    import app.notifications as notif_mod
    import app.websocket as ws_mod

    # Patch _get_webhook_url so the test doesn't depend on DB state ordering
    monkeypatch.setattr(notif_mod, "_get_webhook_url", lambda: "http://example.com/hook")
    monkeypatch.setattr(ws_mod, "broadcast", AsyncMock())

    post_calls: list[str] = []

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):  # type: ignore[return]
            post_calls.append(url)
            return MagicMock(status_code=200)

    monkeypatch.setattr(notif_mod.httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    from app.notifications import notify

    await notify(event="test_event", level="info", message="Hello webhook")

    assert len(post_calls) == 1
    assert post_calls[0] == "http://example.com/hook"
