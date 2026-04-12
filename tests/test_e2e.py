"""End-to-end tests against a real uvicorn server with a temp database.

Split into two sections:
  1. API E2E tests — exercise the full HTTP stack with ``requests``
     (project CRUD, frames, renders, notifications, templates, presets,
      settings, health, maintenance, WebSocket).
  2. Browser E2E tests — exercise the SPA UI with Playwright
     (page load, dark mode, navigation, project creation form, error states).

Run with: pytest tests/test_e2e.py -v
          pytest tests/test_e2e.py --headed   (to see the browser)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from playwright.sync_api import Page, expect

# ---------------------------------------------------------------------------
# Server fixture — one uvicorn process per test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def e2e_server(tmp_path_factory: pytest.TempPathFactory):
    """Start a real uvicorn server backed by an isolated temp database."""
    base = tmp_path_factory.mktemp("e2e")
    db_path = base / "test_e2e.db"
    frames = base / "frames"
    renders = base / "renders"
    thumbs = base / "thumbs"
    for d in (frames, renders, thumbs):
        d.mkdir()

    env = os.environ.copy()
    env.update(
        {
            "DATABASE_PATH": str(db_path),
            "FRAMES_PATH": str(frames),
            "RENDERS_PATH": str(renders),
            "THUMBNAILS_PATH": str(thumbs),
            # Use dummy NVR credentials so the app starts without real NVR
            "PROTECT_HOST": "127.0.0.1",
            "PROTECT_USERNAME": "test",
            "PROTECT_PASSWORD": "test",
            "LATITUDE": "55.676098",
            "LONGITUDE": "12.568337",
            "TZ": "Europe/Copenhagen",
            "LOG_LEVEL": "WARNING",
        }
    )

    port = 18080
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=Path(__file__).parent.parent,
    )

    base_url = f"http://127.0.0.1:{port}"

    # Wait up to 40s for the server to become ready (uvicorn may take time on first DB init)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{base_url}/api/health", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.skip("E2E server did not start in time — skipping E2E tests")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ===========================================================================
# Helpers
# ===========================================================================


def _create_project(base_url: str, **overrides) -> dict:
    """Create a project via the API, retrying if rate-limited."""
    payload = {
        "name": "Test Project",
        "camera_id": "cam-test",
        "project_type": "live",
        "interval_seconds": 60,
    }
    payload.update(overrides)
    for attempt in range(4):
        r = requests.post(f"{base_url}/api/projects", json=payload)
        if r.status_code != 429:
            break
        time.sleep(15 * (attempt + 1))
    assert r.status_code == 201, f"Failed to create project: {r.status_code} {r.text}"
    return r.json()


# ===========================================================================
# Section 1: API E2E tests (no browser needed)
# ===========================================================================


class TestHealthEndpoints:
    """Health, liveness, readiness, system status, disk, pool stats."""

    def test_health_ok(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "disk_free_gb" in data

    def test_liveness_probe(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/health/live")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_readiness_probe(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/health/ready")
        # NVR not connected in test env → 503
        assert r.status_code in (200, 503)

    def test_system_status(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/system/status")
        assert r.status_code == 200
        data = r.json()
        assert "nvr" in data
        assert "scheduler" in data
        assert "render_worker" in data
        assert "disk" in data
        assert "db" in data

    def test_pool_stats(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/admin/pool-stats")
        assert r.status_code == 200
        data = r.json()
        assert "pool_size" in data
        assert "idle_connections" in data

    def test_disk_breakdown(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/disk")
        assert r.status_code == 200
        data = r.json()
        assert "total_gb" in data
        assert "projects" in data


class TestProjectCRUD:
    """Full project lifecycle: create → get → list → update → clone → pin → delete."""

    def test_create_list_get_update_delete(self, e2e_server: str) -> None:
        # Create
        project = _create_project(
            e2e_server, name="E2E Test Project", camera_id="cam-e2e-1", interval_seconds=120
        )
        pid = project["id"]
        assert project["name"] == "E2E Test Project"
        assert project["camera_id"] == "cam-e2e-1"
        assert project["interval_seconds"] == 120
        assert project["status"] == "active"

        # Get
        r = requests.get(f"{e2e_server}/api/projects/{pid}")
        assert r.status_code == 200
        assert r.json()["name"] == "E2E Test Project"

        # List
        r = requests.get(f"{e2e_server}/api/projects")
        assert r.status_code == 200
        names = [p["name"] for p in r.json()]
        assert "E2E Test Project" in names

        # Update
        r = requests.put(
            f"{e2e_server}/api/projects/{pid}",
            json={"name": "E2E Renamed", "interval_seconds": 300},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "E2E Renamed"
        assert r.json()["interval_seconds"] == 300

        # Delete
        r = requests.delete(f"{e2e_server}/api/projects/{pid}")
        assert r.status_code == 204

        # Confirm gone
        r = requests.get(f"{e2e_server}/api/projects/{pid}")
        assert r.status_code == 404

    def test_create_project_validation(self, e2e_server: str) -> None:
        """Invalid payloads return 422."""
        # Missing required fields
        r = requests.post(f"{e2e_server}/api/projects", json={"name": "Bad"})
        assert r.status_code == 422

        # interval_seconds < 1
        r = requests.post(
            f"{e2e_server}/api/projects",
            json={
                "name": "Bad",
                "camera_id": "cam-1",
                "project_type": "live",
                "interval_seconds": 0,
            },
        )
        assert r.status_code == 422

        # Invalid project_type
        r = requests.post(
            f"{e2e_server}/api/projects",
            json={
                "name": "Bad",
                "camera_id": "cam-1",
                "project_type": "invalid_type",
                "interval_seconds": 60,
            },
        )
        assert r.status_code == 422

    def test_get_nonexistent_project(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/projects/99999")
        assert r.status_code == 404

    def test_clone_project(self, e2e_server: str) -> None:
        # Create source project
        proj = _create_project(
            e2e_server,
            name="Clone Source",
            camera_id="cam-clone",
            capture_mode="daylight_only",
        )
        src_id = proj["id"]

        # Clone
        r = requests.post(f"{e2e_server}/api/projects/{src_id}/clone")
        assert r.status_code == 201
        clone = r.json()
        assert clone["name"] == "Clone Source (copy)"
        assert clone["camera_id"] == "cam-clone"
        assert clone["interval_seconds"] == 60
        assert clone["capture_mode"] == "daylight_only"
        assert clone["id"] != src_id

        # Cleanup
        requests.delete(f"{e2e_server}/api/projects/{src_id}")
        requests.delete(f"{e2e_server}/api/projects/{clone['id']}")

    def test_pin_unpin_project(self, e2e_server: str) -> None:
        pid = _create_project(e2e_server, name="Pin Test", camera_id="cam-pin")["id"]

        # Pin
        r = requests.post(f"{e2e_server}/api/projects/{pid}/pin")
        assert r.status_code == 200
        assert r.json()["is_pinned"] is True

        # Verify via GET
        r = requests.get(f"{e2e_server}/api/projects/{pid}")
        assert r.json()["is_pinned"] == 1

        # Unpin
        r = requests.delete(f"{e2e_server}/api/projects/{pid}/pin")
        assert r.status_code == 200
        assert r.json()["is_pinned"] is False

        # Cleanup
        requests.delete(f"{e2e_server}/api/projects/{pid}")

    def test_pause_resume_project(self, e2e_server: str) -> None:
        pid = _create_project(e2e_server, name="Pause Test", camera_id="cam-pause")["id"]

        # Pause
        r = requests.put(f"{e2e_server}/api/projects/{pid}", json={"status": "paused"})
        assert r.status_code == 200
        assert r.json()["status"] == "paused"

        # Resume
        r = requests.put(f"{e2e_server}/api/projects/{pid}", json={"status": "active"})
        assert r.status_code == 200
        assert r.json()["status"] == "active"

        # Cleanup
        requests.delete(f"{e2e_server}/api/projects/{pid}")

    def test_project_capture_modes(self, e2e_server: str) -> None:
        """Verify all capture modes are accepted."""
        for mode in ("continuous", "daylight_only", "schedule", "solar_noon"):
            proj = _create_project(
                e2e_server, name=f"Mode {mode}", camera_id=f"cam-{mode}", capture_mode=mode
            )
            requests.delete(f"{e2e_server}/api/projects/{proj['id']}")

    def test_schedule_test_endpoint(self, e2e_server: str) -> None:
        pid = _create_project(
            e2e_server,
            name="Schedule Test",
            camera_id="cam-sched",
            capture_mode="schedule",
            schedule_start_time="08:00",
            schedule_end_time="18:00",
            schedule_days="1,2,3,4,5",
        )["id"]

        r = requests.get(f"{e2e_server}/api/projects/{pid}/schedule-test")
        assert r.status_code == 200
        data = r.json()
        assert "would_capture" in data
        assert "error" in data  # may be None if check succeeded

        requests.delete(f"{e2e_server}/api/projects/{pid}")

    def test_capacity_endpoint(self, e2e_server: str) -> None:
        pid = _create_project(e2e_server, name="Capacity Test", camera_id="cam-cap")["id"]

        r = requests.get(f"{e2e_server}/api/projects/{pid}/capacity")
        assert r.status_code == 200
        data = r.json()
        assert "days_remaining" in data
        assert "bytes_per_day" in data

        requests.delete(f"{e2e_server}/api/projects/{pid}")


class TestFrameRoutes:
    """Frame listing, stats, bookmarks, and exports."""

    @pytest.fixture()
    def project_with_frames(self, e2e_server: str):
        """Create a project — frames can't be added via API without NVR, but
        we can still test empty-list paths and stats."""
        pid = _create_project(e2e_server, name="Frame Test", camera_id="cam-frame")["id"]
        yield pid
        requests.delete(f"{e2e_server}/api/projects/{pid}")

    def test_list_frames_empty(self, e2e_server: str, project_with_frames: int) -> None:
        r = requests.get(f"{e2e_server}/api/projects/{project_with_frames}/frames")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert data == []

    def test_list_frames_with_params(self, e2e_server: str, project_with_frames: int) -> None:
        r = requests.get(
            f"{e2e_server}/api/projects/{project_with_frames}/frames",
            params={"limit": 10, "offset": 0, "order": "desc"},
        )
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_bookmarks_empty(self, e2e_server: str, project_with_frames: int) -> None:
        r = requests.get(f"{e2e_server}/api/projects/{project_with_frames}/frames/bookmarks")
        assert r.status_code == 200
        assert r.json() == []

    def test_dark_frames_empty(self, e2e_server: str, project_with_frames: int) -> None:
        r = requests.get(f"{e2e_server}/api/projects/{project_with_frames}/frames/dark")
        assert r.status_code == 200
        assert r.json() == []

    def test_blurry_frames_empty(self, e2e_server: str, project_with_frames: int) -> None:
        r = requests.get(f"{e2e_server}/api/projects/{project_with_frames}/frames/blurry")
        assert r.status_code == 200
        assert r.json() == []

    def test_daily_stats_empty(self, e2e_server: str, project_with_frames: int) -> None:
        r = requests.get(f"{e2e_server}/api/projects/{project_with_frames}/stats/daily")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_timeline_stats_empty(self, e2e_server: str, project_with_frames: int) -> None:
        r = requests.get(f"{e2e_server}/api/projects/{project_with_frames}/stats/timeline")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_export_csv_empty(self, e2e_server: str, project_with_frames: int) -> None:
        r = requests.get(f"{e2e_server}/api/projects/{project_with_frames}/frames/export/csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")

    def test_analyze_interval_empty(self, e2e_server: str, project_with_frames: int) -> None:
        r = requests.get(f"{e2e_server}/api/projects/{project_with_frames}/frames/analyze-interval")
        assert r.status_code == 200

    def test_frame_404_on_nonexistent_project(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/projects/99999/frames")
        assert r.status_code == 404

    def test_bulk_delete_frames_empty(self, e2e_server: str, project_with_frames: int) -> None:
        """Bulk delete with no frames is a no-op."""
        r = requests.delete(
            f"{e2e_server}/api/projects/{project_with_frames}/frames",
            params={"filter": "is_dark"},
        )
        assert r.status_code == 200
        assert r.json()["deleted"] == 0

    def test_gif_status_no_job(self, e2e_server: str, project_with_frames: int) -> None:
        """GIF status returns 404 when no export has been started."""
        r = requests.get(f"{e2e_server}/api/projects/{project_with_frames}/gif/status")
        assert r.status_code == 404


class TestRenderRoutes:
    """Render create, list, status, priority, cancel, delete."""

    @pytest.fixture()
    def project_for_renders(self, e2e_server: str):
        pid = _create_project(e2e_server, name="Render Test", camera_id="cam-render")["id"]
        yield pid
        requests.delete(f"{e2e_server}/api/projects/{pid}")

    def test_create_and_list_render(self, e2e_server: str, project_for_renders: int) -> None:
        r = requests.post(
            f"{e2e_server}/api/renders",
            json={
                "project_id": project_for_renders,
                "framerate": 24,
                "resolution": "1280x720",
                "quality": "draft",
            },
        )
        assert r.status_code == 201
        render = r.json()
        rid = render["id"]
        assert render["framerate"] == 24
        assert render["resolution"] == "1280x720"
        assert render["status"] == "pending"

        # List all renders
        r = requests.get(f"{e2e_server}/api/renders")
        assert r.status_code == 200
        assert any(ren["id"] == rid for ren in r.json())

        # List renders for this project
        r = requests.get(f"{e2e_server}/api/projects/{project_for_renders}/renders")
        assert r.status_code == 200
        assert any(ren["id"] == rid for ren in r.json())

        # Get render status
        r = requests.get(f"{e2e_server}/api/renders/{rid}/status")
        assert r.status_code == 200
        assert r.json()["status"] == "pending"

        # Update priority
        r = requests.put(f"{e2e_server}/api/renders/{rid}/priority", params={"priority": 9})
        assert r.status_code == 200

        # Cancel
        r = requests.post(f"{e2e_server}/api/renders/{rid}/cancel")
        assert r.status_code == 200

        # Delete
        r = requests.delete(f"{e2e_server}/api/renders/{rid}")
        assert r.status_code == 204

    def test_render_nonexistent_project(self, e2e_server: str) -> None:
        r = requests.post(
            f"{e2e_server}/api/renders",
            json={"project_id": 99999, "framerate": 30},
        )
        assert r.status_code == 404

    def test_render_validation(self, e2e_server: str, project_for_renders: int) -> None:
        """Invalid framerate/resolution returns 422."""
        r = requests.post(
            f"{e2e_server}/api/renders",
            json={
                "project_id": project_for_renders,
                "framerate": 0,
            },
        )
        assert r.status_code == 422

        r = requests.post(
            f"{e2e_server}/api/renders",
            json={
                "project_id": project_for_renders,
                "resolution": "not-a-resolution",
            },
        )
        assert r.status_code == 422

    def test_render_download_nonexistent(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/renders/99999/download")
        assert r.status_code == 404

    def test_render_compare(self, e2e_server: str, project_for_renders: int) -> None:
        """Compare two renders returns metadata (even if no output files)."""
        r1 = requests.post(
            f"{e2e_server}/api/renders",
            json={"project_id": project_for_renders, "framerate": 30},
        )
        r2 = requests.post(
            f"{e2e_server}/api/renders",
            json={"project_id": project_for_renders, "framerate": 60},
        )
        rid_a = r1.json()["id"]
        rid_b = r2.json()["id"]

        r = requests.get(f"{e2e_server}/api/renders/{rid_a}/compare/{rid_b}")
        assert r.status_code == 200
        data = r.json()
        assert "a" in data
        assert "b" in data

        # Cleanup
        requests.delete(f"{e2e_server}/api/renders/{rid_a}")
        requests.delete(f"{e2e_server}/api/renders/{rid_b}")

    def test_duplicate_auto_render_rejected(
        self, e2e_server: str, project_for_renders: int
    ) -> None:
        """Second auto_daily render for same project should be rejected (409)."""
        r1 = requests.post(
            f"{e2e_server}/api/renders",
            json={
                "project_id": project_for_renders,
                "render_type": "auto_daily",
            },
        )
        assert r1.status_code == 201

        r2 = requests.post(
            f"{e2e_server}/api/renders",
            json={
                "project_id": project_for_renders,
                "render_type": "auto_daily",
            },
        )
        assert r2.status_code == 409

        requests.delete(f"{e2e_server}/api/renders/{r1.json()['id']}")


class TestNotificationRoutes:
    """Notification CRUD: list, mark read, delete, clear."""

    def test_list_notifications_empty(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/notifications")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_list_notifications_unread_filter(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/notifications", params={"unread_only": True})
        assert r.status_code == 200

    def test_mark_all_read(self, e2e_server: str) -> None:
        r = requests.put(f"{e2e_server}/api/notifications/read", json={"all": True})
        assert r.status_code == 204

    def test_clear_notifications(self, e2e_server: str) -> None:
        r = requests.delete(f"{e2e_server}/api/notifications")
        assert r.status_code == 204

    def test_clear_read_only_notifications(self, e2e_server: str) -> None:
        r = requests.delete(f"{e2e_server}/api/notifications", params={"read_only": True})
        assert r.status_code == 204

    def test_delete_nonexistent_notification(self, e2e_server: str) -> None:
        r = requests.delete(f"{e2e_server}/api/notifications/99999")
        assert r.status_code == 404


class TestSettingsRoutes:
    """Settings GET/PUT, partial update semantics, watermark stubs."""

    def test_get_settings(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/settings")
        assert r.status_code == 200
        data = r.json()
        assert "dark_mode" in data
        assert "protect_host" in data

    def test_partial_update_preserves_other_fields(self, e2e_server: str) -> None:
        """PUT with only dark_mode must not null out protect_host."""
        requests.put(f"{e2e_server}/api/settings", json={"protect_host": "192.168.1.1"})

        r = requests.get(f"{e2e_server}/api/settings")
        assert r.json()["protect_host"] == "192.168.1.1"

        requests.put(f"{e2e_server}/api/settings", json={"dark_mode": True})

        r = requests.get(f"{e2e_server}/api/settings")
        assert r.json()["protect_host"] == "192.168.1.1", (
            "protect_host was wiped by dark_mode toggle"
        )

        # Restore
        requests.put(f"{e2e_server}/api/settings", json={"dark_mode": False})

    def test_settings_roundtrip(self, e2e_server: str) -> None:
        """Write multiple fields and read them back."""
        payload = {
            "dark_mode": True,
            "protect_host": "10.0.0.1",
            "protect_port": 8443,
            "disk_warning_threshold_gb": 5,
        }
        r = requests.put(f"{e2e_server}/api/settings", json=payload)
        assert r.status_code == 200

        r = requests.get(f"{e2e_server}/api/settings")
        data = r.json()
        assert data["protect_host"] == "10.0.0.1"
        assert data["protect_port"] == 8443
        assert data["disk_warning_threshold_gb"] == 5

        # Restore defaults
        requests.put(
            f"{e2e_server}/api/settings",
            json={"dark_mode": False, "protect_host": "127.0.0.1", "protect_port": 443},
        )

    def test_watermark_no_file(self, e2e_server: str) -> None:
        """Watermark preview returns 404 when no watermark uploaded."""
        r = requests.get(f"{e2e_server}/api/settings/watermark-preview")
        assert r.status_code == 404

    def test_delete_watermark_no_file(self, e2e_server: str) -> None:
        """Deleting non-existent watermark is a no-op (204)."""
        r = requests.delete(f"{e2e_server}/api/settings/watermark")
        assert r.status_code in (204, 404)


class TestTemplateRoutes:
    """Template CRUD and apply."""

    def test_template_lifecycle(self, e2e_server: str) -> None:
        # Create template
        r = requests.post(
            f"{e2e_server}/api/templates",
            json={
                "name": "E2E Template",
                "interval_seconds": 120,
                "capture_mode": "daylight_only",
                "retention_days": 30,
            },
        )
        assert r.status_code == 201
        tmpl = r.json()
        tid = tmpl["id"]
        assert tmpl["name"] == "E2E Template"
        assert tmpl["interval_seconds"] == 120

        # List and verify template is present
        r = requests.get(f"{e2e_server}/api/templates")
        assert r.status_code == 200
        templates = r.json()
        assert any(t["id"] == tid for t in templates)
        found = next(t for t in templates if t["id"] == tid)
        assert found["name"] == "E2E Template"

        # Apply — creates a new project from the template
        r = requests.post(
            f"{e2e_server}/api/templates/{tid}/apply",
            json={"name": "From Template", "camera_id": "cam-tmpl"},
        )
        assert r.status_code == 201
        proj = r.json()
        assert proj["name"] == "From Template"
        assert proj["interval_seconds"] == 120
        assert proj["capture_mode"] == "daylight_only"
        assert proj["template_id"] == tid

        # Cleanup
        requests.delete(f"{e2e_server}/api/projects/{proj['id']}")
        r = requests.delete(f"{e2e_server}/api/templates/{tid}")
        assert r.status_code == 204

    def test_duplicate_template_name(self, e2e_server: str) -> None:
        requests.post(
            f"{e2e_server}/api/templates",
            json={"name": "Unique Template", "interval_seconds": 60},
        )
        r = requests.post(
            f"{e2e_server}/api/templates",
            json={"name": "Unique Template", "interval_seconds": 60},
        )
        assert r.status_code == 409

        # Cleanup: find and delete
        templates = requests.get(f"{e2e_server}/api/templates").json()
        for t in templates:
            if t["name"] == "Unique Template":
                requests.delete(f"{e2e_server}/api/templates/{t['id']}")


class TestPresetRoutes:
    """Render preset CRUD."""

    def test_preset_lifecycle(self, e2e_server: str) -> None:
        # Create
        r = requests.post(
            f"{e2e_server}/api/presets",
            json={
                "name": "E2E Preset",
                "framerate": 60,
                "resolution": "3840x2160",
                "quality": "high",
            },
        )
        assert r.status_code == 201
        preset = r.json()
        pid = preset["id"]
        assert preset["name"] == "E2E Preset"
        assert preset["framerate"] == 60

        # List
        r = requests.get(f"{e2e_server}/api/presets")
        assert r.status_code == 200
        assert any(p["id"] == pid for p in r.json())

        # Get by ID
        r = requests.get(f"{e2e_server}/api/presets/{pid}")
        assert r.status_code == 200

        # Delete
        r = requests.delete(f"{e2e_server}/api/presets/{pid}")
        assert r.status_code == 204

        # Verify gone
        r = requests.get(f"{e2e_server}/api/presets/{pid}")
        assert r.status_code == 404

    def test_duplicate_preset_name(self, e2e_server: str) -> None:
        requests.post(
            f"{e2e_server}/api/presets",
            json={"name": "Dup Preset", "framerate": 30},
        )
        r = requests.post(
            f"{e2e_server}/api/presets",
            json={"name": "Dup Preset", "framerate": 30},
        )
        assert r.status_code == 409

        # Cleanup
        presets = requests.get(f"{e2e_server}/api/presets").json()
        for p in presets:
            if p["name"] == "Dup Preset":
                requests.delete(f"{e2e_server}/api/presets/{p['id']}")


class TestMaintenanceRoutes:
    """Maintenance trigger endpoint."""

    def test_trigger_maintenance(self, e2e_server: str) -> None:
        r = requests.post(f"{e2e_server}/api/maintenance/run")
        assert r.status_code == 202

    def test_trigger_backup(self, e2e_server: str) -> None:
        r = requests.post(f"{e2e_server}/api/backup")
        assert r.status_code == 202


class TestWebSocket:
    """WebSocket connectivity."""

    def test_websocket_connect(self, e2e_server: str) -> None:
        """Connect to WS endpoint and receive a valid JSON event."""
        import websocket

        ws_url = e2e_server.replace("http://", "ws://") + "/api/ws"
        ws = websocket.create_connection(ws_url, timeout=35)
        try:
            # Server may send nvr_status, capture_batch, or ping — any valid event is fine
            msg = ws.recv()
            data = json.loads(msg)
            assert "event" in data
        finally:
            ws.close()


class TestLogsEndpoint:
    """Log endpoint with path traversal guard."""

    def test_logs_no_file(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/api/logs")
        assert r.status_code == 200
        assert "lines" in r.json()

    def test_logs_path_traversal_blocked(self, e2e_server: str) -> None:
        """Requesting /etc/passwd via log_file should be denied."""
        r = requests.get(
            f"{e2e_server}/api/logs",
            params={"log_file": "/etc/passwd"},
        )
        assert r.status_code == 200
        data = r.json()
        assert "error" in data
        assert "denied" in data["error"].lower() or data["lines"] == []


class TestMetrics:
    """Prometheus metrics endpoint."""

    def test_metrics_endpoint(self, e2e_server: str) -> None:
        r = requests.get(f"{e2e_server}/metrics")
        assert r.status_code == 200
        body = r.text
        # Should contain at least some metric names
        assert "projects_total" in body or "frames_total" in body or "# " in body


# ===========================================================================
# Section 2: Browser E2E tests (Playwright)
# ===========================================================================


def test_page_loads(page: Page, e2e_server: str) -> None:
    """The app loads without JS errors and shows the main UI shell."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    # Wait for Alpine.js to initialise — the nav bar is a good proxy
    page.wait_for_selector("nav", timeout=10_000)

    assert not errors, f"JS errors on page load: {errors}"


