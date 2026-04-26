"""Source fetchers for Influx ingestion pipeline.

Provides a unified ``make_item_provider`` that combines arXiv and RSS
sub-providers with a shared :class:`FetchCache` so that multiple profiles
sharing the same source within a single scheduled fire reuse the same fetch
result instead of making redundant network calls (R-8 mitigation, AC-09-D).
"""

from __future__ import annotations

import asyncio
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

    Caches source fetch results so that profile runs within a single
    scheduled fire reuse the same fetch result.  The cache stores an
    ``asyncio.Future`` per key so concurrent profile fires await the
    same in-flight fetch rather than racing past ``has()`` and double
    fetching (review finding 2).

    Cache lifetime is bounded by reference-counted "fire scope":
    :meth:`begin_fire` clears the cache on the first concurrent fire and
    :meth:`end_fire` clears it again when the last concurrent fire
    completes.  This prevents cached results from one cron tick (or one
    HTTP-triggered run) from leaking into a later one.
    """

    def __init__(self) -> None:
        # Each entry is either a literal cached value or an in-flight
        # ``asyncio.Future`` shared by concurrent ``get_or_fetch``
        # callers.  Successful Futures are collapsed back into literals
        # so completed cache entries do not retain a reference to the
        # event loop the fetch ran on.
        self._store: dict[str, Any] = {}
        self._active_fires: int = 0

    @staticmethod
    def _resolved_value(value: Any) -> Any:
        if isinstance(value, asyncio.Future):
            if not value.done() or value.cancelled():
                raise KeyError("future not resolved")
            exc = value.exception()
            if exc is not None:
                raise KeyError("future raised") from exc
            return value.result()
        return value

    def has(self, key: str) -> bool:
        """Return ``True`` if *key* holds a successfully-completed value."""
        if key not in self._store:
            return False
        value = self._store[key]
        if isinstance(value, asyncio.Future):
            return (
                value.done()
                and not value.cancelled()
                and value.exception() is None
            )
        return True

    def get(self, key: str) -> Any:
        """Return the cached value for *key*.

        Raises ``KeyError`` if the key is absent or the cached future
        has not yet completed successfully — callers should check
        :meth:`has` first.
        """
        if key not in self._store:
            raise KeyError(key)
        return self._resolved_value(self._store[key])

    def put(self, key: str, value: Any) -> None:
        """Store *value* under *key* as a completed cache entry."""
        self._store[key] = value

    async def get_or_fetch(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Return the cached value or run *factory* exactly once.

        Concurrent callers that arrive before *factory* completes await
        the same in-flight ``asyncio.Future``, so a shared source is
        fetched exactly once per fire even when two profiles race
        (R-8 mitigation, AC-09-D).
        """
        existing = self._store.get(key)
        if existing is not None:
            if isinstance(existing, asyncio.Future):
                return await existing
            return existing

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._store[key] = future
        try:
            result = await factory()
        except BaseException as exc:
            # Drop the failed future so a later attempt can retry rather
            # than receiving the same cached failure forever.
            self._store.pop(key, None)
            future.set_exception(exc)
            raise
        future.set_result(result)
        # Collapse the resolved future to a literal so subsequent
        # callers do not need to await a future tied to this loop.
        self._store[key] = result
        return result

    @property
    def keys(self) -> list[str]:
        """Return all cached keys (for test introspection)."""
        return list(self._store.keys())

    def clear(self) -> None:
        """Drop all cached entries."""
        self._store.clear()

    def begin_fire(self) -> None:
        """Mark the start of a fire; clears the cache on the first entry."""
        if self._active_fires == 0:
            self._store.clear()
        self._active_fires += 1

    def end_fire(self) -> None:
        """Mark the end of a fire; clears the cache once all fires finish."""
        if self._active_fires > 0:
            self._active_fires -= 1
        if self._active_fires == 0:
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
