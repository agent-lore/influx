"""RunService — request-lifecycle wrapper around :class:`influx.run.Run`.

The collaborator that owns "build :class:`RunPlan` → execute
:class:`Run` → record outcome → tick lifecycle metrics" for one
request (CONTEXT.md ``RunService``).

Architecture (after #58 → #59 → #60 → #61)::

    RunService.execute(plan)
        ├─ skip-gate checks (#40 circuit breaker, #69 LCMA tools)
        ├─ ledger.start + run-level metrics + contextvars + tracer span
        ├─ Run(plan, deps).execute()      ← five stages + Lithos task CM
        └─ ledger.complete / ledger.fail / ledger.skip
             → RunCompletionAssessment → metrics + log lines

The scheduler's three entry points (scheduled tick, ``POST /runs``,
``POST /backfills``) route through :func:`influx.scheduler.run_profile`,
which builds the :class:`RunPlan` via :meth:`RunPlan.for_request` and
hands it to :class:`RunService`.

This module replaces the legacy ``_run_profile_body`` orchestrator
plus the inline prelude/postlude that lived in
``scheduler.run_profile``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from influx import metrics
from influx.config import AppConfig
from influx.errors import format_exception_for_ledger
from influx.run import (
    ItemProvider,
    Run,
    RunDeps,
    RunOutcome,
    RunPlan,
    default_item_provider,
)
from influx.run_assessment import (
    ArchiveFailureRecord,
    RunCompletionAssessment,
    RunEvidence,
)
from influx.run_ledger import RunLedger
from influx.telemetry import (
    current_cache_hits,
    current_dedup_lookup_errors,
    current_empty_source_writes,
    current_fetched_total,
    current_filter_errors,
    current_invalid_url_rejections,
    current_run_id,
    current_source_acquisition_errors,
    current_source_cooldown_skips,
    current_source_retry_counts,
    current_summary_thin_drops,
    current_tier3_fallback_counts,
    current_write_outcomes,
    get_tracer,
)

__all__ = ["RunService", "ledger_lifecycle"]

logger = logging.getLogger(__name__)


# ── Per-run accumulators ────────────────────────────────────────────

# The per-run buckets the pipeline writes into through the ``record_*``
# helpers in :mod:`influx.telemetry` (#20 #85 #129 #131 #146 #151 #152
# #166 #189 #234): ``(evidence field, contextvar, fresh bucket)``.
# Counters are one-element lists and tallies plain dicts so source
# adapters can mutate the shared bucket without re-setting the var.
_RUN_BUCKETS: tuple[tuple[str, ContextVar[Any], Callable[[], Any]], ...] = (
    ("source_acquisition_errors", current_source_acquisition_errors, list),
    ("source_cooldown_skips", current_source_cooldown_skips, list),
    ("dedup_lookup_errors", current_dedup_lookup_errors, list),
    ("fetched_total", current_fetched_total, lambda: [0]),
    ("filter_errors_total", current_filter_errors, lambda: [0]),
    ("invalid_url_rejections_total", current_invalid_url_rejections, lambda: [0]),
    ("summary_thin_drops_total", current_summary_thin_drops, lambda: [0]),
    ("empty_source_writes_total", current_empty_source_writes, lambda: [0]),
    ("cache_hits_total", current_cache_hits, lambda: [0]),
    ("source_retry_counts", current_source_retry_counts, dict),
    ("tier3_fallbacks", current_tier3_fallback_counts, dict),
    ("write_outcomes", current_write_outcomes, dict),
)


@contextmanager
def _bind_run_counters() -> Iterator[None]:
    """Bind a fresh bucket to every per-run contextvar; reset all on exit."""
    tokens = [(var, var.set(make())) for _name, var, make in _RUN_BUCKETS]
    try:
        yield
    finally:
        for var, token in tokens:
            var.reset(token)


def _read_run_counters() -> dict[str, Any]:
    """The buckets as the pipeline left them (a var reset to ``None`` reads empty)."""
    return {
        name: (bucket if (bucket := var.get()) is not None else make())
        for name, var, make in _RUN_BUCKETS
    }


# Note tag → the archive policy tally it feeds (#149 / #161).  Only
# ``influx:archive-missing`` items are archive *failures*; ``unsupported``
# is a declared-terminal outcome that is tallied for the INFO notice but
# never degrades the run.
_ARCHIVE_POLICY_TAGS: dict[str, str] = {
    "influx:archive-blocked": "archive_blocked_total",
    "influx:archive-rate-limited": "archive_rate_limited_total",
    "influx:archive-skipped-by-policy": "archive_skipped_by_policy_total",
    "influx:archive-unsupported": "archive_unsupported_total",
}


def _evidence_from(outcome: RunOutcome | None) -> RunEvidence:
    """Read the run's contextvar buckets and its :class:`RunOutcome` into evidence.

    ``outcome`` is ``None`` when the body returned nothing (legacy test
    shape); the counters still land so the entry carries them.
    """
    buckets = _read_run_counters()
    archive_failures: list[ArchiveFailureRecord] = []
    policy_totals = dict.fromkeys(_ARCHIVE_POLICY_TAGS.values(), 0)
    items = (
        outcome.profile_run_result.items
        if outcome is not None and outcome.profile_run_result is not None
        else []
    )
    for item in items:
        if "influx:archive-missing" in item.tags:
            archive_failures.append({"url": item.url, "tags": list(item.tags)})
        for tag, total in _ARCHIVE_POLICY_TAGS.items():
            if tag in item.tags:
                policy_totals[total] += 1
    return RunEvidence(
        sources_checked=outcome.sources_checked if outcome is not None else None,
        ingested=outcome.ingested if outcome is not None else None,
        fetched_total=buckets["fetched_total"][0],
        filter_errors_total=buckets["filter_errors_total"][0],
        invalid_url_rejections_total=buckets["invalid_url_rejections_total"][0],
        archive_failures_total=len(archive_failures),
        summary_thin_drops_total=buckets["summary_thin_drops_total"][0],
        empty_source_writes_total=buckets["empty_source_writes_total"][0],
        cache_hits_total=buckets["cache_hits_total"][0],
        source_acquisition_errors=tuple(buckets["source_acquisition_errors"]),
        source_cooldown_skips=tuple(buckets["source_cooldown_skips"]),
        dedup_lookup_errors=tuple(buckets["dedup_lookup_errors"]),
        archive_failures=tuple(archive_failures),
        source_retry_counts={
            source: dict(by_kind)
            for source, by_kind in buckets["source_retry_counts"].items()
        },
        tier3_fallbacks=dict(buckets["tier3_fallbacks"]),
        write_outcomes=dict(buckets["write_outcomes"]),
        **policy_totals,
    )


# ── Outer ledger lifecycle (Q2 grilling — Option B, structural CM) ──


@dataclass(slots=True)
class _LedgerSession:
    """Mutable handle :func:`ledger_lifecycle` yields to its body.

    The body sets ``skip_reason`` to record a skip-before-task case
    (circuit breaker, LCMA tools unavailable); the CM then records
    ``ledger.skip(...)`` instead of ``ledger.complete(...)``.
    ``outcome`` flows back so the evidence carries ``sources_checked``
    / ``ingested`` / the archive tags.
    """

    run_id: str
    started_at: datetime
    skip_reason: str | None = None
    outcome: RunOutcome | None = None
    error: BaseException | None = None


def _record_completion(
    assessment: RunCompletionAssessment, *, elapsed: float, metric_attrs: dict[str, str]
) -> None:
    """Tick the completion metrics from the assessment — every outcome."""
    for reason in assessment.stall_metric_reasons:
        metrics.ingestion_stalls().add(
            1, {"profile": metric_attrs["profile"], "reason": reason}
        )
    metrics.run_duration().record(elapsed, metric_attrs)
    metrics.run_completions().add(
        1,
        {
            **metric_attrs,
            "outcome": assessment.outcome,
            "severity": assessment.severity,
        },
    )


def _log_completion(
    assessment: RunCompletionAssessment,
    *,
    plan: RunPlan,
    elapsed: float,
    has_outcome: bool,
) -> None:
    """Emit the operator log lines for a ``completed`` run.

    One ``run flagged <reason>`` WARNING per degraded reason (counts +
    guidance from the assessment), one INFO notice per observe-only
    count, then the single ``run completed`` line whose ``degraded`` /
    ``severity`` / ``reasons`` / ``top_drivers`` fields are the same
    values the ledger persisted and the metric labelled.
    """
    profile, kind, run_id = plan.profile, plan.kind.value, assessment.run_id
    for reason in assessment.reasons:
        logger.warning(
            "run flagged %s profile=%s kind=%s run_id=%s %s",
            reason,
            profile,
            kind,
            run_id,
            assessment.warning_detail(reason),
        )
    for notice in assessment.notices:
        logger.info(
            "run %s profile=%s kind=%s run_id=%s %s=%d (%s)",
            notice.name,
            profile,
            kind,
            run_id,
            notice.count_label,
            notice.count,
            notice.guidance,
        )
    if not has_outcome:
        logger.info(
            "run completed profile=%s kind=%s run_id=%s duration=%.1fs result=none",
            profile,
            kind,
            run_id,
            elapsed,
        )
        return
    top_drivers = assessment.top_drivers
    logger.info(
        "run completed profile=%s kind=%s run_id=%s duration=%.1fs "
        "sources_checked=%d ingested=%d degraded=%s severity=%s reasons=%s%s",
        profile,
        kind,
        run_id,
        elapsed,
        assessment.evidence.sources_checked or 0,
        assessment.evidence.ingested or 0,
        assessment.degraded,
        assessment.severity,
        ",".join(assessment.reasons),
        f" top_drivers={top_drivers}" if top_drivers else "",
    )


@asynccontextmanager
async def ledger_lifecycle(
    plan: RunPlan, deps: RunDeps
) -> AsyncIterator[_LedgerSession]:
    """Outer lifecycle CM — ledger entry + run-level metrics + contextvars.

    ``ledger.start`` on enter; on exit exactly one of ``ledger.complete``
    / ``ledger.skip`` / ``ledger.fail`` produces the
    :class:`RunCompletionAssessment` that the metrics and log lines are
    derived from.  The body's exception propagates after the failure is
    recorded; the per-run contextvars are reset on every path.
    """
    config = deps.config
    profile = plan.profile
    run_id = deps.run_id or str(uuid.uuid4())
    ledger = deps.ledger or RunLedger(Path(config.storage.state_dir))
    started_at = datetime.now(UTC)
    session = _LedgerSession(run_id=run_id, started_at=started_at)
    metric_attrs = {"profile": profile, "run_type": plan.kind.value}

    run_id_token = current_run_id.set(run_id)
    with _bind_run_counters():
        ledger.start(
            run_id=run_id,
            profile=profile,
            kind=plan.kind.value,
            run_range=plan.date_window,
        )
        metrics.run_starts().add(1, metric_attrs)
        metrics.active_runs().add(1, {"profile": profile})
        logger.info(
            "run started profile=%s kind=%s run_id=%s range=%s",
            profile,
            plan.kind.value,
            run_id,
            plan.date_window or {},
        )
        try:
            try:
                with get_tracer().span(
                    "influx.run",
                    attributes={
                        "influx.profile": profile,
                        "influx.run_id": run_id,
                        "influx.run_type": plan.kind.value,
                    },
                ):
                    try:
                        yield session
                    except BaseException as exc:
                        session.error = exc
                        raise
            except BaseException:
                elapsed = (datetime.now(UTC) - started_at).total_seconds()
                exc = session.error
                # #234: the structured helper keeps the Lithos ``detail``
                # in both the log line and the ledger ``error`` field.
                error_text = (
                    format_exception_for_ledger(exc) if exc is not None else "unknown"
                )
                logger.exception(
                    "run failed profile=%s kind=%s run_id=%s duration=%.1fs error=%s",
                    profile,
                    plan.kind.value,
                    run_id,
                    elapsed,
                    error_text,
                )
                assessment = ledger.fail(run_id=run_id, error=error_text)
                _record_completion(
                    assessment, elapsed=elapsed, metric_attrs=metric_attrs
                )
                raise

            # The body returned: finalise outside the failure scope so a
            # metric or log problem here can never re-finalise the run.
            elapsed = (datetime.now(UTC) - started_at).total_seconds()
            if session.skip_reason is not None:
                assessment = ledger.skip(run_id=run_id, reason=session.skip_reason)
                metrics.runs_skipped().add(
                    1, {"profile": profile, "reason": session.skip_reason}
                )
                _record_completion(
                    assessment, elapsed=elapsed, metric_attrs=metric_attrs
                )
                logger.warning(
                    "run skipped profile=%s kind=%s run_id=%s reason=%s",
                    profile,
                    plan.kind.value,
                    run_id,
                    session.skip_reason,
                )
            else:
                assessment = ledger.complete(
                    run_id=run_id, evidence=_evidence_from(session.outcome)
                )
                _record_completion(
                    assessment, elapsed=elapsed, metric_attrs=metric_attrs
                )
                _log_completion(
                    assessment,
                    plan=plan,
                    elapsed=elapsed,
                    has_outcome=session.outcome is not None,
                )
        finally:
            metrics.active_runs().add(-1, {"profile": profile})
            current_run_id.reset(run_id_token)


# ── RunService ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunService:
    """Request-lifecycle wrapper that drives one :class:`Run` execution.

    Built once per request from a :class:`AppConfig` plus the
    long-lived dependencies (item_provider, probe_loop, ledger).  Call
    :meth:`execute` with a :class:`RunPlan`; returns the
    :class:`RunOutcome`.

    Each :meth:`execute` call:

    1. Checks the two probe-time skip gates (``lithos_circuit_open``,
       ``lcma_tools_unavailable``).  When either is set, writes a
       ``ledger.skip`` entry, ticks the ``runs_skipped`` metric, and
       returns ``RunOutcome(skipped=True, ...)`` without entering the
       Run body.
    2. Opens :func:`ledger_lifecycle` (ledger entry + metrics +
       contextvars + tracer span).
    3. Delegates to ``Run(plan, deps).execute()`` for the body.
    4. The CM finalises ``ledger.complete`` / ``ledger.fail`` based on
       success / exception path.
    """

    config: AppConfig
    item_provider: ItemProvider | None = None
    probe_loop: Any | None = None
    ledger: RunLedger | None = None

    async def execute(
        self,
        plan: RunPlan,
        *,
        run_id: str | None = None,
    ) -> RunOutcome:
        """Run the full request lifecycle and return the outcome."""
        provider = (
            self.item_provider
            if self.item_provider is not None
            else default_item_provider
        )
        deps = RunDeps(
            config=self.config,
            item_provider=provider,
            probe_loop=self.probe_loop,
            ledger=self.ledger,
            run_id=run_id,
        )

        # ── Skip gates ────────────────────────────────────────────
        skip_reason = self._skip_reason_for(plan)
        if skip_reason is not None:
            return await self._record_skip(plan, deps, skip_reason)

        # ── Lifecycle CM + body ──────────────────────────────────
        async with ledger_lifecycle(plan, deps) as session:
            outcome = await Run(plan, deps).execute()
            session.outcome = outcome
            return outcome

    def _skip_reason_for(self, plan: RunPlan) -> str | None:
        """Return the ``ledger.skip`` reason if a probe latch is set."""
        probe_loop = self.probe_loop
        if probe_loop is None:
            return None
        if (
            hasattr(probe_loop, "lithos_circuit_open")
            and probe_loop.lithos_circuit_open()
        ):
            return "lithos_unhealthy"
        if (
            hasattr(probe_loop, "lcma_tools_unavailable")
            and probe_loop.lcma_tools_unavailable()
        ):
            return "lcma_tools_unavailable"
        return None

    async def _record_skip(
        self, plan: RunPlan, deps: RunDeps, skip_reason: str
    ) -> RunOutcome:
        """Write a ``ledger.skip`` entry and tick the runs_skipped metric.

        Goes through :func:`ledger_lifecycle` so the skip path emits
        the same metric/log shape (``ledger.start`` + ``ledger.skip``
        + ``runs_skipped`` + ``run_completions{outcome=skipped}``) as
        the legacy ``run_profile`` prelude did.
        """
        async with ledger_lifecycle(plan, deps) as session:
            session.skip_reason = skip_reason
        return RunOutcome(skipped=True, skip_reason=skip_reason)
