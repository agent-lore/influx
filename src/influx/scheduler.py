"""APScheduler setup with per-job limits and the ``run_profile`` hook.

Configures an in-process APScheduler that registers one cron job per
profile from the loaded config, with ``max_instances=1``,
``coalesce=True``, and ``misfire_grace_time=schedule.misfire_grace_seconds``
(FR-SCHED-2).

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
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from influx.config import AppConfig
from influx.coordinator import Coordinator, ProfileBusyError, RunKind
from influx.errors import LCMAError
from influx.feedback import build_negative_examples_block
from influx.lcma import after_write as lcma_after_write
from influx.lcma import resolve_builds_on as lcma_resolve_builds_on
from influx.lithos_client import LithosClient
from influx.notifications import HighlightItem, ProfileRunResult, RunStats
from influx.repair import SweepWriteError
from influx.repair import sweep as repair_sweep

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
) -> ProfileRunResult | None:
    """Execute a single ingestion cycle for the given profile.

    The body is implemented in PRD 05: connects to Lithos via
    :class:`~influx.lithos_client.LithosClient`, fetches the rejection
    feedback block (FR-FB-1..3), composes the filter prompt, walks the
    *item_provider* once and for each candidate item performs a
    source-agnostic ``lithos_cache_lookup`` (FR-MCP-3) followed by
    ``lithos_write`` (FR-MCP-6/7) when no cache hit is found.  After
    the loop completes, :func:`influx.service.post_run_webhook_hook`
    fires for non-backfill runs (FR-NOT-4).

    Source acquisition is delegated to *item_provider* so PRD 04 can
    plug in the real arXiv + RSS fetcher without touching this module.

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
    config:
        Loaded :class:`AppConfig`.  When ``None`` the function is a
        no-op — used by tests that exercise the scheduler wiring
        without bootstrapping a Lithos connection.
    item_provider:
        Async callable that yields the candidate items for this run.
        When ``None``, :func:`default_item_provider` is used (PRD 04
        replaces this default with the real arXiv + RSS pipeline).
    """
    if config is None:
        return None

    # When no ``item_provider`` is configured, fall back to the default
    # no-op provider so the run still goes through its full pipeline
    # (repair sweep + feedback ingestion + post-run hook).  Source
    # acquisition becomes a no-op until a downstream PRD wires a real
    # provider via ``app.state.item_provider``.
    provider = item_provider if item_provider is not None else default_item_provider
    profile_cfg = next((p for p in config.profiles if p.name == profile), None)

    def _handle_lcma_unknown_tool(exc: LCMAError, *, fallback_tool: str) -> None:
        """Log + latch readiness for an LCMA ``unknown_tool`` failure.

        Centralises the FR-LCMA-6 / US-007 deployment-error handling so
        the same diagnostics fire whether the failure originates from
        ``lithos_task_create``, an LCMA call inside the run body, or
        ``lithos_task_complete`` in the finally block.  ``stage`` on the
        ``LCMAError`` carries the offending tool name (set by
        ``LithosClient._call_lcma_tool``); ``fallback_tool`` is used when
        the exception was raised without a ``stage``.
        """
        tool = getattr(exc, "stage", "") or fallback_tool
        logger.error(
            "LCMA deployment error: unknown_tool for %s during profile %r run "
            "— aborting run. Check that the connected Lithos deployment "
            "supports the required LCMA tools.",
            tool,
            profile,
        )
        if probe_loop is not None and hasattr(
            probe_loop, "mark_lcma_unknown_tool_failure"
        ):
            probe_loop.mark_lcma_unknown_tool_failure(
                profile=profile,
                detail=f"tool={tool!r}",
            )

    client = LithosClient(url=config.lithos.url, transport=config.lithos.transport)
    try:
        # ── LCMA task bracketing (FR-LCMA-5, FR-BF-5, AC-M2-10) ──
        # Every run creates a Lithos task that brackets the per-profile
        # run.  The task tag depends on ``kind``:
        #   - scheduled / manual → ``influx:run``
        #   - backfill           → ``influx:backfill``
        run_task_id: str | None = None
        task_tag = "influx:backfill" if kind == RunKind.BACKFILL else "influx:run"
        run_date = datetime.now(UTC).date().isoformat()
        task_title = f"Influx run {profile} {run_date}"
        try:
            task_result = await client.task_create(
                title=task_title,
                agent="influx",
                tags=[task_tag, f"profile:{profile}"],
            )
        except LCMAError as exc:
            if str(exc) == "unknown_tool":
                _handle_lcma_unknown_tool(exc, fallback_tool="lithos_task_create")
            raise
        task_body = json.loads(
            task_result.content[0].text  # type: ignore[union-attr]
        )
        run_task_id = task_body["task_id"]

        outcome = "success"
        body_failed = False
        try:
            # 0. Repair sweep — durable retry for failed enrichment (PRD 06 §5.1).
            #    Runs for scheduled and manual runs only; backfills skip (FR-REP-2).
            if kind != RunKind.BACKFILL:
                try:
                    await repair_sweep(profile, client=client, config=config)
                except SweepWriteError:
                    # §5.4 failure mode 1: terminal write failure aborts the
                    # run AND degrades readiness (US-011).
                    if probe_loop is not None and hasattr(
                        probe_loop, "mark_repair_write_failure"
                    ):
                        probe_loop.mark_repair_write_failure(profile=profile)
                    raise
                else:
                    # Successful sweep clears the readiness latch.
                    if probe_loop is not None and hasattr(
                        probe_loop, "clear_repair_write_failure"
                    ):
                        probe_loop.clear_repair_write_failure()

            # 1. Feedback ingestion → negative examples block (FR-FB-1..3, AC-05-H).
            neg_block = await build_negative_examples_block(
                client,
                profile=profile,
                limit=config.feedback.negative_examples_per_profile,
            )

            # 2. Compose filter prompt for this run (consumed by PRD 04 LLM filter).
            prompt_text = config.prompts.filter.text or ""
            try:
                filter_prompt = prompt_text.format(
                    profile_description=(
                        profile_cfg.description if profile_cfg else profile
                    ),
                    negative_examples=neg_block,
                    min_score_in_results=config.filter.min_score_in_results,
                )
            except (KeyError, IndexError):
                filter_prompt = prompt_text

            # 3. Source acquisition (PRD 04 plugs in arXiv + RSS).
            items = await provider(profile, kind, run_range, filter_prompt)

            # 4. Per-item: cache_lookup → write_note.
            ingested: list[HighlightItem] = []
            sources_checked = 0
            for item in items:
                sources_checked += 1
                title = item["title"]
                source_url = item["source_url"]
                cache_result = await client.cache_lookup_for_item(
                    title=title,
                    source_url=source_url,
                    abstract_or_summary=item.get("abstract_or_summary"),
                )
                cache_body = json.loads(
                    cache_result.content[0].text  # type: ignore[union-attr]
                )
                if cache_body.get("hit"):
                    # FR-BF-2: backfills skip already-ingested items
                    # entirely — no write attempt, no network traffic.
                    if kind == RunKind.BACKFILL:
                        continue

                    # US-005: multi-profile merge — still attempt a write
                    # so that version_conflict handling merges profile tags
                    # and Profile Relevance entries from the existing note.
                    write_result = await client.write_note(
                        title=title,
                        content=item.get("content", ""),
                        path=item.get("path", ""),
                        source_url=source_url,
                        tags=list(item.get("tags", [])),
                        confidence=float(item.get("confidence", 0.0)),
                    )
                    if write_result.status in ("created", "updated"):
                        ingested.append(
                            HighlightItem(
                                id=item.get("id", f"note-{len(ingested) + 1}"),
                                title=title,
                                score=int(item.get("score", 0)),
                                tags=list(item.get("tags", [])),
                                reason=item.get("reason", ""),
                                url=source_url,
                                related_in_lithos=[],
                            )
                        )
                    continue

                write_result = await client.write_note(
                    title=title,
                    content=item.get("content", ""),
                    path=item.get("path", ""),
                    source_url=source_url,
                    tags=list(item.get("tags", [])),
                    confidence=float(item.get("confidence", 0.0)),
                )
                if write_result.status in ("created", "updated"):
                    # ── LCMA post-write hook (FR-LCMA-2/3, AC-M2-5/6) ──
                    related_in_lithos: list[dict[str, Any]] = []
                    if run_task_id is not None:
                        # The just-written note's id is plumbed through
                        # so LCMA edges carry a real source endpoint
                        # rather than an empty string (PRD 08 graph
                        # wiring; see finding 1).
                        source_note_id = write_result.note_id
                        related_in_lithos = await lcma_after_write(
                            client=client,
                            title=title,
                            contributions=item.get("contributions"),
                            run_task_id=run_task_id,
                            profile=profile,
                            lcma_edge_score=profile_cfg.thresholds.lcma_edge_score
                            if profile_cfg
                            else 0.75,
                            source_note_id=source_note_id,
                        )
                        # ── Tier 3 builds_on resolver (FR-LCMA-4, AC-M2-7/8) ──
                        await lcma_resolve_builds_on(
                            client=client,
                            builds_on=item.get("builds_on"),
                            source_note_id=source_note_id,
                        )

                    ingested.append(
                        HighlightItem(
                            id=item.get("id", f"note-{len(ingested) + 1}"),
                            title=title,
                            score=int(item.get("score", 0)),
                            tags=list(item.get("tags", [])),
                            reason=item.get("reason", ""),
                            url=source_url,
                            related_in_lithos=related_in_lithos,
                        )
                    )

            # 5. Build result + fire post-run webhook hook (FR-NOT-1..6, AC-05-I).
            result = ProfileRunResult(
                run_date=datetime.now(UTC).date().isoformat(),
                profile=profile,
                stats=RunStats(sources_checked=sources_checked, ingested=len(ingested)),
                items=ingested,
            )
            # Lazy import to avoid the service ↔ scheduler import cycle.
            from influx.service import post_run_webhook_hook

            post_run_webhook_hook(result, config, kind=kind)
            return result
        except LCMAError as exc:
            outcome = "error"
            body_failed = True
            if str(exc) == "unknown_tool":
                _handle_lcma_unknown_tool(exc, fallback_tool="lcma_call")
            raise
        except Exception:
            outcome = "error"
            body_failed = True
            raise
        finally:
            if run_task_id is not None:
                try:
                    await client.task_complete(
                        task_id=run_task_id,
                        agent="influx",
                        outcome=outcome,
                    )
                except LCMAError as exc:
                    if str(exc) == "unknown_tool":
                        _handle_lcma_unknown_tool(
                            exc, fallback_tool="lithos_task_complete"
                        )
                        # Only re-raise from finally when the body did
                        # not already fail — otherwise the body's
                        # exception propagates after this block.
                        if not body_failed:
                            raise
                    else:
                        logger.warning(
                            "lithos_task_complete failed for profile %r: %s",
                            profile,
                            exc,
                            exc_info=True,
                        )
                except Exception:
                    logger.warning(
                        "lithos_task_complete failed for profile %r",
                        profile,
                        exc_info=True,
                    )
    finally:
        await client.close()


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
        *,
        item_provider: ItemProvider | None = None,
        probe_loop: Any | None = None,
        fetch_cache: Any | None = None,
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
        # around each profile fire so cached fetches do not leak across
        # cron ticks (review finding 2).
        self._fetch_cache = fetch_cache

    @property
    def jobs(self) -> list[Any]:
        """Return the list of registered APScheduler jobs."""
        return self._scheduler.get_jobs()

    def start(self) -> None:
        """Register a single tick-dispatcher job that fans out to all profiles.

        Replaces the previous per-profile job registration with one
        ``influx-tick`` job that brackets a single
        :meth:`FetchCache.begin_fire` / :meth:`FetchCache.end_fire` around
        the entire fan-out (review finding 1).  This guarantees that all
        profiles firing for a single cron tick share the same cache
        scope — so two profiles both subscribed to ``cs.AI`` fetch
        ``cs.AI`` exactly once for the run, even when one profile
        finishes before the other starts.
        """
        if self._config.profiles:
            trigger = CronTrigger.from_crontab(
                self._config.schedule.cron,
                timezone=self._config.schedule.timezone,
            )
            self._scheduler.add_job(
                self._fire_tick,
                trigger=trigger,
                id="influx-tick",
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

    async def _fire_tick(self) -> None:
        """Single-tick dispatcher: bracket fetch-cache scope, fan out profiles.

        Registers the dispatch task on ``active_tasks`` (if provided) so
        ``InfluxService.stop`` can await it within
        ``schedule.shutdown_grace_seconds`` (US-008).  Calls
        :meth:`FetchCache.begin_fire` / :meth:`FetchCache.end_fire` once
        around the whole fan-out so per-source fetches are deduplicated
        across all profiles for the cron tick (review finding 1, R-8,
        AC-09-D).
        """
        if self._active_tasks is not None:
            current = asyncio.current_task()
            if current is not None:
                self._active_tasks.add(current)
                current.add_done_callback(self._active_tasks.discard)
        if self._fetch_cache is not None:
            self._fetch_cache.begin_fire()
        try:
            await asyncio.gather(
                *(
                    self._fire_profile(profile.name)
                    for profile in self._config.profiles
                ),
                return_exceptions=True,
            )
        finally:
            if self._fetch_cache is not None:
                self._fetch_cache.end_fire()

    async def _fire_profile(self, profile_name: str) -> None:
        """Per-profile fire: acquire lock, call ``run_profile``, release.

        A same-profile lock conflict is logged and swallowed so that
        the scheduler is not crashed by overlap (FR-SCHED-3).

        The fetch-cache scope is bracketed by the parent
        :meth:`_fire_tick` dispatcher so all profiles within one cron
        tick share the same dedup window (review finding 1).  Tests
        that invoke ``_fire_profile`` directly intentionally do NOT
        bracket the cache here — they exercise the per-profile lock /
        coordinator behaviour without per-tick fan-out.
        """
        try:
            async with self._coordinator.hold(profile_name):
                await run_profile(
                    profile_name,
                    RunKind.SCHEDULED,
                    config=self._config,
                    item_provider=self._item_provider,
                    probe_loop=self._probe_loop,
                )
        except ProfileBusyError:
            logger.info(
                "Scheduled fire for %r skipped — profile already busy",
                profile_name,
            )