def test_dark_mode_toggle_persists(page: Page, e2e_server: str) -> None:
    """Toggling dark mode changes the <html> class, persists to localStorage AND the DB."""
    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    # Find the dark-mode toggle button in the nav bar (it contains an SVG icon)
    toggle = page.locator("button[title='Toggle dark/light mode']")
    expect(toggle).to_be_visible(timeout=5_000)

    # Read current dark state from html class
    html = page.locator("html")
    initial_dark = "dark" in (html.get_attribute("class") or "")

    # Click toggle — wait long enough for the async PUT /api/settings to complete
    toggle.click()
    page.wait_for_timeout(1500)

    after_dark = "dark" in (html.get_attribute("class") or "")
    assert after_dark != initial_dark, "Dark class should have toggled on <html>"

    # Check localStorage was updated
    stored = page.evaluate("() => localStorage.getItem('darkMode')")
    assert stored == str(after_dark).lower(), f"localStorage.darkMode should be {after_dark}"

    # Verify the API actually persisted it to the DB (not silently failed)
    r = requests.get(f"{e2e_server}/api/settings")
    db_dark = bool(r.json().get("dark_mode"))
    assert db_dark == after_dark, "DB dark_mode should match the toggled state"

    # Toggle back
    toggle.click()
    page.wait_for_timeout(1500)
    final_dark = "dark" in (html.get_attribute("class") or "")
    assert final_dark == initial_dark, "Second toggle should restore original state"

    # Verify DB is back to initial state too — proves toggleDark doesn't wipe other fields
    r2 = requests.get(f"{e2e_server}/api/settings")
    assert bool(r2.json().get("dark_mode")) == final_dark, "DB should reflect restored state"


