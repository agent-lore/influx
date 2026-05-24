"""Local run ledger for operator visibility.

The ledger is intentionally local process state rather than Lithos
knowledge.  It records operational facts about Influx runs so the
admin API and support scripts can answer "what has this deployment
been doing?" without making run history part of the user's knowledge
base.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

RunEntry = dict[str, Any]

# How many prior runs to consider when computing the stall flags.
#
# Twenty is chosen to roughly match a *day* of scheduled runs at the
# typical hourly cadence, so the historical-ratchet for #50
# (``fetch_stall``) and #85 (``filter_stall``) tolerates a half-day
# window of zero-checked runs before stale history disqualifies the
# profile from ratcheting.  It also matches the historical default of
# :meth:`recent` which the ``ingestion_stall`` (#36) check used
# implicitly.
_STALL_HISTORY_LIMIT = 20


# How many entries to surface in each top-N breakdown inside the
# ``degradation_summary`` (#152).  Five is enough to spotlight the main
# drivers (typically a single domain or kind dominates) without dumping
# the long tail of one-off failures into the ledger and the
# ``/runs/recent`` payload.  Tied to a constant so the bound is
# discoverable from one place.
_DEGRADATION_SUMMARY_TOP_N = 5


# Issue #164: severity classification of degraded reasons.  Splits the
# existing flat ``degraded_reasons`` list into two operational
# severities so dashboards / paging / triage can separate
# release-noise (tolerated upstream lossiness) from real breakage
# (data integrity / pipeline regressions).
#
# Reasons listed here ALWAYS escalate the run to
# ``unexpected_failure`` even when expected-lossy reasons co-occur.
# This matches the issue's acceptance criterion ("Invalid note state
# or malformed persisted metadata is always treated as unexpected
# failure").
#
# Reasons NOT in this set are classified as ``expected_lossy`` when
# they are the only reasons present:
#
# * ``source_acquisition`` — transient upstream errors that the
#   in-fetch retry loop already absorbed; the swallowed-error list
#   is surfaced for operator awareness but does not warrant paging.
# * ``source_cooldown_skip`` — we intentionally backed off; the
#   operator's policy decision firing as designed.
# * ``archive_acquisition`` — archive download failed for items that
#   were still ingested (summary text / abstract preserved).  Policy
#   modes (``unsupported`` / ``blocked``) are explicitly tolerated
#   per #149 / #161; generic transient failures are also acceptable
#   noise.
#
# Stalls and ``invalid_*`` shapes are unexpected because they
# represent actionable regressions — a 2-run streak of "nothing
# fetched / ingested / passed the filter" or an upstream feed
# emitting only broken URLs.  ``filter_error`` is unexpected because
# the in-fetch scorer execution failed (provider config / model
# availability / response schema bug) — not a soft rate-limit.
_UNEXPECTED_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "invalid_note_state",
        "invalid_url_stall",
        "ingestion_stall",
        "fetch_stall",
        "filter_stall",
        "filter_error",
    }
)


# Issue #164 review: the complement set of expected-lossy reasons.
# Together with :data:`_UNEXPECTED_FAILURE_REASONS` this is the
# exhaustive list of degraded reasons :meth:`RunLedger.complete` may
# append.  A meta-test in ``tests/unit/test_run_ledger.py`` scans the
# source of ``complete`` and asserts every ``reasons.append("…")``
# literal lands in :data:`_KNOWN_DEGRADED_REASONS`, so a new reason
# cannot be added without an explicit severity classification.
_EXPECTED_LOSSY_REASONS: frozenset[str] = frozenset(
    {
        "source_acquisition",
        "source_cooldown_skip",
        "archive_acquisition",
        # Issue #177: ``invalid_url_rejections`` fires when an upstream
        # feed is shipping many bad URLs (loopback / private / scheme
        # / malformed) per run but at least some good ones still
        # reach the filter — distinct from ``invalid_url_stall``,
        # which requires that EVERY URL be rejected.  The Sourcegraph
        # 5174 leak (2026-05-09 → 2026-05-10) is the canonical
        # incident: 30 URLs/run rejected silently for five days
        # because the stall variant never tripped.  Classed as
        # expected_lossy because the validator is doing its job; the
        # signal is "go look at this feed" rather than a data
        # integrity regression.
        "invalid_url_rejections",
    }
)


# Issue #177: ``invalid_url_rejections`` burst threshold — fires when
# ``invalid_url_rejections_total`` for a single run meets or exceeds
# this value AND ``invalid_url_stall`` is not already firing (the
# stall variant is the all-bad case; this is the many-bad case).  Set
# above the noise floor of occasional typo'd entries in an otherwise
# healthy feed (~1–3) and well below the Sourcegraph incident shape
# (~30/run).  Operator tunable in a follow-up if the default rate
# of false positives proves wrong.
_INVALID_URL_REJECTIONS_BURST_THRESHOLD: int = 10


# Union of the two classified sets.  Used by the meta-test only;
# production callers consult the individual sets.
_KNOWN_DEGRADED_REASONS: frozenset[str] = (
    _UNEXPECTED_FAILURE_REASONS | _EXPECTED_LOSSY_REASONS
)


# Severity literal — surfaced on the ledger entry as
# ``degradation_severity`` and as the ``severity`` label on the
# ``run_completions`` metric.  Mutually exclusive across a single
# run; a run with mixed reasons is escalated to the higher severity
# (``unexpected_failure`` beats ``expected_lossy``).
_SeverityLiteral = Literal["success", "expected_lossy", "unexpected_failure"]


def classify_degradation_severity(degraded_reasons: list[str]) -> _SeverityLiteral:
    """Return the severity classification for *degraded_reasons*.

    * ``"success"`` when no reasons fired.
    * ``"unexpected_failure"`` when any reason in
      :data:`_UNEXPECTED_FAILURE_REASONS` is present.
    * ``"expected_lossy"`` otherwise (only tolerated reasons fired).

    Public — exposed for callers (run service, admin HTTP) that need
    to surface severity without re-implementing the mapping.  The
    classification is stable for a given reason list and does not
    consult any ambient state.
    """
    if not degraded_reasons:
        return "success"
    for reason in degraded_reasons:
        if reason in _UNEXPECTED_FAILURE_REASONS:
            return "unexpected_failure"
    return "expected_lossy"


def _top_n_by_count(counts: dict[str, int], *, n: int) -> list[dict[str, Any]]:
    """Return *counts* as a bounded, sorted ``[{"key": ..., "count": ...}, ...]`` list.

    Sorted descending by count then ascending by key so the top-N is
    deterministic across runs with the same shape.  Only positive
    counts are surfaced (zero-count keys are noise).  Caller supplies
    the wrapping key name (``"kind"`` / ``"domain"`` / ``"source"``)
    by mapping the result; this helper keeps the sort + truncate
    discipline in one place.
    """
    filtered = [(k, v) for k, v in counts.items() if v > 0]
    filtered.sort(key=lambda kv: (-kv[1], kv[0]))
    return [{"key": k, "count": v} for k, v in filtered[:n]]


def _archive_failure_kind_from_tags(tags: list[str]) -> str:
    """Classify a degraded item's archive failure kind from its note tags.

    Returns one of: ``"blocked"``, ``"rate_limited"``,
    ``"missing_by_policy"``, or ``"unspecified"``.  The classification
    is structural (it mirrors the tag taxonomy from
    :mod:`influx.archive_policy`) rather than parsing free-form error
    detail strings, so future taxonomy refinements only need to add
    cases here.

    ``"unspecified"`` is the default for items that landed
    ``influx:archive-missing`` without a more specific policy tag —
    these are generic upstream failures (HTTP 5xx, timeouts, content
    type mismatches) that the archive policy did not classify into a
    structural bucket.
    """
    tag_set = set(tags)
    if "influx:archive-skipped-by-policy" in tag_set:
        return "missing_by_policy"
    if "influx:archive-blocked" in tag_set:
        return "blocked"
    if "influx:archive-rate-limited" in tag_set:
        return "rate_limited"
    return "unspecified"


def _domain_from_url(url: str) -> str:
    """Return the lowercase host of *url* or ``""`` when unparseable.

    Duplicated locally so :mod:`influx.run_ledger` does not depend on
    :mod:`influx.archive_policy` (and indirectly :mod:`re`/parsers it
    pulls).  Mirror of
    :func:`influx.archive_policy.extract_domain`.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
    except (TypeError, ValueError):
        return ""
    return host.lower()


