"""FastAPI routes for the Influx admin HTTP API.

Health endpoints (``/live``, ``/ready``, ``/status``) read cached state
from the probe loop, coordinator, and scheduler — they MUST NOT issue
fresh probes per request (FR-HTTP-7).

``POST /runs`` accepts manual run requests with coordinator-based
overlap protection (FR-HTTP-4, FR-SCHED-3).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator

import influx
from influx.coordinator import Coordinator, ProfileBusyError, RunKind
from influx.scheduler import run_profile

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


# ── POST /runs ──────────────────────────────────────────────────────


class RunRequest(BaseModel):
    """Request body for ``POST /runs`` (FR-HTTP-4)."""

    profile: str | None = None
    all_profiles: bool | None = None

    @model_validator(mode="after")
    def _exactly_one_scope(self) -> RunRequest:
        has_profile = self.profile is not None
        has_all = self.all_profiles is not None and self.all_profiles
        if has_profile and has_all:
            raise ValueError(
                "Supply exactly one of 'profile' or 'all_profiles', not both"
            )
        if not has_profile and not has_all:
            raise ValueError(
                "Supply exactly one of 'profile' or 'all_profiles'"
            )
        return self


@router.post("/runs")
async def post_runs(body: RunRequest, request: Request) -> JSONResponse:
    """Accept a manual run request (FR-HTTP-4).

    Acquires the per-profile lock via the coordinator.  Returns ``202``
    on acceptance or ``409 Conflict`` with ``reason="profile_busy"``
    when the profile is already running.

    Multi-profile (``all_profiles``) fan-out of ``run_profile()``
    execution is deferred to PRD 09.
    """
    from influx.config import AppConfig

    coordinator: Coordinator = request.app.state.coordinator
    config: AppConfig = request.app.state.config

    request_id = str(uuid.uuid4())
    submitted_at = datetime.now(UTC).isoformat()

    if body.profile is not None:
        # Validate the profile name exists in config.
        known = {p.name for p in config.profiles}
        if body.profile not in known:
            return JSONResponse(
                {"detail": f"Unknown profile: {body.profile!r}"},
                status_code=422,
            )

        try:
            acquired = await coordinator.try_acquire(body.profile)
            if not acquired:
                raise ProfileBusyError(body.profile)
        except ProfileBusyError:
            return JSONResponse(
                {"reason": "profile_busy", "profile": body.profile},
                status_code=409,
            )

        # Launch the run in the background so the response returns immediately.
        asyncio.get_event_loop().create_task(
            _run_and_release(coordinator, body.profile, RunKind.MANUAL)
        )

        return JSONResponse(
            {
                "status": "accepted",
                "request_id": request_id,
                "kind": "manual",
                "scope": body.profile,
                "submitted_at": submitted_at,
            },
            status_code=202,
        )

    # all_profiles path — accept but defer multi-profile execution to PRD 09.
    return JSONResponse(
        {
            "status": "accepted",
            "request_id": request_id,
            "kind": "manual",
            "scope": "all",
            "submitted_at": submitted_at,
        },
        status_code=202,
    )


async def _run_and_release(
    coordinator: Coordinator, profile: str, kind: RunKind
) -> None:
    """Run ``run_profile`` and release the coordinator lock afterward."""
    try:
        await run_profile(profile, kind)
    finally:
        coordinator.release(profile)