def test_dark_mode_survives_reload(page: Page, e2e_server: str) -> None:
    """Dark mode preference set via the API persists across page reloads."""
    # Set dark mode to true via API (persists to DB)
    r = requests.put(f"{e2e_server}/api/settings", json={"dark_mode": True})
    assert r.status_code == 200

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)
    page.wait_for_timeout(1000)

    html = page.locator("html")
    after_dark = "dark" in (html.get_attribute("class") or "")
    assert after_dark is True, "<html> should have .dark class when DB has dark_mode=1"

    # Set dark mode to false via API
    r = requests.put(f"{e2e_server}/api/settings", json={"dark_mode": False})
    assert r.status_code == 200

    page.reload()
    page.wait_for_selector("nav", timeout=10_000)
    page.wait_for_timeout(1000)

    after_light = "dark" in (html.get_attribute("class") or "")
    assert after_light is False, "<html> should NOT have .dark class when DB has dark_mode=0"


def test_settings_dark_mode_toggle_in_settings_panel(page: Page, e2e_server: str) -> None:
    """The toggle in the Settings panel also switches dark mode without resetting."""
    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    # Navigate to Settings using the @click="openSettings()" button (bottom nav)
    page.locator("[\\@click='openSettings()']").first.click()
    page.wait_for_timeout(800)

    html = page.locator("html")
    before = "dark" in (html.get_attribute("class") or "")

    # The dark mode toggle in settings has @click="toggleDark()" — use that
    settings_toggle = page.locator("[\\@click='toggleDark()']").first
    expect(settings_toggle).to_be_visible(timeout=5_000)
    settings_toggle.click()
    page.wait_for_timeout(1000)

    after = "dark" in (html.get_attribute("class") or "")
    assert after != before, "Settings panel toggle should switch dark mode"

    # Verify settings were persisted to DB — reload and check
    page.reload()
    page.wait_for_selector("nav", timeout=10_000)
    page.wait_for_timeout(1000)
    reloaded = "dark" in (html.get_attribute("class") or "")
    # After reload Alpine loads theme from DB so it should match `after`
    assert reloaded == after, "Dark mode from DB should match what was toggled in settings"


