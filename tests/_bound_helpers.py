"""Test helpers for the #125 BoundScoredCandidate seam.

The Run pipeline now expects ``ItemProvider`` to return
:class:`BoundScoredCandidate` instances rather than fully-acquired
ProfileItem dicts.  Tests that hand-build their own item providers wrap
each ProfileItem dict via :func:`bound_for` so the closure simply
returns the dict — keeping pre-#125 fixture data intact while
satisfying the new seam.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from influx.source import BoundScoredCandidate, Candidate, ScoredCandidate


def bound_for(
    item: dict[str, Any],
    *,
    source_label: str | None = None,
) -> BoundScoredCandidate:
    """Wrap a ProfileItem dict as a BoundScoredCandidate.

    The ``acquire`` closure returns the dict verbatim.  ``source_label``
    defaults to the item's ``source`` key (typically ``"arxiv"`` or
    ``"rss"``) so metric labels emitted by the Run stage match what the
    real providers would have produced.
    """
    if source_label is None:
        direct = item.get("source")
        source_label = direct if isinstance(direct, str) and direct else "arxiv"

    async def _acquire() -> dict[str, Any]:
        return item

    return BoundScoredCandidate(
        scored=ScoredCandidate(
            candidate=Candidate(
                item_id=str(item.get("source_url") or item.get("id") or "test-id"),
                title=str(item.get("title", "")),
                abstract=str(item.get("abstract_or_summary") or ""),
                source_url=str(item.get("source_url", "")),
            ),
            score=int(item.get("score", 0)),
            confidence=float(item.get("confidence", 0.0)),
            reason=str(item.get("reason", "")),
            filter_tags=tuple(item.get("filter_tags", [])),
        ),
        acquire=_acquire,
        source_label=source_label,
    )


def bounds_for(items: Iterable[dict[str, Any]]) -> list[BoundScoredCandidate]:
    """Map :func:`bound_for` across a list of ProfileItem dicts."""
    return [bound_for(it) for it in items]