# Concrete archive-failure record consumed by
# :func:`build_degradation_summary` — one entry per item the run ingested
# with ``influx:archive-missing``.  ``url`` is used to derive the domain;
# ``tags`` are scanned for the per-policy archive tags so the summary
# can split blocked / rate-limited / skipped-by-policy from the long
# tail of generic ``unspecified`` failures.
ArchiveFailureRecord = dict[str, Any]


# Write-outcome strings that represent a materially-failed write that
# the run continued past — issue #152 review.  These count toward the
# ``invalid_note_state.by_kind`` breakdown AND the ``invalid_note_state``
# degraded reason.  Anything else (``created``, ``updated``,
# ``duplicate`` …) is recorded under ``writes.by_outcome`` for
# operational visibility but does NOT count as degradation.
#
# Membership rationale:
#
# - ``invalid_input`` — Lithos rejected the payload as malformed
#   (missing required field / bad schema); the item was skipped.
# - ``version_conflict`` — both retries exhausted; the write was
#   skipped.
# - ``slug_collision`` — the slug-collision recovery chain
#   (#31 / #148) gave up; the item was skipped and a backlog entry
#   was appended.
# - ``content_too_large`` — the Tier-2 trim retry was not invoked or
#   bubbled out of the trimming retry path; the item was skipped.
# - ``content_too_large_skipped`` — the Tier-2 trim retry also
#   produced ``content_too_large``; the item was skipped.
#
# ``duplicate`` is deliberately NOT in this set: PR #153 / issue #148
# formalised that duplicate writes are the expected outcome of
# scheduled re-runs and must not be treated as degradation.
_INVALID_NOTE_STATE_OUTCOMES: frozenset[str] = frozenset(
    {
        "invalid_input",
        "version_conflict",
        "slug_collision",
        "content_too_large",
        "content_too_large_skipped",
    }
)


def _is_invalid_note_state_outcome(outcome: str) -> bool:
    """Return ``True`` when *outcome* counts as a real write failure (#152).

    Used by :func:`build_degradation_summary` to gate the
    ``invalid_note_state`` bucket so duplicate / created / updated
    outcomes do not inflate degradation counts.
    """
    return outcome in _INVALID_NOTE_STATE_OUTCOMES


# Per-run write-outcome counter shape consumed by
# :func:`build_degradation_summary`.  Keyed by ``(outcome, source)``
# tuples to keep the per-source attribution (an arXiv ``duplicate`` is
# operationally different from an RSS ``duplicate`` even though the
# outcome string is the same).  Mirrors
# :data:`influx.telemetry.WriteOutcomeCounts`.
WriteOutcomeCounts = dict[tuple[str, str], int]


