"""Tests for the maintenance worker, websocket manager, thumbnails, and protect manager."""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from app.database import get_connection

# ===========================================================================
# Maintenance worker
# ===========================================================================


@pytest.fixture(autouse=True)
def _patch_maintenance_connection(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure app.maintenance.get_connection uses the tmp_db-patched connection."""
    import app.database as db_mod
    import app.maintenance as maint_mod

    monkeypatch.setattr(maint_mod, "get_connection", db_mod.get_connection)


def _insert_project(conn, retention_days: int = 0, auto_daily: int = 0) -> int:
    cur = conn.execute(
        """INSERT INTO projects
           (name, camera_id, project_type, interval_seconds, retention_days,
            auto_render_daily, auto_render_weekly, auto_render_monthly)
           VALUES (?,?,?,?,?,?,0,0)""",
        ("MP", "cam-1", "live", 10, retention_days, auto_daily),
    )
    conn.commit()
    return cur.lastrowid


def _insert_frame(
    conn, project_id: int, captured_at: str, file_path: str = "", is_dark: int = 0
) -> int:
    cur = conn.execute(
        "INSERT INTO frames (project_id, file_path, thumbnail_path, captured_at, file_size, is_dark) VALUES (?,?,?,?,?,?)",
        (project_id, file_path, file_path, captured_at, 1000, is_dark),
    )
    conn.commit()
    return cur.lastrowid


def _insert_render(
    conn, project_id: int, render_type: str, status: str = "done", output_path: str = ""
) -> int:
    cur = conn.execute(
        "INSERT INTO renders (project_id, framerate, resolution, render_type, status, output_path) VALUES (?,?,?,?,?,?)",
        (project_id, 30, "1920x1080", render_type, status, output_path),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Frame retention pruning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_old_frames_removes_expired(tmp_db: Path, tmp_path: Path) -> None:
    """Frames older than retention_days are deleted from DB (file_path is empty so no disk error)."""

    old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()

    with get_connection() as conn:
        pid = _insert_project(conn, retention_days=7)
        _insert_frame(conn, pid, old_ts)  # older than 7 days → should be pruned
        _insert_frame(conn, pid, datetime.now(UTC).isoformat())  # today → should stay

    from app.maintenance import _prune_old_frames

    await _prune_old_frames()

    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM frames WHERE project_id = ?", (pid,)).fetchall()
        project = conn.execute("SELECT frame_count FROM projects WHERE id = ?", (pid,)).fetchone()

    assert len(rows) == 1  # only today's frame remains
    assert project["frame_count"] == 1


@pytest.mark.asyncio
async def test_prune_old_frames_skips_no_retention(tmp_db: Path) -> None:
    """Projects with retention_days=0 are skipped entirely."""
    with get_connection() as conn:
        pid = _insert_project(conn, retention_days=0)
        old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        _insert_frame(conn, pid, old_ts)

    from app.maintenance import _prune_old_frames

    await _prune_old_frames()

    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM frames WHERE project_id = ?", (pid,)).fetchall()

    assert len(rows) == 1  # not pruned


@pytest.mark.asyncio
async def test_prune_old_frames_recalculates_frame_count(tmp_db: Path) -> None:
    """frame_count is recalculated correctly after pruning."""
    old_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    now_ts = datetime.now(UTC).isoformat()

    with get_connection() as conn:
        pid = _insert_project(conn, retention_days=5)
        _insert_frame(conn, pid, old_ts)
        _insert_frame(conn, pid, old_ts)
        _insert_frame(conn, pid, now_ts)
        # Set frame_count to incorrect value to verify recalculation
        conn.execute("UPDATE projects SET frame_count = 99 WHERE id = ?", (pid,))
        conn.commit()

    from app.maintenance import _prune_old_frames

    await _prune_old_frames()

    with get_connection() as conn:
        row = conn.execute("SELECT frame_count FROM projects WHERE id = ?", (pid,)).fetchone()

    assert row["frame_count"] == 1


# ---------------------------------------------------------------------------
# Auto-render rolling window pruning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_old_renders_removes_excess_daily(tmp_db: Path) -> None:
    """More than 7 auto_daily done renders triggers pruning of oldest."""
    with get_connection() as conn:
        pid = _insert_project(conn)
        for _ in range(9):  # 9 daily renders → should prune 2 oldest
            _insert_render(conn, pid, "auto_daily", "done")

    from app.maintenance import _prune_old_renders

    await _prune_old_renders()

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM renders WHERE project_id = ? AND render_type = 'auto_daily'",
            (pid,),
        ).fetchall()

    assert len(rows) == 7


@pytest.mark.asyncio
async def test_prune_old_renders_keeps_within_limit(tmp_db: Path) -> None:
    """Fewer than 7 daily renders → nothing pruned."""
    with get_connection() as conn:
        pid = _insert_project(conn)
        for _ in range(5):
            _insert_render(conn, pid, "auto_daily", "done")

    from app.maintenance import _prune_old_renders

    await _prune_old_renders()

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM renders WHERE project_id = ? AND render_type = 'auto_daily'",
            (pid,),
        ).fetchall()

    assert len(rows) == 5


# ---------------------------------------------------------------------------
# Auto-render scheduling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_auto_renders_inserts_daily(tmp_db: Path) -> None:
    """auto_render_daily=1 and frames in yesterday's window → pending render inserted."""
    yesterday = datetime.now(UTC) - timedelta(days=1)
    ts = yesterday.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()

    with get_connection() as conn:
        pid = _insert_project(conn, auto_daily=1)
        _insert_frame(conn, pid, ts)

    from app.maintenance import _schedule_auto_renders

    await _schedule_auto_renders()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM renders WHERE project_id = ? AND render_type = 'auto_daily'",
            (pid,),
        ).fetchone()

    assert row is not None
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_schedule_auto_renders_skips_no_frames(tmp_db: Path) -> None:
    """No frames in yesterday's window → no render inserted."""
    with get_connection() as conn:
        pid = _insert_project(conn, auto_daily=1)
        # No frames inserted

    from app.maintenance import _schedule_auto_renders

    await _schedule_auto_renders()

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM renders WHERE project_id = ?", (pid,)).fetchone()

    assert row is None


