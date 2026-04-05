"""Tests for the render worker (mocked ffmpeg subprocess)."""

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from app.database import get_connection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jpeg(brightness: int = 200) -> bytes:
    img = Image.new("RGB", (100, 100), color=(brightness, brightness, brightness))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _insert_project(conn) -> int:
    cur = conn.execute(
        "INSERT INTO projects (name, camera_id, project_type, interval_seconds) VALUES (?,?,?,?)",
        ("RenderProj", "cam-1", "live", 10),
    )
    conn.commit()
    return cur.lastrowid


def _insert_frame(conn, project_id: int, file_path: str, is_dark: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO frames (project_id, file_path, thumbnail_path, file_size, is_dark) VALUES (?,?,?,?,?)",
        (project_id, file_path, file_path, 10000, is_dark),
    )
    conn.commit()
    return cur.lastrowid


def _insert_render(conn, project_id: int, render_type: str = "manual") -> int:
    cur = conn.execute(
        "INSERT INTO renders (project_id, framerate, resolution, render_type) VALUES (?,?,?,?)",
        (project_id, 30, "1920x1080", render_type),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def render_env(tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up storage paths and settings for render tests."""
    import app.config as config_mod

    frames_dir = tmp_path / "frames"
    renders_dir = tmp_path / "renders"
    frames_dir.mkdir(parents=True)
    renders_dir.mkdir(parents=True)

    monkeypatch.setenv("FRAMES_PATH", str(frames_dir))
    monkeypatch.setenv("RENDERS_PATH", str(renders_dir))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setattr(config_mod, "_settings", None)

    return {"frames_dir": frames_dir, "renders_dir": renders_dir, "tmp_path": tmp_path}


# ---------------------------------------------------------------------------
# Fake subprocess process
# ---------------------------------------------------------------------------


def _make_mock_proc(returncode: int = 0) -> MagicMock:
    """Build an asyncio.subprocess.Process mock that exits cleanly."""

    async def _wait():
        return returncode

    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.wait = _wait
    mock_proc.stderr = None
    return mock_proc


# ---------------------------------------------------------------------------
# Common stub helper
# ---------------------------------------------------------------------------


def _stub_render_side_effects(monkeypatch: pytest.MonkeyPatch, mock_proc, broadcast_mock=None):
    """Patch subprocess, websocket, and notifications for a render test."""
    import asyncio

    import app.notifications as notif_mod
    import app.websocket as ws_mod

    async def _fake_exec(*args, **kwargs):
        return mock_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(ws_mod, "broadcast", broadcast_mock or AsyncMock())
    monkeypatch.setattr(notif_mod, "notify", AsyncMock())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_worker_processes_pending_render(
    render_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending render is picked up, ffmpeg is called, and status → done."""
    frames_dir = render_env["frames_dir"]

    with get_connection() as conn:
        pid = _insert_project(conn)
        frame_path = str(frames_dir / str(pid) / "frame.jpg")
        (frames_dir / str(pid)).mkdir(parents=True)
        (frames_dir / str(pid) / "frame.jpg").write_bytes(_make_jpeg())
        _insert_frame(conn, pid, frame_path)
        rid = _insert_render(conn, pid)

    renders_proj_dir = render_env["renders_dir"] / str(pid)
    renders_proj_dir.mkdir(parents=True)
    (renders_proj_dir / f"{rid}.mp4").write_bytes(b"fake video content")

    _stub_render_side_effects(monkeypatch, _make_mock_proc(returncode=0))

    from app.render import _process_next_render

    await _process_next_render()

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM renders WHERE id = ?", (rid,)).fetchone()

    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_render_worker_handles_no_frames(render_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A render with no frames transitions to error status."""
    with get_connection() as conn:
        pid = _insert_project(conn)
        rid = _insert_render(conn, pid)

    _stub_render_side_effects(monkeypatch, _make_mock_proc())

    from app.render import _process_next_render

    await _process_next_render()

    with get_connection() as conn:
        row = conn.execute("SELECT status, error_msg FROM renders WHERE id = ?", (rid,)).fetchone()

    assert row["status"] == "error"
    assert "No frames" in row["error_msg"]


@pytest.mark.asyncio
async def test_render_worker_handles_ffmpeg_failure(
    render_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ffmpeg exits non-zero, render transitions to error."""
    frames_dir = render_env["frames_dir"]

    with get_connection() as conn:
        pid = _insert_project(conn)
        frame_path = str(frames_dir / str(pid) / "frame.jpg")
        (frames_dir / str(pid)).mkdir(parents=True)
        (frames_dir / str(pid) / "frame.jpg").write_bytes(_make_jpeg())
        _insert_frame(conn, pid, frame_path)
        rid = _insert_render(conn, pid)

    _stub_render_side_effects(monkeypatch, _make_mock_proc(returncode=1))

    from app.render import _process_next_render

    await _process_next_render()

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM renders WHERE id = ?", (rid,)).fetchone()

    assert row["status"] == "error"


@pytest.mark.asyncio
async def test_render_worker_no_op_when_no_pending(
    render_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no pending renders exist, _process_next_render returns immediately."""
    import asyncio

    called = False

    async def _fake_exec(*args, **kwargs):
        nonlocal called
        called = True
        return _make_mock_proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    with get_connection() as conn:
        _insert_project(conn)

    from app.render import _process_next_render

    await _process_next_render()

    assert not called


@pytest.mark.asyncio
async def test_render_worker_broadcasts_complete_event(
    render_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render_complete WebSocket event is broadcast on success."""
    frames_dir = render_env["frames_dir"]

    with get_connection() as conn:
        pid = _insert_project(conn)
        frame_path = str(frames_dir / str(pid) / "frame.jpg")
        (frames_dir / str(pid)).mkdir(parents=True)
        (frames_dir / str(pid) / "frame.jpg").write_bytes(_make_jpeg())
        _insert_frame(conn, pid, frame_path)
        rid = _insert_render(conn, pid)

    renders_proj_dir = render_env["renders_dir"] / str(pid)
    renders_proj_dir.mkdir(parents=True)
    (renders_proj_dir / f"{rid}.mp4").write_bytes(b"fake video")

    broadcast_mock = AsyncMock()
    _stub_render_side_effects(monkeypatch, _make_mock_proc(returncode=0), broadcast_mock)

    from app.render import _process_next_render

    await _process_next_render()

    events = [call.args[0] for call in broadcast_mock.call_args_list]
    assert "render_complete" in events
