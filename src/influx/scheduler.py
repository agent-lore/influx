"""APScheduler setup with per-job limits and the ``run_profile`` hook.

Configures an in-process APScheduler that registers a single
``influx-tick`` dispatcher job (``coalesce=True``,
``misfire_grace_time=schedule.misfire_grace_seconds``).  The cron-fired
callable is a *thin dispatcher* that snapshots a fresh per-tick
provider/cache, spawns the real fan-out as a background ``asyncio``
task on ``active_tasks``, and returns immediately so APScheduler's
instance slot is never held for the duration of the fan-out.  This
keeps APScheduler off the same-profile non-overlap path entirely —
that contract is enforced solely by the
:class:`~influx.coordinator.Coordinator` (FR-SCHED-2).

Each fire routes through :func:`run_profile`, which executes one
ingestion cycle: feedback ingestion → per-item dedup lookup →
``lithos_write`` → post-run webhook hook.  Source acquisition (arXiv /
RSS) is supplied by an injectable ``item_provider`` callback on
``app.state`` — PRD 04 replaces the default no-op provider with the
real arXiv + RSS pipeline; PRD 06 layers backfill-specific date-range
logic on top; PRD 09 adds multi-profile fan-out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from influx.config import AppConfig
from influx.coordinator import Coordinator, ProfileBusyError, RunKind
from influx.notifications import ProfileRunResult
from influx.run_ledger import RunLedger

__all__ = [
    "InfluxScheduler",
    "ProfileItem",
    "default_item_provider",
    "run_profile",
]

logger = logging.getLogger(__name__)


# An item provider yields the iterable of dicts that ``run_profile``
# turns into ``lithos_write`` calls.  Each dict must include ``title``,
# ``source_url``, ``content``, ``tags``, and ``confidence``; ``score``,
# ``path``, and ``abstract_or_summary`` are optional.  PRD 04 replaces
# the default no-op provider with the real arXiv + RSS pipeline.
ProfileItem = dict[str, Any]
ItemProvider = Callable[
    [str, RunKind, dict[str, str | int] | None, str],
    Awaitable[Iterable[ProfileItem]],
]
# Per-tick factory: returns a fresh ``(item_provider, fetch_cache)`` pair so
# each cron fire has its own dedup scope and cron tick N+1 cannot see cron
# tick N's data.  ``FetchCache`` is typed as ``Any`` to avoid an import
# cycle with ``influx.sources``.
ItemProviderFactory = Callable[[], tuple[ItemProvider, Any]]


# The cron-registered callable is a thin dispatcher that returns
# immediately after spawning the fan-out as a background task, so
# APScheduler's ``max_instances`` is never the gate on a slow tick.
# Same-profile non-overlap is enforced by the coordinator alone.
_TICK_MAX_INSTANCES = 1


def _item_source(item: ProfileItem) -> str:
    """Derive the source family (``"arxiv"`` | ``"rss"`` | ``"unknown"``).

    Used as a bounded metric label.  Builders may stamp the ``source``
    key directly; this helper falls back to scanning ``tags`` for the
    canonical ``source:*`` provenance entry so RSS feeds with custom
    ``source_tag`` values (e.g. ``"blog"``) still roll up to ``"rss"``
    at the metric layer rather than fan out into per-feed cardinality.
    """
    direct = item.get("source")
    if isinstance(direct, str) and direct:
        return direct
    for tag in item.get("tags", []) or []:
        if isinstance(tag, str) and tag.startswith("source:"):
            value = tag.split(":", 1)[1]
            return "rss" if value not in ("arxiv",) else "arxiv"
    return "unknown"


async def default_item_provider(
    profile: str,
    kind: RunKind,
    run_range: dict[str, str | int] | None,
    filter_prompt: str,
) -> Iterable[ProfileItem]:
    """No-op item provider — PRD 04 replaces this with arXiv + RSS fetch.

    Returns an empty iterable so that PRD 03 / PRD 05 production runs
    still complete cleanly (feedback ingestion + post-run webhook hook
    fire) until source ingestion is wired by the next PRD.
    """
    del profile, kind, run_range, filter_prompt
    return ()


async def run_profile(
    profile: str,
    kind: RunKind,
    run_range: dict[str, str | int] | None = None,
    *,
    config: AppConfig | None = None,
    item_provider: ItemProvider | None = None,
    probe_loop: Any | None = None,
    run_id: str | None = None,
    run_ledger: RunLedger | None = None,
) -> ProfileRunResult | None:
    """Backward-compatible thin wrapper over :class:`influx.run_service.RunService`.

    Existing callers (HTTP admin handlers, ``InfluxScheduler._fire_profile``,
    ``influx.backfill.run_backfill``, integration tests) still call
    ``run_profile`` with the legacy signature.  Internally we now build
    a :class:`RunPlan`, hand it to :class:`RunService`, and unwrap the
    :class:`RunOutcome` back into a legacy :class:`ProfileRunResult` so
    no caller has to change.

    The full lifecycle (skip gates, ledger entry, metrics, contextvars,
    tracer span, body, ``ledger.complete`` / ``ledger.fail`` / ``ledger.skip``)
    lives in :class:`RunService` (#61).  The body itself runs through
    :class:`influx.run.Run`.

    ``config=None`` remains a no-op so tests that exercise the scheduler
    wiring without bootstrapping a Lithos connection continue to work.
    """
    if config is None:
        return None

    from influx.run_service import run_via_service

    return await run_via_service(
        profile,
        kind,
        run_range,
        config=config,
        item_provider=item_provider,
        probe_loop=probe_loop,
        run_id=run_id,
        run_ledger=run_ledger,
    )


class InfluxScheduler:
    """In-process APScheduler wrapper for the ``influx-tick`` dispatcher.

    Registers a single dispatcher job with ``coalesce=True`` and
    ``misfire_grace_time`` from the schedule config.  The cron-fired
    callable (:meth:`_cron_dispatch`) is a thin dispatcher that
    snapshots a fresh per-tick provider/cache, spawns the real fan-out
    as a background task on ``active_tasks``, and returns immediately
    so APScheduler is never the gate on a slow tick.  Same-profile
    non-overlap is enforced solely by the shared
    :class:`~influx.coordinator.Coordinator`, which is consulted before
    each call to :func:`run_profile`.
    """

    def __init__(
        self,
        config: AppConfig,
        coordinator: Coordinator,
        active_tasks: set[asyncio.Task[Any]] | None = None,
        *,
        item_provider: ItemProvider | None = None,
        probe_loop: Any | None = None,
        fetch_cache: Any | None = None,
        item_provider_factory: ItemProviderFactory | None = None,
    ) -> None:
        self._config = config
        self._coordinator = coordinator
        self._scheduler = AsyncIOScheduler()
        # Optional shared set for tracking in-flight scheduler fires so
        # that the service layer can await them within
        # ``schedule.shutdown_grace_seconds`` during graceful shutdown
        # (US-008).  ``None`` disables tracking.
        self._active_tasks = active_tasks
        # Optional injected item provider — replaced by PRD 04 with the
        # real arXiv + RSS pipeline.  Defaults to ``default_item_provider``
        # at fire time so scheduled runs still execute the repair sweep
        # and feedback ingestion (US-013).
        self._item_provider = item_provider
        # Optional probe loop — when set, terminal repair write failures
        # (SweepWriteError) flip a readiness latch so ``/ready`` reports
        # degraded per US-011 (§5.4 failure mode 1).
        self._probe_loop = probe_loop
        # Optional ``FetchCache`` whose per-fire scope is bracketed
        # around each profile fire (legacy single-shared-cache path —
        # used by tests that pre-build a provider with a shared cache).
        self._fetch_cache = fetch_cache
        # Optional per-tick factory.  When set, ``_cron_dispatch`` calls
        # the factory once per cron tick to obtain a fresh
        # ``(item_provider, fetch_cache)`` pair, so cron tick N+1 starts
        # with a clean dedup scope even when tick N is still running.
        # The dispatcher returns immediately so APScheduler's
        # ``max_instances`` is never the gate; same-profile non-overlap
        # is enforced solely by the coordinator.
        self._item_provider_factory = item_provider_factory

    @property
    def jobs(self) -> list[Any]:
        """Return the list of registered APScheduler jobs."""
        return self._scheduler.get_jobs()

    def start(self) -> None:
        """Register a single tick-dispatcher job that fans out to all profiles.

        The registered callable, :meth:`_cron_dispatch`, returns as soon
        as it has spawned the fan-out as a background task on
        ``active_tasks``.  Because APScheduler's instance slot is held
        only for the duration of that microsecond-scale dispatch — not
        for the duration of the fan-out — APScheduler is never the gate
        on a slow tick.  Same-profile non-overlap is enforced solely by
        the :class:`~influx.coordinator.Coordinator`.  When an
        ``item_provider_factory`` is wired, each tick allocates its own
        fresh fetch cache so cross-tick fetches are not deduplicated.
        """
        if self._config.profiles:
            trigger = CronTrigger.from_crontab(
                self._config.schedule.cron,
                timezone=self._config.schedule.timezone,
            )
            self._scheduler.add_job(
                self._cron_dispatch,
                trigger=trigger,
                id="influx-tick",
                max_instances=_TICK_MAX_INSTANCES,
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

    async def _cron_dispatch(self) -> asyncio.Task[None]:
        """Cron entrypoint — spawn the fan-out task and return immediately.

        APScheduler invokes this at every cron fire.  The dispatcher
        snapshots a fresh per-tick ``(provider, cache)`` pair, spawns
        :meth:`_fire_tick` as a background task, registers it on
        ``active_tasks`` (if provided) so :meth:`InfluxService.stop` can
        await it within ``schedule.shutdown_grace_seconds`` (US-008),
        and returns.  APScheduler's instance slot is held only for the
        duration of this microsecond-scale dispatch, not for the
        fan-out itself, so a slow profile in tick N never blocks tick
        N+M from being dispatched (review finding).  Same-profile
        non-overlap is enforced by the coordinator.
        """
        if self._item_provider_factory is not None:
            tick_provider, tick_cache = self._item_provider_factory()
        else:
            tick_provider = self._item_provider
            tick_cache = self._fetch_cache

        task = asyncio.create_task(
            self._fire_tick(provider=tick_provider, cache=tick_cache),
            name="influx-tick-fanout",
        )
        if self._active_tasks is not None:
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)
        return task

    async def _fire_tick(
        self,
        *,
        provider: ItemProvider | None = None,
        cache: Any | None = None,
    ) -> None:
        """Single-tick fan-out: bracket fetch-cache scope, fan out profiles.

        When called via :meth:`_cron_dispatch`, ``provider`` and
        ``cache`` are the per-tick snapshot from the dispatcher.  When
        called directly (e.g. by tests or by the legacy in-process
        path), ``provider``/``cache`` default to the scheduler's
        configured values — a per-tick factory is preferred over a
        shared cache so cross-tick fetches are not deduplicated.

        Per-source fetches within a single tick are deduplicated across
        profiles for that tick's fan-out (R-8, AC-09-D).
        """
        if provider is None and self._item_provider_factory is not None:
            provider, cache = self._item_provider_factory()
        if provider is None:
            provider = self._item_provider
        if cache is None:
            cache = self._fetch_cache

        if cache is not None:
            cache.begin_fire()
        try:
            await asyncio.gather(
                *(
                    self._fire_profile(profile.name, item_provider=provider)
                    for profile in self._config.profiles
                ),
                return_exceptions=True,
            )
        finally:
            if cache is not None:
                cache.end_fire()

    async def _fire_profile(
        self,
        profile_name: str,
        *,
        item_provider: ItemProvider | None = None,
    ) -> None:
        """Per-profile fire: acquire lock, call ``run_profile``, release.

        A same-profile lock conflict is logged and swallowed so that
        the scheduler is not crashed by overlap (FR-SCHED-3).

        The fetch-cache scope is bracketed by the parent
        :meth:`_fire_tick` dispatcher so all profiles within one cron
        tick share the same dedup window.  Tests that invoke
        ``_fire_profile`` directly intentionally do NOT bracket the
        cache here — they exercise the per-profile lock / coordinator
        behaviour without per-tick fan-out.

        ``item_provider`` overrides the scheduler-level default so that
        a per-tick provider built by the dispatcher can be passed
        through without mutating ``self``.
        """
        provider = item_provider if item_provider is not None else self._item_provider
        try:
            async with self._coordinator.hold(profile_name):
                await run_profile(
                    profile_name,
                    RunKind.SCHEDULED,
                    config=self._config,
                    item_provider=provider,
                    probe_loop=self._probe_loop,
                )
        except ProfileBusyError:
            logger.info(
                "Scheduled fire for %r skipped — profile already busy",
                profile_name,
            )
