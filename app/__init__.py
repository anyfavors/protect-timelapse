"""
FastAPI application factory.
Lifespan manages: storage dirs, DB init, NVR connection, APScheduler, render worker.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse

from app.config import get_settings
from app.database import init_database
from app.protect import protect_manager
from app.routes import (
    cameras,
    frames,
    health,
    maintenance,
    metrics,
    notifications,
    presets,
    projects,
    renders,
    settings,
    templates,
)
from app.websocket import router as ws_router

log = logging.getLogger("app")

_CSP = (
    "default-src 'self'; "
    # Alpine.js requires 'unsafe-eval' (expression parser) and 'unsafe-inline'
    # (inline event handlers and x-show style injection)
    "script-src 'self' 'unsafe-eval' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' wss:; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'"
)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):  # type: ignore[override]
        response: StarletteResponse = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = _CSP
        # Add Vary for all compressible responses so caches key correctly (CS2)
        ct = response.headers.get("Content-Type", "")
        if any(ct.startswith(t) for t in ("text/", "application/json", "application/javascript")):
            response.headers.setdefault("Vary", "Accept-Encoding")
        return response


@asynccontextmanager
async def lifespan(application: FastAPI):  # type: ignore[type-arg]
    # ------------------------------------------------------------------ #
    # STARTUP                                                              #
    # ------------------------------------------------------------------ #
    cfg = get_settings()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # 1. Ensure storage directories exist
    for path in (cfg.frames_path, cfg.thumbnails_path, cfg.renders_path):
        os.makedirs(path, exist_ok=True)

    # 2. Initialise database (create tables, run migrations, recover zombies)
    init_database()

    # 3. Connect to NVR (non-fatal)
    await protect_manager.setup()

    # 4. Start capture scheduler + register maintenance cron
    from app.capture import scheduler, start_scheduler
    from app.maintenance import register_maintenance_job

    await start_scheduler()
    register_maintenance_job(scheduler)

    # 5. Start render worker
    from app.render import start_render_worker, stop_render_worker

    render_task = await start_render_worker()

    log.info("Protect Timelapse started")

    yield  # application is running

    # ------------------------------------------------------------------ #
    # SHUTDOWN                                                             #
    # ------------------------------------------------------------------ #
    log.info("Shutting down…")

    from app.capture import stop_scheduler

    await stop_scheduler()

    try:
        await stop_render_worker(render_task)
    except Exception as exc:
        log.warning("Render worker shutdown error (ignored): %s", exc)  # (#30)

    from app.websocket import manager as ws_manager

    await ws_manager.close_all()

    await protect_manager.teardown()
    log.info("Shutdown complete")


def create_app() -> FastAPI:
    application = FastAPI(title="Protect Timelapse", lifespan=lifespan)
    application.add_middleware(GZipMiddleware, minimum_size=500)

    # Security headers on every response (SH1, CSP)
    application.add_middleware(_SecurityHeadersMiddleware)

    # Attach rate limiter state (slowapi)
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from app.limiter import limiter

    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    application.include_router(health.router)
    application.include_router(cameras.router)
    application.include_router(projects.router)
    application.include_router(frames.router)
    application.include_router(renders.router)
    application.include_router(templates.router)
    application.include_router(notifications.router)
    application.include_router(settings.router)
    application.include_router(presets.router)
    application.include_router(maintenance.router)
    application.include_router(metrics.router)
    application.include_router(ws_router)

    # Serve compiled CSS + app.js
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    if os.path.isdir(static_dir):
        application.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Serve the SPA shell with cache-busting query strings on static assets
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
    if os.path.isdir(templates_dir):
        import hashlib

        from fastapi.templating import Jinja2Templates
        from starlette.requests import Request

        _jinja = Jinja2Templates(directory=templates_dir)

        def _file_hash(path: str) -> str:
            """Return first 8 hex chars of the SHA-256 of a file, or 'dev' if missing."""
            try:
                with open(path, "rb") as fh:
                    return hashlib.sha256(fh.read()).hexdigest()[:8]
            except OSError:
                return "dev"

        _js_hash = _file_hash(os.path.join(static_dir, "app.js"))
        _css_hash = _file_hash(os.path.join(static_dir, "app.css"))

        @application.get("/", include_in_schema=False)
        async def serve_spa(request: Request):  # type: ignore[return]
            # no-cache: always revalidate so fresh asset hashes are picked up after deploys (CS1)
            return _jinja.TemplateResponse(
                request,
                "index.html",
                {"js_hash": _js_hash, "css_hash": _css_hash},
                headers={"Cache-Control": "no-cache"},
            )

    return application


app = create_app()
