"""Source fetchers for Influx ingestion pipeline.

Provides a unified ``make_item_provider`` that combines arXiv and RSS
sub-providers with a shared :class:`FetchCache` so that multiple profiles
sharing the same source within a single scheduled fire reuse the same fetch
result instead of making redundant network calls (R-8 mitigation, AC-09-D).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from influx.config import AppConfig
from influx.coordinator import RunKind

__all__ = [
    "FetchCache",
    "make_item_provider",
]


class FetchCache:
    """Per-fire fetch deduplication cache (R-8).

    Caches source fetch results so that concurrent profile runs within
    a single scheduled fire reuse the same fetch result.  The cache is
    keyed by a string identifier (arXiv query URL, RSS feed URL, etc.).
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def has(self, key: str) -> bool:
        """Return ``True`` if *key* is cached."""
        return key in self._store

    def get(self, key: str) -> Any:
        """Return the cached value for *key*.

        Raises ``KeyError`` if *key* is not present — callers should
        check :meth:`has` first.
        """
        return self._store[key]

    def put(self, key: str, value: Any) -> None:
        """Store *value* under *key*."""
        self._store[key] = value

    @property
    def keys(self) -> list[str]:
        """Return all cached keys (for test introspection)."""
        return list(self._store.keys())

    def clear(self) -> None:
        """Drop all cached entries."""
        self._store.clear()


def make_item_provider(
    config: AppConfig,
    *,
    fetch_cache: FetchCache | None = None,
    arxiv_scorer: Any | None = None,
    arxiv_filter_scorer: Any | None = None,
) -> Callable[
    [str, RunKind, dict[str, str | int] | None, str],
    Awaitable[Iterable[dict[str, Any]]],
]:
    """Build a unified item provider for arXiv + RSS with fetch dedup.

    The returned async callable conforms to
    :data:`~influx.scheduler.ItemProvider`.  It delegates to the arXiv
    and RSS sub-providers, passing a shared :class:`FetchCache` so that
    overlapping source fetches across profiles are deduplicated (R-8).

    Parameters
    ----------
    config:
        Loaded :class:`~influx.config.AppConfig`.
    fetch_cache:
        Optional shared cache.  When ``None`` a new cache is created —
        callers that want cross-profile dedup should share a single
        instance across all profiles in a scheduled fire.
    arxiv_scorer:
        Per-item synchronous scorer override (test seam).
    arxiv_filter_scorer:
        Batched LLM filter scorer (production default).
    """
    from influx.sources.arxiv import make_arxiv_item_provider
    from influx.sources.rss import make_rss_item_provider

    cache = fetch_cache if fetch_cache is not None else FetchCache()

    arxiv_provider = make_arxiv_item_provider(
        config,
        scorer=arxiv_scorer,
        filter_scorer=arxiv_filter_scorer,
        fetch_cache=cache,
    )
    rss_provider = make_rss_item_provider(config, fetch_cache=cache)

    async def provider(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> Iterable[dict[str, Any]]:
        arxiv_items = list(
            await arxiv_provider(profile, kind, run_range, filter_prompt)
        )
        rss_items = list(
            await rss_provider(profile, kind, run_range, filter_prompt)
        )
        return arxiv_items + rss_items

    return provider