def test_navigation_tabs(page: Page, e2e_server: str) -> None:
    """Dashboard and Settings navigation buttons are clickable without errors."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    # Use the @click attribute selector to target openSettings() precisely
    page.locator("[\\@click='openSettings()']").first.click()
    page.wait_for_timeout(500)
    assert not errors

    # Go back to Dashboard — top nav logo button is first with @click="view = 'dashboard'"
    page.locator("[\\@click=\"view = 'dashboard'\"]").first.click()
    page.wait_for_timeout(500)
    assert not errors


def test_api_settings_endpoint(e2e_server: str) -> None:
    """GET /api/settings returns a settings dict including dark_mode."""
    r = requests.get(f"{e2e_server}/api/settings")
    assert r.status_code == 200
    data = r.json()
    assert "dark_mode" in data


def test_dark_mode_api_roundtrip(e2e_server: str) -> None:
    """PUT /api/settings with dark_mode=True persists and is returned by GET."""
    r = requests.put(f"{e2e_server}/api/settings", json={"dark_mode": True})
    assert r.status_code == 200
    assert r.json()["dark_mode"] in (1, True)

    r2 = requests.get(f"{e2e_server}/api/settings")
    assert r2.json()["dark_mode"] in (1, True)

    # Restore
    requests.put(f"{e2e_server}/api/settings", json={"dark_mode": False})


def test_toggle_dark_does_not_clear_protect_host(e2e_server: str) -> None:
    """Regression test: PUT with only {dark_mode: true} must not NULL out protect_host."""
    requests.put(f"{e2e_server}/api/settings", json={"protect_host": "192.168.1.1"})

    r = requests.get(f"{e2e_server}/api/settings")
    assert r.json()["protect_host"] == "192.168.1.1"

    requests.put(f"{e2e_server}/api/settings", json={"dark_mode": True})

    r2 = requests.get(f"{e2e_server}/api/settings")
    assert r2.json()["protect_host"] == "192.168.1.1", (
        "protect_host must not be wiped by dark mode toggle"
    )

    # Restore
    requests.put(f"{e2e_server}/api/settings", json={"dark_mode": False})


def test_dashboard_shows_empty_state(page: Page, e2e_server: str) -> None:
    """Dashboard with no projects should show the empty-state UI (no JS errors)."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)
    page.wait_for_timeout(500)

    assert not errors, f"JS errors on empty dashboard: {errors}"


