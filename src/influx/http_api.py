"""FastAPI routes for ``/live``, ``/ready``, and ``/status``.

All three endpoints read cached state from the probe loop, coordinator,
and scheduler — they MUST NOT issue fresh probes per request (FR-HTTP-7).

Later user stories (US-005, US-006) extend this module with
``POST /runs`` and ``POST /backfills``.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import influx

router = APIRouter()


@router.get("/live")
async def live() -> JSONResponse:
    """Liveness probe — ``200 OK`` while the process is alive (FR-HTTP-1)."""
    return JSONResponse({"live": True})


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness probe — ``200`` when cached probes pass, ``503`` otherwise.

    Reads the cached probe state from the :class:`~influx.probes.ProbeLoop`
    stored on ``app.state``; never triggers a fresh probe (FR-HTTP-2,
    FR-HTTP-7).
    """
    from influx.probes import ProbeLoop

    probe_loop: ProbeLoop = request.app.state.probe_loop
    state = probe_loop.state
    if state.is_ready:
        return JSONResponse({"ready": True}, status_code=200)
    return JSONResponse({"ready": False}, status_code=503)


@router.get("/status")
async def status(request: Request) -> JSONResponse:
    """Detailed operator-facing status — always ``200 OK`` (FR-HTTP-3).

    Returns a JSON body satisfying the §14.4 status contract of
    ``docs/REQUIREMENTS.md``.
    """
    from influx.config import AppConfig
    from influx.coordinator import Coordinator
    from influx.probes import ProbeLoop
    from influx.scheduler import InfluxScheduler

    probe_loop: ProbeLoop = request.app.state.probe_loop
    scheduler: InfluxScheduler = request.app.state.scheduler
    coordinator: Coordinator = request.app.state.coordinator
    config: AppConfig = request.app.state.config

    probe_state = probe_loop.state

    # Build per-profile status.
    profiles: dict[str, Any] = {}
    job_map = {j.id: j for j in scheduler.jobs}
    for profile in config.profiles:
        job_id = f"profile-{profile.name}"
        job = job_map.get(job_id)
        next_run: str | None = None
        if job is not None and job.next_run_time is not None:
            next_run = job.next_run_time.astimezone(UTC).isoformat()

        profiles[profile.name] = {
            "scheduled": job is not None,
            "currently_running": coordinator.is_busy(profile.name),
            "next_run_at": next_run,
            "last_run_at": None,
            "last_run_status": None,
        }

    body: dict[str, Any] = {
        "status": probe_state.overall_status,
        "ready": probe_state.is_ready,
        "version": influx.__version__,
        "dependencies": {
            "lithos": {
                "status": probe_state.lithos.status,
            },
            "llm_credentials": {
                "status": probe_state.llm_credentials.status,
            },
        },
        "profiles": profiles,
    }
    return JSONResponse(body, status_code=200)
