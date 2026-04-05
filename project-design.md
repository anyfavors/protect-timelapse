# Protect Timelapse — Complete Implementation Blueprint

> **Purpose of this document:** This is the single, authoritative specification for building the Protect Timelapse application from an empty repository to a deployed v1.0. It is written to be consumed sequentially by a developer (human or LLM) and contains every decision, schema, command, and constraint needed to implement the system without ambiguity.
>
> **Language convention:** Technical specifications, code, and architecture are written in English. Inline commentary and UX descriptions use Danish where it aids clarity for the target developer.

---

# 1. Project Overview & Executive Summary

## 1.1 What Are We Building?

**Protect Timelapse** is a self-hosted, highly autonomous background service and single-page web application (SPA). It connects directly to a UniFi Protect NVR to orchestrate the long-term capture, filtering, and assembly of timelapse videos.

The system bypasses heavy video streaming by fetching lightweight JPEG snapshots (or extracting frames from historical video chunks) at precise intervals. It stores these frames locally, evaluates them for usability, and uses background task queues to compile them into MP4s via `ffmpeg`.

## 1.2 The Technology Stack

The application is built on a modern, lightweight Python/JS stack designed to run as a single Docker container:

- **Backend & API:** `FastAPI` (Python 3.12, async) handling REST routes and application lifespan.
- **Database:** `SQLite3` (Raw SQL, no ORM) utilizing a strict single-connection factory with WAL mode.
- **NVR Integration:** `uiprotect` (community Python API client) for authenticating and pulling frames.
- **Task Scheduling:** `APScheduler` for precision loop timing and cron-like maintenance jobs.
- **Video Engine:** `ffmpeg` (via `asyncio.create_subprocess_exec`) for rendering and concatenating video.
- **Frontend UI:** `Alpine.js 3` and `Tailwind CSS v4`, served natively by FastAPI as a single HTML shell without requiring a separate Node.js runtime.

## 1.3 High-Level Architecture

To achieve 24/7 autonomy without blocking the web server, the application is split into a REST API and three distinct asynchronous background workers:

1. **The Capture Worker (Data Ingestion):** Wakes up at user-defined intervals. It fetches JPEGs from the active UniFi cameras. Before saving to disk, it runs an astronomical check (`astral`) to ensure the sun is up, and a programmatic image check (`Pillow`) to ensure the frame isn't pitch black or obscured.
2. **The Render Worker (Video Assembly):** A strictly queued background loop. It takes thousands of raw JPEGs and runs them through `ffmpeg` (applying deflicker filters and timestamp burn-ins) to produce MP4s. It also performs "rollups" — stitching together daily MP4s to create weekly and monthly videos without re-encoding the raw images.
3. **The Maintenance Worker (System Health):** A daily cron job that enforces data retention policies (hard-deleting old JPEGs), prunes the rolling window of generated videos, and monitors disk space to prevent host storage exhaustion.

## 1.4 The User Experience

The user interacts with the system through a responsive, dark-mode-enabled UI with real-time WebSocket updates. From the dashboard, they can:

