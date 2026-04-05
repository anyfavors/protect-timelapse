"""
Single SQLite connection factory. This is the ONLY file that calls sqlite3.connect().
All route handlers and workers must import get_connection() from here.
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

from app.config import get_settings

# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

_PRAGMAS = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
"""


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """Open a SQLite connection, apply WAL pragmas, yield, then close."""
    settings = get_settings()
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_PRAGMAS)
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_V0 = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    webhook_url TEXT,
    disk_warning_threshold_gb INTEGER DEFAULT 5,
    timestamp_burn_in BOOLEAN DEFAULT 0,
    default_framerate INTEGER DEFAULT 30,
    render_poll_interval_seconds INTEGER DEFAULT 5
);

INSERT OR IGNORE INTO settings (
    id, disk_warning_threshold_gb, timestamp_burn_in,
    default_framerate, render_poll_interval_seconds
) VALUES (1, 5, 0, 30, 5);

CREATE TABLE IF NOT EXISTS project_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    interval_seconds INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    max_frames INTEGER,
    capture_mode TEXT DEFAULT 'continuous',
    use_luminance_check BOOLEAN DEFAULT 0,
    luminance_threshold INTEGER DEFAULT 15,
    schedule_start_time TEXT,
    schedule_end_time TEXT,
    schedule_days TEXT,
    auto_render_daily BOOLEAN DEFAULT 0,
    auto_render_weekly BOOLEAN DEFAULT 0,
    auto_render_monthly BOOLEAN DEFAULT 0,
    retention_days INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    project_type TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    max_frames INTEGER,
    start_date TIMESTAMP,
    end_date TIMESTAMP,
    capture_mode TEXT DEFAULT 'continuous',
    use_luminance_check BOOLEAN DEFAULT 0,
    luminance_threshold INTEGER DEFAULT 15,
    schedule_start_time TEXT,
    schedule_end_time TEXT,
    schedule_days TEXT,
    auto_render_daily BOOLEAN DEFAULT 0,
    auto_render_weekly BOOLEAN DEFAULT 0,
    auto_render_monthly BOOLEAN DEFAULT 0,
    retention_days INTEGER DEFAULT 0,
    template_id INTEGER,
    status TEXT DEFAULT 'active',
    consecutive_failures INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    frame_count INTEGER DEFAULT 0,
    FOREIGN KEY(template_id) REFERENCES project_templates(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT NOT NULL,
    thumbnail_path TEXT,
    file_size INTEGER NOT NULL,
    is_dark BOOLEAN DEFAULT 0,
    bookmark_note TEXT,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_frames_project_time
    ON frames(project_id, captured_at);

CREATE INDEX IF NOT EXISTS idx_frames_render
    ON frames(project_id, is_dark, captured_at);

CREATE INDEX IF NOT EXISTS idx_frames_bookmarks
    ON frames(project_id, bookmark_note)
    WHERE bookmark_note IS NOT NULL;

CREATE TABLE IF NOT EXISTS renders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    framerate INTEGER NOT NULL,
    resolution TEXT NOT NULL,
    render_type TEXT DEFAULT 'manual',
    status TEXT DEFAULT 'pending',
    progress_pct INTEGER DEFAULT 0,
    error_msg TEXT,
    range_start TIMESTAMP,
    range_end TIMESTAMP,
    label TEXT,
    output_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    file_size INTEGER,
    estimated_duration_seconds INTEGER,
    estimated_file_size_bytes INTEGER,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_renders_status ON renders(status);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    level TEXT NOT NULL,
    project_id INTEGER,
    message TEXT NOT NULL,
    is_read BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notifications_unread
    ON notifications(is_read, created_at);
"""


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


_SCHEMA_V1 = """
ALTER TABLE settings ADD COLUMN protect_host TEXT;
ALTER TABLE settings ADD COLUMN protect_port INTEGER;
ALTER TABLE settings ADD COLUMN protect_verify_ssl INTEGER;
ALTER TABLE settings ADD COLUMN latitude REAL;
ALTER TABLE settings ADD COLUMN longitude REAL;
ALTER TABLE settings ADD COLUMN tz TEXT;
ALTER TABLE settings ADD COLUMN dark_mode INTEGER DEFAULT 1;
"""


def _migrate_v0(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_V0)


def _migrate_v1(conn: sqlite3.Connection) -> None:
    # ALTER TABLE does not support multi-statement executescript in all SQLite
    # versions — run each statement individually, ignoring "duplicate column" errors.
    import contextlib

    for stmt in [s.strip() for s in _SCHEMA_V1.strip().split(";") if s.strip()]:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(stmt)
    conn.commit()


MIGRATIONS: dict[int, object] = {
    0: _migrate_v0,
    1: _migrate_v1,
}


def init_database() -> None:
    """
    Apply pending schema migrations and recover zombie renders.
    Called once during FastAPI lifespan startup.
    """
    import logging
    import os

    log = logging.getLogger("app.database")

    with get_connection() as conn:
        current: int = conn.execute("PRAGMA user_version").fetchone()[0]
        log.info("Database schema version: %d", current)

        for version in sorted(MIGRATIONS):
            if version >= current:
                log.info("Applying migration %d", version)
                MIGRATIONS[version](conn)  # type: ignore[operator]
                conn.commit()

        new_version = max(MIGRATIONS) + 1
        conn.execute(f"PRAGMA user_version = {new_version}")
        conn.commit()

        # Zombie render recovery: any render stuck in 'rendering' from a
        # previous crash is reset to 'pending' so the worker picks it up.
        zombies = conn.execute(
            "SELECT id, output_path FROM renders WHERE status = 'rendering'"
        ).fetchall()

        for row in zombies:
            log.warning("Recovering zombie render id=%d", row["id"])
            if row["output_path"]:
                with __import__("contextlib").suppress(FileNotFoundError):
                    os.remove(row["output_path"])
            conn.execute(
                "UPDATE renders SET status = 'pending', progress_pct = 0 WHERE id = ?",
                (row["id"],),
            )

        if zombies:
            conn.commit()
            log.info("Recovered %d zombie render(s)", len(zombies))


# ---------------------------------------------------------------------------
# Runtime overrides
# ---------------------------------------------------------------------------


def get_db_overrides() -> dict:
    """
    Return the non-NULL override columns from the settings row.
    Used by protect.py and capture.py to let DB values supersede env vars.
    """
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if not row:
            return {}
        return {k: v for k, v in dict(row).items() if v is not None}
    except Exception:
        return {}
