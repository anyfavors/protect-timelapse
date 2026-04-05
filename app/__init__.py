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

from app.config import get_settings
from app.database import init_database
from app.protect import protect_manager
from app.routes import (
    cameras,
    frames,
    health,
    notifications,
    projects,
    renders,
    settings,
    templates,
)
from app.websocket import router as ws_router

log = logging.getLogger("app")


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

    await stop_render_worker(render_task)

    from app.websocket import manager as ws_manager

    await ws_manager.close_all()

    await protect_manager.teardown()
    log.info("Shutdown complete")


def create_app() -> FastAPI:
    application = FastAPI(title="Protect Timelapse", lifespan=lifespan)
    application.add_middleware(GZipMiddleware, minimum_size=500)

    application.include_router(health.router)
    application.include_router(cameras.router)
    application.include_router(projects.router)
    application.include_router(frames.router)
    application.include_router(renders.router)
    application.include_router(templates.router)
    application.include_router(notifications.router)
    application.include_router(settings.router)
    application.include_router(ws_router)

    # Serve compiled CSS + app.js
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    if os.path.isdir(static_dir):
        application.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Serve the SPA shell
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
    if os.path.isdir(templates_dir):
        from fastapi.responses import FileResponse

        @application.get("/", include_in_schema=False)
        async def serve_spa() -> FileResponse:
            return FileResponse(os.path.join(templates_dir, "index.html"))

    return application


app = create_app()
