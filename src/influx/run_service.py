"""RunService — request-lifecycle wrapper around :class:`influx.run.Run`.

The collaborator that owns "build :class:`RunPlan` → execute
:class:`Run` → record outcome → tick lifecycle metrics" for one
request (CONTEXT.md ``RunService``).

Architecture (after #58 → #59 → #60 → #61)::

    RunService.execute(plan)
        ├─ skip-gate checks (#40 circuit breaker, #69 LCMA tools)
        ├─ ledger.start + run-level metrics + contextvars + tracer span
        ├─ Run(plan, deps).execute()      ← five stages + Lithos task CM
        └─ ledger.complete / ledger.fail / ledger.skip + metric tick

The scheduler's three entry points (scheduled tick, ``POST /runs``,
``POST /backfills``) become thin :class:`RunPlan` builders that hand
off to :class:`RunService`.

This module replaces the legacy ``_run_profile_body`` orchestrator
plus the inline prelude/postlude that lived in
``scheduler.run_profile``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from influx import metrics
from influx.config import AppConfig
from influx.notifications import ProfileRunResult
from influx.run import (
    ItemProvider,
    Run,
    RunDeps,
    RunOutcome,
    RunPlan,
    default_item_provider,
)
from influx.run_ledger import RunLedger
from influx.telemetry import (
    current_cache_hits,
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


def _format_top_drivers_tail(
    *,
    archive_failures: list[dict[str, Any]],
    source_acquisition_errors: list[dict[str, str]],
    reasons: list[str],
    top_n: int = 3,
) -> str:
    """Return a compact log tail summarising the top degradation drivers (#152).

    Builds a short string like
    ``"archive:ieee.org=4,arxiv.org=2;source:arxiv/rate_limit_upstream_capacity=1"``
    so a single log line carries the dominant per-domain / per-kind
    drivers without the operator having to fetch ``/runs/recent`` to
    triage.  Returns ``""`` for non-degraded runs (``reasons`` empty).

    Bounded to *top_n* entries per category to keep the line readable.
    """
    if not reasons:
        return ""

    # Build a tiny on-the-fly aggregation rather than re-running the
    # full ``build_degradation_summary`` — the log line only needs the
    # one-line top-N digest, not the full structured shape.
    archive_by_domain: dict[str, int] = {}
    for record in archive_failures:
        url = str(record.get("url") or "")
        if not url:
            continue
        try:
            from urllib.parse import urlparse

            host = (urlparse(url).hostname or "").lower()
        except (TypeError, ValueError):
            host = ""
        if host:
            archive_by_domain[host] = archive_by_domain.get(host, 0) + 1

    source_by_pair: dict[str, int] = {}
    for err in source_acquisition_errors:
        source = str(err.get("source") or "unknown")
        kind = str(err.get("kind") or "unknown")
        key = f"{source}/{kind}"
        source_by_pair[key] = source_by_pair.get(key, 0) + 1

    parts: list[str] = []
    if archive_by_domain:
        items = sorted(archive_by_domain.items(), key=lambda kv: (-kv[1], kv[0]))[
            :top_n
        ]
        parts.append("archive:" + ",".join(f"{k}={v}" for k, v in items))
    if source_by_pair:
        items = sorted(source_by_pair.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
        parts.append("source:" + ",".join(f"{k}={v}" for k, v in items))
    return ";".join(parts)


# ── Outer ledger lifecycle (Q2 grilling — Option B, structural CM) ──


@dataclass(slots=True)
class _LedgerSession:
    """Mutable handle :func:`ledger_lifecycle` yields to its body.

    The body sets ``skip_reason`` to record a skip-before-task case
    (circuit breaker, LCMA tools unavailable); the CM writes
    ``ledger.skip(...)`` on exit instead of ``ledger.complete(...)``.
    ``outcome`` flows back from the body so the CM can hand the
    correct ``sources_checked`` / ``ingested`` to ``ledger.complete``.
    """

    run_id: str
    started_at: datetime
    skip_reason: str | None = None
    outcome: RunOutcome | None = None
    error: BaseException | None = None


@asynccontextmanager
async def ledger_lifecycle(
    plan: RunPlan, deps: RunDeps
) -> AsyncIterator[_LedgerSession]:
    """Outer lifecycle CM — ledger entry + run-level metrics + contextvars.

    Mirrors the prelude/postlude block from the legacy
    ``run_profile``: ``ledger.start`` on enter, ``ledger.complete``
    or ``ledger.fail`` on exit, with metrics ticked at both ends.
    Skip cases (circuit breaker / LCMA tools unavailable) set
    ``session.skip_reason`` and the CM writes a ``ledger.skip`` entry.
    """
    config = deps.config
    profile = plan.profile
    run_id = deps.run_id or str(uuid.uuid4())
    ledger = deps.ledger or RunLedger(Path(config.storage.state_dir))

    started_at = datetime.now(UTC)
    session = _LedgerSession(run_id=run_id, started_at=started_at)
    run_id_token = current_run_id.set(run_id)
    source_errors_token = current_source_acquisition_errors.set([])
    # Issue #146: per-run list of cooldown-suppressed source fetches.
    # Distinct from ``current_source_acquisition_errors`` because a
    # cooldown skip is a deliberate operational decision, not an
    # upstream failure — the run-ledger entry tags it as
    # ``source_cooldown_skip`` so operators can tell the two apart.
    source_cooldown_skips_token = current_source_cooldown_skips.set([])
    # #85: per-run pre-filter fetched-count.  A list-of-ints so source
    # adapters increment a shared mutable container; reads at run-end
    # land the value into the run-ledger entry for the
    # filter_stall vs fetch_stall split.
    fetched_total_counter: list[int] = [0]
    fetched_total_token = current_fetched_total.set(fetched_total_counter)
    # #85 review: per-run count of FilterScorerError catches.  Lets the
    # ledger fire ``filter_error`` (scorer execution failed) instead of
    # misclassifying as ``filter_stall`` (scorer ran, rejected all).
    filter_errors_counter: list[int] = [0]
    filter_errors_token = current_filter_errors.set(filter_errors_counter)
    # #131: per-run count of source items rejected because their article
    # URL failed syntactic validation (loopback / private / malformed).
    # Surfaced on the ledger entry as ``invalid_url_rejections_total``
    # so a healthy fetch with bad item links is visibly distinct from a
    # transient archive-download failure.
    invalid_url_rejections_counter: list[int] = [0]
    invalid_url_rejections_token = current_invalid_url_rejections.set(
        invalid_url_rejections_counter
    )
    # #166: per-run count of items dropped by the thin-summary rule.
    # Source adapters increment via :func:`record_summary_thin_drop`
    # when the archive fetch failed and the feed summary was thin.
    # Surfaced on the run-ledger entry as ``summary_thin_drops_total``
    # so an operator can see "we suppressed N summary-only notes this
    # run" distinct from archive failures and URL rejections.
    summary_thin_drops_counter: list[int] = [0]
    summary_thin_drops_token = current_summary_thin_drops.set(
        summary_thin_drops_counter
    )
    # #189: per-run count of notes written despite having no usable
    # source (blank source tag AND no arxiv-inferable URL).  Source
    # builders increment via :func:`record_empty_source_write`.  Surfaced
    # on the run-ledger entry as ``empty_source_writes_total`` so an
    # operator can measure how often this happens; observe-only, never a
    # drop and never a degraded reason (mirrors summary_thin_drops).
    empty_source_writes_counter: list[int] = [0]
    empty_source_writes_token = current_empty_source_writes.set(
        empty_source_writes_counter
    )
    # #129: per-run counter of recovered source-fetch retries (i.e.
    # retries followed by another attempt, distinct from the swallowed
    # final errors recorded via :func:`record_source_acquisition_error`).
    # A shared mutable dict so source adapters can ``setdefault`` /
    # increment without ContextVar.set semantics — same pattern as
    # ``current_source_acquisition_errors``.
    source_retry_counts: dict[str, dict[str, int]] = {}
    source_retry_counts_token = current_source_retry_counts.set(source_retry_counts)
    # #151: per-run counts of Tier 3 enrichment fallbacks split by
    # classification (``harmless`` vs ``degraded``).  Surfaced on the
    # run-ledger entry as ``tier3_fallbacks`` so ``/runs/recent`` can
    # distinguish "Tier 3 noise that didn't break notes" from "Tier 3
    # failed AND Tier 1 was also missing — note materially incomplete".
    tier3_fallback_counts: dict[str, int] = {}
    tier3_fallback_counts_token = current_tier3_fallback_counts.set(
        tier3_fallback_counts
    )
    # #152: per-run cache-hit counter so the ledger's
    # ``degradation_summary.totals.cache_hits`` carries the dedupe
    # volume.  Deliberately segregated from the degradation-driving
    # counters: cache hits are expected behaviour (especially on
    # backfills) and must NOT show up in archive / source-acquisition
    # breakdowns.
    cache_hits_counter: list[int] = [0]
    cache_hits_token = current_cache_hits.set(cache_hits_counter)
    # #152 review: per-run write-outcome counter so the ledger's
    # ``degradation_summary.writes.by_outcome`` /
    # ``degradation_summary.invalid_note_state.by_kind`` carry the
    # write-time breakdowns.  Shared mutable dict so the Ingest stage
    # can increment via :func:`record_write_outcome` without re-setting
    # the ContextVar — same pattern as
    # ``current_source_acquisition_errors``.
    write_outcomes_counter: dict[tuple[str, str], int] = {}
    write_outcomes_token = current_write_outcomes.set(write_outcomes_counter)
    metric_attrs = {"profile": profile, "run_type": plan.kind.value}

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

    tracer = get_tracer()
    try:
        with tracer.span(
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
        # Telemetry span exited cleanly; finalise the ledger entry.
        elapsed = (datetime.now(UTC) - started_at).total_seconds()
        source_errors = current_source_acquisition_errors.get() or []
        source_cooldown_skips = current_source_cooldown_skips.get() or []

        if session.skip_reason is not None:
            ledger.skip(run_id=run_id, reason=session.skip_reason)
            metrics.runs_skipped().add(
                1, {"profile": profile, "reason": session.skip_reason}
            )
            metrics.run_duration().record(elapsed, metric_attrs)
            # Issue #164 review: every ``run_completions`` series carries
            # the ``severity`` label so dashboard queries can filter
            # uniformly.  ``skipped`` runs never ran the body and have
            # no degraded-reasons list, so the value is ``not_applicable``.
            metrics.run_completions().add(
                1,
                {
                    **metric_attrs,
                    "outcome": "skipped",
                    "severity": "not_applicable",
                },
            )
            logger.warning(
                "run skipped profile=%s kind=%s run_id=%s reason=%s",
                profile,
                plan.kind.value,
                run_id,
                session.skip_reason,
            )
            return

        outcome = session.outcome
        sources_checked = outcome.sources_checked if outcome is not None else None
        ingested = outcome.ingested if outcome is not None else None
        archive_failures_total = 0
        # Issue #149: per-policy-kind breakdown of archive failures so
        # run summaries distinguish blocked / rate-limited / skip
        # diagnostic shapes from generic failures.  Computed alongside
        # the existing ``archive_failures_total`` so the ledger /
        # degraded-reasons logic is unchanged for downstream callers.
        archive_blocked_total = 0
        archive_rate_limited_total = 0
        archive_skipped_by_policy_total = 0
        # Issue #161: separate counter for ``unsupported`` policy items.
        # These deliberately do NOT contribute to
        # ``archive_failures_total`` (the operator declared the domain
        # has no archive surface), but the per-run total still needs to
        # surface so an operator sees "we hit N unsupported-policy
        # items this run" in the INFO log line below.
        archive_unsupported_total = 0
        # #152: per-item archive-failure records (url + tags) so the
        # ledger's degradation summary can compute the by-domain and
        # by-kind top-N.  One entry per item that landed
        # ``influx:archive-missing`` regardless of which (if any) more
        # specific policy tag accompanies it.
        archive_failures: list[dict[str, Any]] = []
        if outcome is not None and outcome.profile_run_result is not None:
            for item in outcome.profile_run_result.items:
                if "influx:archive-missing" in item.tags:
                    archive_failures_total += 1
                    archive_failures.append({"url": item.url, "tags": list(item.tags)})
                if "influx:archive-blocked" in item.tags:
                    archive_blocked_total += 1
                if "influx:archive-rate-limited" in item.tags:
                    archive_rate_limited_total += 1
                if "influx:archive-skipped-by-policy" in item.tags:
                    archive_skipped_by_policy_total += 1
                if "influx:archive-unsupported" in item.tags:
                    archive_unsupported_total += 1
        # #85: pull the pre-filter fetched-count from the contextvar
        # bucket the source layer accumulated into.  ``outcome`` may be
        # ``None`` on the early-skip / abort paths; in that case we
        # still pass the counter value (typically 0) so the ledger
        # entry carries the field for downstream consumers.
        fetched_total = fetched_total_counter[0]
        # #85 review: same pattern for filter-execution failures.
        filter_errors_total = filter_errors_counter[0]
        # #131: total per-run pre-acquire URL rejections.
        invalid_url_rejections_total = invalid_url_rejections_counter[0]
        # #166: total per-run thin-summary suppressions (items dropped
        # because archive fetch failed AND the feed summary was thin).
        # Deliberately distinct from archive_failures_total — these
        # items never reached the write loop.
        summary_thin_drops_total = summary_thin_drops_counter[0]
        # #189: total per-run notes written with no usable source.
        # Observe-only — distinct from any failure total; never feeds a
        # degraded reason.
        empty_source_writes_total = empty_source_writes_counter[0]
        # #152: total cache hits this run, surfaced under the
        # degradation summary's ``totals.cache_hits``.
        cache_hits_total = cache_hits_counter[0]
        degraded_reasons = ledger.complete(
            run_id=run_id,
            sources_checked=sources_checked,
            ingested=ingested,
            fetched_total=fetched_total,
            filter_errors_total=filter_errors_total,
            invalid_url_rejections_total=invalid_url_rejections_total,
            archive_failures_total=archive_failures_total,
            summary_thin_drops_total=summary_thin_drops_total,
            empty_source_writes_total=empty_source_writes_total,
            source_acquisition_errors=source_errors,
            # Issue #146: cooldown skips are run-level state distinct
            # from swallowed acquisition errors — the ledger fires the
            # ``source_cooldown_skip`` reason on a non-empty list.
            source_cooldown_skips=source_cooldown_skips,
            # #129: surface recovered retry counts so the ledger entry
            # carries "we hit arXiv 429 twice but recovered" alongside
            # the swallowed-error list, letting an operator distinguish
            # transient retry success from a burned retry budget.
            source_retry_counts=source_retry_counts,
            # #151: surface harmless-vs-degraded Tier 3 fallback counts
            # so run summaries / ``/runs/recent`` can tell apart noise
            # from material degradation without scraping logs.
            tier3_fallbacks=tier3_fallback_counts,
            # #152: feed per-item archive failures + cache-hit total
            # into the bounded top-N degradation summary computed by
            # ``ledger.complete``.
            archive_failures=archive_failures,
            cache_hits_total=cache_hits_total,
            # #152 review: per-(outcome, source) write counts collected
            # at the Ingest stage drive
            # ``degradation_summary.writes.by_outcome`` (all outcomes
            # including duplicate) and
            # ``degradation_summary.invalid_note_state.by_kind`` (only
            # materially-failed outcomes — feeds the
            # ``invalid_note_state`` degraded reason when non-zero).
            write_outcomes=write_outcomes_counter,
        )
        run_outcome = (
            "degraded" if (source_errors or source_cooldown_skips) else "success"
        )
        if "ingestion_stall" in degraded_reasons:
            metrics.ingestion_stalls().add(
                1, {"profile": profile, "reason": "ingestion_stall"}
            )
            if run_outcome == "success":
                run_outcome = "degraded"
            logger.warning(
                "run flagged ingestion_stall profile=%s kind=%s run_id=%s "
                "(this + prior scheduled run both ingested 0 with "
                "sources_checked > 0)",
                profile,
                plan.kind.value,
                run_id,
            )
        if "fetch_stall" in degraded_reasons:
            metrics.ingestion_stalls().add(
                1, {"profile": profile, "reason": "fetch_stall"}
            )
            if run_outcome == "success":
                run_outcome = "degraded"
            logger.warning(
                "run flagged fetch_stall profile=%s kind=%s run_id=%s "
                "(this + prior scheduled run both saw fetched_total == 0 "
                "despite prior non-zero history — likely too-narrow "
                "lookback_days or upstream feed change)",
                profile,
                plan.kind.value,
                run_id,
            )
        if "filter_stall" in degraded_reasons:
            # #85: items WERE fetched (fetched_total > 0), the filter
            # ran cleanly, and rejected every candidate so
            # sources_checked landed at 0.  Operator triage points at
            # filter-side causes — distinct from fetch_stall (fetch
            # side) and filter_error (scorer execution failure).
            metrics.ingestion_stalls().add(
                1, {"profile": profile, "reason": "filter_stall"}
            )
            if run_outcome == "success":
                run_outcome = "degraded"
            logger.warning(
                "run flagged filter_stall profile=%s kind=%s run_id=%s "
                "fetched_total=%d "
                "(this + prior scheduled run both fetched items but the "
                "filter rejected all candidates — check profile "
                "description, filter prompt, or min_score_in_results)",
                profile,
                plan.kind.value,
                run_id,
                fetched_total,
            )
        if "filter_error" in degraded_reasons:
            # #85 review: the filter scorer raised FilterScorerError at
            # least once this run (transport / parse / provider).
            # Distinct from filter_stall (scorer ran cleanly, rejected
            # all) — operators should look at provider config, model
            # availability, and recent prompt schema changes, NOT the
            # profile description or score threshold.  Single-run
            # signal: no consecutive-runs gate.
            metrics.ingestion_stalls().add(
                1, {"profile": profile, "reason": "filter_error"}
            )
            if run_outcome == "success":
                run_outcome = "degraded"
            logger.warning(
                "run flagged filter_error profile=%s kind=%s run_id=%s "
                "filter_errors=%d "
                "(filter scorer raised FilterScorerError — check "
                "provider config, model availability, response "
                "schema; not a profile/prompt issue)",
                profile,
                plan.kind.value,
                run_id,
                filter_errors_total,
            )
        if "archive_acquisition" in degraded_reasons:
            if run_outcome == "success":
                run_outcome = "degraded"
            logger.warning(
                "run flagged archive_acquisition profile=%s kind=%s run_id=%s "
                "archive_failures=%d blocked=%d rate_limited=%d "
                "skipped_by_policy=%d "
                "(accepted items were ingested with influx:archive-missing "
                "after archive download failed; issue #149: blocked / "
                "rate_limited / skipped_by_policy break down the structural "
                "domain-policy failure shapes)",
                profile,
                plan.kind.value,
                run_id,
                archive_failures_total,
                archive_blocked_total,
                archive_rate_limited_total,
                archive_skipped_by_policy_total,
            )
        if "source_cooldown_skip" in degraded_reasons:
            # Issue #146: one or more source fetches were suppressed by
            # the adaptive 429 cooldown.  Distinct from
            # ``source_acquisition``: we deliberately chose not to call
            # upstream, so the run-level outcome is "we backed off"
            # rather than "we tried and lost".  Operators triaging this
            # reason check the cooldown thresholds / cooldown duration
            # and the recent 429 classification breakdown.
            if run_outcome == "success":
                run_outcome = "degraded"
            logger.warning(
                "run flagged source_cooldown_skip profile=%s kind=%s "
                "run_id=%s cooldown_skips=%d "
                "(arxiv fetch was skipped after repeated 429 bursts — "
                "see resilience.arxiv_429_cooldown_threshold / "
                "arxiv_429_cooldown_seconds)",
                profile,
                plan.kind.value,
                run_id,
                len(source_cooldown_skips),
            )
        if "invalid_url_stall" in degraded_reasons:
            # #131 review concern 2: feed returned items but every URL
            # was upstream-malformed.  Single-run signal — operators
            # should look at the upstream feed's emitted ``<link>``
            # values, not the profile description, filter prompt, or
            # provider config.
            metrics.ingestion_stalls().add(
                1, {"profile": profile, "reason": "invalid_url_stall"}
            )
            if run_outcome == "success":
                run_outcome = "degraded"
            logger.warning(
                "run flagged invalid_url_stall profile=%s kind=%s run_id=%s "
                "invalid_url_rejections=%d fetched_total=%d "
                "(feed returned items but every entry link was "
                "upstream-malformed — check the upstream feed publisher)",
                profile,
                plan.kind.value,
                run_id,
                invalid_url_rejections_total,
                fetched_total,
            )
        if "invalid_note_state" in degraded_reasons:
            # #152 review: at least one write-time outcome was a real
            # write failure (``invalid_input`` / ``version_conflict`` /
            # ``slug_collision`` / ``content_too_large*``).  Distinct
            # from duplicates (the contract-expected outcome of
            # scheduled re-runs, PR #153) — operators should look at
            # schema drift, repair backlog, or id collisions, not
            # the dedupe pipeline.
            if run_outcome == "success":
                run_outcome = "degraded"
            # Build the by-kind digest locally from the counter to
            # avoid re-reading the ledger entry we just appended.
            invalid_kind_counts: dict[str, int] = {}
            for (
                write_outcome_kind,
                _write_outcome_source,
            ), count in write_outcomes_counter.items():
                if count <= 0:
                    continue
                if write_outcome_kind in (
                    "invalid_input",
                    "version_conflict",
                    "slug_collision",
                    "content_too_large",
                    "content_too_large_skipped",
                ):
                    invalid_kind_counts[write_outcome_kind] = (
                        invalid_kind_counts.get(write_outcome_kind, 0) + count
                    )
            invalid_note_state_summary = ",".join(
                f"{kind}={count}"
                for kind, count in sorted(
                    invalid_kind_counts.items(), key=lambda kv: (-kv[1], kv[0])
                )
            )
            logger.warning(
                "run flagged invalid_note_state profile=%s kind=%s run_id=%s "
                "by_kind=%s "
                "(write-time failures landed in the materially-failed set "
                "— check schema drift, repair backlog, or upstream id "
                "collisions; duplicate outcomes are deliberately excluded "
                "from this flag)",
                profile,
                plan.kind.value,
                run_id,
                invalid_note_state_summary or "<none>",
            )
        # Issue #161: surface the unsupported-policy total at INFO so an
        # operator can see "we hit N items on archive-unsupported
        # domains this run" without the run being flagged as degraded.
        # Distinct from the ``archive_acquisition`` WARNING above —
        # ``unsupported`` is the expected, declared-terminal outcome,
        # not a failure.
        if archive_unsupported_total > 0:
            logger.info(
                "run archive_unsupported profile=%s kind=%s run_id=%s "
                "unsupported_items=%d "
                "(items on archive-unsupported policy domains — "
                "expected, not counted toward archive_acquisition)",
                profile,
                plan.kind.value,
                run_id,
                archive_unsupported_total,
            )
        # Issue #166: surface per-run thin-summary suppression total at
        # INFO so an operator sees "we dropped N summary-only notes
        # this run" alongside the unsupported total.  Distinct from
        # ``archive_acquisition`` — these items never received the
        # ``influx:archive-missing`` tag because they were dropped
        # before the write loop.
        if summary_thin_drops_total > 0:
            logger.info(
                "run summary_thin_drops profile=%s kind=%s run_id=%s "
                "dropped_items=%d "
                "(items suppressed by the thin-summary rule — archive "
                "fetch failed AND feed summary was thin; not counted "
                "toward archive_failures_total or archive_acquisition)",
                profile,
                plan.kind.value,
                run_id,
                summary_thin_drops_total,
            )
        # Issue #189: surface per-run empty-source writes at INFO so an
        # operator sees "we wrote N notes with no usable source this run"
        # — observe-only, never dropped, never a degraded reason.  This
        # is the signal that tells us whether a future #187-class
        # metadata loss is silently producing source-invalid candidates.
        if empty_source_writes_total > 0:
            logger.info(
                "run empty_source_writes profile=%s kind=%s run_id=%s "
                "written_items=%d "
                "(notes written with a blank source tag AND no "
                "arxiv-inferable URL; still written, not counted toward "
                "archive_failures_total or any degraded reason)",
                profile,
                plan.kind.value,
                run_id,
                empty_source_writes_total,
            )

        # Issue #164: classify the degraded reasons into the
        # ``severity`` bucket the ledger entry stores so the metric
        # label and the run-completed log line agree on the
        # ``"expected_lossy"`` / ``"unexpected_failure"`` split.  The
        # ledger has the authoritative computation; we mirror it
        # here for the metric attribute.
        from influx.run_ledger import classify_degradation_severity

        severity = classify_degradation_severity(list(degraded_reasons))

        metrics.run_duration().record(elapsed, metric_attrs)
        metrics.run_completions().add(
            1, {**metric_attrs, "outcome": run_outcome, "severity": severity}
        )

        if outcome is not None:
            # #79: tie ``degraded`` to ``run_outcome`` (the same source of
            # truth as the ``run_completions`` metric attribute) so any
            # future stall-reason additions stay in sync without touching
            # this call site.  ``reasons=...`` is appended for the
            # ``influx-diagnose failures`` grep path.
            #
            # #152: when degraded, append a compact ``top_drivers=...``
            # tail with the dominant archive-by-domain / source-by-kind
            # entries so the log line itself answers "what's actually
            # broken?" without requiring a ledger fetch.  Computed in
            # one shot via :func:`build_degradation_summary` so the log
            # tail and the persisted summary stay in lockstep.
            #
            # #164: emit ``severity=...`` alongside ``degraded=...``
            # so operators can immediately distinguish expected
            # upstream lossiness from real pipeline regressions
            # without re-deriving the bucket from the reason list.
            top_drivers = _format_top_drivers_tail(
                archive_failures=archive_failures,
                source_acquisition_errors=source_errors,
                reasons=degraded_reasons,
            )
            logger.info(
                "run completed profile=%s kind=%s run_id=%s duration=%.1fs "
                "sources_checked=%d ingested=%d degraded=%s severity=%s "
                "reasons=%s%s",
                profile,
                plan.kind.value,
                run_id,
                elapsed,
                outcome.sources_checked,
                outcome.ingested,
                run_outcome == "degraded",
                severity,
                ",".join(degraded_reasons) if degraded_reasons else "",
                f" top_drivers={top_drivers}" if top_drivers else "",
            )
        else:
            logger.info(
                "run completed profile=%s kind=%s run_id=%s duration=%.1fs result=none",
                profile,
                plan.kind.value,
                run_id,
                elapsed,
            )
    except BaseException:
        # The body raised — finalise as failure.
        elapsed = (datetime.now(UTC) - started_at).total_seconds()
        exc = session.error
        logger.exception(
            "run failed profile=%s kind=%s run_id=%s duration=%.1fs",
            profile,
            plan.kind.value,
            run_id,
            elapsed,
        )
        ledger.fail(
            run_id=run_id,
            error=f"{type(exc).__name__}: {exc}" if exc is not None else "unknown",
        )
        metrics.run_duration().record(elapsed, metric_attrs)
        # Issue #164 review: failure path also carries the ``severity``
        # label so the ``run_completions`` series shape is uniform
        # across outcomes.  ``failure`` runs raised before
        # ``degraded_reasons`` was computed, so the value is
        # ``not_applicable``.
        metrics.run_completions().add(
            1,
            {
                **metric_attrs,
                "outcome": "failure",
                "severity": "not_applicable",
            },
        )
        raise
    finally:
        metrics.active_runs().add(-1, {"profile": profile})
        current_run_id.reset(run_id_token)
        current_source_acquisition_errors.reset(source_errors_token)
        current_source_cooldown_skips.reset(source_cooldown_skips_token)
        current_fetched_total.reset(fetched_total_token)
        current_filter_errors.reset(filter_errors_token)
        current_invalid_url_rejections.reset(invalid_url_rejections_token)
        current_summary_thin_drops.reset(summary_thin_drops_token)
        current_empty_source_writes.reset(empty_source_writes_token)
        current_source_retry_counts.reset(source_retry_counts_token)
        current_tier3_fallback_counts.reset(tier3_fallback_counts_token)
        current_cache_hits.reset(cache_hits_token)
        current_write_outcomes.reset(write_outcomes_token)


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


# ── Backwards-compatibility helper for run_profile callers ──────────


async def run_via_service(
    profile: str,
    kind: Any,
    run_range: dict[str, str | int] | None = None,
    *,
    config: AppConfig,
    item_provider: ItemProvider | None = None,
    probe_loop: Any | None = None,
    run_id: str | None = None,
    run_ledger: RunLedger | None = None,
) -> ProfileRunResult | None:
    """Build a :class:`RunPlan`, drive a :class:`RunService`, return the
    legacy :class:`ProfileRunResult` for backward-compatible callers.

    ``run_profile`` (in :mod:`influx.scheduler`) delegates to this
    helper; the three scheduler entry points (cron tick, ``POST /runs``,
    ``POST /backfills``) ultimately route through here.

    The mapping from ``RunKind`` to ``RunPlan`` flag values mirrors the
    body that #58 / #59 / #60 migrated:

    - ``BACKFILL`` → ``skip_repair=True``, ``skip_cache_hits=True``,
      ``notify=False``.
    - everything else → all flags False (run_repair, write cache hits,
      notify) except ``notify=True``.
    """
    from influx.coordinator import RunKind

    is_backfill = kind == RunKind.BACKFILL
    plan = RunPlan(
        profile=profile,
        kind=kind,
        date_window=run_range,
        skip_repair=is_backfill,
        skip_cache_hits=is_backfill,
        notify=not is_backfill,
    )
    service = RunService(
        config=config,
        item_provider=item_provider,
        probe_loop=probe_loop,
        ledger=run_ledger,
    )
    outcome = await service.execute(plan, run_id=run_id)
    # Legacy ``_run_profile_body`` could return ``None`` (used by tests
    # that patched the body out).  Preserve that contract here.
    if outcome is None:
        return None
    return outcome.profile_run_result
