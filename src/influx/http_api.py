"""FastAPI routes for the Influx admin HTTP API.

Health endpoints (``/live``, ``/ready``, ``/status``) read cached state
from the probe loop, coordinator, and scheduler — they MUST NOT issue
fresh probes per request (FR-HTTP-7).

``POST /runs`` accepts manual run requests with coordinator-based
overlap protection (FR-HTTP-4, FR-SCHED-3).

``POST /backfills`` accepts backfill requests with a stub estimator
and confirm-required flow (FR-HTTP-5).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

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
            raise ValueError("Supply exactly one of 'profile' or 'all_profiles'")
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
        _spawn_tracked_task(
            request.app,
            _run_and_release(coordinator, body.profile, RunKind.MANUAL),
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
    coordinator: Coordinator,
    profile: str,
    kind: RunKind,
    run_range: dict[str, str | int] | None = None,
) -> None:
    """Run ``run_profile`` and release the coordinator lock afterward."""
    try:
        await run_profile(profile, kind, run_range=run_range)
    finally:
        coordinator.release(profile)


def _spawn_tracked_task(
    app: FastAPI, coro: Coroutine[Any, Any, Any]
) -> asyncio.Task[Any]:
    """Create an ``asyncio.Task`` and register it on ``app.state.active_tasks``.

    The task set is consulted by :meth:`InfluxService.stop` so HTTP-triggered
    work can complete within ``schedule.shutdown_grace_seconds`` before
    the service shuts down (US-008 shutdown-grace contract).
    """
    # Preserve the existing set even when empty — ``... or set()`` would
    # treat an empty set as falsy and replace it with a throwaway local
    # set that is never written back to ``app.state``.
    active_tasks: set[asyncio.Task[Any]] | None = getattr(
        app.state, "active_tasks", None
    )
    if active_tasks is None:
        active_tasks = set()
        app.state.active_tasks = active_tasks
    task = asyncio.get_event_loop().create_task(coro)
    active_tasks.add(task)
    task.add_done_callback(active_tasks.discard)
    return task


# ── POST /backfills ────────────────────────────────────────────────

# Stub backfill estimator — returns 0 by default.  PRD 09 replaces
# the body with the naive ``days × categories × avg_results`` formula.
# Override ``_backfill_estimate_override`` in tests to force a value > 1000.
_backfill_estimate_override: int | None = None


def estimate_backfill_items(
    profile: str,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """Return the estimated number of items a backfill would produce.

    This is a **constant stub** in PRD 03.  By default it returns ``0``.
    Set :data:`_backfill_estimate_override` to force a specific value
    for same-process testing (monkeypatch), or export the env var
    ``INFLUX_TEST_BACKFILL_ESTIMATE=<int>`` to force the value when the
    server runs in a subprocess (end-to-end CLI tests for AC-M3-8).

    PRD 09 replaces the body with the ``days × categories × avg_results``
    formula.
    """
    env_override = os.environ.get("INFLUX_TEST_BACKFILL_ESTIMATE")
    if env_override is not None:
        try:
            return int(env_override)
        except ValueError:
            pass
    if _backfill_estimate_override is not None:
        return _backfill_estimate_override
    return 0


class BackfillRequest(BaseModel):
    """Request body for ``POST /backfills`` (FR-HTTP-5)."""

    profile: str | None = None
    all_profiles: bool | None = None
    days: int | None = None
    date_from: str | None = Field(None, alias="from")
    date_to: str | None = Field(None, alias="to")
    confirm: bool | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_scope_and_range(self) -> BackfillRequest:
        # Exactly one of profile / all_profiles.
        has_profile = self.profile is not None
        has_all = self.all_profiles is not None and self.all_profiles
        if has_profile and has_all:
            raise ValueError(
                "Supply exactly one of 'profile' or 'all_profiles', not both"
            )
        if not has_profile and not has_all:
            raise ValueError("Supply exactly one of 'profile' or 'all_profiles'")

        # Exactly one of days / (from, to).
        has_days = self.days is not None
        has_range = self.date_from is not None or self.date_to is not None
        if has_days and has_range:
            raise ValueError("Supply exactly one of 'days' or ('from', 'to'), not both")
        if not has_days and not has_range:
            raise ValueError("Supply exactly one of 'days' or ('from', 'to')")
        if has_range and (self.date_from is None or self.date_to is None):
            raise ValueError("Both 'from' and 'to' are required when using date range")
        return self


@router.post("/backfills")
async def post_backfills(body: BackfillRequest, request: Request) -> JSONResponse:
    """Accept a backfill request (FR-HTTP-5).

    Uses a stub estimator (replaced by PRD 09).  When the estimate
    exceeds 1000 and ``confirm`` is not truthy, returns ``400`` with
    ``reason="confirm_required"`` so the CLI can reprompt the operator.

    Backfills go through the same coordinator as scheduled and manual
    runs, ensuring non-overlap for the same profile (AC-M3-7).
    """
    from influx.config import AppConfig

    coordinator: Coordinator = request.app.state.coordinator
    config: AppConfig = request.app.state.config

    request_id = str(uuid.uuid4())
    submitted_at = datetime.now(UTC).isoformat()

    # Build the run_range dict for run_profile.
    run_range: dict[str, str | int] = {}
    if body.days is not None:
        run_range["days"] = body.days
    else:
        assert body.date_from is not None and body.date_to is not None
        run_range["from"] = body.date_from
        run_range["to"] = body.date_to

    # Determine scope label.
    scope = body.profile if body.profile is not None else "all"

    # Check the stub estimator for confirm-required flow.
    estimated = estimate_backfill_items(
        profile=scope,
        days=body.days,
        date_from=body.date_from,
        date_to=body.date_to,
    )
    if estimated > 1000 and not body.confirm:
        return JSONResponse(
            {
                "reason": "confirm_required",
                "estimated_items": estimated,
            },
            status_code=400,
        )

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

        # Launch the backfill in the background.
        _spawn_tracked_task(
            request.app,
            _run_and_release(coordinator, body.profile, RunKind.BACKFILL, run_range),
        )

        return JSONResponse(
            {
                "status": "accepted",
                "request_id": request_id,
                "kind": "backfill",
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
            "kind": "backfill",
            "scope": "all",
            "submitted_at": submitted_at,
        },
        status_code=202,
    )
