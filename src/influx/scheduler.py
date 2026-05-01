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
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from influx import metrics
from influx.config import AppConfig
from influx.coordinator import Coordinator, ProfileBusyError, RunKind
from influx.errors import LCMAError
from influx.feedback import build_negative_examples_block
from influx.lcma import after_write as lcma_after_write
from influx.lcma import resolve_builds_on as lcma_resolve_builds_on
from influx.lithos_client import LithosClient
from influx.notifications import HighlightItem, ProfileRunResult, RunStats
from influx.rejection_rate import on_run_complete as rejection_rate_on_run_complete
from influx.rejection_rate import record_filter_result
from influx.repair import SweepWriteError
from influx.repair import sweep as repair_sweep
from influx.run_ledger import RunLedger
from influx.telemetry import (
    current_archive_terminal_arxiv_ids,
    current_run_id,
    current_source_acquisition_errors,
    get_tracer,
)

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

    # ── Telemetry: influx.run span (FR-OBS-4) ──
    provider = item_provider if item_provider is not None else default_item_provider
    run_id = run_id or str(uuid.uuid4())
    ledger = run_ledger or RunLedger(Path(config.storage.state_dir))
    ledger.start(
        run_id=run_id,
        profile=profile,
        kind=kind.value,
        run_range=run_range,
    )
    run_id_token = current_run_id.set(run_id)
    # Initialise the per-run record of swallowed source-fetch failures.
    # Source providers (arxiv/rss) append on ``NetworkError`` paths via
    # ``record_source_acquisition_error``; we drain the list before the
    # ledger entry is finalised below.  See issue #20.
    source_errors_token = current_source_acquisition_errors.set([])
    tracer = get_tracer()
    started_at = datetime.now(UTC)
    # Run-lifecycle metrics (issue #6): start counter + active-runs gauge
    # bracket the run end-to-end so dashboards can answer "is anything
    # actually running?" without relying on docker logs.
    run_metric_attrs = {"profile": profile, "run_type": kind.value}
    metrics.run_starts().add(1, run_metric_attrs)
    metrics.active_runs().add(1, {"profile": profile})
    logger.info(
        "run started profile=%s kind=%s run_id=%s range=%s",
        profile,
        kind.value,
        run_id,
        run_range or {},
    )

    try:
        with tracer.span(
            "influx.run",
            attributes={
                "influx.profile": profile,
                "influx.run_id": run_id,
                "influx.run_type": kind.value,
            },
        ):
            result = await _run_profile_body(
                profile,
                kind,
                run_range,
                config=config,
                item_provider=provider,
                probe_loop=probe_loop,
            )
            elapsed = (datetime.now(UTC) - started_at).total_seconds()
            source_errors = current_source_acquisition_errors.get() or []
            outcome = "degraded" if source_errors else "success"
            if result is None:
                logger.info(
                    "run completed profile=%s kind=%s run_id=%s duration=%.1fs "
                    "result=none",
                    profile,
                    kind.value,
                    run_id,
                    elapsed,
                )
                ledger.complete(
                    run_id=run_id,
                    sources_checked=None,
                    ingested=None,
                    source_acquisition_errors=source_errors,
                )
            else:
                logger.info(
                    "run completed profile=%s kind=%s run_id=%s duration=%.1fs "
                    "sources_checked=%d ingested=%d degraded=%s",
                    profile,
                    kind.value,
                    run_id,
                    elapsed,
                    result.stats.sources_checked,
                    result.stats.ingested,
                    bool(source_errors),
                )
                ledger.complete(
                    run_id=run_id,
                    sources_checked=result.stats.sources_checked,
                    ingested=result.stats.ingested,
                    source_acquisition_errors=source_errors,
                )
            metrics.run_duration().record(elapsed, run_metric_attrs)
            metrics.run_completions().add(1, {**run_metric_attrs, "outcome": outcome})
            return result
    except Exception as exc:
        elapsed = (datetime.now(UTC) - started_at).total_seconds()
        logger.exception(
            "run failed profile=%s kind=%s run_id=%s duration=%.1fs",
            profile,
            kind.value,
            run_id,
            elapsed,
        )
        ledger.fail(run_id=run_id, error=f"{type(exc).__name__}: {exc}")
        metrics.run_duration().record(elapsed, run_metric_attrs)
        metrics.run_completions().add(1, {**run_metric_attrs, "outcome": "failure"})
        raise
    finally:
        metrics.active_runs().add(-1, {"profile": profile})
        current_run_id.reset(run_id_token)
        current_source_acquisition_errors.reset(source_errors_token)


