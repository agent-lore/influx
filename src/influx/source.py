"""Unified Source seam (CONTEXT.md ``Source``).

Defines the protocol every source adapter (arXiv, RSS, blog) implements
and the candidate / scored-candidate value types that flow through the
Run's Acquire stage.

Per CONTEXT.md the Run's Acquire stage walks::

    Source.fetch_candidates → Filter.score → Source.acquire → Acquired

This module owns the seam.  Source adapters live under
``influx.sources.*``; the Filter that scores candidates lives in
``influx.filter``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from influx.config import AppConfig, ProfileConfig
from influx.coordinator import RunKind

__all__ = [
    "BoundScoredCandidate",
    "Candidate",
    "ScoredCandidate",
    "Source",
]


@dataclass(frozen=True, slots=True)
class Candidate:
    """An unscored item returned from :meth:`Source.fetch_candidates`.

    Carries the minimal identity surface every Filter needs (id, title,
    abstract, source URL) plus a ``payload`` slot for the source-native
    metadata the adapter's :meth:`Source.acquire` will consume.

    The ``payload`` is opaque to the Filter — typically the original
    :class:`influx.sources.arxiv.ArxivItem` or
    :class:`influx.sources.rss.RssFeedItem`.
    """

    item_id: str
    title: str
    abstract: str
    source_url: str
    payload: Any = None


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """A :class:`Candidate` plus the Filter's 1–10 relevance score.

    ``filter_tags`` carries the LLM-filter tags (FR-FLT-3) used by
    rejection-rate logging, distinct from the persisted note tags the
    source builder later attaches.  Items below
    ``thresholds.relevance`` or absent from the filter response are
    dropped by the Filter and never reach this stage.
    """

    candidate: Candidate
    score: int
    confidence: float
    reason: str
    filter_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundScoredCandidate:
    """A :class:`ScoredCandidate` plus a thunk that performs source-specific
    acquire (#125).

    Returned by the unified ``ItemProvider`` so the orchestration layer
    can run ``lithos_cache_lookup`` on candidate identity *before* the
    source adapter pays the full download/archive/extract cost.

    ``acquire`` is a no-arg async callable that captures the source
    adapter, ``profile_cfg``, and ``config`` from the provider scope;
    invoking it runs the per-item acquire (typically wrapping
    :func:`asyncio.to_thread` around blocking work, #124) and returns
    the ready-to-yield ``ProfileItem`` dict.

    ``source_label`` carries the source family used for metric labels and
    log lines — ``"arxiv"`` or ``"rss:<feed_name>"``.
    """

    scored: ScoredCandidate
    acquire: Callable[[], Awaitable[dict[str, Any] | None]]
    source_label: str


@runtime_checkable
class Source(Protocol):
    """Unified Source seam (CONTEXT.md).

    A Source adapter exposes two stages:

    - :meth:`fetch_candidates` — bulk per-Profile, called once per Run.
      Returns the unscored candidates the Filter will score.
    - :meth:`acquire` — per-item, called by the orchestrator after
      Filter scoring.  Performs download/archive/extract and returns
      the ready-to-yield ``ProfileItem`` dict consumed by the
      scheduler's ``run_profile``.

    The score-gated cascade (Tier 1/2/3 + Renderer) is reached only
    via :meth:`acquire`; sources do not score candidates themselves.
    """

    name: str

    def fetch_candidates(
        self,
        *,
        profile_cfg: ProfileConfig,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
    ) -> Awaitable[list[Candidate]]: ...

    def acquire(
        self,
        scored: ScoredCandidate,
        *,
        profile_cfg: ProfileConfig,
        config: AppConfig,
    ) -> dict[str, Any] | None: ...