- Browse available NVR cameras with live visual previews, organized by camera with quick-action controls.
- Create distinct "Projects" from scratch or from saved templates (e.g., a "Live" 10-second interval weather timelapse, or a "Historical" extraction from last week's video buffer).
- Define capture schedules (specific time windows and weekdays) for construction site or office scenarios.
- View progress via a GitHub-style capture heatmap, hourly timeline, and a low-res frame scrubber with bookmarking.
- Select frame ranges for targeted renders, compare render settings side-by-side, and control playback speed.
- Monitor system health via an in-app notification center and receive alerts through external webhooks.

## 1.5 Deployment Constraints & Safeguards

- **Environment:** Deployed as a single, self-contained Docker container on a K3s cluster (GMKtec node).
- **Storage:** Relies completely on a persistent Docker volume (PVC) mounted to `/data` for the SQLite DB, frames, and renders. Container-local storage is treated as strictly ephemeral.
- **Compute Safeguards:** Because video rendering is computationally aggressive, the application strictly caps `ffmpeg` subprocesses (`-threads 4`) and limits the Render Worker to exactly one concurrent job, protecting the host machine from CPU starvation.
- **Timezones:** To survive multi-month projects crossing Daylight Saving Time boundaries, the database and backend operate strictly in UTC. Local time (`TZ`) is only applied at the frontend layer and for geolocation math (`astral`).

---

# 2. Features & Capabilities (Technical Specification)

## 2.1 Dual Data Ingestion Modes

### A. Live Tracking (Forward-Looking)

- **Trigger:** `APScheduler` registers an `IntervalTrigger` based on `projects.interval_seconds`.
- **Execution:** Calls `ProtectApiClient.cameras[id].get_snapshot(width, height)`.
- **Failure Handling:** If the NVR times out (`httpx.ReadTimeout` or `NvrError`), the worker suppresses the exception, logs a warning, increments `projects.consecutive_failures`, and skips the interval to prevent worker thread crashing. See Section 7.2 for escalation logic.
- **Storage:** Raw bytes are written to `/data/frames/{project_id}/{timestamp_utc}.jpg`.

### B. Historical Extraction (Backward-Looking)

- **Trigger:** One-off async task upon project creation when `project_type == 'historical'`.
- **Execution:**
  1. Validates `start_date` and `end_date` against NVR retention limits.
  2. Because the NVR cannot stream massive files in one go, the backend chunks the request into 1-hour segments.
  3. Downloads `.mp4` chunks via `camera.get_video(start, end)` to a `tmp/` directory.
  4. Runs `ffmpeg -threads 4 -i chunk.mp4 -vf fps=1/{interval_seconds} -q:v 2 /data/frames/{project_id}/%014d.jpg`.
  5. **Timestamp Reconstruction:** After extraction, iterates the sequentially numbered files and renames them to UTC timestamps calculated from `chunk_start_time + (frame_index * interval_seconds)`. This ensures the frames table has correct `captured_at` values for heatmap rendering and render ordering.
  6. Cleans up the `tmp/` directory immediately after extraction to protect disk space.
- **Completion:** When all chunks are processed, the project `status` transitions to `'completed'` (historical projects do not remain active — they capture a fixed window).

## 2.2 Visibility Filtering Pipeline

Executed sequentially *before* saving a Live Tracking frame to disk.

### A. Astronomical Filtering (`astral`)

- **Inputs:** `LATITUDE`, `LONGITUDE`, `TZ` (from `app/config.py`), and current UTC time.
- **Logic:** Calculates `sun.sunrise` and `sun.sunset` for the current date.
- **Action:** If `projects.capture_mode == 'daylight_only'` and current time is outside the sun window, the pipeline exits early. No NVR request is made.

### B. Luminance Filtering (`Pillow`)

- **Inputs:** JPEG bytes from the NVR, `projects.luminance_threshold` (0-255 scale).
- **Logic:** Loads image bytes into memory (`Image.open(io.BytesIO)`), converts to grayscale (`.convert('L')`), and calculates the mean pixel value using `ImageStat.Stat().mean[0]`.
- **Action:** If the mean value < `luminance_threshold`, the frame is saved but flagged in SQLite as `is_dark = True`. The render worker will filter out these records during video assembly.

## 2.3 Video Assembly & Concatenation Rollups

The `render_worker` processes jobs sequentially to respect the `-threads 4` cap.

- **Raw Rendering (Standard):** Queries the `frames` table `WHERE project_id = X AND is_dark = 0 ORDER BY captured_at ASC`. Passes the ordered file paths to `ffmpeg` via a concat demuxer `.txt` list.
- **Concatenation Rollups:** For auto-generated weekly/monthly renders.
  - *Constraint:* Requires existing daily renders of the exact same resolution and encoding profile.
  - *Execution:* Generates a `concat.txt` file listing the daily MP4s. Runs `ffmpeg -f concat -safe 0 -i concat.txt -c copy output.mp4`.
- **Dynamic Frame Skipping:** Applied during standard renders for long-term projects to prevent hyper-speed playback.
  - *Math:* `skip_factor = math.ceil(total_frames / target_frames)`.
  - *Execution:* Uses `ffmpeg`'s `select` filter (e.g., `select='not(mod(n\,5))',setpts=N/FRAME_RATE/TB`) to encode only every Nth frame and recalculate presentation timestamps.
- **Timestamp Burn-in:** Uses `ffmpeg` `drawtext` filter. Timestamp is calculated from the filename (UTC), converted to local `TZ` for the visual overlay. Applied *after* the deflicker filter to prevent text luminance fluctuation.

## 2.4 UI Visualizations & Data Feeds

- **Live Framing Preview:** Triggered by changing the camera dropdown. Hits `GET /api/cameras/{id}/preview`, which proxies a single low-res `get_snapshot()` call, bypassing the database entirely.
- **Capture Heatmap:** Hits `GET /api/projects/{id}/stats/daily`. Backend runs a `GROUP BY DATE(captured_at)` SQLite query. Frontend plots this array onto a 7x52 CSS grid.
- **Frame Scrubber:** Hits `GET /api/projects/{id}/frames/{frame_id}/thumbnail`. The scrubber first loads a lightweight index via `GET /api/projects/{id}/frames?fields=id,captured_at&limit=500`, then fetches individual thumbnails on slider interaction. This avoids transferring full image lists.

## 2.5 System Failsafes & State Management

- **Storage Protection:** During the FastAPI lifespan `startup`, and subsequently every 5 minutes in the capture loop, `shutil.disk_usage('/data')` is checked.
  - *Threshold Breach:* If free space < `disk_warning_threshold_gb`, all active projects are transitioned to `status = 'paused_error'`. A webhook is fired.
- **Runaway Protection:** `projects.frame_count` is incremented on every save. If `frame_count >= projects.max_frames`, the project transitions to `status = 'completed'` and the scheduler job is removed.

## 2.6 Thumbnail Cache Layer

To ensure responsive UI interactions (frame scrubber, heatmap hover previews, dark frame gallery), the system maintains a parallel directory of low-resolution thumbnails.

- **Generation:** At capture time, after the full-res JPEG is saved, a 320px-wide thumbnail (preserving aspect ratio) is generated via Pillow and saved to `/data/thumbs/{project_id}/{timestamp}.jpg` with quality=60.
- **Storage:** Thumbnails are stored in a separate directory tree to allow independent cleanup and to prevent the scrubber from loading multi-MB full-res images.
- **Historical Extraction:** Thumbnails are also generated during historical frame extraction as a post-processing step.
- **Cleanup:** Thumbnail files are deleted alongside their parent frames during retention pruning (maintenance worker).

## 2.7 Project Templates & Presets

Users can save a project's configuration as a reusable template and apply it when creating new projects. This eliminates repetitive form-filling for common scenarios.

- **Save as Template:** From the project detail view or create view, a "Save as Template" button writes the current configuration (interval, capture mode, schedule, luminance settings, auto-render flags, retention) to the `project_templates` table.
- **Apply Template:** The create project form includes a template dropdown. Selecting a template pre-fills all fields, which the user can then override before saving.
- **Management:** Templates can be renamed and deleted from the Settings view.

## 2.8 Camera-Level Grouping

Since a single camera can have multiple active projects (e.g., a 5-second weather timelapse and a 10-minute long-term construction timelapse), the dashboard organizes projects hierarchically by camera.

- **Camera Sections:** The dashboard groups project cards under their parent camera, showing the camera name, model, and online status as a section header.
- **Expandable:** Each camera section is collapsible. Cameras with errors or offline status are expanded by default.
- **Camera Count Badge:** Each camera header shows the count of active/paused/error projects.

## 2.9 Frame Annotation & Bookmarking

Users can annotate individual frames with text notes to mark significant moments in a timelapse (e.g., "Kran ankommet", "Snestorm", "Byggeri nået 2. sal").

- **Bookmark Action:** Clicking a frame in the scrubber or frame list reveals a "Bookmark" button. Entering a note writes to `frames.bookmark_note`.
- **Bookmark Gallery:** The project detail view includes a "Bookmarks" tab that shows only bookmarked frames with their notes, sorted chronologically.
- **Render from Bookmarks:** A future enhancement (v2) could allow rendering only the segments around bookmarked frames into a "highlights" video.

## 2.10 Comparative Side-by-Side View

Users can select two timestamps from the same project and view them side-by-side in a before/after comparison. This is particularly useful for construction site progress and seasonal changes.

- **Selection:** From the project detail view, the user picks two frames (either by clicking the heatmap, using the scrubber, or selecting bookmarks).
- **Display:** The two frames are shown in a split-view with a draggable center divider (CSS-based, no external library). The timestamps are displayed below each frame.
- **No backend change required:** This is purely a frontend feature using existing frame thumbnail endpoints.

## 2.11 Scheduled Capture Windows

In addition to `continuous` and `daylight_only`, the `schedule` capture mode allows users to define specific time windows and weekdays for capture.

- **Configuration:** The project creation form shows time pickers for start/end time and weekday checkboxes when `schedule` mode is selected.
- **Storage:** `schedule_start_time` (e.g., `'07:00'`), `schedule_end_time` (e.g., `'17:00'`), and `schedule_days` (e.g., `'1,2,3,4,5'` for Mon-Fri) are stored on the `projects` row.
- **Execution:** The capture worker converts the current UTC time to local time and checks if the current weekday and time fall within the schedule window. If not, the capture is skipped without making an NVR request.
- **Combination with Luminance:** Schedule mode can be combined with `use_luminance_check` for belt-and-suspenders filtering.

## 2.12 WebSocket Real-Time Updates

To eliminate polling latency and reduce unnecessary HTTP requests, the application provides a WebSocket endpoint for real-time event streaming to the frontend.

- **Endpoint:** `WS /api/ws`
- **Events pushed to clients:**
  - `capture_event`: Fired after each frame save, includes `project_id`, `frame_count`, `is_dark`.
  - `render_progress`: Fired every second during a render, includes `render_id`, `progress_pct`.
  - `render_complete`: Fired when a render finishes (success or error).
  - `notification`: Fired when any webhook event is generated (disk warning, NVR offline, etc.).
  - `disk_update`: Fired after each disk space check with current free/total values.
- **Implementation:** FastAPI's native WebSocket support via `@app.websocket("/api/ws")`. The lifespan manager maintains a set of connected clients. Background workers publish events to this set via an `asyncio.Queue` or by calling a shared `broadcast()` function.
- **Fallback:** The frontend gracefully degrades to polling if the WebSocket connection fails or is not available (e.g., proxy misconfiguration). Polling intervals remain as defined in Section 9.4.

---

# 3. Architecture & Tech Stack

## 3.1 High-Level System Architecture

The application runs as a single, self-contained Uvicorn process. FastAPI acts as the central router, using its `lifespan` context manager to spawn and manage asynchronous background workers that interact with the external NVR and the local persistent volume.

```
[ UniFi Protect NVR ]
        ▲  │ (uiprotect API / WSS)
        │  ▼
┌───────┴────────────────────────────────────────────────────────┐
│ FastAPI Application (Uvicorn / Docker Container)               │
│                                                                │
│  ┌─────────────────┐       ┌────────────────────────────────┐  │
│  │ REST API Routes  │ ◄───► │ FastAPI Lifespan Manager       │  │
│  └──────┬──────────┘       └─┬─────────┬─────────┬──────────┘  │
│         │                    │         │         │              │
│  ┌──────┴──────────┐       ┌─▼─┐     ┌─▼─┐     ┌─▼─┐          │
│  │ Alpine.js UI    │       │ 1 │     │ 2 │     │ 3 │          │
│  └─────────────────┘       └─┬─┘     └─┬─┘     └─┬─┘          │
└─────────┬────────────────────┼─────────┼─────────┼─────────────┘
          │                    │         │         │
          │ 1. Capture Worker (APScheduler)        │
          │ 2. Render Worker (Strict Queue)        │
          │ 3. Maintenance Worker (Cron)           │
          ▼                    ▼         ▼         ▼
  [ /data (Kubernetes Persistent Volume Claim) ]
  ├── timelapse.db (SQLite3 via singleton factory)
  ├── /frames/{project_id}/{timestamp_utc}.jpg
  ├── /thumbs/{project_id}/{timestamp_utc}.jpg
  └── /renders/{project_id}/{render_id}.mp4
```

## 3.2 Core Technology Stack (Non-Negotiable)

| Component | Choice & Version | Technical Justification |
| :--- | :--- | :--- |
| **Web Framework** | FastAPI (0.115+) | Asynchronous routing necessary for non-blocking NVR calls. Lifespan context handles worker thread mounting natively. |
| **NVR Protocol** | `uiprotect` (7.x+) | Provides authenticated, reverse-engineered access to the UniFi OS API. Bypasses the need to enable RTSP streams. Check PyPI for latest compatible version at implementation time. |
| **Database** | SQLite3 (`sqlite3`) | Runs in WAL mode. No ORM is used to minimize overhead and allow complex raw `DATE()` queries for heatmaps and rollups. |
| **Task Scheduling** | `APScheduler` (3.10+) | Uses `AsyncIOScheduler`. Prevents the time-drift inherent in `while True: await asyncio.sleep(X)` loops over multi-month uptimes. |
| **Image Analysis** | `Pillow` (10.2+) | Operates entirely in memory (`io.BytesIO`). Evaluates JPEG luminance before touching the disk. |
| **Video Engine** | `ffmpeg` (CLI) | Handled via `asyncio.create_subprocess_exec`. Required for deflicker, timestamp burn-in (`drawtext`), and high-speed MP4 concatenation (`-f concat`). |
| **Frontend UI** | Alpine.js 3 | Reactive state management without a Virtual DOM or build step. Loaded via `<script defer>` in the HTML `head`. |
| **CSS Framework** | Tailwind CSS v4 | Compiled during the Docker multi-stage build via `@tailwindcss/cli`. Zero runtime CSS processing. |

## 3.3 Strict Application Directory Layout

```
app/
├── __init__.py        # FastAPI instance factory. Defines the `lifespan` context.
├── config.py          # STRICT RULE: The ONLY file allowed to import `os` and read env vars.
├── database.py        # STRICT RULE: Contains `get_connection()`. Routes MUST NOT open connections directly.
├── protect.py         # Defines `ProtectClientManager` (Singleton with `asyncio.Lock()`).
├── capture.py         # Contains `APScheduler` instance and `snapshot_worker()` logic.
├── render.py          # Contains the strict sequential queue loop and `ffmpeg` subprocess calls.
├── maintenance.py     # Contains daily cron tasks for SQLite cleanup and file deletion (`os.remove`).
├── notifications.py   # Handles async `httpx` POST requests to webhook AND writes to notifications table.
├── websocket.py       # WebSocket connection manager and broadcast function.
├── thumbnails.py      # Pillow-based thumbnail generation (320px wide, quality=60).
└── routes/            # REST API controllers.
    ├── health.py      # Checks `shutil.disk_usage()` and NVR connectivity.
    ├── projects.py    # Standard CRUD. Mutates `APScheduler` jobs on update/delete.
    ├── frames.py      # Pagination queries, thumbnail/full serving, bookmarks, dark frame gallery, and `.zip` exports.
    ├── renders.py     # Inserts pending renders with estimates; returns render status and progress.
    ├── settings.py    # Reads/writes the single row (id=1) in the settings table.
    ├── cameras.py     # Proxies `uiprotect` bootstrap dict and live previews.
    ├── templates.py   # CRUD for project templates and template-to-project application.
    └── notifications.py  # In-app notification listing and read-marking.

static/
├── app.js             # Alpine.js logic: `document.addEventListener('alpine:init', ...)`
└── app.css            # Generated by Tailwind CLI in Docker. STRICT RULE: Do not edit manually.

templates/
└── index.html         # Single HTML shell using Jinja2 strictly for initial path routing, relying on Alpine for state.

tests/
├── conftest.py        # Yields `tmp_db` fixture (in-memory or tempfile SQLite) and Uvicorn `TestClient`.
└── test_*.py          # Uses `pytest-asyncio`. Uses `monkeypatch.setattr` for mocking (never `unittest.mock.patch`).
```

## 3.4 Application Lifespan Sequence (Startup & Shutdown)

To ensure the system never starts in an unstable state or corrupts data on exit, FastAPI utilizes a strict lifecycle sequence defined in `app/__init__.py`.

**Startup Sequence:**

1. **Config Load:** `config.py` reads all environment variables via Pydantic `BaseSettings`.
2. **Storage Validation:** Checks if `/data/frames`, `/data/thumbs`, and `/data/renders` exist (create if missing via `os.makedirs(exist_ok=True)`). Runs `shutil.disk_usage()`; aborts if 0 bytes remain.
3. **Database Init:** Calls `database.init_database()` to execute `CREATE TABLE IF NOT EXISTS` statements and apply SQLite WAL mode pragmas. Also runs zombie render recovery (see Section 7.4).
4. **NVR Authentication:** `protect.py` attempts a connection to the UniFi NVR. If it fails, logs a critical warning but *does not crash* (allows user to fix IP/creds in the settings UI later).
5. **Scheduler Boot:** Starts the `AsyncIOScheduler`. Queries SQLite for all projects where `status == 'active'` and dynamically registers their interval capture jobs (respecting `capture_mode` — schedule-mode projects use `IntervalTrigger` like continuous projects but the worker itself checks the schedule window).
6. **Worker Mount:** Starts the `render_worker` background task to listen for pending DB records, and the `maintenance_worker` cron task.
7. **WebSocket Manager Init:** Initializes the `ConnectionManager` in `websocket.py` with an empty client set and an `asyncio.Queue` for event broadcasting. Registers the `/api/ws` endpoint.

**Shutdown Sequence (SIGINT/SIGTERM):**

1. **Pause Scheduler:** Immediately stops `APScheduler` from triggering new snapshots.
2. **Graceful Worker Exit:** Signals the `render_worker` to stop picking up new jobs. A currently running `ffmpeg` process receives `SIGINT` to allow it to finalize the MP4 container. If it doesn't exit within 30 seconds, `SIGKILL` is sent.
3. **Close WebSocket Connections:** Sends a close frame to all connected WebSocket clients.
4. **Close Connections:** Closes the `uiprotect` websocket session and all active SQLite connections.

---

# 4. Database Schema & Data Modeling

## 4.1 Data Modeling Principles & Constraints

The application relies entirely on SQLite3, running in Write-Ahead Logging (WAL) mode to allow concurrent reads (e.g., UI polling) while background workers are actively writing. Because the application interacts with `ffmpeg` and local file systems, the database acts as the single source of truth linking metadata to physical files on the persistent volume.

**Strict UTC Rule:** All timestamps (`created_at`, `captured_at`, `completed_at`) are saved and evaluated strictly in UTC. The frontend converts these to the local timezone (`TZ`) for display. The only time the backend requires timezone awareness is when calculating local sunrise/sunset via `astral`.

**Connection Pragmas:** Every connection opened via `get_connection()` MUST immediately execute:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
```

## 4.2 Schema Definitions

Below are the exact table structures utilized by `database.py` during the `init_database()` lifespan event.

### 1. `settings` Table (Global Configuration)

This table strictly enforces a single row to hold global application state that can be modified via the UI without restarting the container.

```sql
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    webhook_url TEXT,
    disk_warning_threshold_gb INTEGER DEFAULT 5,
    timestamp_burn_in BOOLEAN DEFAULT 0,
    default_framerate INTEGER DEFAULT 30,
    render_poll_interval_seconds INTEGER DEFAULT 5
);