def build_degradation_summary(
    *,
    source_acquisition_errors: list[dict[str, str]] | None,
    archive_failures: list[ArchiveFailureRecord] | None,
    source_retry_counts: dict[str, dict[str, int]] | None,
    filter_errors_total: int | None,
    invalid_url_rejections_total: int | None,
    cache_hits_total: int | None,
    write_outcomes: WriteOutcomeCounts | None = None,
    top_n: int = _DEGRADATION_SUMMARY_TOP_N,
) -> dict[str, Any]:
    """Build the bounded ``degradation_summary`` block for one run (#152).

    The summary is additive over the existing per-reason totals on the
    ledger entry: it does not replace ``degraded`` /
    ``degraded_reasons`` / ``source_acquisition_errors`` /
    ``archive_failures_total`` — it provides the per-source / per-domain
    / per-failure-class top-N breakdowns operators need to identify
    drivers (arXiv 429s, IEEE 403s, slow timeouts) without grepping
    raw event logs.

    Shape::

        {
            "totals": {
                "source_acquisition_errors": int,
                "archive_failures": int,
                "filter_errors": int,
                "invalid_url_rejections": int,
                "cache_hits": int,
                "retries_recovered": int,
                "writes": int,
                "invalid_note_state": int,
            },
            "archive": {
                "by_kind":   [{"kind": "...", "count": N}, ...],
                "by_domain": [{"domain": "...", "count": N}, ...],
            },
            "source_acquisition": {
                "by_kind":   [{"kind": "...", "count": N}, ...],
                "by_source": [{"source": "...", "count": N}, ...],
            },
            "retries_recovered": {
                "by_kind":   [{"kind": "...", "count": N}, ...],
                "by_source": [{"source": "...", "count": N}, ...],
            },
            "writes": {
                "by_outcome": [{"outcome": "...", "count": N}, ...],
                "by_source":  [{"source":  "...", "count": N}, ...],
            },
            "invalid_note_state": {
                "by_kind":   [{"kind":   "...", "count": N}, ...],
                "by_source": [{"source": "...", "count": N}, ...],
            },
        }

    Design notes:

    - **Dedupe is not degradation.**  ``cache_hits`` is reported under
      ``totals`` so operators can see the dedupe volume, but it is
      deliberately segregated from the archive / source-acquisition
      breakdowns.  A run whose only "issue" is cache hits is not a
      degraded run (#152 acceptance criteria).
    - **Write-time duplicate outcomes are not degradation either.**
      Every write outcome — including ``created`` / ``updated`` /
      ``duplicate`` — is surfaced under ``writes.by_outcome`` so the
      dedupe volume is visible to operators, but only the
      invalid-note-state outcomes (``invalid_input``,
      ``version_conflict``, ``slug_collision``,
      ``content_too_large*``) feed into the ``invalid_note_state``
      bucket.  PR #153 / issue #148 formalised the duplicate-is-OK
      contract: a scheduled re-run whose every write returns
      ``duplicate`` is steady-state, not a regression (#152 review).
    - **Top-N bounded.**  Each breakdown is truncated to *top_n* entries
      sorted descending by count.  The long tail is intentionally
      dropped — operators inspecting recent runs need to see the
      dominant drivers, not the every-warning histogram.
    - **Failure-class taxonomy is shallow.**  Archive failures are
      bucketed by ``blocked`` / ``rate_limited`` / ``missing_by_policy``
      / ``unspecified``; source-acquisition failures use the refined
      ``kind`` already attached by the source adapter (``rate_limit``,
      ``rate_limit_upstream_capacity``, ``rate_limit_local``,
      ``rate_limit_unknown``, ``timeout``, ``network``, ``ssrf``,
      ``oversize``, etc.).  New refinements (e.g. issue #145's
      arXiv 429 split) flow through automatically because the
      classification is captured at error-record time, not here.
    """
    errors = source_acquisition_errors or []
    archive = archive_failures or []
    retries = source_retry_counts or {}
    writes = write_outcomes or {}

    archive_by_kind: dict[str, int] = {}
    archive_by_domain: dict[str, int] = {}
    for record in archive:
        kind = _archive_failure_kind_from_tags(list(record.get("tags") or []))
        archive_by_kind[kind] = archive_by_kind.get(kind, 0) + 1
        domain = _domain_from_url(str(record.get("url") or ""))
        if domain:
            archive_by_domain[domain] = archive_by_domain.get(domain, 0) + 1

    source_by_kind: dict[str, int] = {}
    source_by_source: dict[str, int] = {}
    for err in errors:
        kind = str(err.get("kind") or "unknown")
        source_by_kind[kind] = source_by_kind.get(kind, 0) + 1
        source = str(err.get("source") or "unknown")
        source_by_source[source] = source_by_source.get(source, 0) + 1

    retries_by_kind: dict[str, int] = {}
    retries_by_source: dict[str, int] = {}
    retries_total = 0
    for source, by_kind in retries.items():
        for kind, count in by_kind.items():
            retries_by_kind[kind] = retries_by_kind.get(kind, 0) + count
            retries_by_source[source] = retries_by_source.get(source, 0) + count
            retries_total += count

    # #152 review: aggregate per-(outcome, source) write counts into the
    # two parallel views the ledger surfaces.  ``writes.*`` carries
    # every outcome — including the benign ``created`` / ``updated`` /
    # ``duplicate`` cases — so operators see the dedupe volume at the
    # write layer alongside the cache-hit total.  ``invalid_note_state.*``
    # filters down to the materially-failed outcomes
    # (:data:`_INVALID_NOTE_STATE_OUTCOMES`) so a duplicate-heavy run
    # doesn't inflate the degradation reason list.
    writes_by_outcome: dict[str, int] = {}
    writes_by_source: dict[str, int] = {}
    writes_total = 0
    invalid_note_state_by_kind: dict[str, int] = {}
    invalid_note_state_by_source: dict[str, int] = {}
    invalid_note_state_total = 0
    for (outcome, source), count in writes.items():
        if count <= 0:
            continue
        writes_by_outcome[outcome] = writes_by_outcome.get(outcome, 0) + count
        writes_by_source[source] = writes_by_source.get(source, 0) + count
        writes_total += count
        if _is_invalid_note_state_outcome(outcome):
            invalid_note_state_by_kind[outcome] = (
                invalid_note_state_by_kind.get(outcome, 0) + count
            )
            invalid_note_state_by_source[source] = (
                invalid_note_state_by_source.get(source, 0) + count
            )
            invalid_note_state_total += count

    def _renamed(rows: list[dict[str, Any]], key_name: str) -> list[dict[str, Any]]:
        """Rename the helper's neutral ``"key"`` field to the caller's label."""
        return [{key_name: row["key"], "count": row["count"]} for row in rows]

    return {
        "totals": {
            "source_acquisition_errors": len(errors),
            "archive_failures": len(archive),
            "filter_errors": int(filter_errors_total or 0),
            "invalid_url_rejections": int(invalid_url_rejections_total or 0),
            "cache_hits": int(cache_hits_total or 0),
            "retries_recovered": retries_total,
            # #152 review: write-layer totals.  ``writes`` covers every
            # outcome (including duplicate / created / updated);
            # ``invalid_note_state`` is the strict subset that feeds the
            # degraded-reasons list.
            "writes": writes_total,
            "invalid_note_state": invalid_note_state_total,
        },
        "archive": {
            "by_kind": _renamed(_top_n_by_count(archive_by_kind, n=top_n), "kind"),
            "by_domain": _renamed(
                _top_n_by_count(archive_by_domain, n=top_n), "domain"
            ),
        },
        "source_acquisition": {
            "by_kind": _renamed(_top_n_by_count(source_by_kind, n=top_n), "kind"),
            "by_source": _renamed(_top_n_by_count(source_by_source, n=top_n), "source"),
        },
        "retries_recovered": {
            "by_kind": _renamed(_top_n_by_count(retries_by_kind, n=top_n), "kind"),
            "by_source": _renamed(
                _top_n_by_count(retries_by_source, n=top_n), "source"
            ),
        },
        # #152 review: all write outcomes including duplicate / dedupe.
        # NOT a degradation driver — operators consult this to see the
        # dedupe volume at the write layer (complementing
        # ``totals.cache_hits`` which counts the pre-write cache).
        "writes": {
            "by_outcome": _renamed(
                _top_n_by_count(writes_by_outcome, n=top_n), "outcome"
            ),
            "by_source": _renamed(_top_n_by_count(writes_by_source, n=top_n), "source"),
        },
        # #152 review: degrading write-time outcomes only
        # (``invalid_input`` / ``version_conflict`` / ``slug_collision``
        # / ``content_too_large*``).  When the total is non-zero, the
        # caller appends ``invalid_note_state`` to the degraded reasons.
        "invalid_note_state": {
            "by_kind": _renamed(
                _top_n_by_count(invalid_note_state_by_kind, n=top_n), "kind"
            ),
            "by_source": _renamed(
                _top_n_by_count(invalid_note_state_by_source, n=top_n), "source"
            ),
        },
    }


