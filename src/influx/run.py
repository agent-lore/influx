"""Run module — score-gated ingestion executor (CONTEXT.md ``Run``).

The body of every Run.  Wrapped in a request-lifecycle by
:class:`influx.run_service.RunService` (#61), which owns the ledger
entry, run-level metrics, contextvars, tracer span, and skip gates.

Architecture
------------
``Run.execute()`` walks five named stages, each returning a per-stage
result type plus a shared :class:`StageDiagnostics`:

1. **Repair** — runs unless ``plan.skip_repair``
2. **Feedback** — load negative examples, render the filter prompt
3. **Acquire** — ``Source.fetch_candidates → Filter.score → Source.acquire``
   (delegated to the injected ``item_provider``; the unified Source
   seam from #57 lives inside the provider)
4. **Ingest** — per item: cache_lookup → ``Cascade.enrich`` →
   ``Renderer.render`` → ``LithosClient.write_note`` → ``LcmaWiring.wire``
5. **Finalise** — fold StageDiagnostics into a :class:`RunOutcome`

Inner lifecycle uses :func:`lithos_task_lifecycle` — a context manager
bracketing ``task_create`` on enter and ``task_complete`` on exit.

:class:`RunAborted` is caught at ``execute()`` so abort-to-outcome
translation lives in one place; the abort exception carries
:class:`StageDiagnostics` so ``execute()`` can apply ``health_actions``
from both success and abort paths uniformly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from influx import metrics
from influx.config import AppConfig
from influx.coordinator import RunKind
from influx.feedback import build_negative_examples_block
from influx.lcma_wiring import CascadeOutput, LcmaWiringDeps
from influx.lcma_wiring import wire as lcma_wire
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
    "AcquireResult",
    "FeedbackResult",
    "FinaliseResult",
    "HealthAction",
    "IngestResult",
    "RepairResult",
    "Run",
    "RunAborted",
    "RunDeps",
    "RunOutcome",
    "RunPlan",
    "StageDiagnostics",
]

logger = logging.getLogger(__name__)


# ── Item provider seam (mirrors influx.scheduler.ItemProvider) ────────


ProfileItem = dict[str, Any]
ItemProvider = Callable[
    [str, RunKind, dict[str, str | int] | None, str],
    Awaitable[Iterable[ProfileItem]],
]


# ── Plan + outcome value types ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunPlan:
    """The data-driven specification a Run executes (CONTEXT.md).

    Built once per request type (cron tick / ``POST /runs`` /
    ``POST /backfills``); handed off to :class:`Run` for execution.
    """

    profile: str
    kind: RunKind
    date_window: dict[str, str | int] | None = None
    skip_repair: bool = False
    skip_cache_hits: bool = False
    notify: bool = True
    ledger_id: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Post-execution record (CONTEXT.md).

    ``profile_run_result`` carries the legacy
    :class:`~influx.notifications.ProfileRunResult` for the post-run
    webhook hook + scheduler return value compatibility.  ``skipped``
    + ``skip_reason`` let the caller distinguish the
    circuit-breaker / LCMA-tools-unavailable paths from real-work
    outcomes.
    """

    sources_checked: int = 0
    ingested: int = 0
    error: str | None = None
    degraded: bool = False
    degraded_reasons: tuple[str, ...] = ()
    source_acquisition_errors: tuple[Any, ...] = ()
    profile_run_result: ProfileRunResult | None = None
    skipped: bool = False
    skip_reason: str | None = None


# ── StageDiagnostics + HealthAction (Q1 grilling — hybrid C) ────────


@dataclass(frozen=True, slots=True)
class HealthAction:
    """Declarative health-latch action emitted by a stage.

    ``execute()`` reads :attr:`StageDiagnostics.health_actions` and
    applies these to ``deps.probe_loop`` after each stage (success or
    abort path).  Keeps stages pure — no probe-loop dep threaded
    through.

    The latch surface shrunk to a single value after #69 lifted the
    LCMA tool-availability check to probe time; only
    ``repair_write_failure`` still flips/clears mid-run.
    """

    op: Literal["flip", "clear"]
    latch: Literal["repair_write_failure"]
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StageDiagnostics:
    """Cross-cutting state every stage may emit alongside its primary result.

    Folded across stages by ``execute()``.  Carried by
    :class:`RunAborted` so the abort path can also surface
    diagnostics.
    """

    degraded_reasons: tuple[str, ...] = ()
    health_actions: tuple[HealthAction, ...] = ()


