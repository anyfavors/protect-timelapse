"""
Single SQLite connection factory. This is the ONLY file that calls sqlite3.connect().
All route handlers and workers must import get_connection() from here.
"""

import contextlib
import os
import queue
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

from app.config import get_settings

# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

_POOL_SIZE = 4
# Queue with maxsize enforces the cap without a racy qsize() check.
_pool: queue.Queue[sqlite3.Connection] = queue.Queue(maxsize=_POOL_SIZE)
_pool_db_path: str | None = None


def _make_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Use individual execute() calls — executescript() issues an implicit COMMIT
    # before running, which would silently commit any open transaction (#42).
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -65536")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def _get_pool_connection() -> sqlite3.Connection:
    """Return a pooled connection, creating one if the pool is empty."""
    global _pool_db_path
    settings = get_settings()
    db_path = settings.database_path

    # If the DB path changed (e.g. test isolation), drain and rebuild.
    if _pool_db_path != db_path:
        _pool_db_path = db_path
        while not _pool.empty():
            with contextlib.suppress(Exception):
                _pool.get_nowait().close()

    try:
        conn = _pool.get_nowait()
        # Health check: discard and replace broken connections (#37)
        try:
            conn.execute("SELECT 1")
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            return _make_connection(db_path)
        return conn
    except queue.Empty:
        return _make_connection(db_path)


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """Check out a pooled SQLite connection; return it when done."""
    conn = _get_pool_connection()
    try:
        yield conn
    finally:
        # put_nowait raises Full when pool is at maxsize — close excess connections.
        try:
            _pool.put_nowait(conn)
        except queue.Full:
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

_SCHEMA_V2 = """
ALTER TABLE renders ADD COLUMN quality TEXT DEFAULT 'standard';
ALTER TABLE renders ADD COLUMN flicker_reduction TEXT DEFAULT 'standard';
ALTER TABLE renders ADD COLUMN frame_blend INTEGER DEFAULT 0;
ALTER TABLE renders ADD COLUMN stabilize INTEGER DEFAULT 0;
ALTER TABLE renders ADD COLUMN color_grade TEXT DEFAULT 'none';
"""

_SCHEMA_V3 = """
ALTER TABLE frames ADD COLUMN sharpness_score REAL;
ALTER TABLE frames ADD COLUMN is_blurry INTEGER DEFAULT 0;
"""

_SCHEMA_V4 = """
ALTER TABLE projects ADD COLUMN use_motion_filter INTEGER DEFAULT 0;
ALTER TABLE projects ADD COLUMN motion_threshold INTEGER DEFAULT 5;
ALTER TABLE settings ADD COLUMN watermark_path TEXT;
"""

_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS frame_stats (
    project_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    hour INTEGER NOT NULL,
    captured INTEGER NOT NULL DEFAULT 0,
    dark INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, date, hour),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_frame_stats_project
    ON frame_stats(project_id, date);
"""


def _migrate_v0(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_V0)


def _migrate_alter(conn: sqlite3.Connection, schema: str) -> None:
    """Apply ALTER TABLE statements, ignoring duplicate-column errors only."""
    for stmt in [s.strip() for s in schema.strip().split(";") if s.strip()]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    conn.commit()


def _migrate_v1(conn: sqlite3.Connection) -> None:
    _migrate_alter(conn, _SCHEMA_V1)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    _migrate_alter(conn, _SCHEMA_V2)


def _migrate_v3(conn: sqlite3.Connection) -> None:
    _migrate_alter(conn, _SCHEMA_V3)


def _migrate_v4(conn: sqlite3.Connection) -> None:
    _migrate_alter(conn, _SCHEMA_V4)


_SCHEMA_V6 = """
ALTER TABLE projects ADD COLUMN solar_noon_window_minutes INTEGER DEFAULT 30;
"""

_SCHEMA_V7 = """
ALTER TABLE renders ADD COLUMN priority INTEGER DEFAULT 5;
"""


def _migrate_v5(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_V5)
    conn.commit()


def _migrate_v6(conn: sqlite3.Connection) -> None:
    _migrate_alter(conn, _SCHEMA_V6)


def _migrate_v7(conn: sqlite3.Connection) -> None:
    _migrate_alter(conn, _SCHEMA_V7)


_SCHEMA_V8 = """
ALTER TABLE renders ADD COLUMN started_at TIMESTAMP;
"""


def _migrate_v8(conn: sqlite3.Connection) -> None:
    _migrate_alter(conn, _SCHEMA_V8)


_SCHEMA_V9_ALTER = """
ALTER TABLE projects ADD COLUMN is_pinned INTEGER DEFAULT 0;
ALTER TABLE settings ADD COLUMN maintenance_hour INTEGER DEFAULT 2;
ALTER TABLE settings ADD COLUMN maintenance_minute INTEGER DEFAULT 0;
ALTER TABLE settings ADD COLUMN nvr_reconnect_backoff_seconds INTEGER DEFAULT 30;
ALTER TABLE settings ADD COLUMN muted_project_ids TEXT DEFAULT '[]';
ALTER TABLE frames ADD COLUMN file_hash TEXT;
"""

_SCHEMA_V9_CREATE = """
CREATE TABLE IF NOT EXISTS render_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    framerate INTEGER NOT NULL DEFAULT 30,
    resolution TEXT NOT NULL DEFAULT '1920x1080',
    quality TEXT NOT NULL DEFAULT 'standard',
    flicker_reduction TEXT NOT NULL DEFAULT 'standard',
    frame_blend INTEGER NOT NULL DEFAULT 0,
    stabilize INTEGER NOT NULL DEFAULT 0,
    color_grade TEXT NOT NULL DEFAULT 'none',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_frames_hash
    ON frames(project_id, file_hash)
    WHERE file_hash IS NOT NULL;
