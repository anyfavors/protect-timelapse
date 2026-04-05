"""Tests for the capture worker pipeline."""

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from app.database import get_connection


def _make_jpeg(width: int = 100, height: int = 100, brightness: int = 128) -> bytes:
    """Create a minimal JPEG in memory."""
    img = Image.new("RGB", (width, height), color=(brightness, brightness, brightness))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture()
def project_id(tmp_db: Path) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (name, camera_id, project_type, interval_seconds)
            VALUES ('Test', 'cam-1', 'live', 10)
            """
        )
        conn.commit()
    return cur.lastrowid


async def _run_worker(project_id: int, snapshot_bytes: bytes, monkeypatch, tmp_path: Path) -> None:
    """Helper: patch NVR + disk + paths, then call snapshot_worker."""
    import app.capture as capture_mod
    import app.config as config_mod
    import app.protect as protect_mod

    # Redirect storage paths
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))

    # Stub disk usage (plenty of space)
    import shutil

    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: MagicMock(free=50 * 1024**3, total=100 * 1024**3)
    )

    # Stub NVR client
    mock_cam = AsyncMock()
    mock_cam.get_snapshot = AsyncMock(return_value=snapshot_bytes)
    mock_client = MagicMock()
    mock_client.bootstrap.cameras = {"cam-1": mock_cam}
    monkeypatch.setattr(
        protect_mod.protect_manager, "get_client", AsyncMock(return_value=mock_client)
    )

    # Stub WebSocket broadcast
    monkeypatch.setattr(capture_mod, "broadcast", AsyncMock())

    from app.capture import snapshot_worker

    await snapshot_worker(project_id)


@pytest.mark.asyncio
async def test_snapshot_saved_to_disk(
    tmp_db: Path, project_id: int, monkeypatch, tmp_path: Path
) -> None:
    jpeg = _make_jpeg(brightness=200)
    await _run_worker(project_id, jpeg, monkeypatch, tmp_path)

    frames_dir = tmp_path / "frames" / str(project_id)
    assert frames_dir.exists()
    files = list(frames_dir.glob("*.jpg"))
    assert len(files) == 1


@pytest.mark.asyncio
async def test_thumbnail_saved(tmp_db: Path, project_id: int, monkeypatch, tmp_path: Path) -> None:
    jpeg = _make_jpeg(brightness=200)
    await _run_worker(project_id, jpeg, monkeypatch, tmp_path)

    thumbs_dir = tmp_path / "thumbs" / str(project_id)
    assert thumbs_dir.exists()
    assert len(list(thumbs_dir.glob("*.jpg"))) == 1


@pytest.mark.asyncio
async def test_frame_inserted_in_db(
    tmp_db: Path, project_id: int, monkeypatch, tmp_path: Path
) -> None:
    jpeg = _make_jpeg(brightness=200)
    await _run_worker(project_id, jpeg, monkeypatch, tmp_path)

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM frames WHERE project_id = ?", (project_id,)).fetchone()
        count_row = conn.execute(
            "SELECT frame_count FROM projects WHERE id = ?", (project_id,)
        ).fetchone()

    assert row is not None
    assert row["is_dark"] == 0
    assert count_row["frame_count"] == 1


@pytest.mark.asyncio
async def test_dark_frame_flagged(tmp_db: Path, monkeypatch, tmp_path: Path) -> None:
    """A very dark image should be saved but flagged as is_dark=1."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (name, camera_id, project_type, interval_seconds,
                use_luminance_check, luminance_threshold)
            VALUES ('Dark Test', 'cam-1', 'live', 10, 1, 50)
            """
        )
        conn.commit()
    pid = cur.lastrowid

    jpeg = _make_jpeg(brightness=10)  # very dark
    await _run_worker(pid, jpeg, monkeypatch, tmp_path)

    with get_connection() as conn:
        row = conn.execute("SELECT is_dark FROM frames WHERE project_id = ?", (pid,)).fetchone()
    assert row["is_dark"] == 1


@pytest.mark.asyncio
async def test_nvr_failure_increments_counter(
    tmp_db: Path, project_id: int, monkeypatch, tmp_path: Path
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

    mock_client = MagicMock()
    mock_cam = AsyncMock()
    mock_cam.get_snapshot = AsyncMock(side_effect=Exception("NVR timeout"))
    mock_client.bootstrap.cameras = {"cam-1": mock_cam}
    monkeypatch.setattr(
        protect_mod.protect_manager, "get_client", AsyncMock(return_value=mock_client)
    )
    monkeypatch.setattr(capture_mod, "broadcast", AsyncMock())

    # Stub _notify_nvr_offline so we don't need a real webhook
    monkeypatch.setattr(capture_mod, "_notify_nvr_offline", AsyncMock())

    from app.capture import snapshot_worker

    await snapshot_worker(project_id)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    assert row["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_max_frames_completes_project(tmp_db: Path, monkeypatch, tmp_path: Path) -> None:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (name, camera_id, project_type, interval_seconds,
                max_frames, frame_count)
            VALUES ('MaxTest', 'cam-1', 'live', 10, 1, 0)
            """
        )
        conn.commit()
    pid = cur.lastrowid

    jpeg = _make_jpeg(brightness=200)

    import app.capture as capture_mod

    monkeypatch.setattr(capture_mod, "remove_project_job", AsyncMock())

    monkeypatch.setattr(capture_mod, "_notify_nvr_offline", AsyncMock())

    await _run_worker(pid, jpeg, monkeypatch, tmp_path)

    with get_connection() as conn:
        row = conn.execute("SELECT status FROM projects WHERE id = ?", (pid,)).fetchone()
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_blurry_frame_flagged(tmp_db: Path, monkeypatch, tmp_path: Path) -> None:
    """A uniformly grey image (very low Laplacian variance) should be flagged as blurry."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (name, camera_id, project_type, interval_seconds)
            VALUES ('BlurTest', 'cam-1', 'live', 10)
            """
        )
        conn.commit()
    pid = cur.lastrowid

    # Completely uniform grey image → zero edge variance → is_blurry=1
    jpeg = _make_jpeg(brightness=128)
    await _run_worker(pid, jpeg, monkeypatch, tmp_path)

    with get_connection() as conn:
        row = conn.execute("SELECT is_blurry, sharpness_score FROM frames WHERE project_id = ?", (pid,)).fetchone()
    assert row is not None
    # sharpness pipeline ran and stored a score regardless of threshold outcome
    assert row["sharpness_score"] is not None