@dataclass(frozen=True)
class RunLedger:
    """Append-only local run ledger backed by JSON files."""

    state_dir: Path

    @property
    def runs_path(self) -> Path:
        return self.state_dir / "runs.jsonl"

    @property
    def active_path(self) -> Path:
        return self.state_dir / "active-runs.json"

    @property
    def unresolved_slug_collisions_path(self) -> Path:
        """JSONL of slug collisions that exhausted the recovery chain (#31).

        Each line is one entry with ``timestamp``, ``run_id``,
        ``profile``, ``source``, ``source_url``, ``title``, ``detail``.
        Append-only; the ``slug-collision-backlog`` diagnose subcommand
        consumes it so an operator can see every still-unresolved
        collision in one place rather than scraping log buffers.
        """
        return self.state_dir / "unresolved-slug-collisions.jsonl"

    def start(
        self,
        *,
        run_id: str,
        profile: str,
        kind: str,
        run_range: dict[str, str | int] | None,
    ) -> None:
        """Record a run as active."""
        started_at = datetime.now(UTC).isoformat()
        entry: RunEntry = {
            "run_id": run_id,
            "profile": profile,
            "kind": kind,
            "status": "running",
            "run_range": run_range or {},
            "started_at": started_at,
            "completed_at": None,
            "duration_seconds": None,
            "sources_checked": None,
            "ingested": None,
            "fetched_total": None,
            "filter_errors_total": None,
            "invalid_url_rejections_total": None,
            "archive_failures_total": None,
            # Issue #166: per-run total of items the source adapters
            # suppressed via the thin-summary rule.  Distinct from
            # ``archive_failures_total`` — these items never reached the
            # write loop and so never received ``influx:archive-missing``.
            # Stays ``None`` while the run is in flight; populated from
            # the per-run contextvar at ``complete`` time.
            "summary_thin_drops_total": None,
            "error": None,
            "degraded": False,
            "degraded_reasons": [],
            # Issue #164: severity bucket on top of the flat
            # ``degraded_reasons`` list.  Starts at ``"success"`` while
            # the run is in flight; finalised by ``_finish`` once
            # ``degraded_reasons`` is known.
            "degradation_severity": "success",
            "source_acquisition_errors": [],
            # Issue #146: cooldown skips are surfaced in the entry
            # alongside ``source_acquisition_errors`` so the active
            # snapshot has the same shape as a completed entry.
            "source_cooldown_skips": [],
            # #129: per-source counter of *recovered* retries (i.e.
            # retries that did not produce a swallowed error).  Shape:
            # ``{"arxiv": {"rate_limit": 2, "timeout": 1}}``.  Empty
            # dict at start; populated from the per-run contextvar at
            # ``complete`` time.
            "source_retry_counts": {},
            # #151: per-run counts of Tier 3 enrichment fallbacks,
            # split by classification.  Shape:
            # ``{"harmless": int, "degraded": int}``.  Empty dict at
            # start; populated from the per-run contextvar at
            # ``complete`` time so run summaries / ``/runs/recent``
            # can distinguish "Tier 3 noise that didn't break notes"
            # from "Tier 3 + Tier 1 both failed — note materially
            # incomplete".
            "tier3_fallbacks": {},
            # #152: bounded, top-N degradation summary aggregated at
            # ``complete`` time.  Stays ``None`` while the run is in
            # flight; populated by ``complete`` with archive +
            # source-acquisition + retry breakdowns.  See
            # :func:`build_degradation_summary` for the full shape.
            "degradation_summary": None,
        }
        try:
            active = self._read_active()
            active[run_id] = entry
            self._write_json_atomic(self.active_path, active)
        except OSError:
            logger.warning("failed to update active run ledger", exc_info=True)

    def complete(
        self,
        *,
        run_id: str,
        sources_checked: int | None,
        ingested: int | None,
        fetched_total: int | None = None,
        filter_errors_total: int | None = None,
        invalid_url_rejections_total: int | None = None,
        archive_failures_total: int | None = None,
        summary_thin_drops_total: int | None = None,
        source_acquisition_errors: list[dict[str, str]] | None = None,
        source_cooldown_skips: list[dict[str, str]] | None = None,
        source_retry_counts: dict[str, dict[str, int]] | None = None,
        tier3_fallbacks: dict[str, int] | None = None,
        archive_failures: list[ArchiveFailureRecord] | None = None,
        cache_hits_total: int | None = None,
        write_outcomes: WriteOutcomeCounts | None = None,
    ) -> list[str]:
        """Mark an active run as completed and append it to history.

        Returns the structured ``degraded_reasons`` list that was
        recorded.  Possible values:

        - ``"source_acquisition"`` (issue #20) — at least one
          source-fetch failure was swallowed.
        - ``"source_cooldown_skip"`` (issue #146) — at least one
          source fetch was deliberately skipped because the
          source-specific adaptive 429 cooldown was active.  Distinct
          from ``source_acquisition`` because the run *chose* not to
          call upstream rather than tried and lost.  Single-run
          signal — like ``filter_error`` — so operators see the skip
          immediately even on the first cooldown-suppressed run.
        - ``"filter_error"`` (issue #85 review) — the LLM filter
          scorer raised :class:`FilterScorerError` (transport, parse,
          or provider failure) at least once during this run.  Single-
          run signal — no consecutive-runs gate, since one filter
          execution failure is immediately actionable.  Mutually
          exclusive with ``filter_stall``: when both would apply,
          ``filter_error`` wins because the scorer-failure diagnosis
          is more specific.
        - ``"archive_acquisition"`` — at least one accepted item was
          ingested with ``influx:archive-missing`` after archive
          acquisition failed, so note quality degraded even though the
          run continued.
        - ``"ingestion_stall"`` (issue #36) — this and the immediately
          prior scheduled run both saw ``ingested == 0`` despite
          ``sources_checked > 0``.  Typical cause: every candidate hits
          ``slug_collision``/``duplicate``.
        - ``"fetch_stall"`` (issue #50) — this and the immediately
          prior scheduled run both saw ``fetched_total == 0`` (no
          source returned any items at all), AND the profile has
          historically seen ``sources_checked > 0``.  Typical cause:
          too-narrow ``lookback_days`` or an upstream feed shape change.
        - ``"filter_stall"`` (issue #85) — this and the immediately
          prior scheduled run both saw ``fetched_total > 0`` but
          ``sources_checked == 0`` (the LLM filter ran cleanly and
          rejected every candidate), AND the profile has historically
          seen ``sources_checked > 0``.  Typical cause: profile
          description drift, filter prompt regression, or
          ``min_score_in_results`` set too high.  Requires
          ``filter_errors_total == 0`` so a scorer failure produces
          ``filter_error`` rather than this, AND
          ``invalid_url_rejections_total < fetched_total`` so a feed
          whose entries were all URL-rejected pre-filter produces
          ``invalid_url_stall`` rather than this.
        - ``"invalid_url_stall"`` (issue #131) — the feed returned
          items but every URL was upstream-malformed (loopback,
          private, link-local, multicast, unparseable, or
          disallowed-scheme), so nothing reached the LLM filter.
          Single-run signal — like ``filter_error`` — because URL
          malformation is an immediately actionable upstream bug.
        - ``"invalid_url_rejections"`` (issue #177) — the feed
          shipped many bad URLs per run but at least some good ones
          still reached the filter.  Distinct from
          ``invalid_url_stall``: this is the partial-bad case.
          Fires when ``invalid_url_rejections_total`` meets or
          exceeds :data:`_INVALID_URL_REJECTIONS_BURST_THRESHOLD`
          (currently ``10``).  Slotted into ``expected_lossy``
          because the validator is doing its job — the signal is
          "operator should look at this feed" rather than a data
          integrity regression.  Suppressed when
          ``invalid_url_stall`` already fires.
        - ``"invalid_note_state"`` (issue #152 review) — at least one
          write-time outcome landed in the materially-failed set
          (``invalid_input``, ``version_conflict``, ``slug_collision``,
          or ``content_too_large*``) so an accepted item was dropped at
          the write layer.  Distinct from ``duplicate``, which is the
          expected steady-state outcome of scheduled re-runs and
          deliberately does NOT count toward degradation (PR #153 /
          issue #148 contract).  Single-run signal — these failure
          shapes are immediately actionable (schema drift / repair
          backlog / upstream id collision) and do not need a
          consecutive-runs ratchet.

        ``fetch_stall`` and ``filter_stall`` partition the
        ``sources_checked == 0`` space by ``fetched_total``: they are
        mutually exclusive on a single run.  ``ingestion_stall``
        requires ``sources_checked > 0``, so it is mutually exclusive
        with both.  ``filter_error`` and ``filter_stall`` are mutually
        exclusive because they describe distinct failure shapes (scorer
        broken vs scorer-rejected-all); ``filter_error`` is independent
        of ``ingestion_stall`` and ``fetch_stall`` and may co-occur
        with either if a partial-failure run still ingested or
        legitimately fetched nothing from some sources.

        Returning the reasons lets the caller (the scheduler) emit
        per-reason metrics without re-deriving the logic.
        """
        errors = list(source_acquisition_errors or [])
        cooldown_skips = list(source_cooldown_skips or [])
        reasons: list[str] = []
        if errors:
            reasons.append("source_acquisition")
        # Issue #146: cooldown skips are reported as a *separate*
        # degraded reason so operator dashboards can distinguish
        # "we backed off on purpose" from "we tried and lost".  Both
        # may co-occur within a single run (e.g. arXiv cooldown, RSS
        # network failure) — the run-level summary surfaces both.
        if cooldown_skips:
            reasons.append("source_cooldown_skip")

        # #85 review: filter_error fires immediately on any
        # FilterScorerError catch, regardless of run kind.  Operators
        # need to know about scorer execution failures even on manual
        # / backfill runs (where the stall family doesn't apply).
        # Mutual exclusion with filter_stall is enforced below by
        # gating the filter_stall branch on filter_errors_total == 0.
        has_filter_error = (
            isinstance(filter_errors_total, int) and filter_errors_total > 0
        )
        if has_filter_error:
            reasons.append("filter_error")
        if isinstance(archive_failures_total, int) and archive_failures_total > 0:
            reasons.append("archive_acquisition")

        # #152 review: ``invalid_note_state`` fires when any write-time
        # outcome lands in the materially-failed set (see
        # :data:`_INVALID_NOTE_STATE_OUTCOMES`).  ``duplicate`` outcomes
        # are deliberately excluded — they are the contract-expected
        # outcome of scheduled re-runs (PR #153 / issue #148).
        # Single-run signal because the underlying shapes (schema
        # drift, repair backlog, version conflict) are immediately
        # actionable, not noisy/transient.
        invalid_note_state_total = sum(
            count
            for (outcome, _source), count in (write_outcomes or {}).items()
            if _is_invalid_note_state_outcome(outcome) and count > 0
        )
        if invalid_note_state_total > 0:
            reasons.append("invalid_note_state")

        # #131 review concern 2: ``invalid_url_stall`` fires when the
        # feed returned items but every URL was upstream-malformed
        # (loopback / private / link-local / multicast / unparseable /
        # disallowed scheme), so nothing reached the LLM filter.
        # Single-run signal — like ``filter_error`` and
        # ``archive_acquisition`` — because URL malformation is a
        # clear-cut, immediately actionable upstream bug, not a
        # noisy/transient signal that needs a consecutive-runs ratchet.
        # Mutually exclusive with ``filter_stall`` (the gate below
        # enforces ``items_reached_filter > 0``) and with
        # ``fetch_stall`` (which requires ``fetched_total == 0``).
        items_reached_filter = (
            (fetched_total - invalid_url_rejections_total)
            if isinstance(fetched_total, int)
            and isinstance(invalid_url_rejections_total, int)
            else None
        )
        has_invalid_url_stall = (
            isinstance(invalid_url_rejections_total, int)
            and invalid_url_rejections_total > 0
            and isinstance(fetched_total, int)
            and fetched_total > 0
            and items_reached_filter == 0
        )
        if has_invalid_url_stall:
            reasons.append("invalid_url_stall")

        # Issue #177: ``invalid_url_rejections`` (burst).  Fires when
        # a run's URL-rejection count meets or exceeds
        # :data:`_INVALID_URL_REJECTIONS_BURST_THRESHOLD` while at
        # least some items still reached the filter — the partial-bad
        # case that the stall variant above misses.  Suppressed when
        # ``invalid_url_stall`` already fires (the stall is the
        # more-specific signal for the all-bad case).
        has_invalid_url_burst = (
            not has_invalid_url_stall
            and isinstance(invalid_url_rejections_total, int)
            and invalid_url_rejections_total >= _INVALID_URL_REJECTIONS_BURST_THRESHOLD
        )
        if has_invalid_url_burst:
            reasons.append("invalid_url_rejections")

        # Resolve profile + kind once for the stall checks.  All three
        # stall flags only apply to scheduled runs (backfills
        # legitimately ingest 0 when every candidate is a cache hit,
        # and operator-triggered manual runs may use a deliberately
        # narrow lookback).
        active = self._read_active()
        entry = active.get(run_id, {})
        profile = entry.get("profile")
        kind = entry.get("kind")
        is_scheduled = isinstance(profile, str) and kind == "scheduled"

        # #36 ingestion-stall detection: only meaningful when the run
        # actually inspected something.
        if (
            is_scheduled
            and isinstance(ingested, int)
            and ingested == 0
            and isinstance(sources_checked, int)
            and sources_checked > 0
        ):
            # Count consecutive prior scheduled runs for the same
            # profile that match the stall shape.  We add 1 for *this*
            # run since it hasn't been written yet.
            assert isinstance(profile, str)  # narrowed by is_scheduled
            consecutive = 1 + self._consecutive_zero_ingestion_runs(
                profile=profile, exclude_run_id=run_id
            )
            if consecutive >= 2:
                reasons.append("ingestion_stall")

        # #50 / #85: zero-sources_checked split.  ``fetch_stall`` keeps
        # the original semantics — *no items reached the filter* —
        # while ``filter_stall`` (#85) catches the case where sources
        # fetched normally but the filter rejected every candidate.
        # Both share the historical-ratchet ("profile has previously
        # seen ``sources_checked > 0``") that silences brand-new
        # profiles and profiles that genuinely never receive items.
        if is_scheduled and isinstance(sources_checked, int) and sources_checked == 0:
            assert isinstance(profile, str)  # narrowed by is_scheduled

            if isinstance(fetched_total, int) and fetched_total > 0:
                # #85 filter_stall: items WERE fetched, the filter
                # rejected them all.  Requires filter_errors_total == 0
                # — a scorer-execution failure has a distinct, more
                # specific reason (``filter_error``) and shouldn't
                # double-fire as a filter_stall (#85 review).  #131
                # review concern 2: also requires that items actually
                # reached the filter (i.e. weren't all dropped pre-acquire
                # by the URL validator); otherwise ``invalid_url_stall``
                # is the correct diagnosis.  Streak counts prior
                # scheduled runs that match the same shape
                # (sources_checked == 0 AND fetched_total > 0).
                if has_filter_error or has_invalid_url_stall:
                    pass  # more-specific reason already appended above
                else:
                    consecutive_filter = (
                        1
                        + self._consecutive_zero_check_with_fetch_runs(
                            profile=profile, exclude_run_id=run_id
                        )
                    )
                    if consecutive_filter >= 2 and self._has_prior_non_zero_fetch(
                        profile=profile, exclude_run_id=run_id
                    ):
                        reasons.append("filter_stall")
            else:
                # #50 fetch_stall: nothing was fetched at all.  Either
                # ``fetched_total`` was explicitly 0 or the caller
                # didn't supply one (legacy path) — both fall to
                # fetch_stall to preserve pre-#85 semantics.
                #
                # Issue #163 review: when the current run also has
                # ``source_cooldown_skips`` non-empty, the zeros are
                # fully explained by the cooldown — adding fetch_stall
                # on top would mis-route the operator to "upstream
                # drift / lookback problem" when the truth is "we
                # deliberately did not fetch".  ``_consecutive_zero_fetch_runs``
                # already skips prior cooldown-skipped runs (see
                # :meth:`_is_zero_fetch_entry`) so the streak is not
                # polluted by past cooldown runs either.
                if cooldown_skips:
                    consecutive_zero_fetch = 0
                else:
                    consecutive_zero_fetch = 1 + self._consecutive_zero_fetch_runs(
                        profile=profile, exclude_run_id=run_id
                    )
                if consecutive_zero_fetch >= 2 and self._has_prior_non_zero_fetch(
                    profile=profile, exclude_run_id=run_id
                ):
                    reasons.append("fetch_stall")

        # #152: build the bounded degradation summary alongside the
        # existing per-reason totals.  Computed at ``complete`` time so
        # the persisted entry carries a stable snapshot — downstream
        # consumers (``/runs/recent``) don't need to re-derive it from
        # raw event logs.  Dedupe cache-hits are surfaced under
        # ``totals.cache_hits`` but deliberately NOT folded into the
        # archive / source-acquisition breakdowns (#152 acceptance
        # criteria: a run whose only "issue" is cache hits is not
        # degraded).
        degradation_summary = build_degradation_summary(
            source_acquisition_errors=errors,
            archive_failures=archive_failures,
            source_retry_counts=source_retry_counts,
            filter_errors_total=filter_errors_total,
            invalid_url_rejections_total=invalid_url_rejections_total,
            cache_hits_total=cache_hits_total,
            write_outcomes=write_outcomes,
        )

        self._finish(
            run_id=run_id,
            status="completed",
            sources_checked=sources_checked,
            ingested=ingested,
            fetched_total=fetched_total,
            filter_errors_total=filter_errors_total,
            invalid_url_rejections_total=invalid_url_rejections_total,
            archive_failures_total=archive_failures_total,
            summary_thin_drops_total=summary_thin_drops_total,
            error=None,
            degraded=bool(reasons),
            source_acquisition_errors=errors,
            source_cooldown_skips=cooldown_skips,
            degraded_reasons=reasons,
            source_retry_counts=source_retry_counts,
            tier3_fallbacks=tier3_fallbacks,
            degradation_summary=degradation_summary,
        )
        return reasons

    def _consecutive_zero_ingestion_runs(
        self, *, profile: str, exclude_run_id: str
    ) -> int:
        """Count consecutive prior scheduled runs that match the stall shape.

        Walks ``recent()`` newest-first, counting runs where
        ``ingested == 0 AND sources_checked > 0`` for *profile* of kind
        ``scheduled``.  Stops at the first run that doesn't match.  Used
        by :meth:`complete` to compute the ``ingestion_stall`` degraded
        reason (#36).
        """
        count = 0
        for prior in self.recent(limit=_STALL_HISTORY_LIMIT, profile=profile):
            if prior.get("run_id") == exclude_run_id:
                continue
            if prior.get("kind") != "scheduled":
                # Backfills don't count — their ingest pattern is
                # different.  But they shouldn't break the streak
                # either; skip silently.
                continue
            ingested = prior.get("ingested")
            sources_checked = prior.get("sources_checked")
            if (
                isinstance(ingested, int)
                and ingested == 0
                and isinstance(sources_checked, int)
                and sources_checked > 0
            ):
                count += 1
                continue
            break
        return count

    def _consecutive_zero_fetch_runs(self, *, profile: str, exclude_run_id: str) -> int:
        """Count consecutive prior scheduled runs with ``fetched_total == 0``.

        Walks ``recent()`` newest-first.  Stops at the first scheduled
        run that doesn't match.  Used by :meth:`complete` to compute the
        ``fetch_stall`` degraded reason (#50).

        ``fetched_total`` was added in #85; legacy ledger entries
        without that field but with ``sources_checked == 0`` are
        treated as zero-fetch for backward-compatibility (the pre-#85
        invariant ``fetched_total == 0 ↔ sources_checked == 0`` holds
        by construction since the LLM-filter rejection was the only
        new way to land at ``sources_checked == 0`` despite a
        successful fetch).

        Issue #163 review: prior runs whose zeros are explained by a
        ``source_cooldown_skip`` (cooldown short-circuited the fetch)
        are *skipped* — neither counted toward the streak nor used to
        terminate it.  Continuing past these entries keeps the streak
        meaningful for the genuine "fetch tried and got nothing"
        pattern that ``fetch_stall`` exists to surface.
        """
        count = 0
        for prior in self.recent(limit=_STALL_HISTORY_LIMIT, profile=profile):
            if prior.get("run_id") == exclude_run_id:
                continue
            if prior.get("kind") != "scheduled":
                continue
            # Issue #163: don't let cooldown-skipped runs pollute the
            # zero-fetch streak.  Their zeros are deliberate, not a
            # symptom of upstream silence.
            if prior.get("source_cooldown_skips"):
                continue
            if self._is_zero_fetch_entry(prior):
                count += 1
                continue
            break
        return count

    def _consecutive_zero_check_with_fetch_runs(
        self, *, profile: str, exclude_run_id: str
    ) -> int:
        """Count consecutive prior scheduled runs matching the filter_stall shape.

        Walks ``recent()`` newest-first, counting runs where
        ``sources_checked == 0 AND fetched_total > 0`` for *profile*
        of kind ``scheduled``.  Stops at the first run that doesn't
        match.  Used by :meth:`complete` to compute the
        ``filter_stall`` degraded reason (#85).
        """
        count = 0
        for prior in self.recent(limit=_STALL_HISTORY_LIMIT, profile=profile):
            if prior.get("run_id") == exclude_run_id:
                continue
            if prior.get("kind") != "scheduled":
                continue
            sources_checked = prior.get("sources_checked")
            fetched_total = prior.get("fetched_total")
            if (
                isinstance(sources_checked, int)
                and sources_checked == 0
                and isinstance(fetched_total, int)
                and fetched_total > 0
            ):
                count += 1
                continue
            break
        return count

    @staticmethod
    def _is_zero_fetch_entry(entry: RunEntry) -> bool:
        """Return ``True`` when *entry* matches the fetch-stall shape.

        ``fetched_total == 0`` is the explicit signal added in #85;
        legacy entries without that field but with
        ``sources_checked == 0`` are treated as zero-fetch.
        """
        sources_checked = entry.get("sources_checked")
        if not (isinstance(sources_checked, int) and sources_checked == 0):
            return False
        fetched_total = entry.get("fetched_total")
        if isinstance(fetched_total, int):
            return fetched_total == 0
        # Legacy entry written before #85 added ``fetched_total``.
        return True

    def _has_prior_non_zero_fetch(self, *, profile: str, exclude_run_id: str) -> bool:
        """Return whether *profile* has ever fetched items in recent history.

        Implements the #50 historical-ratchet: ``fetch_stall`` only
        fires for profiles that have previously seen
        ``sources_checked > 0`` within the last ``_STALL_HISTORY_LIMIT``
        runs.  This silences brand-new profiles (no history at all) and
        profiles that genuinely never receive items — for them, a
        run of zero fetches is the steady-state, not a regression.
        """
        for prior in self.recent(limit=_STALL_HISTORY_LIMIT, profile=profile):
            if prior.get("run_id") == exclude_run_id:
                continue
            sources_checked = prior.get("sources_checked")
            if isinstance(sources_checked, int) and sources_checked > 0:
                return True
        return False

    def fail(self, *, run_id: str, error: str) -> None:
        """Mark an active run as failed and append it to history."""
        self._finish(
            run_id=run_id,
            status="failed",
            sources_checked=None,
            ingested=None,
            error=error,
            degraded=False,
            source_acquisition_errors=[],
        )

    def skip(self, *, run_id: str, reason: str) -> None:
        """Mark an active run as ``skipped`` and append it to history (#40).

        Distinct from ``fail``: ``failed`` means the run started and
        crashed; ``skipped`` means the run never started any
        source-fetch / LLM / write work because a circuit breaker (e.g.
        Lithos extended outage) opened first.  ``reason`` is recorded
        so dashboards can distinguish skip causes.
        """
        self._finish(
            run_id=run_id,
            status="skipped",
            sources_checked=None,
            ingested=None,
            error=reason,
            degraded=False,
            source_acquisition_errors=[],
        )

    def record_unresolved_slug_collision(
        self,
        *,
        profile: str,
        source: str,
        source_url: str,
        title: str,
        detail: str,
        run_id: str,
    ) -> None:
        """Append one unresolved slug-collision entry to the backlog (#31).

        Called by the scheduler when ``LithosClient._retry_slug_collision``
        cannot recover the write (squatter is genuinely-distinct AND the
        suffix retry also collides).  Each entry is a self-contained JSON
        object so the diagnose script can stream the file without parsing
        run-ledger context.  Best-effort: a write error is logged but
        does not propagate, mirroring the run-ledger discipline.
        """
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "profile": profile,
            "source": source,
            "source_url": source_url,
            "title": title,
            "detail": detail,
        }
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with self.unresolved_slug_collisions_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            logger.warning(
                "failed to append unresolved slug-collision entry",
                exc_info=True,
            )

    def unresolved_slug_collisions(self) -> list[dict[str, Any]]:
        """Return every unresolved slug-collision entry from the backlog (#31).

        Newest-last to match the on-disk order.  Returns an empty list
        when the backlog file does not yet exist.
        """
        path = self.unresolved_slug_collisions_path
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
        except OSError:
            logger.warning(
                "failed to read unresolved slug-collision backlog",
                exc_info=True,
            )
        return entries

    def active_runs(self) -> list[RunEntry]:
        """Return currently active runs ordered by start time."""
        active = self._read_active()
        return sorted(
            active.values(),
            key=lambda entry: str(entry.get("started_at") or ""),
            reverse=True,
        )

    def abandon_active(self, *, reason: str) -> None:
        """Mark active runs from a previous process as abandoned."""
        try:
            active = self._read_active()
            if not active:
                return
            completed_at = datetime.now(UTC)
            for entry in active.values():
                entry.update(
                    {
                        "status": "abandoned",
                        "completed_at": completed_at.isoformat(),
                        "duration_seconds": self._duration_seconds(
                            entry.get("started_at"),
                            completed_at,
                        ),
                        "error": reason,
                        "degraded": entry.get("degraded", False),
                        "degraded_reasons": list(entry.get("degraded_reasons") or []),
                        # Issue #164: preserve the in-flight severity
                        # on abandon — typically ``"success"`` since
                        # the field is only finalised by ``_finish``,
                        # but a restart hand-off should not silently
                        # downgrade an already-set value.
                        "degradation_severity": entry.get(
                            "degradation_severity", "success"
                        ),
                        "source_acquisition_errors": list(
                            entry.get("source_acquisition_errors") or []
                        ),
                        "source_cooldown_skips": list(
                            entry.get("source_cooldown_skips") or []
                        ),
                        "source_retry_counts": {
                            source: dict(by_kind)
                            for source, by_kind in (
                                entry.get("source_retry_counts") or {}
                            ).items()
                        },
                        # #152: preserve any in-flight degradation
                        # summary on abandon (typically ``None`` since
                        # the summary is only populated by ``complete``,
                        # but allow restart hand-offs to keep partial
                        # data).
                        "degradation_summary": entry.get("degradation_summary"),
                    }
                )
                self._append(entry)
            self._write_json_atomic(self.active_path, {})
        except OSError:
            logger.warning("failed to abandon stale active runs", exc_info=True)

    def recent(
        self,
        *,
        limit: int = 20,
        profile: str | None = None,
    ) -> list[RunEntry]:
        """Return recent completed or failed runs, newest first."""
        limit = max(1, min(limit, 100))
        entries: list[RunEntry] = []
        try:
            lines = self.runs_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("failed to read run ledger", exc_info=True)
            return []

        for line in reversed(lines):
            if len(entries) >= limit:
                break
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed run ledger line")
                continue
            if profile is not None and entry.get("profile") != profile:
                continue
            entries.append(entry)
        return entries

    def last_by_profile(self) -> dict[str, RunEntry]:
        """Return the most recent terminal run for each profile."""
        latest: dict[str, RunEntry] = {}
        for entry in self.recent(limit=100):
            profile = entry.get("profile")
            if isinstance(profile, str) and profile not in latest:
                latest[profile] = entry
        return latest

    def _finish(
        self,
        *,
        run_id: str,
        status: str,
        sources_checked: int | None,
        ingested: int | None,
        error: str | None,
        fetched_total: int | None = None,
        filter_errors_total: int | None = None,
        invalid_url_rejections_total: int | None = None,
        archive_failures_total: int | None = None,
        summary_thin_drops_total: int | None = None,
        degraded: bool = False,
        source_acquisition_errors: list[dict[str, str]] | None = None,
        source_cooldown_skips: list[dict[str, str]] | None = None,
        degraded_reasons: list[str] | None = None,
        source_retry_counts: dict[str, dict[str, int]] | None = None,
        tier3_fallbacks: dict[str, int] | None = None,
        degradation_summary: dict[str, Any] | None = None,
    ) -> None:
        try:
            active = self._read_active()
            entry = active.pop(run_id, None)
            if entry is None:
                entry = {
                    "run_id": run_id,
                    "profile": None,
                    "kind": None,
                    "status": "running",
                    "run_range": {},
                    "started_at": None,
                }

            completed_at = datetime.now(UTC)

            entry.update(
                {
                    "status": status,
                    "completed_at": completed_at.isoformat(),
                    "duration_seconds": self._duration_seconds(
                        entry.get("started_at"),
                        completed_at,
                    ),
                    "sources_checked": sources_checked,
                    "ingested": ingested,
                    "fetched_total": fetched_total,
                    "filter_errors_total": filter_errors_total,
                    "invalid_url_rejections_total": invalid_url_rejections_total,
                    "archive_failures_total": archive_failures_total,
                    # Issue #166: persist per-run total of thin-summary
                    # suppressions so operators can see how many items
                    # the rule is dropping without scraping logs.
                    "summary_thin_drops_total": summary_thin_drops_total,
                    "error": error,
                    "degraded": degraded,
                    "degraded_reasons": list(degraded_reasons or []),
                    # Issue #164: precompute the severity bucket from
                    # the reason list so downstream consumers
                    # (``/runs/recent``, dashboards, paging) read a
                    # stable classification without reimplementing the
                    # mapping.  ``"unexpected_failure"`` wins when any
                    # unexpected reason co-occurs with expected-lossy
                    # ones.
                    "degradation_severity": classify_degradation_severity(
                        list(degraded_reasons or [])
                    ),
                    "source_acquisition_errors": list(source_acquisition_errors or []),
                    # Issue #146: persist cooldown skips alongside
                    # ``source_acquisition_errors`` so the same JSONL
                    # consumers can read both lists with the same shape.
                    "source_cooldown_skips": list(source_cooldown_skips or []),
                    # #129: deep-copy the per-source retry-count dict so
                    # later mutations of the contextvar bucket do not
                    # leak into the persisted ledger entry.
                    "source_retry_counts": {
                        source: dict(by_kind)
                        for source, by_kind in (source_retry_counts or {}).items()
                    },
                    # #151: shallow-copy the Tier 3 fallback counts so
                    # later mutations of the contextvar bucket do not
                    # leak into the persisted ledger entry.  ``int``
                    # values are already immutable, so a flat ``dict()``
                    # is sufficient.
                    "tier3_fallbacks": dict(tier3_fallbacks or {}),
                    # #152: persist the precomputed degradation summary
                    # so downstream consumers (``/runs/recent``) see the
                    # top-N breakdowns without re-aggregating.  ``None``
                    # on the fail / skip / abandon paths — those use
                    # the legacy zero-data shape.
                    "degradation_summary": degradation_summary,
                }
            )
            self._append(entry)
            self._write_json_atomic(self.active_path, active)
        except OSError:
            logger.warning("failed to update run ledger", exc_info=True)

    def _read_active(self) -> dict[str, RunEntry]:
        try:
            data = json.loads(self.active_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            logger.warning("active run ledger is malformed; resetting")
            return {}
        except OSError:
            logger.warning("failed to read active run ledger", exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(run_id): entry
            for run_id, entry in data.items()
            if isinstance(entry, dict)
        }

    def _append(self, entry: RunEntry) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.runs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def _duration_seconds(
        self,
        started_at_raw: Any,
        completed_at: datetime,
    ) -> float | None:
        if not isinstance(started_at_raw, str):
            return None
        try:
            started_at = datetime.fromisoformat(started_at_raw)
        except ValueError:
            return None
        return (completed_at - started_at).total_seconds()

    def _write_json_atomic(self, path: Path, data: Any) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
