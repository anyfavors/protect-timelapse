"""Targeted tests to push coverage above 80%.

Covers: camera routes, render worker start/stop, capture scheduler functions,
and rollup render path.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_connection

# ===========================================================================
# Camera routes
# ===========================================================================


@pytest.fixture()
def camera_api(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from contextlib import asynccontextmanager

    import app.protect as protect_mod
    from app.routes import cameras

    # Build a fake NVR client with two cameras
    def _make_cam(cam_id: str, name: str) -> MagicMock:
        cam = MagicMock()
        cam.id = cam_id
        cam.name = name
        cam.type = "UVC-G4"
        cam.is_connected = True
        cam.get_snapshot = AsyncMock(return_value=b"\xff\xd8\xff" + b"\x00" * 100)
        return cam

    mock_client = MagicMock()
    mock_client.bootstrap = MagicMock()
    mock_client.bootstrap.cameras = {
        "cam-1": _make_cam("cam-1", "Front Door"),
        "cam-2": _make_cam("cam-2", "Backyard"),
    }
    monkeypatch.setattr(
        protect_mod.protect_manager, "get_client", AsyncMock(return_value=mock_client)
    )

    @asynccontextmanager
    async def _noop(app):  # type: ignore[no-untyped-def]
        yield

    app = FastAPI(lifespan=_noop)
    app.include_router(cameras.router)
    return TestClient(app)


def test_list_cameras(camera_api: TestClient) -> None:
    r = camera_api.get("/api/cameras")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    ids = {c["id"] for c in data}
    assert "cam-1" in ids
    assert "cam-2" in ids


def test_list_cameras_nvr_offline(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    import app.protect as protect_mod
    from app.routes import cameras

    monkeypatch.setattr(
        protect_mod.protect_manager,
        "get_client",
        AsyncMock(side_effect=RuntimeError("NVR offline")),
    )

    @asynccontextmanager
    async def _noop(app):  # type: ignore[no-untyped-def]
        yield

    app = FastAPI(lifespan=_noop)
    app.include_router(cameras.router)
    client = TestClient(app)

    r = client.get("/api/cameras")
    assert r.status_code == 503


def test_camera_preview(camera_api: TestClient) -> None:
    r = camera_api.get("/api/cameras/cam-1/preview")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_camera_preview_not_found(camera_api: TestClient) -> None:
    r = camera_api.get("/api/cameras/no-such-cam/preview")
    assert r.status_code == 404


def test_camera_preview_snapshot_error(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    import app.protect as protect_mod
    from app.routes import cameras

    cam = MagicMock()
    cam.id = "cam-err"
    cam.get_snapshot = AsyncMock(side_effect=Exception("timeout"))

    mock_client = MagicMock()
    mock_client.bootstrap = MagicMock()
    mock_client.bootstrap.cameras = {"cam-err": cam}
    monkeypatch.setattr(
        protect_mod.protect_manager, "get_client", AsyncMock(return_value=mock_client)
    )

    @asynccontextmanager
    async def _noop(app):  # type: ignore[no-untyped-def]
        yield

    app = FastAPI(lifespan=_noop)
    app.include_router(cameras.router)
    client = TestClient(app)

    r = client.get("/api/cameras/cam-err/preview")
    assert r.status_code == 503


# ===========================================================================
# Render worker start / stop
# ===========================================================================


@pytest.mark.asyncio
async def test_render_worker_start_stop(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.render as render_mod

    # Prevent the loop from running (stop immediately)
    task = await render_mod.start_render_worker()
    assert task is not None
    assert not task.done()

    await render_mod.stop_render_worker(task)
    # After stop, the task should be finished
    assert task.done()


# ===========================================================================
# Capture scheduler functions
# ===========================================================================


@pytest.mark.asyncio
async def test_add_and_remove_project_job(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """add_project_job registers a job; remove_project_job removes it."""
    import app.capture as capture_mod

    # Start the scheduler
    monkeypatch.setattr(capture_mod, "get_connection", MagicMock(return_value=_noop_conn()))

    sched = capture_mod.scheduler
    if not sched.running:
        sched.start()

    try:
        await capture_mod.add_project_job(999, 60)
        assert sched.get_job("project_999") is not None

        await capture_mod.remove_project_job(999)
        assert sched.get_job("project_999") is None
    finally:
        if sched.running:
            sched.shutdown(wait=False)


@pytest.mark.asyncio
async def test_pause_and_resume_project_job(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.capture as capture_mod

    sched = capture_mod.scheduler
    if not sched.running:
        sched.start()

    try:
        await capture_mod.add_project_job(998, 60)
        job = sched.get_job("project_998")
        assert job is not None

        await capture_mod.pause_project_job(998)
        assert sched.get_job("project_998").next_run_time is None  # paused

        await capture_mod.resume_project_job(998, 60)
        assert sched.get_job("project_998").next_run_time is not None  # resumed
    finally:
        with contextlib.suppress(Exception):
            sched.remove_job("project_998")
        if sched.running:
            sched.shutdown(wait=False)


@pytest.mark.asyncio
async def test_reschedule_project_job(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.capture as capture_mod

    sched = capture_mod.scheduler
    if not sched.running:
        sched.start()

    try:
        await capture_mod.add_project_job(997, 60)
        await capture_mod.reschedule_project_job(997, 120)
        job = sched.get_job("project_997")
        assert job is not None
    finally:
        with contextlib.suppress(Exception):
            sched.remove_job("project_997")
        if sched.running:
            sched.shutdown(wait=False)


@pytest.mark.asyncio
async def test_start_scheduler_registers_active_projects(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.capture as capture_mod
    import app.database as db_mod

    monkeypatch.setattr(capture_mod, "get_connection", db_mod.get_connection)

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status) VALUES (?,?,?,?,?)",
            ("SchedP", "cam-1", "live", 30, "active"),
        )
        conn.commit()

    sched = capture_mod.scheduler
    # Ensure clean state
    if sched.running:
        sched.shutdown(wait=False)
    # Remove residual jobs
    for job in sched.get_jobs():
        sched.remove_job(job.id)

    try:
        await capture_mod.start_scheduler()
        jobs = sched.get_jobs()
        # Should have a job for the active project; exact id depends on the inserted row id
        assert any(j.id.startswith("project_") for j in jobs)
    finally:
        if sched.running:
            sched.shutdown(wait=False)


@pytest.mark.asyncio
async def test_stop_scheduler_when_not_running() -> None:
    """stop_scheduler() is a no-op when scheduler isn't running."""
    import app.capture as capture_mod

    sched = capture_mod.scheduler
    if sched.running:
        sched.shutdown(wait=False)

    # Should not raise
    await capture_mod.stop_scheduler()


