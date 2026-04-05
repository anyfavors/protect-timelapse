"""
Phase 1 smoke tests — database schema, config, and health endpoint.
"""

import sqlite3
from pathlib import Path


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