-- Ensures the default row always exists on boot if empty
INSERT OR IGNORE INTO settings (id, disk_warning_threshold_gb, timestamp_burn_in, default_framerate, render_poll_interval_seconds)
VALUES (1, 5, 0, 30, 5);
```

### 2. `projects` Table (Timelapse Configurations)

Stores the configuration, limits, and real-time status of every defined timelapse.

```sql
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    project_type TEXT NOT NULL,              -- 'live' or 'historical'
    interval_seconds INTEGER NOT NULL,

    -- Dimensions (NULL means native camera resolution)
    width INTEGER,
    height INTEGER,

    -- Caps & Safety Limits
    max_frames INTEGER,                      -- e.g., 5000 (halts project when reached)

    -- Historical Extraction Window (only used when project_type = 'historical')
    start_date TIMESTAMP,
    end_date TIMESTAMP,

    -- Daylight & Environmental Filtering
    capture_mode TEXT DEFAULT 'continuous',   -- 'continuous', 'daylight_only', or 'schedule'
    use_luminance_check BOOLEAN DEFAULT 0,
    luminance_threshold INTEGER DEFAULT 15,  -- 0-255 scale

    -- Schedule Mode Fields (only used when capture_mode = 'schedule')
    schedule_start_time TEXT,                -- Local time, e.g., '07:00'
    schedule_end_time TEXT,                  -- Local time, e.g., '17:00'
    schedule_days TEXT,                      -- Comma-separated day numbers: '1,2,3,4,5' (Mon-Fri)

    -- Background Automation
    auto_render_daily BOOLEAN DEFAULT 0,
    auto_render_weekly BOOLEAN DEFAULT 0,
    auto_render_monthly BOOLEAN DEFAULT 0,
    retention_days INTEGER DEFAULT 0,        -- 0 = keep forever, >0 = hard delete old frames

    -- Template System
    template_id INTEGER,                     -- FK to project_templates, NULL if not created from template

    -- Current State
    status TEXT DEFAULT 'active',            -- 'active', 'paused', 'paused_error', 'completed', 'error'
    consecutive_failures INTEGER DEFAULT 0,  -- NVR failure counter, reset on success
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    frame_count INTEGER DEFAULT 0,           -- Denormalized cache for fast UI loading

    FOREIGN KEY(template_id) REFERENCES project_templates(id) ON DELETE SET NULL
);

### 3. `frames` Table (Captured JPEG Metadata)

Links a physical `.jpg` file in the persistent volume to its parent project.

```sql
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT NOT NULL,                 -- Absolute path: /data/frames/{project_id}/{timestamp}.jpg
    thumbnail_path TEXT,                     -- Low-res preview: /data/thumbs/{project_id}/{timestamp}.jpg (320px wide)
    file_size INTEGER NOT NULL,              -- In bytes, used for storage calculation
    is_dark BOOLEAN DEFAULT 0,              -- 1 if it failed the Pillow luminance check
    bookmark_note TEXT,                      -- User annotation, NULL if not bookmarked

    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- Crucial index for ffmpeg render worker sorting and UI heatmaps
CREATE INDEX IF NOT EXISTS idx_frames_project_time ON frames(project_id, captured_at);
-- Composite index for render queries that filter on is_dark
CREATE INDEX IF NOT EXISTS idx_frames_render ON frames(project_id, is_dark, captured_at);
-- Sparse index for bookmarked frames (most rows are NULL)
CREATE INDEX IF NOT EXISTS idx_frames_bookmarks ON frames(project_id, bookmark_note) WHERE bookmark_note IS NOT NULL;
```

### 4. `renders` Table (Video Outputs)

Acts as the queue for the Render Worker and stores metadata for completed MP4s.

```sql
CREATE TABLE IF NOT EXISTS renders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    framerate INTEGER NOT NULL,              -- e.g., 24, 30, 60
    resolution TEXT NOT NULL,                -- e.g., "1920x1080"

    render_type TEXT DEFAULT 'manual',       -- 'manual', 'auto_daily', 'auto_weekly', 'auto_monthly', 'range'
    status TEXT DEFAULT 'pending',           -- 'pending', 'rendering', 'done', 'error'
    progress_pct INTEGER DEFAULT 0,          -- 0-100, updated by render worker during ffmpeg execution
    error_msg TEXT,                          -- Captured from ffmpeg stderr on failure

    -- Range selection (used when render_type = 'range')
    range_start TIMESTAMP,                   -- Start of selected frame window
    range_end TIMESTAMP,                     -- End of selected frame window

    -- Render metadata for comparison
    label TEXT,                              -- User-defined label, e.g., "30fps deflicker" vs "24fps raw"

    output_path TEXT,                        -- Absolute path: /data/renders/{project_id}/{id}.mp4
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    file_size INTEGER,                       -- In bytes
    estimated_duration_seconds INTEGER,      -- Pre-render estimate based on frame count and framerate
    estimated_file_size_bytes INTEGER,       -- Pre-render estimate based on avg frame size and CRF

    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

-- Index for the strict queue worker polling
CREATE INDEX IF NOT EXISTS idx_renders_status ON renders(status);
```

### 5. `project_templates` Table (Reusable Presets)

Stores saved project configurations that can be applied when creating new projects.

```sql
CREATE TABLE IF NOT EXISTS project_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,               -- e.g., "Byggeplads standard", "Vejr-cam 5s"
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
```

### 6. `notifications` Table (In-App Event Log)

Stores webhook-equivalent events for display in the UI notification center. This table is written to whenever `notifications.py` fires a webhook, creating a local copy regardless of whether the external webhook delivery succeeds.

```sql
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,                      -- e.g., 'storage_critical', 'nvr_offline', 'render_error', 'project_completed'
    level TEXT NOT NULL,                      -- 'info', 'warning', 'error'
    project_id INTEGER,                      -- NULL for system-level events
    message TEXT NOT NULL,
    is_read BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(is_read, created_at);
```

## 4.3 Data Cascade & Integrity Rules