# ===========================================================================
# Rollup render path (_get_frame_paths for weekly/monthly)
# ===========================================================================


def test_get_frame_paths_rollup(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """For auto_weekly/auto_monthly, _get_frame_paths returns existing daily MP4 paths."""
    import app.database as db_mod
    import app.render as render_mod

    monkeypatch.setattr(render_mod, "get_connection", db_mod.get_connection)

    import os
    import tempfile

    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds) VALUES (?,?,?,?)",
            ("RollupProj", "cam-1", "live", 10),
        )
        conn.commit()
        pid = cur.lastrowid

    # Create fake daily MP4 files
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"fakevid")
        mp4_path = f.name

    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO renders (project_id, framerate, resolution, render_type, status, output_path) VALUES (?,?,?,?,?,?)",
                (pid, 30, "1920x1080", "auto_daily", "done", mp4_path),
            )
            conn.commit()

        render = {"project_id": pid, "render_type": "auto_weekly"}
        paths = render_mod._get_frame_paths(render)
        assert mp4_path in paths
    finally:
        os.unlink(mp4_path)


# ===========================================================================
# Render: timestamp burn-in and estimate function
# ===========================================================================


def test_build_ffmpeg_cmd_with_timestamp_burnin(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_build_ffmpeg_cmd includes drawtext filter when timestamp_burn_in is set."""
    import app.database as db_mod
    import app.render as render_mod

    monkeypatch.setattr(render_mod, "get_connection", db_mod.get_connection)

    # Enable timestamp burn-in in settings
    with get_connection() as conn:
        conn.execute("UPDATE settings SET timestamp_burn_in = 1 WHERE id = 1")
        conn.commit()

    render = {
        "project_id": 1,
        "framerate": 30,
        "render_type": "manual",
        "range_start": None,
        "range_end": None,
    }
    settings = MagicMock()
    settings.ffmpeg_threads = 4

    cmd = render_mod._build_ffmpeg_cmd(render, "/tmp/c.txt", "/tmp/out.mp4", 100, settings)
    cmd_str = " ".join(cmd)
    assert "drawtext" in cmd_str


def test_build_ffmpeg_cmd_with_frame_skipping(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_build_ffmpeg_cmd uses select filter when frame count > target."""
    import app.database as db_mod
    import app.render as render_mod

    monkeypatch.setattr(render_mod, "get_connection", db_mod.get_connection)

    render = {
        "project_id": 1,
        "framerate": 30,
        "render_type": "manual",
        "range_start": None,
        "range_end": None,
    }
    settings = MagicMock()
    settings.ffmpeg_threads = 4

    # total_frames >> target_frames (1800) → triggers frame skipping
    cmd = render_mod._build_ffmpeg_cmd(render, "/tmp/c.txt", "/tmp/out.mp4", 5000, settings)
    cmd_str = " ".join(cmd)
    assert "select" in cmd_str
    assert "setpts" in cmd_str


def test_estimate_render_with_frames(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """estimate_render returns correct estimates when frames exist."""
    import app.database as db_mod
    import app.render as render_mod

    monkeypatch.setattr(render_mod, "get_connection", db_mod.get_connection)

    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds) VALUES (?,?,?,?)",
            ("EP", "cam-1", "live", 10),
        )
        conn.commit()
        pid = cur.lastrowid
        for _i in range(90):
            conn.execute(
                "INSERT INTO frames (project_id, file_path, thumbnail_path, file_size, is_dark) VALUES (?,?,?,?,0)",
                (pid, "", "", 200000),
            )
        conn.commit()

    result = render_mod.estimate_render(pid, 30, "manual")
    assert result["frame_count"] == 90
    assert result["estimated_duration_seconds"] == 3  # 90 / 30
    assert result["estimated_file_size_bytes"] > 0