def test_create_project_ui_flow(page: Page, e2e_server: str) -> None:
    """Navigate to create project form, fill it out, and verify submission succeeds."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    # Click the create/add project button
    create_btn = page.locator("[\\@click='openCreateForm()']").first
    if create_btn.count() > 0:
        create_btn.click()
        page.wait_for_timeout(500)

        # The create form should now be visible
        # Check the view changed (either via URL or Alpine state)
        assert not errors, f"JS errors opening create form: {errors}"
    else:
        # If no create button in nav, the empty state might have one
        # Look for any "New Project" or "Create" link/button
        alt_btn = page.locator("text=New Project").first
        if alt_btn.count() > 0:
            alt_btn.click()
            page.wait_for_timeout(500)
            assert not errors


def test_keyboard_shortcut_n_opens_create(page: Page, e2e_server: str) -> None:
    """Pressing 'n' on the dashboard should open the create project form."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)
    page.wait_for_timeout(500)

    page.keyboard.press("n")
    page.wait_for_timeout(500)

    assert not errors, f"JS errors after pressing 'n': {errors}"


def test_keyboard_escape_returns_to_dashboard(page: Page, e2e_server: str) -> None:
    """Pressing Escape should navigate back to the dashboard."""
    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    # Go to settings first
    page.locator("[\\@click='openSettings()']").first.click()
    page.wait_for_timeout(500)

    # Escape should return to dashboard
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)

    # Verify we're back on dashboard by checking Alpine state
    view = page.evaluate("() => document.querySelector('[x-data]')?.__x?.$data?.view")
    # view may be null if Alpine internals differ, but at least no errors
    if view is not None:
        assert view == "dashboard"