1. **Foreign Key Cascading:** `PRAGMA foreign_keys = ON;` must be executed immediately upon establishing any SQLite connection. Deleting a project via the API must instantly cascade to delete all related SQLite rows in `frames` and `renders`.
2. **File System Cleanup Sync:** Because SQLite cascading deletes the records but *not* the physical files on disk, the `DELETE /api/projects/{id}` REST route must use Python's `shutil.rmtree()` to wipe `/data/frames/{id}`, `/data/thumbs/{id}`, and `/data/renders/{id}` **before** executing the SQL delete command. This order ensures that if the file deletion succeeds but the DB delete fails, a retry will still find the rows.
3. **UI Data Hydration:** The `frame_count` in the `projects` table is denormalized to prevent massive `COUNT(*)` queries blocking the database every time the UI dashboard polls for updates. It is incremented `+1` by the capture worker upon every successful frame save.

## 4.4 Schema Versioning

Because SQLite does not support native migrations, `database.py` maintains a `PRAGMA user_version` integer. On startup, `init_database()` reads the current `user_version` and applies any pending migration functions sequentially:

```python
MIGRATIONS = {
    0: initial_schema,   # CREATE TABLE statements above
    1: add_progress_pct,  # Example future migration
}

def init_database():
    with get_connection() as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version in sorted(MIGRATIONS):
            if version >= current:
                MIGRATIONS[version](conn)
        conn.execute(f"PRAGMA user_version = {max(MIGRATIONS) + 1}")
```

---

# 5. Background Workers & Automation

## 5.1 Task Scheduling Architecture (`APScheduler`)

The application mounts `apscheduler.schedulers.asyncio.AsyncIOScheduler` to the FastAPI lifespan.

- **Job Mapping:** Every active project in the database maps 1:1 to an APScheduler job. The APScheduler `job_id` MUST be strictly formatted as `project_{id}` to allow O(1) lookups when mutating the schedule.
- **Route Integration:** When `POST /api/projects` or `PUT /api/projects/{id}` is called, the route handler must directly call `scheduler.add_job()` or `scheduler.reschedule_job()`, passing the `capture_worker` function and `trigger='interval', seconds=interval_seconds`.
- **Resiliency:** On FastAPI boot, a setup function queries `SELECT id, interval_seconds FROM projects WHERE status = 'active'` and injects them into the scheduler, ensuring jobs survive container restarts.

## 5.2 The Capture Worker (`app/capture.py`)

This async function is triggered by the scheduler. It handles data ingestion, visibility filtering, and storage.

**Execution & Logic Flow:**

1. **Disk Failsafe Check:**
   - Executes `shutil.disk_usage(settings.frames_path)`.
   - If `free_bytes / (1024**3) < settings.disk_warning_threshold_gb`:
     - Executes `UPDATE projects SET status = 'paused_error'`.
     - Calls `scheduler.pause_all()`.
     - Fires async webhook to `settings.webhook_url` and `return`.
2. **Astronomical Filtering (`astral`):**
   - If `capture_mode == 'daylight_only'`:
     - Initializes `LocationInfo` using `settings.LATITUDE`, `settings.LONGITUDE`, and `settings.TZ`.
     - Calculates `sun_data = sun(city.observer, date=datetime.now(timezone.utc))`.
     - If `now < sun_data['sunrise']` or `now > sun_data['sunset']`: `return` (skip capture).
   - If `capture_mode == 'schedule'`:
     - Converts current UTC time to local time using `TZ`.
     - Checks if current weekday number is in `schedule_days` (comma-separated string, e.g., `'1,2,3,4,5'` for Mon-Fri).
     - Checks if current local time is between `schedule_start_time` and `schedule_end_time`.
     - If outside the schedule window: `return` (skip capture).
3. **NVR Data Fetch:**
   - Calls `await protect_manager.get_client().cameras[camera_id].get_snapshot(width, height)`.
   - **Strict Exception Handling:** Must wrap the call in `try...except (httpx.ReadTimeout, uiprotect.exceptions.NvrError)`. On exception, increment `projects.consecutive_failures`, log the error, and `return`. Do NOT crash the worker.
   - On success, reset `consecutive_failures = 0`.
4. **Luminance Filtering (`Pillow`):**
   - If `use_luminance_check == 1`:
     - Loads bytes: `img = Image.open(io.BytesIO(snapshot_bytes)).convert('L')`.
     - Calculates brightness: `stat = ImageStat.Stat(img); brightness = stat.mean[0]`.
     - Sets local variable `is_dark = 1` if `brightness < luminance_threshold`, else `0`.
5. **Storage & DB Commit:**
   - Generates `timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")`.
   - Creates project directory if missing: `os.makedirs(f"/data/frames/{project_id}", exist_ok=True)`.
   - Writes raw bytes to `/data/frames/{project_id}/{timestamp}.jpg`.
   - **Thumbnail Generation:** Resizes the in-memory Pillow image (already loaded for luminance check, or loaded now) to 320px wide (preserving aspect ratio), saves to `/data/thumbs/{project_id}/{timestamp}.jpg` with quality=60. This cache is used by the frame scrubber and heatmap hover previews.
   - Opens SQLite connection: `INSERT INTO frames (project_id, file_path, thumbnail_path, file_size, is_dark) VALUES (...)`.
   - Executes `UPDATE projects SET frame_count = frame_count + 1, consecutive_failures = 0 WHERE id = ?`.

## 5.3 The Render Worker (`app/render.py`)