def _merge_diagnostics(a: StageDiagnostics, b: StageDiagnostics) -> StageDiagnostics:
    """Concat-fold two diagnostics records (deduping degraded_reasons)."""
    return StageDiagnostics(
        degraded_reasons=tuple(
            dict.fromkeys((*a.degraded_reasons, *b.degraded_reasons))
        ),
        health_actions=(*a.health_actions, *b.health_actions),
    )


# ── Per-stage result types ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Stage 1 output."""

    candidates_visited: int = 0


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    """Stage 2 output — rendered filter prompt for the Acquire stage."""

    filter_prompt: str = ""


@dataclass(frozen=True, slots=True)
class AcquireResult:
    """Stage 3 output — ProfileItem dicts ready for Ingest."""

    items: tuple[ProfileItem, ...] = ()


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Stage 4 output — ingested HighlightItems + sources_checked."""

    ingested: tuple[HighlightItem, ...] = ()
    sources_checked: int = 0


@dataclass(frozen=True, slots=True)
class FinaliseResult:
    """Stage 5 output — the post-execution :class:`RunOutcome`."""

    outcome: RunOutcome


# ── Abort signal ────────────────────────────────────────────────────


class RunAborted(Exception):
    """Stage-level abort that ``execute()`` translates into a failed RunOutcome.

    Carries :class:`StageDiagnostics` so the abort path can surface
    ``degraded_reasons`` and ``health_actions`` (e.g. the
    ``repair_write_failure`` flip on a terminal sweep-write failure).
    """

    def __init__(self, reason: str, diagnostics: StageDiagnostics) -> None:
        super().__init__(reason)
        self.reason = reason
        self.diagnostics = diagnostics


# ── RunDeps ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunDeps:
    """Per-Run dependencies bundled into one value.

    ``probe_loop`` and ``ledger`` are typed as ``Any`` to avoid an
    import cycle and to mirror the legacy ``run_profile`` signature
    (which accepted duck-typed substitutes from tests).
    """

    config: AppConfig
    item_provider: ItemProvider
    probe_loop: Any | None = None
    ledger: RunLedger | None = None
    run_id: str | None = None


@asynccontextmanager
async def lithos_task_lifecycle(
    plan: RunPlan, client: LithosClient
) -> AsyncIterator[str]:
    """Inner lifecycle CM — Lithos task bracketing.

    On enter: ``task_create`` per-Run task with the run-kind tag.
    On exit: ``task_complete`` with ``success`` or ``error`` outcome
    based on whether the body raised.  ``LCMAError("unknown_tool")``
    in either call is no longer special-cased — the probe-time gate
    (#69) keeps it off the runtime path in steady state.
    """
    profile = plan.profile
    task_tag = "influx:backfill" if plan.kind == RunKind.BACKFILL else "influx:run"
    run_date = datetime.now(UTC).date().isoformat()
    task_title = f"Influx run {profile} {run_date}"
    task_result = await client.task_create(
        title=task_title,
        agent="influx",
        tags=[task_tag, f"profile:{profile}"],
    )
    task_body = json.loads(
        task_result.content[0].text  # type: ignore[union-attr]
    )
    run_task_id = str(task_body["task_id"])

    body_failed = False
    try:
        yield run_task_id
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            await client.task_complete(
                task_id=run_task_id,
                agent="influx",
                outcome="error" if body_failed else "success",
            )
        except Exception:
            logger.warning(
                "lithos_task_complete failed for profile %r",
                profile,
                exc_info=True,
            )
            if not body_failed:
                raise


# ── Stages ──────────────────────────────────────────────────────────


async def _run_repair_stage(
    plan: RunPlan,
    *,
    client: LithosClient,
    config: AppConfig,
) -> tuple[RepairResult, StageDiagnostics]:
    """Stage 1 — repair sweep (FR-REP-2).

    Skipped when ``plan.skip_repair`` (backfills).  ``SweepWriteError``
    raises :class:`RunAborted` carrying a flip ``HealthAction`` for the
    ``repair_write_failure`` latch; success emits a clear action.
    """
    if plan.skip_repair:
        return RepairResult(candidates_visited=0), StageDiagnostics()

    try:
        repaired = await repair_sweep(plan.profile, client=client, config=config)
    except SweepWriteError as exc:
        diagnostics = StageDiagnostics(
            health_actions=(
                HealthAction(
                    op="flip",
                    latch="repair_write_failure",
                    detail=f"profile={plan.profile!r}",
                ),
            ),
        )
        raise RunAborted("repair_write_failure", diagnostics) from exc

    logger.info(
        "repair sweep completed profile=%s candidates_visited=%d",
        plan.profile,
        len(repaired),
    )
    return (
        RepairResult(candidates_visited=len(repaired)),
        StageDiagnostics(
            health_actions=(HealthAction(op="clear", latch="repair_write_failure"),),
        ),
    )


async def _run_feedback_stage(
    plan: RunPlan,
    *,
    client: LithosClient,
    config: AppConfig,
) -> tuple[FeedbackResult, StageDiagnostics]:
    """Stage 2 — feedback ingestion + filter prompt rendering."""
    profile_cfg = next((p for p in config.profiles if p.name == plan.profile), None)
    neg_block = await build_negative_examples_block(
        client,
        profile=plan.profile,
        limit=config.feedback.negative_examples_per_profile,
        max_title_chars=config.filter.negative_example_max_title_chars,
    )
    prompt_text = config.prompts.filter.text or ""
    try:
        filter_prompt = prompt_text.format(
            profile_description=(
                profile_cfg.description if profile_cfg else plan.profile
            ),
            negative_examples=neg_block,
            min_score_in_results=config.filter.min_score_in_results,
        )
    except (KeyError, IndexError):
        filter_prompt = prompt_text

    return FeedbackResult(filter_prompt=filter_prompt), StageDiagnostics()


async def _run_acquire_stage(
    plan: RunPlan,
    *,
    item_provider: ItemProvider,
    client: LithosClient,
    filter_prompt: str,
) -> tuple[AcquireResult, StageDiagnostics]:
    """Stage 3 — Source.fetch_candidates → Filter.score → Source.acquire."""
    terminal_ids = await client.list_archive_terminal_arxiv_ids(
        profile=plan.profile,
    )
    terminal_token = current_archive_terminal_arxiv_ids.set(terminal_ids)
    try:
        items = list(
            await item_provider(
                plan.profile, plan.kind, plan.date_window, filter_prompt
            )
        )
    finally:
        current_archive_terminal_arxiv_ids.reset(terminal_token)
    logger.info(
        "source acquisition completed profile=%s kind=%s candidates=%d",
        plan.profile,
        plan.kind.value,
        len(items),
    )
    return AcquireResult(items=tuple(items)), StageDiagnostics()


def _item_source(item: ProfileItem) -> str:
    """Derive the ``source`` family for metric labels (mirrors scheduler)."""
    direct = item.get("source")
    if isinstance(direct, str) and direct:
        return direct
    for tag in item.get("tags", []) or []:
        if isinstance(tag, str) and tag.startswith("source:"):
            value = tag.split(":", 1)[1]
            return "rss" if value not in ("arxiv",) else "arxiv"
    return "unknown"


async def _run_ingest_stage(
    plan: RunPlan,
    *,
    items: tuple[ProfileItem, ...],
    client: LithosClient,
    lcma_deps: LcmaWiringDeps,
    ledger: RunLedger | None,
) -> tuple[IngestResult, StageDiagnostics]:
    """Stage 4 — per-item: cache_lookup → write → LcmaWiring.wire.

    Behaviour preserved verbatim from ``_run_profile_body``: cache-hit
    multi-profile-merge writes, slug-collision unresolved backlog,
    LCMA wiring on every successful write.  Backfills skip cache-hit
    items (FR-BF-2) when ``plan.skip_cache_hits`` is true.
    """
    profile = plan.profile
    ingested: list[HighlightItem] = []
    sources_checked = 0
    tracer = get_tracer()

    for item in items:
        sources_checked += 1
        title = item["title"]
        record_filter_result(profile, title, item.get("filter_tags", []))
        source_url = item["source_url"]
        item_source = item.get("source") or _item_source(item)
        metrics.articles_inspected().add(1, {"profile": profile, "source": item_source})
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
        cache_hit = bool(cache_body.get("hit"))
        if cache_hit:
            metrics.cache_hits().add(1, {"profile": profile, "source": item_source})
            logger.info(
                "article cache hit profile=%s source_url=%s title=%r kind=%s action=%s",
                profile,
                source_url,
                title,
                plan.kind.value,
                "skip" if plan.skip_cache_hits else "merge-profile",
            )
            if plan.skip_cache_hits:
                continue
            # Multi-profile merge — fall through to write path.

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
                "title=%r status=%s note_id=%s cache_hit=%s",
                profile,
                source_url,
                title,
                write_result.status,
                write_result.note_id,
                str(cache_hit).lower(),
            )
            related_in_lithos = await lcma_wire(
                written_note_id=write_result.note_id,
                cascade=CascadeOutput(
                    title=title,
                    contributions=item.get("contributions"),
                    builds_on=item.get("builds_on"),
                ),
                deps=lcma_deps,
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
        elif not cache_hit:
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
            if write_result.status == "slug_collision":
                metrics.slug_collision_unresolved().add(
                    1,
                    {"profile": profile, "source": item_source},
                )
                if ledger is not None:
                    ledger.record_unresolved_slug_collision(
                        profile=profile,
                        source=item_source,
                        source_url=source_url,
                        title=title,
                        detail=write_result.detail,
                        run_id=current_run_id.get() or "",
                    )

    return (
        IngestResult(
            ingested=tuple(ingested),
            sources_checked=sources_checked,
        ),
        StageDiagnostics(),
    )


async def _run_finalise_stage(
    plan: RunPlan,
    *,
    ingest: IngestResult,
    client: LithosClient,
    config: AppConfig,
) -> tuple[FinaliseResult, StageDiagnostics]:
    """Stage 5 — assemble RunOutcome, fire rejection-rate + webhook hooks."""
    profile = plan.profile
    sources_checked = ingest.sources_checked
    ingested_items = list(ingest.ingested)
    profile_run_result = ProfileRunResult(
        run_date=datetime.now(UTC).date().isoformat(),
        profile=profile,
        stats=RunStats(
            sources_checked=sources_checked,
            ingested=len(ingested_items),
        ),
        items=ingested_items,
    )

    await rejection_rate_on_run_complete(
        profile,
        config=config,
        client=client,
        sources_checked=sources_checked,
        ingested=len(ingested_items),
    )

    if plan.notify:
        # Lazy import to avoid the service ↔ run import cycle.
        from influx.service import post_run_webhook_hook

        post_run_webhook_hook(
            profile_run_result,
            config,
            kind=plan.kind,
            run_id=current_run_id.get() or None,
        )

    outcome = RunOutcome(
        sources_checked=sources_checked,
        ingested=len(ingested_items),
        profile_run_result=profile_run_result,
        source_acquisition_errors=tuple(current_source_acquisition_errors.get() or []),
    )
    return FinaliseResult(outcome=outcome), StageDiagnostics()


# ── Health-action dispatch ──────────────────────────────────────────


def _apply_health_actions(
    actions: Iterable[HealthAction], probe_loop: Any | None
) -> None:
    """Apply each declarative ``HealthAction`` to the probe loop.

    Non-fatal: a probe loop that doesn't expose the latch method is
    silently ignored, mirroring the legacy ``hasattr`` guards.
    """
    if probe_loop is None:
        return
    for action in actions:
        if action.latch == "repair_write_failure":
            method_name = (
                "mark_repair_write_failure"
                if action.op == "flip"
                else "clear_repair_write_failure"
            )
            method = getattr(probe_loop, method_name, None)
            if method is None:
                continue
            try:
                if action.op == "flip":
                    method(profile=action.detail or "")
                else:
                    method()
            except Exception:
                logger.warning(
                    "Failed to apply health action %s/%s",
                    action.op,
                    action.latch,
                    exc_info=True,
                )


# ── Run executor ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Run:
    """Score-gated ingestion executor (CONTEXT.md ``Run``).

    Built once per request from a :class:`RunPlan` + :class:`RunDeps`;
    :meth:`execute` runs the full lifecycle and returns the
    :class:`RunOutcome`.
    """

    plan: RunPlan
    deps: RunDeps

    async def execute(self) -> RunOutcome:
        """Run the body: Lithos task lifecycle + five stages.

        For #58 the outer ledger lifecycle stays in
        :func:`influx.scheduler.run_profile`'s prelude/postlude;
        ``execute()`` only owns the body (Lithos task CM + five
        stages).  Body exceptions propagate; the caller's
        prelude/postlude marks the ledger failed.

        ``RunAborted`` is caught here so its diagnostics' health
        actions are applied before re-raising — keeping latch-flip
        behaviour consistent with the legacy code path.
        """
        plan = self.plan
        deps = self.deps
        config = deps.config
        probe_loop = deps.probe_loop

        client = LithosClient(url=config.lithos.url, transport=config.lithos.transport)
        try:
            async with lithos_task_lifecycle(plan, client) as run_task_id:
                profile_cfg = next(
                    (p for p in config.profiles if p.name == plan.profile),
                    None,
                )
                lcma_deps = LcmaWiringDeps(
                    client=client,
                    profile=plan.profile,
                    run_task_id=run_task_id,
                    lcma_edge_score=(
                        profile_cfg.thresholds.lcma_edge_score if profile_cfg else 0.75
                    ),
                )

                diagnostics = StageDiagnostics()
                try:
                    # Stage 1 — Repair (may RunAbort)
                    _, d1 = await _run_repair_stage(plan, client=client, config=config)
                    diagnostics = _merge_diagnostics(diagnostics, d1)
                    _apply_health_actions(d1.health_actions, probe_loop)

                    # Stage 2 — Feedback
                    feedback, d2 = await _run_feedback_stage(
                        plan, client=client, config=config
                    )
                    diagnostics = _merge_diagnostics(diagnostics, d2)
                    _apply_health_actions(d2.health_actions, probe_loop)

                    # Stage 3 — Acquire
                    acquire, d3 = await _run_acquire_stage(
                        plan,
                        item_provider=deps.item_provider,
                        client=client,
                        filter_prompt=feedback.filter_prompt,
                    )
                    diagnostics = _merge_diagnostics(diagnostics, d3)
                    _apply_health_actions(d3.health_actions, probe_loop)

                    # Stage 4 — Ingest
                    ingest, d4 = await _run_ingest_stage(
                        plan,
                        items=acquire.items,
                        client=client,
                        lcma_deps=lcma_deps,
                        ledger=deps.ledger,
                    )
                    diagnostics = _merge_diagnostics(diagnostics, d4)
                    _apply_health_actions(d4.health_actions, probe_loop)

                    # Stage 5 — Finalise
                    finalise, d5 = await _run_finalise_stage(
                        plan,
                        ingest=ingest,
                        client=client,
                        config=config,
                    )
                    diagnostics = _merge_diagnostics(diagnostics, d5)
                    _apply_health_actions(d5.health_actions, probe_loop)

                    return finalise.outcome
                except RunAborted as exc:
                    diagnostics = _merge_diagnostics(diagnostics, exc.diagnostics)
                    _apply_health_actions(exc.diagnostics.health_actions, probe_loop)
                    # Re-raise the original cause (e.g. SweepWriteError)
                    # so callers see the same exception type the legacy
                    # ``_run_profile_body`` raised.  ``RunAborted`` is a
                    # purely internal signal that lets stages declare
                    # diagnostics alongside the abort; it never escapes
                    # the Run module.
                    if exc.__cause__ is not None:
                        raise exc.__cause__ from None
                    raise
        finally:
            await client.close()


# ── ItemProvider helper ─────────────────────────────────────────────


async def default_item_provider(
    profile: str,
    kind: RunKind,
    run_range: dict[str, str | int] | None,
    filter_prompt: str,
) -> Iterable[ProfileItem]:
    """No-op fallback (mirrors ``influx.scheduler.default_item_provider``)."""
    del profile, kind, run_range, filter_prompt
    return ()


# Re-export of :class:`asyncio.CancelledError` so ``execute()`` callers
# can match the same cancellation contract as the legacy code path.
CancelledError = asyncio.CancelledError
