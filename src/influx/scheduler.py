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

    def __init__(self, config: AppConfig, coordinator: Coordinator) -> None:
        self._config = config
        self._coordinator = coordinator
        self._scheduler = AsyncIOScheduler()

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
        """
        try:
            async with self._coordinator.hold(profile_name):
                await run_profile(profile_name, RunKind.SCHEDULED)
        except ProfileBusyError:
            logger.info(
                "Scheduled fire for %r skipped — profile already busy",
                profile_name,
            )