@pytest.mark.asyncio
async def test_schedule_auto_renders_skips_duplicate(tmp_db: Path) -> None:
    """Existing pending render for same window → no duplicate inserted."""
    yesterday = datetime.now(UTC) - timedelta(days=1)
    ts = yesterday.replace(hour=12).isoformat()

    with get_connection() as conn:
        pid = _insert_project(conn, auto_daily=1)
        _insert_frame(conn, pid, ts)

    from app.maintenance import _schedule_auto_renders

    await _schedule_auto_renders()
    await _schedule_auto_renders()  # second call should be a no-op

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM renders WHERE project_id = ? AND render_type = 'auto_daily'",
            (pid,),
        ).fetchall()

    assert len(rows) == 1


# ===========================================================================
# WebSocket ConnectionManager
# ===========================================================================


@pytest.mark.asyncio
async def test_ws_connect_and_disconnect() -> None:
    from app.websocket import ConnectionManager

    mgr = ConnectionManager()

    mock_ws = AsyncMock()
    await mgr.connect(mock_ws)
    assert mock_ws in mgr._clients

    mgr.disconnect(mock_ws)
    assert mock_ws not in mgr._clients


@pytest.mark.asyncio
async def test_ws_broadcast_sends_to_all() -> None:
    from app.websocket import ConnectionManager

    mgr = ConnectionManager()
    ws1, ws2 = AsyncMock(), AsyncMock()
    mgr._clients = {ws1, ws2}

    await mgr.broadcast("test_event", {"key": "val"})

    ws1.send_text.assert_awaited_once()
    ws2.send_text.assert_awaited_once()
    # Verify event name in payload
    payload = ws1.send_text.call_args.args[0]
    assert '"test_event"' in payload
    assert '"key"' in payload


