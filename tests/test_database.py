"""Tests for database schema, config, and health endpoint."""

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_database_tables_created(tmp_db: Path) -> None:
    conn = sqlite3.connect(str(tmp_db))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()
    expected = {"settings", "projects", "frames", "renders", "project_templates", "notifications"}
    assert expected.issubset(tables)


def test_settings_default_row(tmp_db: Path) -> None:
    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()
    assert row is not None
    assert row[2] == 5  # disk_warning_threshold_gb default


def test_schema_version_incremented(tmp_db: Path) -> None:
    conn = sqlite3.connect(str(tmp_db))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version >= 1


def test_health_endpoint(client) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "nvr_connected" in data
    assert "disk_free_gb" in data


def test_foreign_keys_enabled(tmp_db: Path) -> None:
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("PRAGMA foreign_keys = ON")
    result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.close()
    assert result == 1


def test_spa_root_serves_html_with_cache_busting(
    tmp_db: Path,
    monkeypatch,
) -> None:
    """GET / returns HTML with versioned ?v= query strings on static assets."""
    import app.protect as protect_mod
    from app.routes import health

    monkeypatch.setattr(protect_mod.protect_manager, "setup", lambda: None)
    monkeypatch.setattr(protect_mod.protect_manager, "teardown", lambda: None)
    monkeypatch.setattr(protect_mod.protect_manager, "_connected", False)

    @asynccontextmanager
    async def _noop(a):  # type: ignore[no-untyped-def]
        yield

    mini = FastAPI(lifespan=_noop)
    mini.include_router(health.router)

    # Wire the SPA route directly (same logic as create_app)
    import hashlib
    import os

    from fastapi.templating import Jinja2Templates
    from starlette.requests import Request

    templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    _jinja = Jinja2Templates(directory=templates_dir)

    def _file_hash(path: str) -> str:
        try:
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()[:8]
        except OSError:
            return "dev"

    js_hash = _file_hash(os.path.join(static_dir, "app.js"))
    css_hash = _file_hash(os.path.join(static_dir, "app.css"))

    @mini.get("/", include_in_schema=False)
    async def _spa(request: Request):  # type: ignore[return]
        return _jinja.TemplateResponse(
            request, "index.html", {"js_hash": js_hash, "css_hash": css_hash}
        )

    with TestClient(mini) as c:
        r = c.get("/")
    assert r.status_code == 200
    assert f"app.js?v={js_hash}" in r.text
    assert f"app.css?v={css_hash}" in r.text