"""


def _migrate_v9(conn: sqlite3.Connection) -> None:
    _migrate_alter(conn, _SCHEMA_V9_ALTER)
    conn.executescript(_SCHEMA_V9_CREATE)
    conn.commit()


_SCHEMA_V10 = """
CREATE INDEX IF NOT EXISTS idx_frames_project_quality
    ON frames(project_id, captured_at, is_dark);
"""


def _migrate_v10(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_V10)
    conn.commit()


_SCHEMA_V11 = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_frames_project_captured_at_unique
    ON frames(project_id, captured_at);
"""


def _migrate_v11(conn: sqlite3.Connection) -> None:
    # Remove any duplicate (project_id, captured_at) rows before adding unique index.
    # Keep the row with the lowest id (earliest insert).
    conn.execute(
        """
        DELETE FROM frames WHERE id NOT IN (
            SELECT MIN(id) FROM frames GROUP BY project_id, captured_at
        )
        """
    )
    conn.executescript(_SCHEMA_V11)
    conn.commit()


_SCHEMA_V12 = """
ALTER TABLE project_templates ADD COLUMN solar_noon_window_minutes INTEGER DEFAULT 30;
"""


def _migrate_v12(conn: sqlite3.Connection) -> None:
    _migrate_alter(conn, _SCHEMA_V12)


_SCHEMA_V13 = """
CREATE INDEX IF NOT EXISTS idx_renders_pending_priority
    ON renders(status, priority DESC, created_at ASC)
    WHERE status = 'pending';
"""


def _migrate_v13(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_V13)
    conn.commit()


# Migration 14: enforce project_type and capture_mode via triggers.
# SQLite does not support ALTER TABLE ADD CONSTRAINT on existing tables, so
# we use BEFORE INSERT/UPDATE triggers to raise an error on invalid values (#49, #50).
_SCHEMA_V14 = """
CREATE TRIGGER IF NOT EXISTS trg_projects_project_type_insert
BEFORE INSERT ON projects
WHEN NEW.project_type NOT IN ('live', 'historical')
BEGIN
    SELECT RAISE(ABORT, 'Invalid project_type: must be live or historical');
END;

CREATE TRIGGER IF NOT EXISTS trg_projects_project_type_update
BEFORE UPDATE OF project_type ON projects
WHEN NEW.project_type NOT IN ('live', 'historical')
BEGIN
    SELECT RAISE(ABORT, 'Invalid project_type: must be live or historical');
END;

CREATE TRIGGER IF NOT EXISTS trg_projects_capture_mode_insert
BEFORE INSERT ON projects
WHEN NEW.capture_mode NOT IN ('continuous', 'daylight_only', 'schedule', 'solar_noon')
BEGIN
    SELECT RAISE(ABORT, 'Invalid capture_mode: must be continuous, daylight_only, schedule, or solar_noon');
END;

CREATE TRIGGER IF NOT EXISTS trg_projects_capture_mode_update
BEFORE UPDATE OF capture_mode ON projects
WHEN NEW.capture_mode NOT IN ('continuous', 'daylight_only', 'schedule', 'solar_noon')
BEGIN
    SELECT RAISE(ABORT, 'Invalid capture_mode: must be continuous, daylight_only, schedule, or solar_noon');
END;
"""


def _migrate_v14(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_V14)
    conn.commit()


MIGRATIONS: dict[int, object] = {
    0: _migrate_v0,
    1: _migrate_v1,
    2: _migrate_v2,
    3: _migrate_v3,
    4: _migrate_v4,
    5: _migrate_v5,
    6: _migrate_v6,
    7: _migrate_v7,
    8: _migrate_v8,
    9: _migrate_v9,
    10: _migrate_v10,
    11: _migrate_v11,
    12: _migrate_v12,
    13: _migrate_v13,
    14: _migrate_v14,
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
        conn.execute(
            f"PRAGMA user_version = {int(new_version)}"
        )  # int() guards against non-integer (#28)
        conn.commit()

        # Zombie render recovery: any render stuck in 'rendering' or 'stalled' from a
        # previous crash is reset to 'pending' so the worker picks it up.
        zombies = conn.execute(
            "SELECT id, output_path FROM renders WHERE status IN ('rendering', 'stalled')"
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


# Reusable SQL fragment for "quality" frames — exclude dark and blurry frames.
# Used in render frame selection and GIF export queries.
VALID_FRAME_SQL = "is_dark = 0 AND (is_blurry IS NULL OR is_blurry = 0)"


def row_to_dict(row: "sqlite3.Row") -> dict:
    """Convert a sqlite3.Row to a plain dict. Shared utility for all route modules."""
    return dict(row)


def get_wal_size_bytes() -> int:
    """Return the WAL file size in bytes (0 if WAL doesn't exist)."""
    from app.config import get_settings

    wal_path = get_settings().database_path + "-wal"
    try:
        return os.path.getsize(wal_path)
    except FileNotFoundError:
        return 0