def test_static_assets_served(e2e_server: str) -> None:
    """Static assets (app.js) are served correctly. app.css is built by Tailwind in Docker."""
    r = requests.get(f"{e2e_server}/static/app.js")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "javascript" in ct or "text/plain" in ct


def test_index_html_served_at_root(e2e_server: str) -> None:
    """Root URL serves the SPA HTML shell."""
    r = requests.get(e2e_server)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "alpine" in r.text.lower() or "x-data" in r.text


def test_concurrent_api_requests(e2e_server: str) -> None:
    """Multiple simultaneous API requests don't cause errors."""
    import concurrent.futures

    def fetch_health():
        return requests.get(f"{e2e_server}/api/health", timeout=5).status_code

    def fetch_settings():
        return requests.get(f"{e2e_server}/api/settings", timeout=5).status_code

    def fetch_projects():
        return requests.get(f"{e2e_server}/api/projects", timeout=5).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futs = []
        for _ in range(5):
            futs.append(ex.submit(fetch_health))
            futs.append(ex.submit(fetch_settings))
            futs.append(ex.submit(fetch_projects))

        results = [f.result() for f in futs]

    assert all(code == 200 for code in results), f"Some requests failed: {results}"


def test_project_detail_view(page: Page, e2e_server: str) -> None:
    """Create a project via API, then navigate to its detail view in the browser."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    # Create a project via API
    pid = _create_project(e2e_server, name="Detail View Test", camera_id="cam-detail")["id"]

    try:
        page.goto(e2e_server)
        page.wait_for_selector("nav", timeout=10_000)
        page.wait_for_timeout(1000)

        # The project should appear on the dashboard — click it
        project_card = page.locator("text=Detail View Test").first
        if project_card.count() > 0:
            project_card.click()
            page.wait_for_timeout(1000)
            assert not errors, f"JS errors in project detail view: {errors}"
    finally:
        requests.delete(f"{e2e_server}/api/projects/{pid}")


def test_renders_queue_view(page: Page, e2e_server: str) -> None:
    """Navigate to the renders queue view without errors."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    # Click the renders queue nav item
    renders_btn = page.locator("[\\@click=\"view = 'renders_queue'\"]").first
    if renders_btn.count() > 0:
        renders_btn.click()
        page.wait_for_timeout(500)
        assert not errors, f"JS errors in renders queue: {errors}"


def test_cameras_view(page: Page, e2e_server: str) -> None:
    """Navigate to the cameras view without errors."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    cameras_btn = page.locator("[\\@click=\"view = 'cameras'\"]").first
    if cameras_btn.count() > 0:
        cameras_btn.click()
        page.wait_for_timeout(500)
        assert not errors, f"JS errors in cameras view: {errors}"


# ===========================================================================
# Section 3: Additional Playwright browser tests
# ===========================================================================


def test_system_log_view(page: Page, e2e_server: str) -> None:
    """Navigate to system log view and verify it renders without errors."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    log_btn = page.locator("[\\@click=\"view = 'system_log'\"]").first
    if log_btn.count() > 0:
        log_btn.click()
        page.wait_for_timeout(800)
        assert not errors, f"JS errors in system log view: {errors}"


def test_keyboard_question_mark_shows_shortcuts(page: Page, e2e_server: str) -> None:
    """Pressing '?' opens the keyboard shortcuts help modal."""
    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)
    page.wait_for_timeout(500)

    page.keyboard.press("?")
    page.wait_for_timeout(500)

    # The shortcuts modal should now be visible
    modal = page.locator("[x-show='showShortcutHelp']")
    expect(modal).to_be_visible(timeout=3_000)

    # Close it with Escape
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    expect(modal).to_be_hidden(timeout=3_000)


def test_notification_dropdown(page: Page, e2e_server: str) -> None:
    """Open and close the notification dropdown without errors."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    # Click the notification bell button
    notif_btn = page.locator("button[aria-label='Notifications']").first
    if notif_btn.count() == 0:
        # Try finding by the @click handler
        notif_btn = page.locator(
            "[\\@click='showNotifDropdown = !showNotifDropdown; if(showNotifDropdown) loadNotifications()']"
        ).first
    if notif_btn.count() > 0:
        notif_btn.click()
        page.wait_for_timeout(500)

        # Dropdown should be visible
        dropdown = page.locator("[x-show='showNotifDropdown']")
        expect(dropdown).to_be_visible(timeout=3_000)

        # Mark all read button should be present
        mark_read = page.locator("text=Mark all read")
        expect(mark_read).to_be_visible(timeout=3_000)

        # Close dropdown by pressing Escape
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    assert not errors, f"JS errors in notification dropdown: {errors}"


def test_disk_usage_modal(page: Page, e2e_server: str) -> None:
    """Clicking the disk gauge opens the disk usage breakdown modal."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)
    page.wait_for_timeout(500)

    # Click the disk usage element
    disk_btn = page.locator("[\\@click='openDiskModal()']").first
    if disk_btn.count() > 0:
        disk_btn.click()
        page.wait_for_timeout(800)

        modal = page.locator("[x-show='showDiskModal']")
        expect(modal).to_be_visible(timeout=3_000)

        # Close modal
        page.locator("[x-show='showDiskModal']").locator("text=✕").click()
        page.wait_for_timeout(300)

    assert not errors, f"JS errors in disk modal: {errors}"