@pytest.mark.asyncio
async def test_motion_filter_skips_static_frame(tmp_db: Path, monkeypatch, tmp_path: Path) -> None:
    """When use_motion_filter=1 and the scene hasn't changed, the second frame should be skipped."""
    import app.capture as capture_mod
    import app.config as config_mod
    import app.protect as protect_mod
    import shutil

    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))
    monkeypatch.setattr(shutil, "disk_usage", lambda _: MagicMock(free=50 * 1024**3, total=100 * 1024**3))
    monkeypatch.setattr(capture_mod, "broadcast", AsyncMock())

    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, use_motion_filter, motion_threshold)"
            " VALUES ('MotionTest', 'cam-1', 'live', 10, 1, 50)",
        )
        conn.commit()
    pid = cur.lastrowid

    jpeg = _make_jpeg(brightness=150)
    mock_cam = AsyncMock()
    mock_cam.get_snapshot = AsyncMock(return_value=jpeg)
    mock_client = MagicMock()
    mock_client.bootstrap.cameras = {"cam-1": mock_cam}
    monkeypatch.setattr(protect_mod.protect_manager, "get_client", AsyncMock(return_value=mock_client))

    # First capture — no previous frame, so it always saves
    from app.capture import snapshot_worker
    await snapshot_worker(pid)

    with get_connection() as conn:
        count1 = conn.execute("SELECT frame_count FROM projects WHERE id = ?", (pid,)).fetchone()["frame_count"]
    assert count1 == 1

    # Second capture — identical frame, motion < threshold=50 → should be skipped
    await snapshot_worker(pid)

    with get_connection() as conn:
        count2 = conn.execute("SELECT frame_count FROM projects WHERE id = ?", (pid,)).fetchone()["frame_count"]
    # Frame count should still be 1 (second frame skipped)
    assert count2 == 1