@pytest.mark.asyncio
async def test_ws_broadcast_removes_dead_client() -> None:
    from app.websocket import ConnectionManager

    mgr = ConnectionManager()
    dead_ws = AsyncMock()
    dead_ws.send_text.side_effect = Exception("connection closed")
    good_ws = AsyncMock()
    mgr._clients = {dead_ws, good_ws}

    await mgr.broadcast("ping", {})

    assert dead_ws not in mgr._clients
    assert good_ws in mgr._clients


@pytest.mark.asyncio
async def test_ws_broadcast_no_op_when_empty() -> None:
    from app.websocket import ConnectionManager

    mgr = ConnectionManager()
    # Should not raise with no clients
    await mgr.broadcast("event", {})


@pytest.mark.asyncio
async def test_ws_close_all() -> None:
    from app.websocket import ConnectionManager

    mgr = ConnectionManager()
    ws1, ws2 = AsyncMock(), AsyncMock()
    mgr._clients = {ws1, ws2}

    await mgr.close_all()

    ws1.close.assert_awaited_once()
    ws2.close.assert_awaited_once()
    assert len(mgr._clients) == 0


# ===========================================================================
# Thumbnails
# ===========================================================================


def _make_jpeg(w: int = 200, h: int = 150) -> bytes:
    img = Image.new("RGB", (w, h), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_generate_thumbnail_resizes_to_320px() -> None:
    from app.thumbnails import generate_thumbnail

    thumb_bytes = generate_thumbnail(_make_jpeg(w=1920, h=1080))
    img = Image.open(io.BytesIO(thumb_bytes))
    assert img.width == 320
    assert img.height == round(320 * 1080 / 1920)


def test_generate_thumbnail_from_pillow() -> None:
    from app.thumbnails import generate_thumbnail_from_pillow

    img = Image.new("RGB", (800, 600), color=(50, 50, 50))
    thumb_bytes = generate_thumbnail_from_pillow(img)
    result = Image.open(io.BytesIO(thumb_bytes))
    assert result.width == 320


def test_generate_thumbnail_small_image_not_upscaled() -> None:
    """Images smaller than 320px wide should not be upscaled."""
    from app.thumbnails import generate_thumbnail

    thumb_bytes = generate_thumbnail(_make_jpeg(w=100, h=100))
    img = Image.open(io.BytesIO(thumb_bytes))
    # Should stay at original size (no upscale)
    assert img.width <= 320


# ===========================================================================
# ProtectClientManager
# ===========================================================================


@pytest.mark.asyncio
async def test_protect_manager_setup_and_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()

    mock_client = AsyncMock()
    mock_client.update = AsyncMock()
    mock_client.close_session = AsyncMock()

    monkeypatch.setattr("app.protect.ProtectApiClient", lambda *a, **kw: mock_client)

    await mgr.setup()
    assert mgr.is_connected

    await mgr.teardown()
    mock_client.close_session.assert_awaited_once()
    assert not mgr.is_connected


@pytest.mark.asyncio
async def test_protect_manager_get_client_returns_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()

    mock_client = AsyncMock()
    mock_client.update = AsyncMock()
    mock_client.bootstrap = MagicMock()
    mock_client.bootstrap.cameras = {}

    monkeypatch.setattr("app.protect.ProtectApiClient", lambda *a, **kw: mock_client)

    await mgr.setup()
    assert mgr.is_connected
    client = await mgr.get_client()
    assert client is mock_client


@pytest.mark.asyncio
async def test_protect_manager_reconnects_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When _connected is False, get_client() calls client.update() to reconnect."""
    from app.protect import ProtectClientManager

    mgr = ProtectClientManager()
    mock_client = AsyncMock()
    mock_client.update = AsyncMock()
    mock_client.bootstrap = MagicMock()
    mock_client.bootstrap.cameras = {}

    monkeypatch.setattr("app.protect.ProtectApiClient", lambda *a, **kw: mock_client)

    await mgr.setup()
    # Force disconnected state
    mgr._connected = False

    await mgr.get_client()  # should call update() to reconnect

    # update() called once during setup() + once during reconnect
    assert mock_client.update.await_count >= 2
    assert mgr.is_connected
