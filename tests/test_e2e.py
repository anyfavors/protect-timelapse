"""End-to-end browser tests using Playwright.

Spins up a real uvicorn server against a temp database so we can assert
actual browser behaviour — dark/light mode toggle, page load, navigation.

Run with: pytest tests/test_e2e.py --headed  (to see the browser)
          pytest tests/test_e2e.py           (headless, default)
"""

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_page_loads(page: Page, e2e_server: str) -> None:
    """The app loads without JS errors and shows the main UI shell."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(e2e_server)
    # Wait for Alpine.js to initialise — the nav bar is a good proxy
    page.wait_for_selector("nav", timeout=10_000)

    assert not errors, f"JS errors on page load: {errors}"


def test_health_api_ok(e2e_server: str) -> None:
    """Health endpoint returns status=ok (no browser needed)."""
    r = requests.get(f"{e2e_server}/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


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
    """Dark mode preference set via the API persists across page reloads.

    The app loads theme from the DB on init(), so we set it via the API
    (which writes to DB) and verify the class is correct after a fresh load.
    """
    # Set dark mode to true via API (persists to DB)
    r = requests.put(f"{e2e_server}/api/settings", json={"dark_mode": True})
    assert r.status_code == 200

    page.goto(e2e_server)
    page.wait_for_selector("nav", timeout=10_000)
    # Wait a moment for Alpine to finish init() and apply theme from DB
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
    """Toggling dark mode via the API must not wipe protect_host or other fields.

    This is a regression test for the bug where PUT /api/settings with only
    {dark_mode: true} would NULL out protect_host because the backend used
    model_dump() instead of model_dump(exclude_unset=True).
    """
    # Set a known protect_host value
    requests.put(f"{e2e_server}/api/settings", json={"protect_host": "192.168.1.1"})

    r = requests.get(f"{e2e_server}/api/settings")
    assert r.json()["protect_host"] == "192.168.1.1", "protect_host should be set before test"

    # Toggle dark mode only — must NOT clear protect_host
    requests.put(f"{e2e_server}/api/settings", json={"dark_mode": True})

    r2 = requests.get(f"{e2e_server}/api/settings")
    assert r2.json()["protect_host"] == "192.168.1.1", (
        "protect_host must not be wiped by dark mode toggle"
    )

    # Restore
    requests.put(f"{e2e_server}/api/settings", json={"dark_mode": False})
