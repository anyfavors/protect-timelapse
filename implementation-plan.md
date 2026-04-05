# Implementation Plan — Protect Timelapse

Track progress here: check off each item when done. Update status after each session.

**Status key:** `[ ]` not started · `[x]` done · `[~]` in progress

---

## Phase 1 — Foundation ✓

> Spec ref: [§3.3 Directory Layout](project-design.md), [§4 Database](project-design.md), [§10.1 Env vars](project-design.md)

- [x] Create directory skeleton: `app/`, `app/routes/`, `static/`, `templates/`, `tests/`
- [x] `pyproject.toml` — ruff, mypy, pytest config (`asyncio_mode = auto`)
- [x] `requirements.txt` — fastapi, uvicorn, uiprotect, apscheduler, pillow, astral, httpx
- [x] `requirements-dev.txt` — pytest, pytest-asyncio, pytest-cov, httpx (test client), ruff, mypy
- [x] `app/config.py` — Pydantic `BaseSettings` for all env vars including `THUMBNAILS_PATH`, `FFMPEG_THREADS`, `FFMPEG_TIMEOUT_SECONDS`, `LATITUDE`, `LONGITUDE`, `PROTECT_VERIFY_SSL`
- [x] `app/database.py`
  - [x] `get_connection()` with WAL pragmas (`foreign_keys`, `journal_mode=WAL`, `synchronous=NORMAL`)
  - [x] `init_database()` — all 6 tables: `settings`, `projects`, `frames`, `renders`, `project_templates`, `notifications`
  - [x] All indexes (see §4.2)
  - [x] Migration system via `PRAGMA user_version` (see §4.4)
  - [x] Zombie render recovery on startup (set `rendering` → `pending`, delete partial output file)
- [x] `app/protect.py` — `ProtectClientManager` singleton with `asyncio.Lock`, lazy init, `setup()` + `get_client()`, auto-reconnect on stale connection
- [x] `tests/conftest.py` — `tmp_db` fixture (tempfile SQLite + `init_database()`), `client` fixture (TestClient with stubbed lifespan, no real NVR, no workers)

---

## Phase 2 — API & Live Capture

> Spec ref: [§5.2 Capture Worker](project-design.md), [§8.2 API endpoints](project-design.md), [§2.2 Visibility Filtering](project-design.md)

- [x] `app/__init__.py` — FastAPI factory with `lifespan`:
  - [x] Startup: storage dir creation (`/data/frames`, `/data/thumbs`, `/data/renders`), disk check, `init_database()`, NVR connect (non-fatal), scheduler boot, render worker mount, WebSocket manager init
  - [x] Shutdown: pause scheduler, graceful ffmpeg SIGINT/SIGKILL (30s timeout), close WS clients, close NVR session, close DB
- [x] `app/routes/health.py` — `GET /api/health` (NVR connected, disk free/total GB, active projects, pending renders)
- [x] `app/routes/cameras.py` — `GET /api/cameras`, `GET /api/cameras/{id}/preview` (proxy live snapshot)
- [x] `app/routes/projects.py` — full CRUD:
  - [x] `GET /api/projects` (with `frame_count`, `status`, `consecutive_failures`)
  - [x] `POST /api/projects` — create dirs, add APScheduler job (`project_{id}`), or trigger historical extraction task
  - [x] `GET /api/projects/{id}`
  - [x] `PUT /api/projects/{id}` — update settings, reschedule APScheduler job
  - [x] `DELETE /api/projects/{id}` — `shutil.rmtree` dirs before SQL delete
- [x] `app/thumbnails.py` — `generate_thumbnail(image_bytes) -> bytes` (Pillow, 320px wide, quality=60)
- [x] `app/capture.py`
  - [x] `AsyncIOScheduler` instance, job ID format: `project_{id}`
  - [x] On startup: query `status='active'` projects, register all jobs
  - [x] `snapshot_worker(project_id)` full pipeline:
    1. Disk failsafe check (`shutil.disk_usage`) — pause all + webhook on breach
    2. Astronomical filter (`astral`) if `capture_mode='daylight_only'`
    3. Schedule window check if `capture_mode='schedule'` (UTC→local, weekday + time window)
    4. NVR call with `try/except (httpx.ReadTimeout, NvrError)` — increment `consecutive_failures` on failure, reset on success
    5. Luminance check (`Pillow` `ImageStat`) if `use_luminance_check=1` — set `is_dark`
    6. Save full-res JPEG to `/data/frames/{project_id}/{timestamp_utc}.jpg`
    7. Save thumbnail via `app/thumbnails.py` to `/data/thumbs/{project_id}/{timestamp_utc}.jpg`
    8. INSERT into `frames`, UPDATE `projects.frame_count += 1`
    9. Check `max_frames` — set `status='completed'` and remove scheduler job if reached