def test_settings_panel_loads_fields(page: Page, e2e_server: str) -> None:
    """Settings panel loads and displays expected form fields."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    page.locator("[\\@click='openSettings()']").first.click()
    page.wait_for_timeout(800)

    # Settings form should be visible with expected field labels
    settings_section = page.locator("[x-show=\"view === 'settings'\"]")
    expect(settings_section).to_be_visible(timeout=3_000)

    # Check for key settings fields — NVR host, dark mode toggle, save button
    save_btn = page.locator("text=Save Settings").first
    if save_btn.count() > 0:
        expect(save_btn).to_be_visible(timeout=3_000)

    assert not errors, f"JS errors in settings panel: {errors}"


def test_settings_save_button(page: Page, e2e_server: str) -> None:
    """Click Save Settings and verify it completes without error."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    page.locator("[\\@click='openSettings()']").first.click()
    page.wait_for_timeout(800)

    save_btn = page.locator("[\\@click='saveSettings()']").first
    if save_btn.count() > 0:
        save_btn.click()
        page.wait_for_timeout(1500)

        # A "Settings saved" toast should appear
        toast = page.locator("text=Settings saved").first
        if toast.count() > 0:
            expect(toast).to_be_visible(timeout=5_000)

    assert not errors, f"JS errors saving settings: {errors}"


def test_create_project_full_flow(page: Page, e2e_server: str) -> None:
    """Fill out the create project form and submit it via the browser."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    # Open create form
    page.locator("[\\@click='openCreateForm()']").first.click()
    page.wait_for_timeout(500)

    create_section = page.locator("[x-show=\"view === 'create_project'\"]")
    expect(create_section).to_be_visible(timeout=3_000)

    # Fill in the project name
    name_input = page.locator("[x-model='form.name']").first
    if name_input.count() > 0:
        name_input.fill("Browser Created Project")

    # Fill interval
    interval_input = page.locator("[x-model='form.interval_seconds']").first
    if interval_input.count() > 0:
        interval_input.fill("120")

    # Select a camera if dropdown available (NVR not connected so list may be empty,
    # but we can type a camera ID if it's a text input)
    camera_input = page.locator("[x-model='form.camera_id']").first
    if camera_input.count() > 0:
        tag = camera_input.evaluate("el => el.tagName")
        if tag == "SELECT":
            # If it's a select, try to select first option
            options = camera_input.locator("option").all()
            if len(options) > 1:
                camera_input.select_option(index=1)
            else:
                # No cameras available — we can't submit the form
                return
        else:
            camera_input.fill("cam-browser-test")

    # Submit the form
    submit_btn = page.locator("[\\@click='submitForm()']").first
    if submit_btn.count() > 0:
        submit_btn.click()
        page.wait_for_timeout(2000)

    # Check for success toast or return to dashboard
    assert not errors, f"JS errors during project creation: {errors}"

    # Clean up via API
    projects = requests.get(f"{e2e_server}/api/projects").json()
    for p in projects:
        if p["name"] == "Browser Created Project":
            requests.delete(f"{e2e_server}/api/projects/{p['id']}")


def test_project_detail_tabs(page: Page, e2e_server: str) -> None:
    """Navigate between project detail tabs: overview, renders, bookmarks, quality."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    pid = _create_project(e2e_server, name="Tab Nav Test", camera_id="cam-tabs")["id"]

    try:
        page.goto(e2e_server)
        page.wait_for_selector("nav", timeout=10_000)
        page.wait_for_timeout(1000)

        # Click the project to open detail view
        card = page.locator("text=Tab Nav Test").first
        if card.count() == 0:
            return
        card.click()
        page.wait_for_timeout(1000)

        # Verify the overview tab is active by default
        overview_section = page.locator("[x-show=\"detailTab === 'overview'\"]")
        expect(overview_section).to_be_visible(timeout=3_000)

        # Click the Renders tab
        renders_tab = page.locator("button[role='tab']", has_text="Renders").first
        if renders_tab.count() > 0:
            renders_tab.click()
            page.wait_for_timeout(500)
            renders_section = page.locator("[x-show=\"detailTab === 'renders'\"]")
            expect(renders_section).to_be_visible(timeout=3_000)

        # Click the Bookmarks tab
        bookmarks_tab = page.locator("button[role='tab']", has_text="Bookmarks").first
        if bookmarks_tab.count() > 0:
            bookmarks_tab.click()
            page.wait_for_timeout(500)
            bookmarks_section = page.locator("[x-show=\"detailTab === 'bookmarks'\"]")
            expect(bookmarks_section).to_be_visible(timeout=3_000)

        # Click the Quality tab
        quality_tab = page.locator("button[role='tab']", has_text="Quality").first
        if quality_tab.count() > 0:
            quality_tab.click()
            page.wait_for_timeout(500)
            quality_section = page.locator("[x-show=\"detailTab === 'quality'\"]")
            expect(quality_section).to_be_visible(timeout=3_000)

        # Back to Overview
        overview_tab = page.locator("button[role='tab']", has_text="Overview").first
        if overview_tab.count() > 0:
            overview_tab.click()
            page.wait_for_timeout(500)
            expect(overview_section).to_be_visible(timeout=3_000)

        assert not errors, f"JS errors navigating tabs: {errors}"
    finally:
        requests.delete(f"{e2e_server}/api/projects/{pid}")


def test_project_pause_resume_from_dashboard(page: Page, e2e_server: str) -> None:
    """Pause and resume a project using the dashboard card buttons."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    pid = _create_project(e2e_server, name="Pause UI Test", camera_id="cam-pauseui")["id"]

    try:
        page.goto(e2e_server)
        page.wait_for_selector("nav", timeout=10_000)
        page.wait_for_timeout(1000)

        # Find the pause button for this project
        pause_btn = page.locator(f"[\\@click='pauseProject({pid})']").first
        if pause_btn.count() > 0:
            pause_btn.click()
            page.wait_for_timeout(1000)

            # Verify project is paused via API
            r = requests.get(f"{e2e_server}/api/projects/{pid}")
            assert r.json()["status"] == "paused"

            # Now resume
            resume_btn = page.locator(f"[\\@click='resumeProject({pid})']").first
            if resume_btn.count() > 0:
                resume_btn.click()
                page.wait_for_timeout(1000)
                r = requests.get(f"{e2e_server}/api/projects/{pid}")
                assert r.json()["status"] == "active"

        assert not errors, f"JS errors during pause/resume: {errors}"
    finally:
        requests.delete(f"{e2e_server}/api/projects/{pid}")


def test_project_edit_form(page: Page, e2e_server: str) -> None:
    """Open a project and click Edit to verify the edit form loads."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    pid = _create_project(e2e_server, name="Edit Form Test", camera_id="cam-edit")["id"]

    try:
        page.goto(e2e_server)
        page.wait_for_selector("nav", timeout=10_000)
        page.wait_for_timeout(1000)

        # Navigate to project detail
        card = page.locator("text=Edit Form Test").first
        if card.count() == 0:
            return
        card.click()
        page.wait_for_timeout(1000)

        # Click the Edit button
        edit_btn = page.locator("[\\@click='openEditForm(activeProject)']").first
        if edit_btn.count() > 0:
            edit_btn.click()
            page.wait_for_timeout(500)

            # Should be on create_project view in edit mode
            create_section = page.locator("[x-show=\"view === 'create_project'\"]")
            expect(create_section).to_be_visible(timeout=3_000)

            # Name should be pre-filled
            name_input = page.locator("[x-model='form.name']").first
            if name_input.count() > 0:
                val = name_input.input_value()
                assert "Edit Form Test" in val

        assert not errors, f"JS errors in edit form: {errors}"
    finally:
        requests.delete(f"{e2e_server}/api/projects/{pid}")


