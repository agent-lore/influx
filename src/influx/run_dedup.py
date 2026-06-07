"""Run-stage pre-acquire dedup helper (#125).

Issue #125 moves :meth:`influx.lithos_client.LithosClient.cache_lookup_for_item_body`
ahead of :meth:`Source.acquire` so duplicate items skip download / archive /
extraction cost in backfill profiles and merge-bound items still flow through
the write path with the cache-hit fact recorded.

Partition rules (one ``cache_lookup`` per scored candidate):

============================  =====================  ===========================
``hit``                       ``skip_cache_hits``    Goes to
============================  =====================  ===========================
``False`` (miss)              ``True``  / ``False``  ``to_acquire`` (cache_hit=False)
``True`` (hit)                ``True``               ``hits_to_skip`` (drop)
``True`` (hit)                ``False``              ``to_acquire`` (cache_hit=True)
============================  =====================  ===========================

The helper emits :func:`metrics.cache_hits` and the ``"article cache hit"``
log line for each hit it decides — those signals move out of the Ingest
stage with the lookup itself.  The defensive source-URL fallback (#128)
stays in Ingest and only fires when ``cache_hit=False`` reaches the write
path.

Error handling (#234): a per-candidate :class:`~influx.errors.LithosError`
from the lookup is caught — the candidate is skipped this run (recorded
via :func:`~influx.telemetry.record_dedup_lookup_error`, surfaced as the
``dedup_cache_lookup`` degraded reason) and the rest of the batch
processes.  Skipping is recoverable: scheduled runs re-fetch the same
lookback window, so the candidate is re-checked next run.  If *every*
attempted lookup fails the helper raises instead (Lithos fully down —
post-#232 connection failures arrive as ``LithosError`` too, and a
silently-empty "degraded" run would be indistinguishable from a quiet
feed window); in that case no skip-and-degrade signals are emitted —
they are buffered during the loop and flushed only when the batch
partially succeeded, so a total outage doesn't overcount swallowed
skips.  Non-Lithos exceptions always propagate.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from influx import metrics
from influx.errors import LithosError
from influx.lithos_client import LithosClient
from influx.source import BoundScoredCandidate
from influx.telemetry import record_dedup_lookup_error

__all__ = [
    "DedupDecision",
    "DedupOutcome",
    "dedup_scored_candidates",
]

logger = logging.getLogger(__name__)

#: Cap on the ``detail`` fragment in the per-candidate skip WARNING —
#: matches the 300-char bound :func:`record_dedup_lookup_error` applies
#: to the persisted telemetry record.
_DETAIL_LOG_MAX_CHARS = 300


def _metric_source(source_label: str) -> str:
    """Normalise ``source_label`` to the bounded ``source`` metric label.

    ``BoundScoredCandidate.source_label`` is rich — ``"arxiv"`` or
    ``"rss:<feed_name>"`` — so logs can carry per-feed provenance.  The
    ``source`` label on :func:`metrics.cache_hits` (and every other
    Influx metric) is contractually bounded to ``"arxiv"`` / ``"rss"``
    (see ``influx/metrics.py`` "Cardinality discipline").  Collapse the
    per-feed suffix here so dashboards/alerts keyed on the bounded set
    keep working and per-feed cardinality never leaks into metrics.
    """
    if source_label == "arxiv":
        return "arxiv"
    if source_label == "rss" or source_label.startswith("rss:"):
        return "rss"
    return "unknown"


@dataclass(frozen=True, slots=True)
class DedupDecision:
    """One scored candidate's pre-acquire dedup outcome.

    ``cache_hit_reason`` is ``"primary"`` when the primary
    title+first-sentence query hit, otherwise ``None``.  ``cache_body``
    is the raw lookup body so downstream code can inspect server-side
    metadata without re-querying.
    """

    bound: BoundScoredCandidate
    cache_hit: bool
    cache_hit_reason: str | None
    cache_body: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class DedupOutcome:
    """Partitioned dedup result for a batch of scored candidates.

    ``lookup_errors`` counts candidates skipped because their
    ``cache_lookup`` raised :class:`~influx.errors.LithosError` (#234)
    — those candidates land in *neither* partition.
    """

    to_acquire: tuple[DedupDecision, ...]
    hits_to_skip: tuple[DedupDecision, ...]
    lookup_errors: int = 0


async def dedup_scored_candidates(
    bounds: Sequence[BoundScoredCandidate],
    *,
    client: LithosClient,
    profile: str,
    skip_cache_hits: bool,
) -> DedupOutcome:
    """Run pre-acquire ``lithos_cache_lookup`` per *bound* and partition.

    Calls :meth:`LithosClient.cache_lookup_for_item_body` once per
    candidate, classifies each result per the matrix in this module's
    docstring, and returns a :class:`DedupOutcome`.

    A per-candidate :class:`LithosError` is caught and the candidate
    skipped this run (#234) — recorded via
    :func:`record_dedup_lookup_error` so the ledger fires the
    ``dedup_cache_lookup`` degraded reason.  Deliberately *not* treated
    as a cache miss: acquiring on an unverified lookup risks exactly the
    duplicate-note / duplicate-cost harm dedup exists to prevent.  If
    every attempted lookup fails, raises :class:`LithosError` (Lithos
    fully down must still fail the run) — in that case the per-candidate
    failures are NOT recorded as swallowed degradations: the
    skip-and-degrade telemetry/metrics/warnings are buffered during the
    loop and flushed only once the batch is known to have partially
    succeeded, so a total outage produces one failed run, not a failed
    run plus N misleading "skipped candidate" signals.  Non-Lithos
    exceptions propagate — matching the pre-#125 Ingest behaviour.
    """
    to_acquire: list[DedupDecision] = []
    hits_to_skip: list[DedupDecision] = []
    failed: list[tuple[BoundScoredCandidate, LithosError]] = []

    for bound in bounds:
        candidate = bound.scored.candidate
        abstract = candidate.abstract or None
        try:
            body = await client.cache_lookup_for_item_body(
                title=candidate.title,
                source_url=candidate.source_url,
                abstract_or_summary=abstract,
            )
        except LithosError as exc:
            # #234: one poisoned candidate must not abort the whole
            # run.  Buffer the failure and keep processing the batch —
            # the skip-and-degrade signals are flushed after the loop,
            # and only in the partial-failure case (a total outage
            # raises instead, and recording "swallowed" degradations
            # for a run that then fails would overcount).
            failed.append((bound, exc))
            continue
        hit = bool(body.get("hit"))

        if hit:
            metrics.cache_hits().add(
                1, {"profile": profile, "source": _metric_source(bound.source_label)}
            )
            action = "skip" if skip_cache_hits else "merge-profile"
            logger.info(
                "article cache hit profile=%s source_url=%s title=%r "
                "action=%s reason=primary source=%s",
                profile,
                candidate.source_url,
                candidate.title,
                action,
                bound.source_label,
            )
            decision = DedupDecision(
                bound=bound,
                cache_hit=True,
                cache_hit_reason="primary",
                cache_body=body,
            )
            if skip_cache_hits:
                hits_to_skip.append(decision)
            else:
                to_acquire.append(decision)
        else:
            to_acquire.append(
                DedupDecision(
                    bound=bound,
                    cache_hit=False,
                    cache_hit_reason=None,
                    cache_body=body,
                )
            )

    # #234 safeguard: every attempted lookup failing means Lithos is
    # effectively down (post-#232 connection failures are LithosError
    # too) — fail the run rather than emit a silently-empty "degraded"
    # outcome indistinguishable from a quiet feed window.  Raised
    # *before* the buffered skip-and-degrade signals are flushed: a
    # total outage must not record per-candidate "swallowed" telemetry
    # or metrics for failures that were not, in fact, swallowed.
    attempted = len(bounds)
    if attempted > 0 and len(failed) == attempted:
        _, last_lookup_error = failed[-1]
        raise LithosError(
            f"cache_lookup failed for all {attempted} candidates",
            # Propagate the underlying operation (e.g. "connect" when
            # Lithos was unreachable) so the formatted ledger error
            # points operators at what actually failed.
            operation=last_lookup_error.operation or "cache_lookup",
            detail=last_lookup_error.detail or str(last_lookup_error),
        ) from last_lookup_error

    # Partial failure (or none): the run continues, so each buffered
    # failure really is a swallowed skip — emit the WARNING, the
    # run-ledger telemetry record, and the metric tick now.
    for bound, exc in failed:
        candidate = bound.scored.candidate
        detail = exc.detail or str(exc)
        logger.warning(
            "dedup lookup failed profile=%s source_url=%s "
            "title=%r operation=%s detail=%s source=%s "
            "— skipping candidate this run",
            profile,
            candidate.source_url,
            candidate.title,
            exc.operation or "cache_lookup",
            # Bound the logged detail like the persisted telemetry —
            # Lithos tool error payloads can carry stack traces.
            detail[:_DETAIL_LOG_MAX_CHARS],
            bound.source_label,
        )
        record_dedup_lookup_error(
            source=_metric_source(bound.source_label),
            kind=exc.operation or "cache_lookup",
            detail=detail,
        )
        metrics.dedup_lookup_errors().add(
            1, {"profile": profile, "source": _metric_source(bound.source_label)}
        )

    return DedupOutcome(
        to_acquire=tuple(to_acquire),
        hits_to_skip=tuple(hits_to_skip),
        lookup_errors=len(failed),
    )
