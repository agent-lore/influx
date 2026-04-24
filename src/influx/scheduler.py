"""APScheduler setup with per-job limits and ``run_profile`` stub hook.

Configures an in-process APScheduler that registers one cron job per
profile from the loaded config, with ``max_instances=1``,
``coalesce=True``, and ``misfire_grace_time=schedule.misfire_grace_seconds``
(FR-SCHED-2).

Each fire routes through :func:`run_profile`, a documented **no-op
stub** in this PRD.  The following PRDs replace the body:

- **PRD 04** — scheduled-run ingestion pipeline
- **PRD 06** — backfill-specific logic
- **PRD 09** — multi-profile fan-out
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from influx.config import AppConfig
from influx.coordinator import Coordinator, ProfileBusyError, RunKind

__all__ = [
    "InfluxScheduler",
    "run_profile",
]

logger = logging.getLogger(__name__)


async def run_profile(
    profile: str,
    kind: RunKind,
    run_range: dict[str, str | int] | None = None,
) -> None:
    """Execute a single ingestion cycle for the given profile.

    This is a **documented stub** in PRD 03.  The following downstream
    PRDs replace the body:

    - **PRD 04** — implements the scheduled-run ingestion pipeline
    - **PRD 06** — implements backfill-specific date-range logic
    - **PRD 09** — implements multi-profile fan-out

    Parameters
    ----------
    profile:
        Profile name from the loaded config.
    kind:
        How the run was initiated (scheduled, manual, backfill).
    run_range:
        Optional date-range parameters for backfills (e.g.
        ``{"days": 7}`` or ``{"from": "...", "to": "..."}``).
        ``None`` for scheduled and manual runs.
    """


class InfluxScheduler:
    """In-process APScheduler wrapper for per-profile cron jobs.

    Registers one job per profile with ``max_instances=1``,
    ``coalesce=True``, and ``misfire_grace_time`` from the schedule
    config.  Each fire acquires the per-profile lock through the
    shared :class:`~influx.coordinator.Coordinator` before calling
    :func:`run_profile`.
    """

    def __init__(
        self,
        config: AppConfig,
        coordinator: Coordinator,
        active_tasks: set[asyncio.Task[Any]] | None = None,
    ) -> None:
        self._config = config
        self._coordinator = coordinator
        self._scheduler = AsyncIOScheduler()
        # Optional shared set for tracking in-flight scheduler fires so
        # that the service layer can await them within
        # ``schedule.shutdown_grace_seconds`` during graceful shutdown
        # (US-008).  ``None`` disables tracking.
        self._active_tasks = active_tasks

    @property
    def jobs(self) -> list[Any]:
        """Return the list of registered APScheduler jobs."""
        return self._scheduler.get_jobs()

    def start(self) -> None:
        """Register per-profile cron jobs and start the scheduler."""
        trigger = CronTrigger.from_crontab(
            self._config.schedule.cron,
            timezone=self._config.schedule.timezone,
        )
        for profile in self._config.profiles:
            self._scheduler.add_job(
                self._fire_profile,
                trigger=trigger,
                args=[profile.name],
                id=f"profile-{profile.name}",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=self._config.schedule.misfire_grace_seconds,
            )
        self._scheduler.start()

    def pause(self) -> None:
        """Stop new fires without cancelling in-flight work.

        Removes all registered jobs so APScheduler won't schedule any
        more fires, but lets already-running ``_fire_profile`` tasks
        continue to completion.  Called by :meth:`InfluxService.stop`
        before the graceful-shutdown grace window begins so that
        scheduler-fired work gets the same bounded completion window
        as HTTP-triggered work (US-008).
        """
        try:
            self._scheduler.remove_all_jobs()
        except Exception:  # pragma: no cover — defensive
            logger.debug("remove_all_jobs() raised during pause()", exc_info=True)

    def stop(self, wait: bool = False) -> None:
        """Shut down the APScheduler.

        Parameters
        ----------
        wait:
            When ``True``, block until running jobs complete.
        """
        self._scheduler.shutdown(wait=wait)

    async def _fire_profile(self, profile_name: str) -> None:
        """Job handler: acquire lock, call ``run_profile``, release.

        A same-profile lock conflict is logged and swallowed so that
        the scheduler is not crashed by overlap (FR-SCHED-3).

        Registers the current task on the shared ``active_tasks`` set
        (if provided) so ``InfluxService.stop`` can await this fire
        within ``schedule.shutdown_grace_seconds`` instead of cancelling
        it immediately (US-008 bounded graceful-shutdown contract).
        """
        if self._active_tasks is not None:
            current = asyncio.current_task()
            if current is not None:
                self._active_tasks.add(current)
                current.add_done_callback(self._active_tasks.discard)
        try:
            async with self._coordinator.hold(profile_name):
                await run_profile(profile_name, RunKind.SCHEDULED)
        except ProfileBusyError:
            logger.info(
                "Scheduled fire for %r skipped — profile already busy",
                profile_name,
            )