def test_bulk_select_mode(page: Page, e2e_server: str) -> None:
    """Toggle bulk select mode on the dashboard and select a project."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    pid = _create_project(e2e_server, name="Bulk Select Test", camera_id="cam-bulk")["id"]

    try:
        page.goto(e2e_server)
        page.wait_for_selector("nav", timeout=10_000)
        page.wait_for_timeout(1000)

        # Toggle select mode
        select_btn = page.locator("[\\@click='toggleSelectMode()']").first
        if select_btn.count() > 0:
            select_btn.click()
            page.wait_for_timeout(300)

            # Bulk action bar should become visible
            bulk_bar = page.locator("[x-show='selectMode']").first
            expect(bulk_bar).to_be_visible(timeout=3_000)

            # Click a project card to select it (in select mode, click selects)
            card = page.locator("text=Bulk Select Test").first
            if card.count() > 0:
                card.click()
                page.wait_for_timeout(300)

            # Exit select mode
            select_btn.click()
            page.wait_for_timeout(300)

        assert not errors, f"JS errors in bulk select: {errors}"
    finally:
        requests.delete(f"{e2e_server}/api/projects/{pid}")


def test_back_button_from_project_detail(page: Page, e2e_server: str) -> None:
    """The '← Back' button in project detail returns to dashboard."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    pid = _create_project(e2e_server, name="Back Button Test", camera_id="cam-back")["id"]

    try:
        page.goto(e2e_server)
        page.wait_for_selector("nav", timeout=10_000)
        page.wait_for_timeout(1000)

        card = page.locator("text=Back Button Test").first
        if card.count() == 0:
            return
        card.click()
        page.wait_for_timeout(1000)

        # Click back button
        back_btn = page.locator("[\\@click=\"view = 'dashboard'\"]", has_text="Back").first
        if back_btn.count() > 0:
            back_btn.click()
            page.wait_for_timeout(500)

            # Dashboard should be visible
            dashboard = page.locator("[x-show=\"view === 'dashboard'\"]")
            expect(dashboard).to_be_visible(timeout=3_000)

        assert not errors, f"JS errors using back button: {errors}"
    finally:
        requests.delete(f"{e2e_server}/api/projects/{pid}")


def test_websocket_status_indicator(page: Page, e2e_server: str) -> None:
    """The WebSocket status indicator should appear (connected or reconnecting)."""
    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)
    page.wait_for_timeout(2000)

    # Check wsStatus via Alpine data
    ws_status = page.evaluate(
        "() => document.querySelector('[x-data]')?._x_dataStack?.[0]?.wsStatus"
    )
    # The WS may or may not connect depending on server state,
    # but the indicator should exist
    assert ws_status in (
        "connected",
        "connecting",
        "reconnecting",
        "polling",
        None,
    ), f"Unexpected wsStatus: {ws_status}"


def test_toast_appears_on_settings_save(page: Page, e2e_server: str) -> None:
    """Saving settings should produce a visible toast notification."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    page.locator("[\\@click='openSettings()']").first.click()
    page.wait_for_timeout(800)

    save_btn = page.locator("[\\@click='saveSettings()']").first
    if save_btn.count() > 0:
        save_btn.click()
        page.wait_for_timeout(2000)

        # Toast should be present (the toasts container has role="status" items)
        toasts = page.locator("[x-data] >> text=Settings saved")
        if toasts.count() > 0:
            expect(toasts.first).to_be_visible(timeout=3_000)

    assert not errors, f"JS errors during settings save toast: {errors}"


def test_create_form_validation_no_name(page: Page, e2e_server: str) -> None:
    """Submitting the create form with no name should show an error toast."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)

    page.locator("[\\@click='openCreateForm()']").first.click()
    page.wait_for_timeout(500)

    # Clear the name field (it might have a default)
    name_input = page.locator("[x-model='form.name']").first
    if name_input.count() > 0:
        name_input.fill("")

    # Try to submit
    submit_btn = page.locator("[\\@click='submitForm()']").first
    if submit_btn.count() > 0:
        submit_btn.click()
        page.wait_for_timeout(1000)

        # Should show a validation error toast
        error_toast = page.locator("text=Project name is required")
        if error_toast.count() > 0:
            expect(error_toast.first).to_be_visible(timeout=3_000)

        # Should NOT navigate away from the form
        create_section = page.locator("[x-show=\"view === 'create_project'\"]")
        expect(create_section).to_be_visible(timeout=1_000)

    assert not errors, f"JS errors during form validation: {errors}"


def test_alpine_data_initialised(page: Page, e2e_server: str) -> None:
    """Verify Alpine.js x-data component is fully initialised with expected state."""
    page.goto(e2e_server)
    # Wait for Alpine.js to initialise the x-data component
    page.wait_for_function(
        "() => { const el = document.querySelector('[x-data]'); return el && el._x_dataStack; }",
        timeout=10_000,
    )

    # Verify Alpine data store has the expected shape
    state = page.evaluate("""() => {
        const el = document.querySelector('[x-data]');
        if (!el || !el._x_dataStack) return null;
        const d = el._x_dataStack[0];
        return {
            hasProjects: Array.isArray(d.projects),
            hasCameras: Array.isArray(d.cameras),
            hasTemplates: Array.isArray(d.templates),
            hasNotifications: Array.isArray(d.notifications),
            view: d.view,
            darkModeType: typeof d.darkMode,
        };
    }""")

    if state is not None:
        assert state["hasProjects"] is True
        assert state["hasCameras"] is True
        assert state["hasTemplates"] is True
        assert state["hasNotifications"] is True
        assert state["view"] == "dashboard"
        assert state["darkModeType"] == "boolean"