- [x] `app/routes/templates.py` — `GET/POST /api/templates`, `DELETE /api/templates/{id}`, `POST /api/templates/{id}/apply`
- [x] Tests for health, cameras, projects CRUD, capture worker (monkeypatched NVR + disk)

---

## Phase 3 — Frontend Shell & Real-Time Layer

> Spec ref: [§9 Frontend](project-design.md), [§2.12 WebSocket](project-design.md), [§9.3A Dashboard](project-design.md)

- [x] `package.json` — `@tailwindcss/cli` dependency
- [x] `static/app.css.src` — Tailwind v4 source with dark mode variant: `@custom-variant dark (&:where(.dark, .dark *))`
- [x] `Dockerfile` — multi-stage: `node:22-slim` CSS build → `python:3.12-slim` runtime with ffmpeg + libmagic
- [x] `app/websocket.py` — `ConnectionManager` (connected client set), `broadcast(event, payload)`, `/api/ws` endpoint
- [x] Wire `snapshot_worker` to broadcast `capture_event` and `disk_update` after each successful capture
- [x] `templates/index.html` — single HTML shell:
  - [x] `<script src="/static/app.js" defer>` before Alpine CDN in `<head>`
  - [x] Top nav: notification bell with unread badge
  - [x] View containers controlled by Alpine `x-show`
- [x] `static/app.js` — `timelapseApp()` Alpine data function:
  - [x] State: `view`, `projects`, `cameras`, `activeProject`, `templates`, `notifications`, `unreadCount`, `ws`, `diskSpace`
  - [x] WebSocket connect with exponential backoff reconnect (1s→2s→4s→30s max), 3-attempt fallback to polling
  - [x] WS event dispatch: update projects, renders, notifications, diskSpace in real-time
  - [x] HTTP polling fallback (30s dashboard, 2s active render)
  - [x] Toast notification component
- [x] **Dashboard view:**
  - [x] Camera-grouped project cards (collapsible sections, errors expanded by default)
  - [x] Status dots: green pulsing (active), yellow (paused), red (paused_error/error), blue (completed)
  - [x] Quick-action buttons: pause/resume, delete (with confirmation)
  - [x] Disk usage progress bar (red when approaching threshold)
  - [x] Notification dropdown with event list, click-to-navigate
  - [x] Onboarding empty state when no projects exist
- [x] **Create/Edit Project view:**
  - [x] Template dropdown → pre-fill form, "Save as template" button
  - [x] Camera picker with live FoV preview (poll `/api/cameras/{id}/preview` every 5s)
  - [x] Interval presets: 5s (Weather), 1m (Construction), 10m (Long-term)
  - [x] Capture mode toggles: Daylight Only, Schedule (time pickers + weekday buttons, default Mon-Fri 07:00-17:00), Luminance Check
  - [x] Live vs Historical project type selection
- [x] Keyboard shortcuts: `N` (create), `Esc` (back to dashboard), `?` (help overlay)
- [~] Tests for WebSocket events, dashboard API integration (deferred — WS integration tests require a running ASGI server)

---

## Phase 4 — Rendering & Video Pipeline

> Spec ref: [§5.3 Render Worker](project-design.md), [§6 FFmpeg specs](project-design.md), [§2.1B Historical extraction](project-design.md), [§7.3 Notifications](project-design.md)