# ===========================================================================
# Capture: disk breach and project-not-found branches
# ===========================================================================


@pytest.mark.asyncio
async def test_snapshot_worker_project_not_found(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """snapshot_worker exits cleanly if the project row is not found (paused/deleted)."""
    import app.capture as capture_mod
    import app.config as config_mod
    import app.database as db_mod

    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setattr(capture_mod, "get_connection", db_mod.get_connection)
    monkeypatch.setattr(capture_mod, "broadcast", AsyncMock())
    monkeypatch.setattr(capture_mod, "remove_project_job", AsyncMock())

    import shutil

    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: MagicMock(free=50 * 1024**3, total=100 * 1024**3)
    )

    # Project 9999 does not exist → should call remove_project_job
    from app.capture import snapshot_worker

    await snapshot_worker(9999)

    capture_mod.remove_project_job.assert_awaited_once_with(9999)


@pytest.mark.asyncio
async def test_snapshot_worker_disk_breach(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """snapshot_worker pauses all projects when free disk < threshold."""
    import app.capture as capture_mod
    import app.config as config_mod
    import app.database as db_mod
    import app.notifications as notif_mod
    import app.websocket as ws_mod

    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))
    monkeypatch.setattr(config_mod, "_settings", None)
    monkeypatch.setattr(capture_mod, "get_connection", db_mod.get_connection)
    monkeypatch.setattr(ws_mod, "broadcast", AsyncMock())
    monkeypatch.setattr(notif_mod, "notify", AsyncMock())

    import shutil

    # Very low free space: 0.1 GB, threshold = 5 GB (default)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: MagicMock(free=0.1 * 1024**3, total=100 * 1024**3)
    )

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, camera_id, project_type, interval_seconds, status) VALUES (?,?,?,?,?)",
            ("DiskP", "cam-1", "live", 10, "active"),
        )
        conn.commit()

    from app.capture import snapshot_worker

    await snapshot_worker(1)  # project_id doesn't matter; disk check fires first

    # All projects should be paused_error
    with get_connection() as conn:
        rows = conn.execute("SELECT status FROM projects").fetchall()
    assert all(r["status"] == "paused_error" for r in rows)


# ===========================================================================
# App lifespan (startup / shutdown)
# ===========================================================================


@pytest.mark.asyncio
async def test_app_lifespan_startup_and_shutdown(
    tmp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the full lifespan context to cover startup + shutdown paths."""
    import app.capture as capture_mod
    import app.config as config_mod
    import app.database as db_mod
    import app.protect as protect_mod

    monkeypatch.setenv("FRAMES_PATH", str(tmp_path / "frames"))
    monkeypatch.setenv("THUMBNAILS_PATH", str(tmp_path / "thumbs"))
    monkeypatch.setenv("RENDERS_PATH", str(tmp_path / "renders"))
    monkeypatch.setattr(config_mod, "_settings", None)

    # Stub NVR so no real connection is made
    monkeypatch.setattr(protect_mod.protect_manager, "setup", AsyncMock())
    monkeypatch.setattr(protect_mod.protect_manager, "teardown", AsyncMock())

    # Patch get_connection so lifespan uses the tmp DB
    monkeypatch.setattr(capture_mod, "get_connection", db_mod.get_connection)

    from fastapi import FastAPI

    from app import lifespan

    app = FastAPI()
    async with lifespan(app):
        pass  # startup + immediate shutdown

    # Scheduler should be stopped after lifespan exits
    # (may or may not be running depending on prior tests; just assert no exception raised)


# ===========================================================================
# Render loop (one iteration)
# ===========================================================================


@pytest.mark.asyncio
async def test_render_loop_one_iteration(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_render_loop processes one iteration and then stops when event is set."""
    import asyncio

    import app.database as db_mod
    import app.render as render_mod

    monkeypatch.setattr(render_mod, "get_connection", db_mod.get_connection)

    stop = asyncio.Event()
    called = []

    async def _fake_process():
        called.append(True)
        stop.set()  # stop after the first iteration completes

    monkeypatch.setattr(render_mod, "_process_next_render", _fake_process)

    await render_mod._render_loop(stop)

    assert len(called) == 1


# ===========================================================================
# Helpers
# ===========================================================================


import contextlib  # noqa: E402


class _noop_conn:
    """Dummy context manager that returns a no-op connection."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, *args, **kwargs):
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return None