To strictly enforce a maximum of ONE concurrent video render (protecting the K3s node's CPU limits), the render worker operates as a continuous `asyncio` background loop initialized on app startup.

**Polling interval:** Controlled by `settings.render_poll_interval_seconds` (default: 5 seconds).

**Execution & Logic Flow:**

1. **Queue Polling & Locking:**
   - Loop sleeps for the configured poll interval between checks.
   - Executes `SELECT id, project_id, render_type FROM renders WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1`.
   - If found, locks the job: `UPDATE renders SET status = 'rendering' WHERE id = ?`.
2. **Input Preparation (concat demuxer format):**
   - **For Standard Renders (Raw JPEGs):**
     - Queries `SELECT file_path FROM frames WHERE project_id = ? AND is_dark = 0 ORDER BY captured_at ASC`.
   - **For Range Renders (User-selected time window):**
     - Queries `SELECT file_path FROM frames WHERE project_id = ? AND is_dark = 0 AND captured_at BETWEEN ? AND ? ORDER BY captured_at ASC` using `renders.range_start` and `renders.range_end`.
   - **For Rollups (Weekly/Monthly):**
     - Queries `SELECT output_path FROM renders WHERE project_id = ? AND render_type = 'auto_daily' AND status = 'done' ORDER BY completed_at ASC`.
   - **File Generation:** Iterates the SQL results and writes a temporary text file (`/tmp/render_{render_id}.txt`) formatted strictly for ffmpeg:
     ```
     file '/data/frames/1/20260404120000.jpg'
     file '/data/frames/1/20260404120010.jpg'
     ```
3. **Subprocess Execution:**
   - Constructs command args per Section 6 specifications.
   - Mounts `asyncio.create_subprocess_exec("ffmpeg", *args, stderr=asyncio.subprocess.PIPE)`.
   - **Progress Parsing:** Reads stderr line-by-line. When a line matches `frame=\s*(\d+)`, calculates `progress_pct = min(100, int(current_frame / total_frames * 100))` and writes to `renders.progress_pct` (throttled to once per second to avoid DB thrashing).
   - **Timeout:** Each ffmpeg job has a 2-hour timeout enforced via `asyncio.wait_for()`. On timeout, the process is killed.
4. **Cleanup & DB Commit:**
   - In a `finally:` block (executes regardless of success/failure):
     - `contextlib.suppress(FileNotFoundError): os.remove(f'/tmp/render_{render_id}.txt')`.
   - If process return code `== 0`:
     - Calculate `file_size` via `os.path.getsize()`.
     - `UPDATE renders SET status = 'done', completed_at = CURRENT_TIMESTAMP, file_size = ?, progress_pct = 100 WHERE id = ?`.
   - If process return code `!= 0`:
     - Capture stderr output.
     - `UPDATE renders SET status = 'error', error_msg = ? WHERE id = ?`.

## 5.4 The Maintenance Worker (`app/maintenance.py`)

Registered in APScheduler to trigger daily at `02:00` local time using a `CronTrigger`. It synchronizes the database with physical file deletions to manage the `/data` volume.

**Execution & Logic Flow:**

1. **Frame Retention Pruning:**
   - Queries: `SELECT id, retention_days FROM projects WHERE retention_days > 0`.
   - For each project, calculates `cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)`.
   - Queries target files: `SELECT id, file_path, thumbnail_path FROM frames WHERE project_id = ? AND captured_at < ?`.
   - Iterates results: executes `contextlib.suppress(FileNotFoundError): os.remove(file_path)` and `contextlib.suppress(FileNotFoundError): os.remove(thumbnail_path)` for each frame.
   - Bulk deletes from DB: `DELETE FROM frames WHERE project_id = ? AND captured_at < ?`.
   - Recalculates and updates `projects.frame_count` from actual `COUNT(*)` to correct any drift in the denormalized counter.
2. **Auto-Render Rolling Windows:**
   - Uses an SQLite Window Function to identify stale auto-renders:
   ```sql
   WITH RankedRenders AS (
       SELECT id, output_path, render_type,
              ROW_NUMBER() OVER (PARTITION BY project_id, render_type ORDER BY created_at DESC) as rn
       FROM renders
       WHERE render_type IN ('auto_daily', 'auto_weekly', 'auto_monthly')
       AND status = 'done'
   )
   SELECT id, output_path FROM RankedRenders
   WHERE (render_type = 'auto_daily' AND rn > 7)
      OR (render_type = 'auto_weekly' AND rn > 4)
      OR (render_type = 'auto_monthly' AND rn > 3);
   ```
   - Iterates results, calls `contextlib.suppress(FileNotFoundError): os.remove(output_path)`, and executes `DELETE FROM renders WHERE id = ?`.
3. **Auto-Render Scheduling:**
   - For projects with `auto_render_daily = 1`: inserts a new `pending` render row for yesterday's frames (if frames exist and no render already exists for that date).
   - Weekly and monthly renders are triggered similarly on the appropriate day boundaries.

---

# 6. Video Processing & FFmpeg Specifications

## 6.1 FFmpeg Execution Context

Video rendering is the most resource-intensive operation in the application. To protect the K3s cluster and the GMKtec node from CPU starvation, every `ffmpeg` subprocess must strictly enforce a thread ceiling (`-threads 4`).

Instead of relying on fragile glob patterns (`*.jpg`) or complex pipe streams, the application strictly uses the `concat` demuxer for **all** video inputs. The Render Worker generates a temporary text file (`/tmp/render_{id}.txt`) listing the absolute paths of the files to process.

*Example `/tmp/render_{id}.txt`:*
```
file '/data/frames/1/20260404120000.jpg'
file '/data/frames/1/20260404120010.jpg'
```

## 6.2 Standard Render Command (Raw Frames → MP4)

Used for manual renders and the initial `auto_daily` renders.

**Command Structure:**
```bash
ffmpeg -threads 4 \
  -r {framerate} \
  -f concat -safe 0 -i /tmp/render_{id}.txt \
  -vf "deflicker=mode=pm:size=10" \
  -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p \
  /data/renders/{project_id}/{render_id}.mp4
```

- **`-r {framerate}`:** Sets the output framerate (e.g., 24, 30, 60).
- **`-vf deflicker`:** Applies a Phase Metering (`pm`) deflicker filter over a sliding window of 10 frames to remove natural daylight strobing.
- **`-pix_fmt yuv420p`:** STRICT RULE — must be included so the output video is playable in standard HTML5 `<video>` tags across all browsers (specifically Safari/iOS).

## 6.3 Timestamp Burn-In Pipeline

If `settings.timestamp_burn_in == 1`, the Render Worker alters the filter graph (`-vf`) to overlay the capture time onto the video.

**Filter Addition (appended after deflicker):**
```bash
-vf "deflicker=mode=pm:size=10,drawtext=text='%{pts\:localtime\:{epoch}\:%Y-%m-%d %H\\\:%M}':x=w-tw-20:y=h-th-20:fontcolor=white:fontsize=32:box=1:boxcolor=black@0.6"
```

**Implementation note:** Because `ffmpeg`'s concat demuxer can drop EXIF metadata from raw JPEGs, the timestamp must be injected dynamically. The render worker calculates the epoch timestamp of the first frame from the database, and ffmpeg's `pts` + `localtime` filter computes subsequent timestamps from the framerate. The `TZ` environment variable inside the container controls the local time display.

The drawtext filter is placed *after* deflicker in the filter chain so the text overlay is not affected by luminance smoothing.

## 6.4 Concatenation Rollups (MP4 → MP4)

Used for `auto_weekly` and `auto_monthly` renders.

### Scenario A: 1:1 Speed (No Frame Skipping)

If the user wants a weekly video that is simply 7 daily videos played back-to-back at the same speed, the application bypasses the encoder entirely and stream-copies the files.

```bash
ffmpeg -threads 4 -f concat -safe 0 -i /tmp/render_{id}.txt -c copy \
  /data/renders/{project_id}/{render_id}.mp4
```

### Scenario B: Dynamic Frame Skipping (Speed Adjustment)

If a monthly video stitched from 30 daily videos would be too long at 1:1, the application calculates a `skip_multiplier` to condense the video. Because frames are dropped, the stream *must* be re-encoded.

**Math Logic (Python):**
```python
# Example: Monthly video target = 60 seconds at 30fps = 1800 target frames
# Total frames across 30 daily renders = 25,920
skip_multiplier = math.ceil(total_frames / target_frames)  # e.g., 15
```

**Command Structure:**
```bash
ffmpeg -threads 4 \
  -f concat -safe 0 -i /tmp/render_{id}.txt \
  -vf "select='not(mod(n\,{skip_multiplier}))',setpts=N/FRAME_RATE/TB" \
  -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p \
  /data/renders/{project_id}/{render_id}.mp4
```

- **`select='not(mod(n\,X))'`:** Drops frames, keeping only frames where `n % X == 0`.
- **`setpts=N/FRAME_RATE/TB`:** Recalculates Presentation Time Stamps so playback is smooth.

## 6.5 Historical Extraction Pipeline (MP4 → JPEGs)

Used when a user wants a timelapse of an event that already happened.

**Workflow:**

1. System requests a video chunk (e.g., 1 hour) from NVR via `camera.get_video(start, end)`.
2. Chunk is saved temporarily as `/tmp/hist_chunk_{project_id}_{chunk_index}.mp4`.
3. FFmpeg extracts frames at the specified interval:

```bash
ffmpeg -threads 4 -i /tmp/hist_chunk_{project_id}_{chunk_index}.mp4 \
  -vf "fps=1/{interval_seconds}" \
  -q:v 2 \
  /data/frames/{project_id}/%014d.jpg
```

- **`fps=1/X`:** Extracts exactly one frame per X seconds of video.
- **`-q:v 2`:** High JPEG quality (scale 1-31, where 2 is near-lossless).
- **`%014d.jpg`:** Sequential numbering. After extraction, the capture worker renames these to UTC timestamps calculated from `chunk_start_time + (frame_index * interval_seconds)`.

4. Temporary chunk is deleted in a `finally:` block.

## 6.6 Deflicker & Image Enhancement

Surveillance cameras frequently shift exposure rapidly (e.g., when clouds pass). This creates "flicker" in a timelapse.

- **Filter:** `deflicker=mode=pm:size=10`
- **Logic:** Phase Metering over a 10-frame sliding buffer smooths luminance between frames. This filter MUST always run *before* `drawtext` so the timestamp text is not affected by luminance smoothing.

## 6.7 Progress Monitoring (FFmpeg Pipe Analysis)

For the Alpine.js frontend to show a progress bar, the Render Worker parses output from ffmpeg in real-time.

**Implementation:**

1. The worker reads `stderr` from the ffmpeg subprocess (ffmpeg writes progress to stderr).
2. Lines are parsed for `frame=\s*(\d+)` patterns.
3. Progress is calculated: `(current_frame / total_frames_from_db) * 100`.
4. This value is written to `renders.progress_pct` in SQLite, throttled to once per second to avoid excessive writes.
5. The API endpoint `GET /api/renders/{id}/status` serves this value to the frontend.

## 6.8 Resource Safeguards & Cleanup

- **Temp Files:** All `/tmp/render_*.txt` and temporary `.mp4` chunks MUST be deleted with `os.remove()` in a `finally:` block, regardless of whether the ffmpeg job succeeded or failed.
- **Timeout:** Each ffmpeg job gets a 2-hour timeout via `asyncio.wait_for()`. If a render hangs, the process is killed to free resources for the capture worker.
- **Bitrate Control:** `-crf 23` provides a good balance between quality and file size (H.264 standard).

---

# 7. System Resiliency & Alerting

## 7.1 Disk Space Failsafe (Storage Protection)

Since timelapse projects can generate thousands of files rapidly, monitoring the `/data` volume is critical to avoid SQLite database corruption.

- **Check interval:** Every 5 minutes (runs as part of the `capture_worker` loop).
- **Logic:** `shutil.disk_usage("/data")` is called.
- **Threshold:** `settings.disk_warning_threshold_gb` (default: 5 GB).
- **Action on breach:**
  1. All active projects are set to `status = 'paused_error'`.
  2. `APScheduler` stops all capture jobs.
  3. A "Critical Disk Space" alarm is sent via webhook.
  4. UI shows a red banner warning with instructions to delete old renders or adjust retention.

## 7.2 NVR Connectivity & Offline Handling

UniFi Protect NVRs can restart (firmware updates) or lose connectivity. The application must never crash due to network errors.

- **Retry Strategy:** The `uiprotect` client in `protect.py` uses an `asyncio.Lock`. If a request fails, it does NOT attempt aggressive retries to avoid blocking the worker thread.
- **Graceful Skip:** If a snapshot request times out (`httpx.ReadTimeout`), the system logs a warning and waits for the next scheduled interval.
- **Failure Escalation:** The `consecutive_failures` counter on the `projects` table is incremented on each failed NVR call and reset to 0 on success. If `consecutive_failures >= 3`, the camera is marked as "Offline" in the UI and a webhook notification is sent. The project continues to attempt captures (the counter keeps incrementing but capture attempts don't stop — the NVR may come back).

## 7.3 Webhook Alerting System (`app/notifications.py`)

The system supports outgoing HTTP POST notifications (compatible with Discord, Slack, ntfy.sh, or Home Assistant).

**Trigger events:**

1. **Storage Critical:** Disk space below threshold.
2. **NVR Offline:** 3+ consecutive failures for a camera.
3. **Render Error:** ffmpeg job failed (return code != 0).
4. **Project Auto-Completed:** Project reached `max_frames`.

**Payload Format (JSON):**
```json
{
  "event": "storage_critical",
  "level": "error",
  "project_id": 1,
  "project_name": "Byggeplads Nord",
  "message": "Capture paused: Less than 5GB free on /data.",
  "timestamp_utc": "2026-04-04T21:40:00Z",
  "details": {
    "free_space_gb": 4.2,
    "path": "/data"
  }
}
```

**Implementation notes:**
- Every notification event is written to the `notifications` table in SQLite *before* attempting the external webhook POST. This ensures the in-app notification center has a complete event log even if the webhook URL is unconfigured or the delivery fails.
- The webhook POST is fire-and-forget (`httpx.AsyncClient` with a 10-second timeout).
- If the webhook URL is empty/null, the external POST is skipped but the DB row is still written.
- Failed webhook deliveries are logged but do NOT block the worker or trigger retries.

## 7.4 Zombie Process Management

Since `ffmpeg` runs as a subprocess, there is a risk of "zombie" renders if the FastAPI container restarts unexpectedly.

- **Startup Recovery:** During lifespan startup (after DB init), the system queries for renders with `status = 'rendering'`. Since a restart means the previous ffmpeg process is dead, these are set to `status = 'pending'` for automatic retry (the partially written output file is deleted first).
- **SIGTERM Handling:** On receiving a termination signal from Docker/K3s, the application sends `SIGINT` to all running ffmpeg subprocesses to let them finalize the MP4 container cleanly. If they don't exit within 30 seconds, `SIGKILL` is sent.

## 7.5 SQLite WAL & Data Integrity

To ensure UI reads don't block capture writes:
- **WAL Mode:** `PRAGMA journal_mode=WAL;` on every connection.
- **Synchronous Normal:** `PRAGMA synchronous=NORMAL;` balances performance and safety against power loss (important on mini-PC hardware like GMKtec).

---

# 8. API Contracts & NVR Integration

## 8.1 NVR Integration (`protect.py`)

To minimize NVR load and ensure fast UI response times, NVR state (bootstrap) is cached in memory.

- **Singleton Pattern:** `ProtectClientManager` initializes one instance of `ProtectApiClient`.
- **Connection Lifecycle:**
  - `setup()` is called at FastAPI startup.
  - `get_client()` returns the client. If the connection is lost, `client.update()` is called automatically.
- **Camera Data:** The system maps NVR cameras to local projects via their unique `camera_id` (e.g., `5e4a...`).

## 8.2 API Endpoints (REST)

All endpoints return JSON unless otherwise noted. Base URL: `/api`.

### Health

- `GET /health`: Returns system status including NVR connectivity, disk usage, and active worker count.
  - *Response:* `{"status": "ok", "nvr_connected": true, "disk_free_gb": 42.1, "disk_total_gb": 100.0, "active_projects": 3, "pending_renders": 1}`

### Cameras & Preview

- `GET /cameras`: Returns list of available cameras from NVR.
  - *Response:* `[{"id": "abc", "name": "Indkørsel", "type": "UVC G4 Bullet", "is_online": true}, ...]`
- `GET /cameras/{id}/preview`: Proxy call to NVR for a live image. Used for "Live FoV preview" in UI.
  - *Response:* `image/jpeg` (binary stream).

### Projects (CRUD)

- `GET /projects`: List of all timelapse projects including `frame_count`, `status`, and `consecutive_failures`.
- `POST /projects`: Creates a new project (Live or Historical).
  - *Payload:* `{"name": "...", "camera_id": "...", "project_type": "live", "interval_seconds": 10, "capture_mode": "daylight_only", ...}`
  - *Logic:* Creates directories on disk and adds job to `APScheduler` (live) or triggers historical extraction task.
- `GET /projects/{id}`: Detailed view of a single project.
- `PUT /projects/{id}`: Updates settings (e.g., pause/resume or change interval). Mutates APScheduler job accordingly.
- `DELETE /projects/{id}`: Deletes project. First removes files from `/data` via `shutil.rmtree()`, then executes SQL delete (cascade handles frames/renders rows).

### Frames & Export

- `GET /projects/{id}/frames`: Paginated list of frames. Supports `?limit=X&offset=Y&order=desc&fields=id,captured_at` (the `fields` parameter enables lightweight index-only queries for the scrubber).
- `GET /projects/{id}/frames/{frame_id}/thumbnail`: Returns a low-res thumbnail JPEG (320px wide). Used by the scrubber to load images on-demand as the slider moves.
- `GET /projects/{id}/frames/{frame_id}/full`: Returns the full-resolution JPEG.
- `PUT /projects/{id}/frames/{frame_id}/bookmark`: Sets or clears a bookmark note on a frame.
  - *Payload:* `{"note": "Kran ankommet"}` or `{"note": null}` to clear.
- `GET /projects/{id}/frames/bookmarks`: Returns only bookmarked frames with their notes.
  - *Response:* `[{"id": 42, "captured_at": "...", "thumbnail_path": "...", "bookmark_note": "Kran ankommet"}, ...]`
- `GET /projects/{id}/frames/dark`: Returns frames flagged as `is_dark = 1` with thumbnails. Used by the dark frame gallery to help users calibrate `luminance_threshold`.
  - *Response:* `[{"id": 99, "captured_at": "...", "thumbnail_path": "...", "brightness": 12}, ...]`
- `GET /projects/{id}/frames/export`: Generates an on-the-fly `.zip` file with all project frames.
  - *Response:* `application/zip` (StreamingResponse).
- `GET /projects/{id}/stats/daily`: Returns data for "Capture Heatmap".
  - *Response:* `[{"date": "2026-04-01", "count": 864}, ...]`
- `GET /projects/{id}/stats/timeline`: Returns capture continuity data for the timeline view, including gaps and dark-frame periods.
  - *Response:* `[{"hour": "2026-04-01T08:00Z", "captured": 120, "dark": 5, "missed": 0}, ...]`

### Renders (Video)

- `POST /renders`: Adds a new render job to the queue. Returns an estimate of render duration and file size.
  - *Payload:* `{"project_id": 1, "framerate": 30, "resolution": "1920x1080", "render_type": "manual", "label": "30fps deflicker test"}` 
  - *Optional range fields:* `{"range_start": "2026-04-01T00:00:00Z", "range_end": "2026-04-03T00:00:00Z"}` for `render_type: "range"`.
  - *Response:* `{"id": 5, "estimated_duration_seconds": 180, "estimated_file_size_bytes": 52428800}`
  - *Estimation logic:* Duration = `frame_count / framerate` (in seconds of video), render time ≈ `frame_count * 0.02s` (empirical baseline for `-preset fast`). File size ≈ `frame_count * avg_frame_size * 0.15` (empirical CRF 23 compression ratio).
- `GET /renders/{id}/status`: Used for UI polling of progress (fallback when WebSocket is unavailable).
  - *Response:* `{"status": "rendering", "progress_pct": 45, "error_msg": null}`
- `GET /projects/{id}/renders`: List all renders for a project. Includes `label` for comparison view.
  - *Response:* `[{"id": 1, "render_type": "manual", "label": "30fps deflicker", "status": "done", "file_size": 52428800, "completed_at": "...", "output_path": "..."}, ...]`
- `DELETE /renders/{id}`: Deletes a render and its output file.

### Templates

- `GET /templates`: List all saved project templates.
- `POST /templates`: Create a new template from a configuration payload.
  - *Payload:* `{"name": "Byggeplads standard", "interval_seconds": 60, "capture_mode": "schedule", "schedule_start_time": "07:00", "schedule_end_time": "17:00", "schedule_days": "1,2,3,4,5", ...}`
- `POST /templates/{id}/apply`: Creates a new project using the template's configuration. Camera and name must still be provided.
  - *Payload:* `{"name": "Byggeplads Nord", "camera_id": "abc123"}`
- `DELETE /templates/{id}`: Deletes a template. Does not affect projects created from it.

### Notifications (In-App)

- `GET /notifications`: Returns recent notifications, newest first. Supports `?unread_only=true&limit=50`.
  - *Response:* `[{"id": 1, "event": "storage_critical", "level": "error", "message": "...", "is_read": false, "created_at": "..."}, ...]`
- `PUT /notifications/read`: Marks notifications as read.
  - *Payload:* `{"ids": [1, 2, 3]}` or `{"all": true}`

### WebSocket

- `WS /api/ws`: Real-time event stream. See Section 2.12 for event types and payload formats.

### Settings

- `GET /settings`: Returns the global settings row.
- `PUT /settings`: Updates global settings (webhook URL, disk threshold, etc.).

## 8.3 Error Handling & Status Codes

The API follows standard HTTP status codes:

- `200 OK`: Success.
- `201 Created`: Project or render created.
- `400 Bad Request`: Invalid parameters (e.g., interval < 1 second).
- `404 Not Found`: Project, frame, or render not found.
- `409 Conflict`: Render already pending for this project/type combination.
- `503 Service Unavailable`: Cannot connect to NVR or disk is full.

All error responses follow a consistent JSON structure:
```json
{"detail": "Human-readable error message"}
```

## 8.4 NVR Client Commands (Code Snippets)

```python
# Get all cameras
cameras = client.bootstrap.cameras.values()

# Get snapshot (Live)
snapshot: bytes = await camera.get_snapshot(width=1920, height=1080)

# Get video chunk (Historical)
# Note: Returns a generator or bytes depending on size
video: bytes = await camera.get_video(start_time, end_time)
```

---

# 9. Frontend & UX Specifications

The frontend is designed as a Single-Page Application (SPA) built on **Alpine.js 3** and styled with **Tailwind CSS v4**. Since the application runs in a Docker container behind an Ingress, the focus is on fast responsiveness and data visualization without heavy build steps.

## 9.1 Global State Management (`app.js`)

Alpine.js manages application state via a central `timelapseApp()` data function, initialized at `alpine:init`.

**Primary state variables:**

- `view`: Current view — `'dashboard'`, `'project_detail'`, `'create_project'`, `'settings'`.
- `projects`: Array of all projects from `/api/projects`.
- `cameras`: List of NVR cameras (used for dropdowns and grouping).
- `activeProject`: Object for the selected project in detail view.
- `templates`: Array of saved project templates.
- `notifications`: Array of recent in-app notifications.
- `unreadCount`: Integer for notification badge.
- `ws`: WebSocket connection instance (nullable, with reconnect logic).
- `diskSpace`: Object with `free`, `total`, and `percent` (used for dashboard gauge).

## 9.2 WebSocket Connection Management

On app initialization, Alpine attempts to open a WebSocket to `/api/ws`. The connection manager handles:

- **Auto-reconnect:** If the connection drops, retry with exponential backoff (1s, 2s, 4s, max 30s).
- **Event Dispatch:** Incoming WS messages are parsed and dispatched to update the relevant Alpine state variables in real-time (project frame counts, render progress, notifications, disk space).
- **Fallback:** If WebSocket fails to connect after 3 attempts, the app silently falls back to HTTP polling at the intervals defined below. A small "Live updates unavailable" indicator appears in the footer.

## 9.3 Views & Components

### A. Dashboard (Overview)

Main screen providing a quick overview of system health, organized by camera.

- **Camera-Grouped Layout:** Projects are grouped under their parent camera as collapsible sections. Each camera section header shows the camera name, model, online/offline indicator, and a badge with active project count. Cameras with errors are expanded by default, healthy cameras are collapsed if there are more than 4 cameras total.
- **Project Cards:** Each card shows project name, status dot, frame count, and interval. **Quick-action icon buttons** on each card allow Pause/Resume and Delete directly from the dashboard without navigating to the detail view.
- **Status Indicators:** Colored dots — `active` (green pulsing), `paused` (yellow), `paused_error` / `error` (red), `completed` (blue).
- **Disk Usage Gauge:** A Tailwind-based progress bar that turns red when approaching `disk_warning_threshold_gb`.
- **Notification Bell:** Top-right icon with unread count badge. Clicking opens a dropdown showing recent events (from the `notifications` table). Each notification is clickable and navigates to the relevant project or settings view.

### B. Project Detail

Visualizes progress and results for a specific timelapse. Organized as a tabbed interface.

**Tab: Overview**

- **Capture Heatmap:** A GitHub-style grid (7 rows × 52 columns).
  - *Logic:* Backend returns daily counts. Alpine maps these to color intensity (e.g., `bg-emerald-200` to `bg-emerald-900`). Gray cells indicate 0 captures (downtime).
  - *Click interaction:* Clicking a cell navigates the frame scrubber to that day and can be used to set render range boundaries (see drag-to-select below).
- **Capture Timeline:** A horizontal bar chart below the heatmap showing hourly capture density, with color-coded segments for successful captures (green), dark frames (amber), and missed intervals (gray gaps). This complements the heatmap by showing continuity and identifying exactly when problems occurred.
- **Frame Scrubber:** An interactive element with `<input type="range">` and `<img>` tag.
  - *Logic:* On mount, fetches a lightweight frame index (`?fields=id,captured_at&limit=500`). When the user moves the slider, the `src` attribute updates to load the thumbnail via `/api/projects/{id}/frames/{frame_id}/thumbnail`. This provides a quick visual sense of progress without rendering video.
  - *Drag-to-select:* Holding Shift while dragging the scrubber highlights a time range (shown as a colored overlay on the slider track). Releasing opens a context menu: "Render this range" or "Compare endpoints". This creates a render with `render_type: 'range'` and the selected `range_start`/`range_end`.
- **Render Estimate:** Before submitting a render, a non-blocking calculation shows estimated render time and file size based on frame count, framerate, and average frame size. Displayed as a subtle line below the render button: "~180s render time, ~50MB output".

**Tab: Renders**

- **Render History:** List of generated MP4 files with label, type, status, file size, and buttons for "Play", "Download", and "Delete".
- **Render Comparison:** When two or more completed renders exist for the same project, a "Compare" button appears. Selecting two renders opens them side-by-side in synchronized `<video>` players with shared play/pause and seek controls. Labels (e.g., "30fps deflicker" vs "24fps raw") are shown above each player. This helps the user pick the best render settings.
- **Playback Speed Controls:** Custom overlay buttons above the native `<video>` element: `0.5×`, `1×`, `2×`, `4×`. These set `video.playbackRate` directly. The currently active speed is highlighted.

**Tab: Bookmarks**

- Shows only frames where `bookmark_note IS NOT NULL`, displayed as a scrollable gallery with the note text below each thumbnail.
- Each bookmark is clickable and scrolls the scrubber to that frame's position.

**Tab: Dark Frames**

- Displays frames flagged as `is_dark = 1`, shown as a thumbnail gallery with the calculated brightness value displayed on each.
- Purpose: Allows the user to visually calibrate `luminance_threshold`. If frames that should be included are being flagged as dark, the threshold needs lowering.
- A "Current threshold: 15" indicator and a "Test threshold" slider let the user preview how many frames would be flagged at different thresholds (using a `COUNT(*)` query with varying threshold values).

**Side-by-Side Comparison View**

- Accessible from the scrubber (Shift+click two frames) or from the bookmarks tab (select two bookmarks).
- Displays two frame thumbnails in a split-view with a draggable CSS-based center divider. Timestamps are shown below each frame.
- No backend dependency — uses existing thumbnail endpoints.

### C. Create/Edit Project (Guided Setup)

A form-based view for setting up new projects.

- **Template Dropdown:** At the top of the form, a "Start from template" dropdown pre-fills all fields from the selected template. Fields can be overridden. A "Save as template" button saves the current form state.
- **Onboarding Empty State:** When no projects exist, the create view is shown automatically with a brief explanation: "Create your first timelapse. Choose 'Live' to start capturing now, or 'Historical' to extract frames from past recordings." Each option includes a small illustration and 1-line description.
- **Live FoV Preview:** When a camera is selected in the dropdown, a preview image is fetched every 5 seconds via `/api/cameras/{id}/preview` so the user can verify the angle.
- **Interval Presets:** Quick-select buttons:
  - *Clouds/Weather:* 5s
  - *Construction Site:* 1m
  - *Long-term Project:* 10m
- **Capture Mode Toggles:** Simple switches for `Daylight Only` (Astral), `Schedule` (shows time/day pickers), and `Luminance Check` (Pillow).
- **Schedule Mode UI:** When `Schedule` is selected, two time pickers appear for start/end time, and a row of weekday toggle buttons (Mon-Sun). Default: Mon-Fri 07:00-17:00.

### D. Settings

- Webhook URL configuration.
- Disk warning threshold.
- Timestamp burn-in toggle.
- Default framerate.
- **Template Management:** List of saved templates with rename and delete buttons.
- **Notification History:** Full scrollable list of all notifications with read/unread status and filtering by level (info/warning/error).

## 9.4 Keyboard Shortcuts

The application registers global keyboard shortcuts for power-user efficiency. Shortcuts are only active when no input field is focused.

| Key | Action | Context |
| :--- | :--- | :--- |
| `N` | Open "Create Project" view | Dashboard |
| `Esc` | Return to Dashboard / Close modal | Any view |
| `Space` | Play/Pause video | Video player focused |
| `←` / `→` | Step one frame backward/forward | Frame scrubber focused |
| `Shift+←` / `Shift+→` | Jump 10 frames backward/forward | Frame scrubber focused |
| `R` | Trigger render for active project | Project detail |
| `B` | Bookmark current frame | Frame scrubber focused |
| `1`-`4` | Set playback speed (0.5×, 1×, 2×, 4×) | Video player focused |
| `?` | Show keyboard shortcut help overlay | Any view |

## 9.5 Real-time Feedback & Communication

Primary channel is WebSocket (Section 9.2). HTTP polling is the fallback:

- **Dashboard Polling (fallback):** Every 30 seconds, the project list and disk usage are refreshed.
- **Render Polling (fallback):** If WebSocket is unavailable and `renders.status == 'rendering'`, the frontend polls `GET /api/renders/{id}/status` every 2 seconds.
- **Toast Notifications:** A Toast component (Alpine `x-show` with transitions) displays messages on successful saves, render completions, or errors. Toasts are triggered by WebSocket events or by API response handlers.

## 9.6 Tailwind v4 Styling (Dark Mode)

The application is built with a "Mobile-First" and "Dark-Mode-First" approach.

- **Color Palette:** Dark gray backgrounds (`bg-slate-950`), white text, and UniFi-blue accents (`text-blue-500`).
- **Responsivity:** Grid layouts switch from 1 column on mobile to 3 or 4 columns on desktop.

## 9.7 Video Player Integration

The application uses the browser's native `<video>` tag with specific attributes for compatibility with ffmpeg output:

- **Attributes:** `controls`, `preload="metadata"`, `loop`, `muted`, `playsinline`.
- **Format:** With `pix_fmt yuv420p` in ffmpeg, playback is supported across Chrome, Firefox, and Safari (iOS).
- **Playback Speed:** Custom overlay buttons (`0.5×`, `1×`, `2×`, `4×`) set `video.playbackRate`. Keyboard shortcuts `1`-`4` map to these speeds.

The application is built with a "Mobile-First" and "Dark-Mode-First" approach.

- **Color Palette:** Dark gray backgrounds (`bg-slate-950`), white text, and UniFi-blue accents (`text-blue-500`).
- **Responsivity:** Grid layouts switch from 1 column on mobile to 3 or 4 columns on desktop.

---

# 10. Environment & Dev Operations

## 10.1 Environment Variables (Single Source of Truth)

All variables MUST be defined in `app/config.py` using Pydantic `BaseSettings`. No other files may call `os.getenv()`.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PROTECT_HOST` | `argos.local` | IP or hostname of UniFi Protect NVR. |
| `PROTECT_PORT` | `443` | NVR HTTPS port. |
| `PROTECT_USERNAME` | — (required) | Local admin user. |
| `PROTECT_PASSWORD` | — (required) | Password. |
| `PROTECT_VERIFY_SSL` | `false` | Whether to verify NVR SSL certificate (self-signed by default). |
| `DATABASE_PATH` | `/data/timelapse.db` | Path to SQLite file (on PVC). |
| `FRAMES_PATH` | `/data/frames` | Directory for JPEG storage. |
| `THUMBNAILS_PATH` | `/data/thumbs` | Directory for low-res thumbnail cache (320px wide). |
| `RENDERS_PATH` | `/data/renders` | Directory for finished MP4 files. |
| `TZ` | `Europe/Copenhagen` | Used for UI display and Astral calculations. |
| `LATITUDE` | `56.0361` | Geolocation for sunrise/sunset (Helsingør). |
| `LONGITUDE` | `12.6136` | Geolocation for sunrise/sunset (Helsingør). |
| `LOG_LEVEL` | `INFO` | Python logging level (DEBUG, INFO, WARNING, ERROR). |
| `FFMPEG_THREADS` | `4` | Thread cap for ffmpeg subprocesses. |
| `FFMPEG_TIMEOUT_SECONDS` | `7200` | Maximum runtime for a single ffmpeg job (default: 2 hours). |

## 10.2 Docker Multi-Stage Build

The container is built in two stages to minimize image size and remove unnecessary build dependencies from the runtime image.

```dockerfile
# Stage 1: Build CSS (Node.js)
FROM node:22-slim AS css-build
WORKDIR /src
COPY package.json .
RUN npm install
COPY app.css.src .
COPY templates/index.html .
RUN npx @tailwindcss/cli -i app.css.src -o app.css --minify

# Stage 2: Final Image (Python)
FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libmagic1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=css-build /src/app.css ./static/app.css

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8080/api/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

## 10.3 Logging

The application uses Python's `logging` module with structured output suitable for container log aggregation.

- **Format:** `%(asctime)s %(levelname)s [%(name)s] %(message)s`
- **Level:** Controlled by `LOG_LEVEL` environment variable.
- **Worker Logging:** `capture.py` and `render.py` use dedicated loggers (`app.capture`, `app.render`) so log output can be filtered by component.
- **No log rotation:** Container stdout/stderr is managed by the container runtime (Docker/K3s handles log rotation). Do not write log files to disk.

## 10.4 CI Pipeline (GitHub Actions)

Pipeline structure follows a "fail-fast" principle:

1. **Lint:** `ruff check .` and `ruff format --check .`
2. **Typecheck:** `mypy app/` (ignores missing imports for external libs).
3. **Test:** `pytest --cov=app` (must pass 80% coverage).
4. **Security Scan:** `trivy` scan of source code and final Docker image.
5. **Build & Push:** Builds image and pushes to registry with tags `sha-${GITHUB_SHA}` and `latest`.

## 10.5 Strict Coding Rules (The "No-Go" List)

To ensure consistency, these rules must be followed:

- **No ORM:** Use exclusively raw SQL queries via `sqlite3` to maintain full control over performance.
- **Single DB Factory:** Use `get_connection()` from `database.py` with context managers (`with get_connection() as conn:`). Always close the connection.
- **Context Suppression:** Use `contextlib.suppress(FileNotFoundError)` instead of empty `try/except` blocks for file operations.
- **Mocking:** Use `monkeypatch.setattr` in the test suite. Avoid `unittest.mock.patch` decorators as they are harder to manage in async tests.
- **Tailwind:** Never edit `static/app.css`. All style changes happen in `templates/index.html` or via Alpine.js classes.
- **No CORS middleware:** Since the API and frontend are served from the same Uvicorn process, CORS is not needed. Do not add it unless the architecture changes.

## 10.6 K3s & Persistence

Although the application is a generic Docker container, it depends on persistent storage:

- **Volume Mount:** `/data` MUST be mounted to a Persistent Volume (PVC).
- **Network:** The application must have access to the NVR's IP on port 443.
- **Resources:** Set `limits.cpu` and `limits.memory` in the deployment YAML, but remember that ffmpeg is internally limited to the configured thread count.

---

# 11. Bootstrap Implementation Plan

This is the step-by-step roadmap from an empty repository to a working v1.0. Follow the order to minimize debugging of complex async errors later. New features (templates, WebSocket, bookmarks, etc.) are integrated into the phases where their dependencies are already in place.

## Phase 1: Foundation & Environment (Day 1)

1. **Repository Setup:** Initialize the directory structure and create `pyproject.toml` (with `requirements.txt` for Docker).
2. **Config & Database:**
   - Implement `app/config.py` with Pydantic `BaseSettings` (all env vars including `THUMBNAILS_PATH`).
   - Write `app/database.py` with `get_connection()`, `init_database()` (all 6 tables + migration system), and WAL pragmas.
   - Verify that `timelapse.db` is created correctly in `/data` at runtime with all tables including `project_templates` and `notifications`.
3. **Protect Singleton:**
   - Implement `app/protect.py` with `ProtectClientManager`.
   - Write a small test script that calls `client.update()` and prints camera names.

## Phase 2: API & Live Ingestion (Day 2)

1. **FastAPI Factory:** Create `app/__init__.py` with `lifespan` (including `/data/thumbs` directory creation) and mount `app/routes/health.py`.
2. **Camera Routes:** Implement `GET /api/cameras` and `GET /api/cameras/{id}/preview`.
3. **Thumbnail Module:** Implement `app/thumbnails.py` — a single function that takes Pillow image bytes and returns a 320px-wide JPEG at quality=60.
4. **Capture Worker (The Loop):**
   - Configure `AsyncIOScheduler` in `app/capture.py`.
   - Implement `snapshot_worker` function with full pipeline: disk check → astronomical filter → **schedule mode filter** → NVR call → Pillow luminance check → disk save (full-res + thumbnail) → DB insert.
   - Test manually by inserting a project into SQLite and verifying images appear in both `/data/frames/1/` and `/data/thumbs/1/`.
5. **Template Routes:** Implement `app/routes/templates.py` (CRUD + apply). This is simple table operations with no dependencies on workers.

## Phase 3: Frontend Shell & Real-Time Layer (Day 3)

1. **Tailwind Setup:** Configure the `css-build` stage in the Dockerfile or run CLI locally for development.
2. **WebSocket Manager:** Implement `app/websocket.py` with `ConnectionManager` class, `broadcast()` function, and `/api/ws` endpoint. Wire the capture worker to emit `capture_event` and `disk_update` events.
3. **Index.html:** Build the base layout with Alpine.js `x-data`, including WebSocket connection logic with auto-reconnect and polling fallback.
4. **Dashboard:**
   - Implement `fetch('/api/projects')` with **camera-grouped layout** (projects organized under camera section headers).
   - Add **quick-action buttons** (pause/resume/delete) on each project card.
   - Add **notification bell** with unread count badge and dropdown.
   - Add disk usage gauge.
5. **Onboarding Empty State:** When no projects exist, show guided "Create your first timelapse" flow with Live vs Historical explanation.
6. **Create View:**
   - Build the form for new projects including the live FoV preview image.
   - Add **template dropdown** that pre-fills form fields, and "Save as template" button.
   - Add **schedule mode UI** (time pickers + weekday toggles, shown when capture_mode = 'schedule').
   - Add interval presets.
7. **Keyboard Shortcuts:** Register global handlers (`N`, `Esc`, `?` for help overlay). Context-specific shortcuts are added in Phase 4-5 as their views are built.

## Phase 4: Rendering & Video Logic (Day 4)

1. **Render Worker:**
   - Implement `app/render.py` with the sequential queue loop, progress monitoring, and **range render support** (filtering frames by `range_start`/`range_end`).
   - Wire render events to WebSocket (`render_progress`, `render_complete`).
   - Write the `ffmpeg` wrapper that generates `/tmp/render_X.txt`.
   - Test a manual render via `POST /api/renders` and verify that the MP4 file is valid (`pix_fmt yuv420p`).
2. **Render Estimation:** Implement the pre-render estimation logic in `POST /renders` response (estimated duration and file size based on frame count, framerate, and average frame size).
3. **Historical Extraction:**
   - Add the logic to fetch video chunks from NVR, run `ffmpeg -vf fps=...`, backfill frames with correct UTC timestamps, and generate thumbnails for extracted frames.
4. **Notification System:** Implement `app/notifications.py` — writes to both `notifications` table and external webhook. Wire into capture worker (disk alerts, NVR offline) and render worker (render errors, completions). Add `app/routes/notifications.py` for listing and read-marking.

## Phase 5: Advanced UI & Polishing (Day 5)

1. **Maintenance Worker:** Implement daily cleanup of old frames + thumbnails, auto-render scheduling, and rolling window pruning.
2. **Project Detail View (Tabbed):**
   - **Overview tab:** Heatmap (with clickable cells), **capture timeline** (hourly bar chart with captured/dark/missed segments), frame scrubber (loading thumbnails from cache).
   - **Drag-to-select:** Implement Shift+drag on scrubber for range selection, with context menu to trigger range renders.
   - **Renders tab:** Render history with labels. **Render comparison** — side-by-side synchronized video players for comparing two renders. **Playback speed controls** (0.5×, 1×, 2×, 4×).
   - **Bookmarks tab:** Implement bookmark creation/deletion on frames (`PUT /frames/{id}/bookmark`), bookmark gallery view.
   - **Dark frames tab:** Gallery of `is_dark = 1` frames with brightness values and threshold calibration slider.
3. **Side-by-Side Comparison View:** CSS-based split view with draggable divider for comparing two frames (from scrubber or bookmarks).
4. **Remaining Keyboard Shortcuts:** `Space` (play/pause), `←/→` (frame step), `R` (render), `B` (bookmark), `1-4` (playback speed).
5. **Settings View:** Template management (rename/delete), notification history with filtering.
6. **CI/CD:** Complete `.github/workflows/main.yml` and deploy the container to the K3s node.

---

## Developer Notes

- **Start small:** Get one frame + thumbnail saved to disk before worrying about ffmpeg.
- **Logs are your friend:** Since most work happens in background threads, use `logging.info()` liberally in `capture.py` and `render.py`.
- **Use SQLite Browser:** Keep a GUI open on your `timelapse.db` to watch rows change in real-time during development.
- **Test WebSocket early:** Use `websocat` or a browser console to verify WS events are flowing before building the Alpine.js integration.
- **Test the shutdown:** Verify that `SIGTERM` correctly stops the scheduler, waits for ffmpeg, closes WebSocket clients, and closes connections cleanly before moving to production.