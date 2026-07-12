"""Request-orchestration collaborator for admin run / backfill requests.

The HTTP router (:mod:`influx.http_api`) does *translation*: it parses a
request body, validates it, and turns an outcome into a status code. The
*orchestration* lives here:

- acquire the per-Profile coordinator locks all-or-nothing (no partial
  fan-out — if any Profile is busy, everything acquired so far is released
  and the request is rejected),
- launch the background Run(s) so the response can return immediately,
- register each task on the shutdown-grace set so :meth:`InfluxService.stop`
  can drain it (US-008),
- release the locks in a ``finally`` so a failed Run never wedges a Profile.

A backfill is just a Run with :attr:`RunKind.BACKFILL`, so this collaborator
is kind-agnostic: manual runs and backfills share one lock lifecycle and one
fan-out path. The router chooses the kind and translates the outcome.

Because it takes plain dependencies (a coordinator, a task set) rather than a
FastAPI app, the orchestration is testable without an ASGI stack.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from influx.config import AppConfig
from influx.coordinator import Coordinator, RunKind
from influx.run_ledger import RunLedger
from influx.scheduler import run_profile

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunAccepted:
    """A dispatch was admitted; the Run(s) are executing in the background."""

    scope: str
    """The request scope label — a Profile name, or ``"all"``."""
    profiles: tuple[str, ...]
    """The Profiles whose locks were acquired, in config order."""


@dataclass(frozen=True, slots=True)
class RunRejectedBusy:
    """A dispatch was refused because a Profile lock was already held."""

    profile: str
    """The Profile found busy; nothing was launched and no lock is held."""


DispatchOutcome = RunAccepted | RunRejectedBusy


def _log_task_completion(
    log_context: dict[str, Any],
) -> Callable[[asyncio.Task[Any]], None]:
    """Build a done-callback that logs task outcome with request context (#105).

    Treats cancellation as graceful shutdown rather than failure so that
    the US-008 shutdown path stays quiet, and routes uncaught exceptions
    to ``logger.error`` with ``exc_info`` so operators can find them by
    ``request_id``.
    """

    def _callback(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "background task failed request_id=%s kind=%s scope=%s: %s",
                log_context.get("request_id"),
                log_context.get("kind"),
                log_context.get("scope"),
                exc,
                exc_info=exc,
                extra={**log_context, "status": "failed"},
            )
            return
        # Task itself succeeded — but for all-profiles fan-out a
        # partial failure surfaces via ``failed_count`` written back
        # onto the shared ``log_context`` by ``_run_many_and_release``.
        # We must NOT report ``completed`` for those requests (#105).
        failed_count = int(log_context.get("failed_count") or 0)
        if failed_count > 0:
            logger.warning(
                "background task partial failure request_id=%s kind=%s "
                "scope=%s failed=%d/%d",
                log_context.get("request_id"),
                log_context.get("kind"),
                log_context.get("scope"),
                failed_count,
                int(log_context.get("total_count") or 0),
                extra={**log_context, "status": "partial_failure"},
            )
            return
        logger.info(
            "background task completed request_id=%s kind=%s scope=%s",
            log_context.get("request_id"),
            log_context.get("kind"),
            log_context.get("scope"),
            extra={**log_context, "status": "completed"},
        )

    return _callback


class RunDispatcher:
    """Acquire Profile locks, launch background Runs, and track them.

    Holds the run dependencies resolved from the app for a single request.
    ``active_tasks`` is the same set object :meth:`InfluxService.stop`
    consults on shutdown (US-008) — the dispatcher only *adds* to it.
    """

    def __init__(
        self,
        coordinator: Coordinator,
        config: AppConfig,
        *,
        run_ledger: RunLedger,
        active_tasks: set[asyncio.Task[Any]],
        item_provider: Any = None,
        probe_loop: Any = None,
        fetch_cache: Any = None,
    ) -> None:
        self._coordinator = coordinator
        self._config = config
        self._run_ledger = run_ledger
        self._active_tasks = active_tasks
        self._item_provider = item_provider
        self._probe_loop = probe_loop
        self._fetch_cache = fetch_cache

    async def dispatch_one(
        self,
        profile: str,
        kind: RunKind,
        *,
        run_range: dict[str, str | int] | None = None,
        request_id: str,
        log_context: dict[str, Any],
    ) -> DispatchOutcome:
        """Acquire one Profile lock and launch a single background Run.

        Returns :class:`RunRejectedBusy` (holding no lock) when the Profile
        is already running, otherwise :class:`RunAccepted` once the Run has
        been launched.
        """
        if not await self._coordinator.try_acquire(profile):
            return RunRejectedBusy(profile)
        self._spawn_tracked(
            self._run_and_release(
                profile, kind, run_range=run_range, run_id=request_id
            ),
            log_context=log_context,
        )
        return RunAccepted(scope=profile, profiles=(profile,))

    async def dispatch_all(
        self,
        kind: RunKind,
        *,
        run_range: dict[str, str | int] | None = None,
        request_id: str,
        log_context: dict[str, Any],
    ) -> DispatchOutcome:
        """Acquire every configured Profile lock all-or-nothing, then fan out.

        If any Profile is busy, every lock acquired so far is released and
        the request is rejected with :class:`RunRejectedBusy` — there is no
        partial fan-out.
        """
        acquired: list[str] = []
        for profile_cfg in self._config.profiles:
            if not await self._coordinator.try_acquire(profile_cfg.name):
                for held in acquired:
                    self._coordinator.release(held)
                return RunRejectedBusy(profile_cfg.name)
            acquired.append(profile_cfg.name)
        self._spawn_tracked(
            self._run_many_and_release(
                acquired,
                kind,
                run_range=run_range,
                request_id=request_id,
                log_context=log_context,
            ),
            log_context=log_context,
        )
        return RunAccepted(scope="all", profiles=tuple(acquired))

    def _spawn_tracked(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        log_context: dict[str, Any] | None = None,
    ) -> asyncio.Task[Any]:
        """Create a task and register it on the shutdown-grace set.

        The task set is consulted by :meth:`InfluxService.stop` so
        HTTP-triggered work can complete within
        ``schedule.shutdown_grace_seconds`` before the service shuts down.
        When ``log_context`` is provided the task gets a second done-callback
        that logs request-keyed completion or failure (#105).
        """
        task = asyncio.get_event_loop().create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        if log_context is not None:
            task.add_done_callback(_log_task_completion(log_context))
        return task

    async def _run_and_release(
        self,
        profile: str,
        kind: RunKind,
        *,
        run_range: dict[str, str | int] | None = None,
        run_id: str | None = None,
    ) -> None:
        """Run ``run_profile`` and release the coordinator lock afterward.

        Failures in ``run_profile`` (e.g. Lithos unreachable per AC-M1-11)
        are logged with request context and re-raised; the surrounding
        asyncio task captures the exception and the request-level done-
        callback (#105) records a failure log keyed by ``request_id``. The
        coordinator lock is always released via ``finally`` so the service
        stays alive (FR-HTTP-4 + AC-M1-11).
        """
        if self._fetch_cache is not None:
            self._fetch_cache.begin_fire()
        try:
            try:
                await run_profile(
                    profile,
                    kind,
                    run_range=run_range,
                    config=self._config,
                    item_provider=self._item_provider,
                    probe_loop=self._probe_loop,
                    run_id=run_id,
                    run_ledger=self._run_ledger,
                )
            except Exception:
                logger.warning(
                    "run_profile %r aborted request_id=%s kind=%s",
                    profile,
                    run_id,
                    kind.value,
                    exc_info=True,
                    extra={
                        "request_id": run_id,
                        "kind": kind.value,
                        "scope": profile,
                        "profile": profile,
                        "status": "failed",
                    },
                )
                raise
        finally:
            self._coordinator.release(profile)
            if self._fetch_cache is not None:
                self._fetch_cache.end_fire()

    async def _run_many_and_release(
        self,
        profiles: list[str],
        kind: RunKind,
        *,
        run_range: dict[str, str | int] | None = None,
        request_id: str | None = None,
        log_context: dict[str, Any] | None = None,
    ) -> None:
        """Run several already-acquired profiles and release all locks.

        Per-profile failures from the underlying ``asyncio.gather`` are
        logged with request context (#105) so that multi-profile partial
        failures are visible. When ``log_context`` is supplied, the helper
        records ``failed_count`` / ``total_count`` / ``failed_profiles``
        onto it so the request-level done-callback can distinguish
        ``completed`` from ``partial_failure`` for the same ``request_id``.
        """
        if self._fetch_cache is not None:
            self._fetch_cache.begin_fire()
        try:
            results = await asyncio.gather(
                *(
                    run_profile(
                        profile,
                        kind,
                        run_range=run_range,
                        config=self._config,
                        item_provider=self._item_provider,
                        probe_loop=self._probe_loop,
                        run_id=f"{request_id}:{profile}" if request_id else None,
                        run_ledger=self._run_ledger,
                    )
                    for profile in profiles
                ),
                return_exceptions=True,
            )
            failed: list[tuple[str, BaseException]] = []
            for profile, result in zip(profiles, results, strict=True):
                if isinstance(result, BaseException):
                    failed.append((profile, result))
                    logger.warning(
                        "profile %r in %s run %s failed: %s",
                        profile,
                        kind.value,
                        request_id,
                        result,
                        exc_info=result,
                        extra={
                            "request_id": request_id,
                            "kind": kind.value,
                            "scope": "all",
                            "profile": profile,
                            "status": "failed",
                        },
                    )
                else:
                    logger.info(
                        "profile %r in %s run %s completed",
                        profile,
                        kind.value,
                        request_id,
                        extra={
                            "request_id": request_id,
                            "kind": kind.value,
                            "scope": "all",
                            "profile": profile,
                            "status": "completed",
                        },
                    )
            if log_context is not None:
                log_context["failed_count"] = len(failed)
                log_context["total_count"] = len(profiles)
                log_context["failed_profiles"] = [p for p, _ in failed]
        finally:
            for profile in profiles:
                self._coordinator.release(profile)
            if self._fetch_cache is not None:
                self._fetch_cache.end_fire()
