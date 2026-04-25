"""Webhook notification digest builder (§11, FR-NOT-2..6).

Builds the JSON digest body for Agent Zero webhook POSTs.  Two shapes:

- **Non-zero-ingest** (§11.1): full digest with ``highlights``,
  ``all_ingested``, and ``stats.high_relevance``.
- **Zero-ingest / quiet** (§11.2): minimal digest with ``message``
  and no ``highlights``/``all_ingested``.

The digest builder is pure — it accepts a run result and config and
returns a JSON-serialisable ``dict``.  The HTTP sender lives in the
same module but is wired separately (US-016).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HighlightItem:
    """One high-relevance item for the digest ``highlights`` array."""

    id: str
    title: str
    score: int
    tags: list[str]
    reason: str
    url: str
    related_in_lithos: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict matching §11.1."""
        return {
            "id": self.id,
            "title": self.title,
            "score": self.score,
            "tags": list(self.tags),
            "reason": self.reason,
            "url": self.url,
            "related_in_lithos": list(self.related_in_lithos),
        }


@dataclass(frozen=True)
class IngestedItem:
    """One ingested item for the ``all_ingested`` array."""

    id: str
    title: str
    score: int
    url: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return {
            "id": self.id,
            "title": self.title,
            "score": self.score,
            "url": self.url,
        }


@dataclass(frozen=True)
class RunStats:
    """Aggregate stats for a single profile run."""

    sources_checked: int
    ingested: int


@dataclass(frozen=True)
class ProfileRunResult:
    """The data a profile run produces, consumed by the digest builder."""

    run_date: str  # YYYY-MM-DD
    profile: str
    stats: RunStats
    items: list[HighlightItem] = field(default_factory=list)


def build_digest(
    result: ProfileRunResult,
    *,
    notify_immediate_threshold: int,
) -> dict[str, Any]:
    """Build the webhook digest body (§11.1 / §11.2).

    Parameters
    ----------
    result:
        The profile-run outcome containing stats and per-item data.
    notify_immediate_threshold:
        The ``profiles.thresholds.notify_immediate`` value from config.
        Items with ``score >= notify_immediate_threshold`` appear in
        ``highlights``.  The threshold is **not** hardcoded — it comes
        from config (AC-X-1, AC-05-I).

    Returns
    -------
    dict
        A JSON-serialisable dict matching §11.1 (non-zero ingest) or
        §11.2 (zero ingest / quiet run).
    """
    if result.stats.ingested == 0:
        return _build_quiet_digest(result)
    return _build_full_digest(result, notify_immediate_threshold)


def _build_quiet_digest(result: ProfileRunResult) -> dict[str, Any]:
    """§11.2 quiet-run digest — no highlights or all_ingested."""
    return {
        "type": "influx_digest",
        "run_date": result.run_date,
        "profile": result.profile,
        "stats": {
            "sources_checked": result.stats.sources_checked,
            "ingested": 0,
        },
        "message": "No new relevant content found today.",
    }


def _build_full_digest(
    result: ProfileRunResult,
    notify_immediate_threshold: int,
) -> dict[str, Any]:
    """§11.1 full digest with highlights and all_ingested."""
    highlights = [
        item
        for item in result.items
        if item.score >= notify_immediate_threshold
    ]
    all_ingested = [
        IngestedItem(
            id=item.id,
            title=item.title,
            score=item.score,
            url=item.url,
        )
        for item in result.items
    ]

    return {
        "type": "influx_digest",
        "run_date": result.run_date,
        "profile": result.profile,
        "stats": {
            "sources_checked": result.stats.sources_checked,
            "ingested": result.stats.ingested,
            "high_relevance": len(highlights),
        },
        "highlights": [h.to_dict() for h in highlights],
        "all_ingested": [i.to_dict() for i in all_ingested],
    }
