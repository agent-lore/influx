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
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from influx import metrics
from influx.lithos_client import LithosClient
from influx.source import BoundScoredCandidate

__all__ = [
    "DedupDecision",
    "DedupOutcome",
    "dedup_scored_candidates",
]

logger = logging.getLogger(__name__)


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
    """Partitioned dedup result for a batch of scored candidates."""

    to_acquire: tuple[DedupDecision, ...]
    hits_to_skip: tuple[DedupDecision, ...]


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

    Errors from ``cache_lookup_for_item_body`` propagate — matching the
    pre-#125 Ingest behaviour, which had no surrounding try/except.
    """
    to_acquire: list[DedupDecision] = []
    hits_to_skip: list[DedupDecision] = []

    for bound in bounds:
        candidate = bound.scored.candidate
        abstract = candidate.abstract or None
        body = await client.cache_lookup_for_item_body(
            title=candidate.title,
            source_url=candidate.source_url,
            abstract_or_summary=abstract,
        )
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

    return DedupOutcome(
        to_acquire=tuple(to_acquire),
        hits_to_skip=tuple(hits_to_skip),
    )