async def _run_profile_body(
    profile: str,
    kind: RunKind,
    run_range: dict[str, str | int] | None = None,
    *,
    config: AppConfig,
    item_provider: ItemProvider,
    probe_loop: Any | None = None,
) -> ProfileRunResult | None:
    """Inner implementation body of :func:`run_profile`."""
    provider = item_provider
    profile_cfg = next((p for p in config.profiles if p.name == profile), None)

    def _handle_lcma_unknown_tool(exc: LCMAError, *, fallback_tool: str) -> None:
        """Log + latch readiness for an LCMA ``unknown_tool`` failure."""
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
                    repaired = await repair_sweep(profile, client=client, config=config)
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
                    logger.info(
                        "repair sweep completed profile=%s candidates_visited=%d",
                        profile,
                        len(repaired),
                    )

            # 1. Feedback ingestion → negative examples block (FR-FB-1..3, AC-05-H).
            neg_block = await build_negative_examples_block(
                client,
                profile=profile,
                limit=config.feedback.negative_examples_per_profile,
                max_title_chars=config.filter.negative_example_max_title_chars,
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
            #    Pre-fetch the set of arxiv-ids whose Lithos notes are
            #    tagged ``influx:archive-terminal`` so the inspector can
            #    skip ``download_archive`` for permanently-unfetchable
            #    papers (issue #14).  One Lithos query per run, even
            #    when the set is empty, so the contextvar contract is
            #    consistent.
            terminal_ids = await client.list_archive_terminal_arxiv_ids(
                profile=profile,
            )
            terminal_token = current_archive_terminal_arxiv_ids.set(terminal_ids)
            try:
                items = list(await provider(profile, kind, run_range, filter_prompt))
            finally:
                current_archive_terminal_arxiv_ids.reset(terminal_token)
            logger.info(
                "source acquisition completed profile=%s kind=%s candidates=%d",
                profile,
                kind.value,
                len(items),
            )

            # 4. Per-item: cache_lookup → write_note.
            ingested: list[HighlightItem] = []
            sources_checked = 0
            for item in items:
                sources_checked += 1
                title = item["title"]
                # Use the LLM filter-result tags (FR-FLT-3 ``FilterResult.tags``)
                # for rejection-rate computation per FR-OBS-5 / US-008 — distinct
                # from the persisted note / provenance tags later attached to the
                # note. The provider populates ``filter_tags``; sources without an
                # LLM filter step (e.g. RSS) leave it absent and contribute no
                # filter-tag entries to the rejection-rate map.
                record_filter_result(profile, title, item.get("filter_tags", []))
                source_url = item["source_url"]
                # Source family for metric labels — derived from the
                # ``source:*`` provenance tag when build_*_note_item did
                # not stamp the dedicated key directly.
                item_source = item.get("source") or _item_source(item)
                metrics.articles_inspected().add(
                    1, {"profile": profile, "source": item_source}
                )
                logger.info(
                    "article inspected profile=%s source_url=%s title=%r "
                    "score=%s path=%s tags=%s",
                    profile,
                    source_url,
                    title,
                    item.get("score", ""),
                    item.get("path", ""),
                    item.get("tags", []),
                )
                cache_result = await client.cache_lookup_for_item(
                    title=title,
                    source_url=source_url,
                    abstract_or_summary=item.get("abstract_or_summary"),
                )
                cache_body = json.loads(
                    cache_result.content[0].text  # type: ignore[union-attr]
                )
                if cache_body.get("hit"):
                    metrics.cache_hits().add(
                        1, {"profile": profile, "source": item_source}
                    )
                    logger.info(
                        "article cache hit profile=%s source_url=%s title=%r "
                        "kind=%s action=%s",
                        profile,
                        source_url,
                        title,
                        kind.value,
                        "skip" if kind == RunKind.BACKFILL else "merge-profile",
                    )
                    # FR-BF-2: backfills skip already-ingested items
                    # entirely — no write attempt, no network traffic.
                    if kind == RunKind.BACKFILL:
                        continue

                    # US-005: multi-profile merge — still attempt a write
                    # so that version_conflict handling merges profile tags
                    # and Profile Relevance entries from the existing note.
                    tracer = get_tracer()
                    with tracer.span(
                        "influx.lithos.write",
                        attributes={
                            "influx.profile": profile,
                            "influx.run_id": current_run_id.get() or "",
                        },
                    ):
                        write_result = await client.write_note(
                            title=title,
                            content=item.get("content", ""),
                            path=item.get("path", ""),
                            source_url=source_url,
                            tags=list(item.get("tags", [])),
                            confidence=float(item.get("confidence", 0.0)),
                        )
                    metrics.lithos_writes().add(
                        1,
                        {
                            "profile": profile,
                            "source": item_source,
                            "status": write_result.status,
                        },
                    )
                    if write_result.status in ("created", "updated"):
                        logger.info(
                            "article write completed profile=%s source_url=%s "
                            "title=%r status=%s note_id=%s cache_hit=true",
                            profile,
                            source_url,
                            title,
                            write_result.status,
                            write_result.note_id,
                        )
                        related_in_lithos: list[dict[str, Any]] = []
                        if run_task_id is not None:
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
                    continue

                tracer = get_tracer()
                with tracer.span(
                    "influx.lithos.write",
                    attributes={
                        "influx.profile": profile,
                        "influx.run_id": current_run_id.get() or "",
                    },
                ):
                    write_result = await client.write_note(
                        title=title,
                        content=item.get("content", ""),
                        path=item.get("path", ""),
                        source_url=source_url,
                        tags=list(item.get("tags", [])),
                        confidence=float(item.get("confidence", 0.0)),
                    )
                metrics.lithos_writes().add(
                    1,
                    {
                        "profile": profile,
                        "source": item_source,
                        "status": write_result.status,
                    },
                )
                if write_result.status in ("created", "updated"):
                    logger.info(
                        "article write completed profile=%s source_url=%s title=%r "
                        "status=%s note_id=%s cache_hit=false",
                        profile,
                        source_url,
                        title,
                        write_result.status,
                        write_result.note_id,
                    )
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
                else:
                    # Promoted INFO→WARNING with structured `extra` so the
                    # underlying Lithos failure is diagnosable from logs.
                    # See lithos_client._parse_write_response for `detail`
                    # population on the undocumented "error" envelope.
                    logger.warning(
                        "article write skipped profile=%s source_url=%s title=%r "
                        "status=%s detail=%r cache_hit=false",
                        profile,
                        source_url,
                        title,
                        write_result.status,
                        write_result.detail,
                        extra={
                            "profile": profile,
                            "source_url": source_url,
                            "title": title,
                            "status": write_result.status,
                            "detail": write_result.detail,
                            "run_id": current_run_id.get() or "",
                            "tags": list(item.get("tags", [])),
                            "cache_hit": False,
                        },
                    )

            # 5. Build result + fire post-run webhook hook (FR-NOT-1..6, AC-05-I).
            result = ProfileRunResult(
                run_date=datetime.now(UTC).date().isoformat(),
                profile=profile,
                stats=RunStats(sources_checked=sources_checked, ingested=len(ingested)),
                items=ingested,
            )

            # 6. Rejection-rate logging (FR-OBS-5, AC-M4-4/5, AC-10-D).
            await rejection_rate_on_run_complete(
                profile,
                config=config,
                client=client,
                sources_checked=sources_checked,
                ingested=len(ingested),
            )

            # Lazy import to avoid the service ↔ scheduler import cycle.
            from influx.service import post_run_webhook_hook

            post_run_webhook_hook(
                result,
                config,
                kind=kind,
                run_id=current_run_id.get() or None,
            )
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