- [x] `app/render.py` — sequential render queue loop:
  - [x] Poll `renders WHERE status='pending' ORDER BY created_at ASC LIMIT 1`
  - [x] Lock row: `UPDATE renders SET status='rendering'`
  - [x] Build concat demuxer file at `/tmp/render_{id}.txt`
  - [x] Standard render (raw JPEGs → MP4): deflicker + `-pix_fmt yuv420p` + `-crf 23 -preset fast`
  - [x] Timestamp burn-in if `settings.timestamp_burn_in=1` (epoch from first frame, `drawtext` after deflicker)
  - [x] Range render: filter frames by `range_start`/`range_end`
  - [x] Rollup render: concat existing daily MP4s (`-c copy` for 1:1, skip filter for speed-adjusted)
  - [x] Frame skipping math: `skip_factor = ceil(total_frames / target_frames)`, `select='not(mod(n,X))',setpts=N/FRAME_RATE/TB`
  - [x] Progress monitoring: parse `frame=\s*(\d+)` from stderr, update `renders.progress_pct` (throttled 1/s)
  - [x] 2-hour timeout via `asyncio.wait_for()` — SIGKILL on timeout
  - [x] `finally:` cleanup of `/tmp/render_{id}.txt` and any temp chunks
  - [x] Broadcast `render_progress` and `render_complete` WebSocket events
- [x] `app/routes/renders.py`:
  - [x] `POST /api/renders` — validate, estimate duration/size, insert pending row
  - [x] `GET /api/renders/{id}/status` — polling fallback endpoint
  - [x] `GET /api/projects/{id}/renders` — list with labels for comparison
  - [x] `DELETE /api/renders/{id}` — delete row + output file
- [x] `app/routes/frames.py`:
  - [x] `GET /api/projects/{id}/frames` — paginated, `?fields=id,captured_at` for lightweight scrubber index
  - [x] `GET /api/projects/{id}/frames/{frame_id}/thumbnail` — serve from thumbs dir
  - [x] `GET /api/projects/{id}/frames/{frame_id}/full` — serve full JPEG
  - [x] `PUT /api/projects/{id}/frames/{frame_id}/bookmark` — set/clear `bookmark_note`
  - [x] `GET /api/projects/{id}/frames/bookmarks` — bookmarked frames only
  - [x] `GET /api/projects/{id}/frames/dark` — `is_dark=1` frames with brightness values
  - [x] `GET /api/projects/{id}/frames/export` — StreamingResponse `.zip` of all frames
  - [x] `GET /api/projects/{id}/stats/daily` — daily counts for heatmap
  - [x] `GET /api/projects/{id}/stats/timeline` — hourly captured/dark/missed counts
- [x] Historical extraction in `app/capture.py`:
  - [x] On `POST /api/projects` with `project_type='historical'`: launch background task
  - [x] Chunk NVR video into 1-hour segments via `camera.get_video(start, end)`
  - [x] Run ffmpeg: extract frames at `fps=1/{interval_seconds}`, `-q:v 2`, sequential numbering
  - [x] Rename extracted files to UTC timestamp filenames
  - [x] Generate thumbnails for all extracted frames
  - [x] `finally:` delete temp `.mp4` chunk
  - [x] Set `status='completed'` when all chunks processed
- [x] `app/notifications.py`:
  - [x] `notify(event, level, message, project_id=None, details=None)` — write to `notifications` table first, then fire webhook POST (`httpx.AsyncClient`, 10s timeout, fire-and-forget)
  - [x] Wire into capture worker: disk breach, NVR offline (3+ consecutive failures), `max_frames` reached
  - [x] Wire into render worker: render error, render complete
- [x] `app/routes/notifications.py` — `GET /api/notifications`, `PUT /api/notifications/read`
- [x] `app/routes/settings.py` — `GET /api/settings`, `PUT /api/settings`
- [x] Tests for render worker (mock ffmpeg subprocess), frame routes, notification delivery

---

## Phase 5 — Advanced UI & Polish

> Spec ref: [§5.4 Maintenance Worker](project-design.md), [§9.3B Project Detail](project-design.md), [§2.6 Thumbnails](project-design.md), [§2.9–2.11 Bookmarks/Comparison/Schedules](project-design.md)

