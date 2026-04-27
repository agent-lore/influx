"""FastAPI routes for the Influx admin HTTP API.

Health endpoints (``/live``, ``/ready``, ``/status``) read cached state
from the probe loop, coordinator, and scheduler — they MUST NOT issue
fresh probes per request (FR-HTTP-7).

``POST /runs`` accepts manual run requests with coordinator-based
overlap protection (FR-HTTP-4, FR-SCHED-3).

``POST /backfills`` accepts backfill requests with a naive estimator
and confirm-required flow (FR-HTTP-5, FR-BF-6).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Coroutine
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

import influx
from influx.config import AppConfig
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
    from influx.probes import ProbeLoop
    from influx.scheduler import InfluxScheduler

    probe_loop: ProbeLoop = request.app.state.probe_loop
    scheduler: InfluxScheduler = request.app.state.scheduler
    coordinator: Coordinator = request.app.state.coordinator
    config: AppConfig = request.app.state.config

    probe_state = probe_loop.state

    # Build per-profile status.  After review finding 1, all profiles
    # fire from a single ``influx-tick`` dispatcher job so per-profile
    # ``next_run_at`` is sourced from that shared trigger.
    profiles: dict[str, Any] = {}
    job_map = {j.id: j for j in scheduler.jobs}
    tick_job = job_map.get("influx-tick")
    next_run: str | None = None
    if tick_job is not None and tick_job.next_run_time is not None:
        next_run = tick_job.next_run_time.astimezone(UTC).isoformat()
    for profile in config.profiles:
        profiles[profile.name] = {
            "scheduled": tick_job is not None,
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
            _run_and_release(
                coordinator,
                body.profile,
                RunKind.MANUAL,
                config=config,
                item_provider=getattr(request.app.state, "item_provider", None),
                probe_loop=getattr(request.app.state, "probe_loop", None),
                fetch_cache=getattr(request.app.state, "fetch_cache", None),
            ),
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

    acquired_profiles: list[str] = []
    for profile_cfg in config.profiles:
        acquired = await coordinator.try_acquire(profile_cfg.name)
        if not acquired:
            for acquired_profile in acquired_profiles:
                coordinator.release(acquired_profile)
            return JSONResponse(
                {"reason": "profile_busy", "profile": profile_cfg.name},
                status_code=409,
            )
        acquired_profiles.append(profile_cfg.name)

    _spawn_tracked_task(
        request.app,
        _run_many_and_release(
            coordinator,
            acquired_profiles,
            RunKind.MANUAL,
            config=config,
            item_provider=getattr(request.app.state, "item_provider", None),
            probe_loop=getattr(request.app.state, "probe_loop", None),
            fetch_cache=getattr(request.app.state, "fetch_cache", None),
        ),
    )
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
    *,
    config: Any = None,
    item_provider: Any = None,
    probe_loop: Any = None,
    fetch_cache: Any = None,
) -> None:
    """Run ``run_profile`` and release the coordinator lock afterward.

    Failures in ``run_profile`` (e.g. Lithos unreachable per AC-M1-11)
    are logged and swallowed so that the manual-run lock is released
    cleanly and the service stays alive (FR-HTTP-4 + AC-M1-11).
    """
    if fetch_cache is not None:
        fetch_cache.begin_fire()
    try:
        try:
            await run_profile(
                profile,
                kind,
                run_range=run_range,
                config=config,
                item_provider=item_provider,
                probe_loop=probe_loop,
            )
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "run_profile %r aborted",
                profile,
                exc_info=True,
            )
    finally:
        coordinator.release(profile)
        if fetch_cache is not None:
            fetch_cache.end_fire()


async def _backfill_and_release(
    coordinator: Coordinator,
    profile: str,
    run_range: dict[str, str | int],
    *,
    config: Any = None,
    item_provider: Any = None,
    probe_loop: Any = None,
    fetch_cache: Any = None,
) -> None:
    """Run ``backfill.run_backfill`` and release the coordinator lock afterward.

    Analogous to :func:`_run_and_release` but routes through the
    :mod:`influx.backfill` module so that backfill-specific logic
    (cache-hit skip, pacing) is exercised end-to-end (US-009).
    """
    from influx.backfill import run_backfill

    if fetch_cache is not None:
        fetch_cache.begin_fire()
    try:
        try:
            await run_backfill(
                profile,
                run_range=run_range,
                config=config,
                item_provider=item_provider,
                probe_loop=probe_loop,
            )
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "run_backfill %r aborted",
                profile,
                exc_info=True,
            )
    finally:
        coordinator.release(profile)
        if fetch_cache is not None:
            fetch_cache.end_fire()


async def _run_many_and_release(
    coordinator: Coordinator,
    profiles: list[str],
    kind: RunKind,
    run_range: dict[str, str | int] | None = None,
    *,
    config: Any = None,
    item_provider: Any = None,
    probe_loop: Any = None,
    fetch_cache: Any = None,
) -> None:
    """Run several already-acquired profiles and release all locks."""
    if fetch_cache is not None:
        fetch_cache.begin_fire()
    try:
        await asyncio.gather(
            *(
                run_profile(
                    profile,
                    kind,
                    run_range=run_range,
                    config=config,
                    item_provider=item_provider,
                    probe_loop=probe_loop,
                )
                for profile in profiles
            ),
            return_exceptions=True,
        )
    finally:
        for profile in profiles:
            coordinator.release(profile)
        if fetch_cache is not None:
            fetch_cache.end_fire()


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

# Override ``_backfill_estimate_override`` in tests to force a specific
# estimate value (monkeypatch), or export ``INFLUX_TEST_BACKFILL_ESTIMATE``
# for subprocess-based CLI tests (AC-M3-8).
_backfill_estimate_override: int | None = None


def _compute_days(
    days: int | None,
    date_from: str | None,
    date_to: str | None,
) -> int:
    """Return the number of days for the backfill range.

    Either ``days`` is provided directly (``--days N``), or
    ``date_from``/``date_to`` are provided (``--from/--to``).
    For the date-range form, returns ``(to - from).days``.
    """
    if days is not None:
        return days
    assert date_from is not None and date_to is not None
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    return max((d_to - d_from).days, 0)


def estimate_backfill_items(
    profile: str,
    config: AppConfig,
    *,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """Return the estimated number of items a backfill would produce.

    Uses the naive formula ``days × len(categories) × max_results_per_category``
    (Q-3, FR-BF-6).

    Test overrides:
    - Set :data:`_backfill_estimate_override` (monkeypatch) to force a value.
    - Export ``INFLUX_TEST_BACKFILL_ESTIMATE=<int>`` for subprocess tests.
    """
    env_override = os.environ.get("INFLUX_TEST_BACKFILL_ESTIMATE")
    if env_override is not None:
        try:
            return int(env_override)
        except ValueError:
            pass
    if _backfill_estimate_override is not None:
        return _backfill_estimate_override

    n_days = _compute_days(days, date_from, date_to)

    profile_cfg = next(
        (p for p in config.profiles if p.name == profile),
        None,
    )
    if profile_cfg is None:
        return 0

    arxiv = profile_cfg.sources.arxiv
    n_categories = len(arxiv.categories)
    max_results = arxiv.max_results_per_category

    return n_days * n_categories * max_results


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
    """Accept a backfill request (FR-HTTP-5, FR-BF-6).

    Uses the naive estimator ``days × categories × max_results`` (Q-3).
    When the estimate exceeds 1000 and ``confirm`` is not truthy, returns
    ``400`` with ``reason="confirm_required"`` so the CLI can reprompt
    the operator.

    Backfills go through the same coordinator as scheduled and manual
    runs, ensuring non-overlap for the same profile (AC-M3-7).
    """
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

    # Naive estimator: days × categories × max_results (Q-3, FR-BF-6).
    if body.profile is None:
        estimated = sum(
            estimate_backfill_items(
                profile_cfg.name,
                config,
                days=body.days,
                date_from=body.date_from,
                date_to=body.date_to,
            )
            for profile_cfg in config.profiles
        )
    else:
        estimated = estimate_backfill_items(
            scope,
            config,
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

        # Launch the backfill in the background via backfill.run_backfill.
        _spawn_tracked_task(
            request.app,
            _backfill_and_release(
                coordinator,
                body.profile,
                run_range,
                config=config,
                item_provider=getattr(request.app.state, "item_provider", None),
                probe_loop=getattr(request.app.state, "probe_loop", None),
                fetch_cache=getattr(request.app.state, "fetch_cache", None),
            ),
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

    acquired_profiles = []
    for profile_cfg in config.profiles:
        acquired = await coordinator.try_acquire(profile_cfg.name)
        if not acquired:
            for acquired_profile in acquired_profiles:
                coordinator.release(acquired_profile)
            return JSONResponse(
                {"reason": "profile_busy", "profile": profile_cfg.name},
                status_code=409,
            )
        acquired_profiles.append(profile_cfg.name)

    _spawn_tracked_task(
        request.app,
        _run_many_and_release(
            coordinator,
            acquired_profiles,
            RunKind.BACKFILL,
            run_range=run_range,
            config=config,
            item_provider=getattr(request.app.state, "item_provider", None),
            probe_loop=getattr(request.app.state, "probe_loop", None),
            fetch_cache=getattr(request.app.state, "fetch_cache", None),
        ),
    )
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
