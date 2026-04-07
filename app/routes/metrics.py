"""Prometheus metrics endpoint."""

import contextlib
import shutil

from fastapi import APIRouter
from fastapi.responses import Response

from app.database import get_connection
from app.protect import protect_manager

router = APIRouter(tags=["metrics"])

# Import prometheus_client lazily to avoid import errors if not installed
try:
    from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest

    _render_queue_depth = Gauge(
        "timelapse_render_queue_depth", "Number of pending renders in queue"
    )
    _disk_free_gb = Gauge("timelapse_disk_free_gb", "Free disk space on /data in GB")
    _nvr_connected = Gauge("timelapse_nvr_connected", "NVR connection state (1=connected)")
    _active_projects = Gauge("timelapse_active_projects", "Number of active capture projects")
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint — returns metrics in text/plain exposition format."""
    if not _PROMETHEUS_AVAILABLE:
        return Response(
            content="# prometheus-client not installed\n",
            media_type="text/plain",
            status_code=200,
        )

    # Update gauges
    with contextlib.suppress(Exception):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM renders WHERE status IN ('pending', 'rendering')"
            ).fetchone()
        _render_queue_depth.set(row["c"] if row else 0)

    with contextlib.suppress(Exception):
        usage = shutil.disk_usage("/data")
        _disk_free_gb.set(round(usage.free / 1024**3, 2))

    with contextlib.suppress(Exception):
        _nvr_connected.set(1 if protect_manager.is_connected else 0)

    with contextlib.suppress(Exception):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM projects WHERE status = 'active'"
            ).fetchone()
        _active_projects.set(row["c"] if row else 0)

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