- [x] `app/maintenance.py` — daily `CronTrigger` at 02:00 local:
  - [x] Frame retention pruning: delete files + DB rows for projects with `retention_days > 0`
  - [x] Recalculate `projects.frame_count` via `COUNT(*)` to fix drift
  - [x] Auto-render rolling window pruning (keep 7 daily, 4 weekly, 3 monthly renders — window function query)
  - [x] Auto-render scheduling: insert pending renders for yesterday's frames (daily), last week (weekly), last month (monthly) — skip if already exists
- [x] **Project Detail view (tabbed):**
  - [x] **Overview tab:**
    - [x] GitHub-style capture heatmap (emerald intensity scale, clickable cells navigate scrubber)
    - [x] Hourly capture timeline bar chart (green=captured, gray=has dark frames)
    - [x] Frame scrubber: lightweight index fetch (`?fields=id,captured_at&limit=500`), thumbnail load on slider move, timestamp display
    - [x] Shift+drag range selection on scrubber → context menu: "Render this range" / "Compare endpoints"
    - [x] Render estimate display below render button ("~Xs video · N frames")
  - [x] **Renders tab:**
    - [x] Render history list (label, type, status badge, file size, created at)
    - [x] Play button with native `<video>` player (`controls preload="metadata" loop muted playsinline`)
    - [x] Download and Delete buttons
    - [x] Render comparison: side-by-side synchronized `<video>` players (shared play/pause/seek, labels above)
    - [x] Playback speed overlay: 0.5×, 1×, 2×, 4× (set `video.playbackRate`)
  - [x] **Bookmarks tab:** scrollable gallery of bookmarked frames, click scrolls scrubber to frame
  - [x] **Dark frames tab:** thumbnail gallery with brightness values, threshold calibration slider (live `COUNT(*)` preview)
- [x] Side-by-side frame comparison: CSS split-view with draggable center divider, timestamps below each frame (Shift+click two frames or select two bookmarks)
- [x] Remaining keyboard shortcuts: `Space` (play/pause video), `←/→` (frame step), `Shift+←/→` (10-frame jump), `R` (trigger render), `B` (bookmark frame), `1–4` (playback speed)
- [x] **Settings view:** template management (rename/delete), notification history with level filtering (info/warning/error)

---

## Phase 6 — Packaging & CI/CD

> Spec ref: [§10.2 Docker](project-design.md), [§10.4 CI Pipeline](project-design.md)

- [x] Finalize `Dockerfile` (multi-stage, healthcheck, no dev deps in runtime layer)
- [x] `.dockerignore`
- [x] `.github/workflows/main.yml`:
  - [x] `lint` job: `ruff check .` + `ruff format --check .`
  - [x] `typecheck` job: `mypy app/`
  - [x] `test` job: `pytest --cov=app` (80% coverage gate)
  - [x] `scan-source` job: trivy filesystem scan
  - [x] `build` job: Docker build (depends on lint + typecheck + test + scan-source)
  - [x] `release` job: semantic versioning via `PaulHatch/semantic-version`, multi-arch image (amd64+arm64), GitHub Release with auto-generated notes, `cleanup.yml` for weekly old-image pruning
  - [x] `scan-image` job: trivy image scan after push
- [~] Verify clean shutdown: `SIGTERM` → scheduler pause → ffmpeg SIGINT → 30s wait → SIGKILL → close WS → close NVR → close DB (implemented in lifespan, not integration-tested)
- [x] K3s deployment YAML: `k8s/deployment.yaml` — Namespace, PVC (50Gi), Secret, Deployment (Recreate, resource limits, probes), Service, Traefik Ingress

---

## Completion Checklist

- [x] All routes return correct HTTP status codes (201 on create, 404 on missing, 409 on conflict, 503 on NVR/disk unavailable)
- [x] All error responses use `{"detail": "..."}` format
- [x] `pix_fmt yuv420p` enforced on all ffmpeg encodes (Safari/iOS compatibility)
- [x] No `os.getenv()` outside `app/config.py`
- [x] No `sqlite3.connect()` outside `app/database.py`
- [x] `static/app.css` never manually edited (excluded from git via `.gitignore`)
- [x] No CORS middleware added
- [x] Full test suite passes with ≥80% coverage (80.44%, 88 tests)
- [~] Docker image builds and healthcheck passes (not locally verified — CI pipeline will validate)
