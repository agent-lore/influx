"""Run-completion assessment — the one owner of "how did this run go?".

A :class:`RunEvidence` is what one run observed (counts and swallowed
error records); a :class:`RunCompletionAssessment` is the ledger's
verdict on it: the degraded reasons, their severity, the metric
``outcome`` label, which reasons tick the stall metric, and the
operator guidance each reason carries.  :class:`influx.run_ledger.RunLedger`
builds the assessment (adding the history-dependent stall reasons it
alone can see), persists it, and hands it back to
:func:`influx.run_service.ledger_lifecycle`, which derives the metrics
and the log lines from it.  Adding a degraded reason therefore means
editing this module: its rule in :func:`single_run_reasons` (or the
ledger's stall detector), its severity set, its stall-metric membership,
and its guidance — the meta-tests in ``tests/unit/test_run_ledger.py``
fail until all four agree.

Foundation tier: stdlib only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

__all__ = [
    "EXPECTED_LOSSY_REASONS",
    "KNOWN_DEGRADED_REASONS",
    "REASON_GUIDANCE",
    "STALL_METRIC_REASONS",
    "UNEXPECTED_FAILURE_REASONS",
    "ArchiveFailureRecord",
    "DegradedReasons",
    "RunCompletionAssessment",
    "RunEvidence",
    "RunNotice",
    "Severity",
    "WriteOutcomeCounts",
    "build_degradation_summary",
    "classify_degradation_severity",
    "single_run_reasons",
]

# One entry per item the run ingested with ``influx:archive-missing``:
# ``url`` derives the domain, ``tags`` carry the per-policy archive tags
# that split blocked / rate-limited / skipped-by-policy from the generic
# ``unspecified`` tail.
ArchiveFailureRecord = dict[str, Any]

# Per-run ``(outcome, source) -> count`` write tallies — mirror of
# :data:`influx.telemetry.WriteOutcomeCounts` (not imported: Foundation).
WriteOutcomeCounts = Mapping[tuple[str, str], int]

DegradedReasons = tuple[str, ...]

# How many entries each top-N breakdown of the ``degradation_summary``
# keeps (#152): enough to spotlight the dominant driver, not the long
# tail of one-offs.
_DEGRADATION_SUMMARY_TOP_N = 5

# Issue #177: ``invalid_url_rejections`` fires when a single run rejects
# at least this many URLs while some still reach the filter — above the
# noise floor of an occasional typo (~1–3), well below the Sourcegraph
# 5174 incident shape (~30/run).
_INVALID_URL_REJECTIONS_BURST_THRESHOLD: int = 10

# Issue #164: reasons that ALWAYS escalate the run to
# ``unexpected_failure`` — actionable regressions (a 2-run stall streak,
# a feed emitting only broken URLs, a scorer or Lithos-side tool that
# stopped executing, a write landing in a materially-failed state).
UNEXPECTED_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "invalid_note_state",
        "invalid_url_stall",
        "ingestion_stall",
        "fetch_stall",
        "filter_stall",
        "filter_error",
        "dedup_cache_lookup",
    }
)

# Issue #164: tolerated upstream / policy-driven lossiness — surfaced for
# awareness, never paged on.  ``invalid_url_rejections`` is here because
# the validator is doing its job; the signal is "go look at this feed".
EXPECTED_LOSSY_REASONS: frozenset[str] = frozenset(
    {
        "source_acquisition",
        "source_cooldown_skip",
        "archive_acquisition",
        "invalid_url_rejections",
    }
)

# Every reason the ledger may record.  Used by the coverage meta-tests.
KNOWN_DEGRADED_REASONS: frozenset[str] = (
    UNEXPECTED_FAILURE_REASONS | EXPECTED_LOSSY_REASONS
)

# Reasons that tick ``influx_ingestion_stall_runs_total{reason}``: the
# "nothing came through" family dashboards alert on per reason (#36,
# #50, #85, #131).
STALL_METRIC_REASONS: frozenset[str] = frozenset(
    {
        "ingestion_stall",
        "fetch_stall",
        "filter_stall",
        "filter_error",
        "invalid_url_stall",
    }
)

# Write outcomes that count as a real write failure the run continued
# past (#152 review).  ``duplicate`` is deliberately absent: PR #153 /
# issue #148 made it the expected steady state of scheduled re-runs.
_INVALID_NOTE_STATE_OUTCOMES: frozenset[str] = frozenset(
    {
        "invalid_input",
        "version_conflict",
        "slug_collision",
        "content_too_large",
        "content_too_large_skipped",
    }
)

# Severity bucket persisted as ``degradation_severity`` and emitted as
# the ``severity`` label of ``run_completions``.  ``not_applicable``
# marks runs that never produced a reason list (failed / skipped).
Severity = Literal["success", "expected_lossy", "unexpected_failure", "not_applicable"]

# ``run_completions{outcome}`` label.
Outcome = Literal["success", "degraded", "failure", "skipped"]

TerminalStatus = Literal["completed", "failed", "skipped"]


def classify_degradation_severity(degraded_reasons: list[str]) -> Severity:
    """Return the severity bucket for *degraded_reasons*.

    ``success`` when empty, ``unexpected_failure`` when any reason is in
    :data:`UNEXPECTED_FAILURE_REASONS`, ``expected_lossy`` otherwise.
    Pure: consults no ambient state, so the promotion gate can
    recompute it for entries written before #164.
    """
    if not degraded_reasons:
        return "success"
    for reason in degraded_reasons:
        if reason in UNEXPECTED_FAILURE_REASONS:
            return "unexpected_failure"
    return "expected_lossy"


def _is_invalid_note_state_outcome(outcome: str) -> bool:
    return outcome in _INVALID_NOTE_STATE_OUTCOMES


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """What one run observed, handed to the ledger at completion time.

    Counts default to ``None`` (not measured — the legacy / abort
    shapes) rather than ``0`` so the persisted entry keeps the
    distinction.  The archive policy tallies (``archive_blocked_total``
    and friends) are log-only: they explain an ``archive_acquisition``
    warning but are not persisted (#149 / #161).
    """

    sources_checked: int | None = None
    ingested: int | None = None
    fetched_total: int | None = None
    filter_errors_total: int | None = None
    invalid_url_rejections_total: int | None = None
    archive_failures_total: int | None = None
    archive_blocked_total: int = 0
    archive_rate_limited_total: int = 0
    archive_skipped_by_policy_total: int = 0
    archive_unsupported_total: int = 0
    summary_thin_drops_total: int | None = None
    empty_source_writes_total: int | None = None
    cache_hits_total: int | None = None
    source_acquisition_errors: Sequence[dict[str, str]] = ()
    source_cooldown_skips: Sequence[dict[str, str]] = ()
    dedup_lookup_errors: Sequence[dict[str, str]] = ()
    archive_failures: Sequence[ArchiveFailureRecord] = ()
    source_retry_counts: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    tier3_fallbacks: Mapping[str, int] = field(default_factory=dict)
    write_outcomes: WriteOutcomeCounts = field(default_factory=dict)

    @property
    def has_filter_error(self) -> bool:
        return (
            isinstance(self.filter_errors_total, int) and self.filter_errors_total > 0
        )

    @property
    def items_reached_filter(self) -> int | None:
        """``fetched_total`` minus the pre-acquire URL rejections (#131)."""
        if isinstance(self.fetched_total, int) and isinstance(
            self.invalid_url_rejections_total, int
        ):
            return self.fetched_total - self.invalid_url_rejections_total
        return None

    @property
    def has_invalid_url_stall(self) -> bool:
        """The feed returned items but every URL was upstream-malformed."""
        return (
            isinstance(self.invalid_url_rejections_total, int)
            and self.invalid_url_rejections_total > 0
            and isinstance(self.fetched_total, int)
            and self.fetched_total > 0
            and self.items_reached_filter == 0
        )

    @property
    def invalid_note_state_total(self) -> int:
        return sum(
            count
            for (outcome, _source), count in self.write_outcomes.items()
            if _is_invalid_note_state_outcome(outcome) and count > 0
        )

    def invalid_note_state_by_kind(self) -> dict[str, int]:
        by_kind: dict[str, int] = {}
        for (outcome, _source), count in self.write_outcomes.items():
            if count > 0 and _is_invalid_note_state_outcome(outcome):
                by_kind[outcome] = by_kind.get(outcome, 0) + count
        return by_kind


def single_run_reasons(evidence: RunEvidence) -> list[str]:
    """The degraded reasons that need no run history.

    The history-dependent stall family (``ingestion_stall``,
    ``fetch_stall``, ``filter_stall``) is appended by
    :meth:`influx.run_ledger.RunLedger.complete`, which owns the
    prior-run window.  Order is the order operators see in the entry
    and the log line.
    """
    reasons: list[str] = []
    if evidence.source_acquisition_errors:
        reasons.append("source_acquisition")
    # #146: "we backed off on purpose" is distinct from "we tried and
    # lost"; both may co-occur in one run.
    if evidence.source_cooldown_skips:
        reasons.append("source_cooldown_skip")
    # #234: the failed call is Influx→Lithos, so the operator checks
    # Lithos health, not the feed.
    if evidence.dedup_lookup_errors:
        reasons.append("dedup_cache_lookup")
    # #85 review: a scorer execution failure is immediately actionable
    # on any run kind; ``filter_stall`` yields to it (see the ledger).
    if evidence.has_filter_error:
        reasons.append("filter_error")
    if (
        isinstance(evidence.archive_failures_total, int)
        and evidence.archive_failures_total > 0
    ):
        reasons.append("archive_acquisition")
    if evidence.invalid_note_state_total > 0:
        reasons.append("invalid_note_state")
    # #131: all-bad URLs is the stall; #177: many-bad is the burst, and
    # the stall suppresses it.
    if evidence.has_invalid_url_stall:
        reasons.append("invalid_url_stall")
    elif (
        isinstance(evidence.invalid_url_rejections_total, int)
        and evidence.invalid_url_rejections_total
        >= _INVALID_URL_REJECTIONS_BURST_THRESHOLD
    ):
        reasons.append("invalid_url_rejections")
    return reasons


# ── degradation summary (#152) ──────────────────────────────────────


def _top_n_by_count(counts: dict[str, int], *, n: int) -> list[dict[str, Any]]:
    """Positive *counts* as a bounded ``[{"key", "count"}]`` list, sorted
    by count desc then key asc so ties are deterministic."""
    filtered = [(k, v) for k, v in counts.items() if v > 0]
    filtered.sort(key=lambda kv: (-kv[1], kv[0]))
    return [{"key": k, "count": v} for k, v in filtered[:n]]


def _renamed(rows: list[dict[str, Any]], key_name: str) -> list[dict[str, Any]]:
    return [{key_name: row["key"], "count": row["count"]} for row in rows]


def _archive_failure_kind_from_tags(tags: list[str]) -> str:
    """``blocked`` / ``rate_limited`` / ``missing_by_policy`` / ``unspecified``
    from the :mod:`influx.archive_policy` tag taxonomy (structural, so
    free-form error text is never parsed)."""
    tag_set = set(tags)
    if "influx:archive-skipped-by-policy" in tag_set:
        return "missing_by_policy"
    if "influx:archive-blocked" in tag_set:
        return "blocked"
    if "influx:archive-rate-limited" in tag_set:
        return "rate_limited"
    return "unspecified"


def _domain_from_url(url: str) -> str:
    """Lowercase host of *url*, ``""`` when unparseable (local mirror of
    :func:`influx.archive_policy.extract_domain` — Foundation must not
    import it)."""
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except (TypeError, ValueError):
        return ""
    return host.lower()


def _count_by(items: Any, *keys: str) -> dict[str, dict[str, int]]:
    """Tally dict records by each of *keys* (``"unknown"`` when absent)."""
    tallies: dict[str, dict[str, int]] = {key: {} for key in keys}
    for record in items:
        for key in keys:
            value = str(record.get(key) or "unknown")
            tallies[key][value] = tallies[key].get(value, 0) + 1
    return tallies


def build_degradation_summary(
    evidence: RunEvidence,
    *,
    top_n: int = _DEGRADATION_SUMMARY_TOP_N,
) -> dict[str, Any]:
    """The bounded, top-N ``degradation_summary`` block for one run (#152).

    Additive over the per-reason totals: per-source / per-domain /
    per-failure-class breakdowns so operators find the driver (arXiv
    429s, IEEE 403s) without grepping event logs.  Shape::

        {
            "totals": {"source_acquisition_errors", "archive_failures",
                       "filter_errors", "invalid_url_rejections",
                       "cache_hits", "retries_recovered", "writes",
                       "invalid_note_state", "dedup_lookup_errors"},
            "archive":            {"by_kind", "by_domain"},
            "source_acquisition": {"by_kind", "by_source"},
            "dedup_lookup":       {"by_source"},
            "retries_recovered":  {"by_kind", "by_source"},
            "writes":             {"by_outcome", "by_source"},
            "invalid_note_state": {"by_kind", "by_source"},
        }

    Cache hits and write-time ``duplicate`` outcomes are surfaced under
    ``totals`` / ``writes`` but never folded into a degradation
    breakdown — dedupe is not degradation (#152, PR #153).
    """
    archive_by_kind: dict[str, int] = {}
    archive_by_domain: dict[str, int] = {}
    for record in evidence.archive_failures:
        kind = _archive_failure_kind_from_tags(list(record.get("tags") or []))
        archive_by_kind[kind] = archive_by_kind.get(kind, 0) + 1
        domain = _domain_from_url(str(record.get("url") or ""))
        if domain:
            archive_by_domain[domain] = archive_by_domain.get(domain, 0) + 1

    source = _count_by(evidence.source_acquisition_errors, "kind", "source")
    dedup = _count_by(evidence.dedup_lookup_errors, "source")

    retries_by_kind: dict[str, int] = {}
    retries_by_source: dict[str, int] = {}
    retries_total = 0
    for retry_source, by_kind in evidence.source_retry_counts.items():
        for kind, count in by_kind.items():
            retries_by_kind[kind] = retries_by_kind.get(kind, 0) + count
            retries_by_source[retry_source] = (
                retries_by_source.get(retry_source, 0) + count
            )
            retries_total += count

    writes_by_outcome: dict[str, int] = {}
    writes_by_source: dict[str, int] = {}
    writes_total = 0
    invalid_by_kind: dict[str, int] = {}
    invalid_by_source: dict[str, int] = {}
    invalid_total = 0
    for (outcome, write_source), count in evidence.write_outcomes.items():
        if count <= 0:
            continue
        writes_by_outcome[outcome] = writes_by_outcome.get(outcome, 0) + count
        writes_by_source[write_source] = writes_by_source.get(write_source, 0) + count
        writes_total += count
        if _is_invalid_note_state_outcome(outcome):
            invalid_by_kind[outcome] = invalid_by_kind.get(outcome, 0) + count
            invalid_by_source[write_source] = (
                invalid_by_source.get(write_source, 0) + count
            )
            invalid_total += count

    def top(counts: dict[str, int], key_name: str) -> list[dict[str, Any]]:
        return _renamed(_top_n_by_count(counts, n=top_n), key_name)

    return {
        "totals": {
            "source_acquisition_errors": len(evidence.source_acquisition_errors),
            "archive_failures": len(evidence.archive_failures),
            "filter_errors": int(evidence.filter_errors_total or 0),
            "invalid_url_rejections": int(evidence.invalid_url_rejections_total or 0),
            "cache_hits": int(evidence.cache_hits_total or 0),
            "retries_recovered": retries_total,
            "writes": writes_total,
            "invalid_note_state": invalid_total,
            "dedup_lookup_errors": len(evidence.dedup_lookup_errors),
        },
        "archive": {
            "by_kind": top(archive_by_kind, "kind"),
            "by_domain": top(archive_by_domain, "domain"),
        },
        "source_acquisition": {
            "by_kind": top(source["kind"], "kind"),
            "by_source": top(source["source"], "source"),
        },
        "dedup_lookup": {
            "by_source": top(dedup["source"], "source"),
        },
        "retries_recovered": {
            "by_kind": top(retries_by_kind, "kind"),
            "by_source": top(retries_by_source, "source"),
        },
        "writes": {
            "by_outcome": top(writes_by_outcome, "outcome"),
            "by_source": top(writes_by_source, "source"),
        },
        "invalid_note_state": {
            "by_kind": top(invalid_by_kind, "kind"),
            "by_source": top(invalid_by_source, "source"),
        },
    }


# ── operator guidance ───────────────────────────────────────────────


def _by_kind_digest(evidence: RunEvidence) -> str:
    by_kind = evidence.invalid_note_state_by_kind()
    return (
        ",".join(
            f"{kind}={count}"
            for kind, count in sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        or "<none>"
    )


# ``reason -> (counts the warning carries, what the operator should do)``.
# The counts are rendered ``k=v`` ahead of the parenthesised guidance so a
# ``run flagged <reason>`` grep answers "how bad" and "where to look" in
# one line.
REASON_GUIDANCE: dict[str, tuple[Callable[[RunEvidence], str], str]] = {
    "source_acquisition": (
        lambda e: f"acquisition_errors={len(e.source_acquisition_errors)}",
        "source fetches failed and were swallowed; the degradation summary "
        "lists them by source and kind — transient upstream lossiness, "
        "investigate if the frequency spikes",
    ),
    "source_cooldown_skip": (
        lambda e: f"cooldown_skips={len(e.source_cooldown_skips)}",
        "arxiv fetch was skipped after repeated 429 bursts — see "
        "resilience.arxiv_429_cooldown_threshold / arxiv_429_cooldown_seconds",
    ),
    "dedup_cache_lookup": (
        lambda e: f"lookup_errors={len(e.dedup_lookup_errors)}",
        "Lithos cache_lookup failed for these candidates; they were skipped "
        "pre-acquire and will be re-checked next run — check Lithos health / "
        "cache_lookup server errors, not the upstream feed",
    ),
    "filter_error": (
        lambda e: f"filter_errors={e.filter_errors_total or 0}",
        "filter scorer raised FilterScorerError — check provider config, model "
        "availability, response schema; not a profile/prompt issue",
    ),
    "archive_acquisition": (
        lambda e: (
            f"archive_failures={e.archive_failures_total or 0} "
            f"blocked={e.archive_blocked_total} "
            f"rate_limited={e.archive_rate_limited_total} "
            f"skipped_by_policy={e.archive_skipped_by_policy_total}"
        ),
        "accepted items were ingested with influx:archive-missing after archive "
        "download failed; issue #149: blocked / rate_limited / skipped_by_policy "
        "break down the structural domain-policy failure shapes",
    ),
    "invalid_note_state": (
        lambda e: f"by_kind={_by_kind_digest(e)}",
        "write-time failures landed in the materially-failed set — check schema "
        "drift, repair backlog, or upstream id collisions; duplicate outcomes are "
        "deliberately excluded from this flag",
    ),
    "invalid_url_stall": (
        lambda e: (
            f"invalid_url_rejections={e.invalid_url_rejections_total or 0} "
            f"fetched_total={e.fetched_total or 0}"
        ),
        "feed returned items but every entry link was upstream-malformed — "
        "check the upstream feed publisher",
    ),
    "invalid_url_rejections": (
        lambda e: (
            f"invalid_url_rejections={e.invalid_url_rejections_total or 0} "
            f"fetched_total={e.fetched_total or 0}"
        ),
        "feed shipped many malformed entry links this run but some still reached "
        "the filter — check the upstream feed; see the URL validation runbook "
        "(issue #177)",
    ),
    "ingestion_stall": (
        lambda e: "",
        "this + prior scheduled run both ingested 0 with sources_checked > 0",
    ),
    "fetch_stall": (
        lambda e: "",
        "this + prior scheduled run both saw fetched_total == 0 despite prior "
        "non-zero history — likely too-narrow lookback_days or upstream feed change",
    ),
    "filter_stall": (
        lambda e: f"fetched_total={e.fetched_total or 0}",
        "this + prior scheduled run both fetched items but the filter rejected "
        "all candidates — check profile description, filter prompt, or "
        "min_score_in_results",
    ),
}


@dataclass(frozen=True, slots=True)
class RunNotice:
    """An observe-only INFO line: a count worth seeing that is not degradation."""

    name: str
    count_label: str
    count: int
    guidance: str


_NOTICES: tuple[tuple[str, str, Callable[[RunEvidence], int], str], ...] = (
    (
        "archive_unsupported",
        "unsupported_items",
        lambda e: e.archive_unsupported_total,
        "items on archive-unsupported policy domains — expected, not counted "
        "toward archive_acquisition",
    ),
    (
        "summary_thin_drops",
        "dropped_items",
        lambda e: e.summary_thin_drops_total or 0,
        "items suppressed by the thin-summary rule — archive fetch failed AND "
        "feed summary was thin; not counted toward archive_failures_total or "
        "archive_acquisition",
    ),
    (
        "empty_source_writes",
        "written_items",
        lambda e: e.empty_source_writes_total or 0,
        "notes written with a blank source tag AND no arxiv-inferable URL; still "
        "written, not counted toward archive_failures_total or any degraded reason",
    ),
)


def _top_drivers(
    evidence: RunEvidence, summary: dict[str, Any] | None, *, top_n: int = 3
) -> str:
    """``archive:ieee.org=4,arxiv.org=2;source:arxiv/rate_limit=1`` — the
    dominant archive domains (from the persisted summary) and
    source/kind pairs, bounded per category."""
    parts: list[str] = []
    domains = (summary or {}).get("archive", {}).get("by_domain", [])[:top_n]
    if domains:
        parts.append(
            "archive:" + ",".join(f"{r['domain']}={r['count']}" for r in domains)
        )
    pairs: dict[str, int] = {}
    for err in evidence.source_acquisition_errors:
        key = f"{err.get('source') or 'unknown'}/{err.get('kind') or 'unknown'}"
        pairs[key] = pairs.get(key, 0) + 1
    top_pairs = _top_n_by_count(pairs, n=top_n)
    if top_pairs:
        parts.append(
            "source:" + ",".join(f"{r['key']}={r['count']}" for r in top_pairs)
        )
    return ";".join(parts)


@dataclass(frozen=True, slots=True)
class RunCompletionAssessment:
    """The ledger's verdict on one finished run.

    Built by :class:`influx.run_ledger.RunLedger` for every terminal
    transition (``completed`` / ``failed`` / ``skipped``), persisted as
    the ledger entry, and the single input to the completion metrics and
    log lines.  ``reasons`` is empty and ``error`` carries the failure
    text or skip reason on the two non-completed statuses.
    """

    run_id: str
    profile: str | None
    kind: str | None
    status: TerminalStatus
    evidence: RunEvidence
    reasons: DegradedReasons
    error: str | None = None
    degradation_summary: dict[str, Any] | None = None

    @property
    def degraded(self) -> bool:
        return bool(self.reasons)

    @property
    def severity(self) -> Severity:
        if self.status != "completed":
            return "not_applicable"
        return classify_degradation_severity(list(self.reasons))

    @property
    def outcome(self) -> Outcome:
        if self.status == "failed":
            return "failure"
        if self.status == "skipped":
            return "skipped"
        return "degraded" if self.reasons else "success"

    @property
    def stall_metric_reasons(self) -> DegradedReasons:
        return tuple(r for r in self.reasons if r in STALL_METRIC_REASONS)

    def warning_detail(self, reason: str) -> str:
        """``k=v … (guidance)`` for a ``run flagged <reason>`` log line."""
        render_counts, guidance = REASON_GUIDANCE[reason]
        counts = render_counts(self.evidence)
        return f"{counts} ({guidance})" if counts else f"({guidance})"

    @property
    def notices(self) -> tuple[RunNotice, ...]:
        return tuple(
            RunNotice(name=name, count_label=label, count=count, guidance=guidance)
            for name, label, counter, guidance in _NOTICES
            if (count := counter(self.evidence)) > 0
        )

    @property
    def top_drivers(self) -> str:
        """Compact tail for the ``run completed`` line; ``""`` when clean."""
        if not self.reasons:
            return ""
        return _top_drivers(self.evidence, self.degradation_summary)
