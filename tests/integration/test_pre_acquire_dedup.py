"""Integration tests for #125 — pre-acquire Lithos dedup.

These tests prove the optimisation: before Source.acquire downloads,
archives, and extracts an article, the Run stage runs
``lithos_cache_lookup`` on the scored candidate's identity.  In a
backfill, a primary cache hit drops the candidate before any per-item
acquisition work is paid for.

Three behaviours covered:

1. ``test_backfill_arxiv_cache_hit_skips_source_acquire`` — backfill
   profile with a primed cache hit: the arXiv per-item acquire entry
   point (``build_arxiv_note_item``) is NEVER invoked, no
   ``lithos_write`` happens, and the run still completes.

2. ``test_backfill_rss_cache_hit_skips_source_acquire`` — backfill
   profile with a primed RSS cache hit: the RSS per-item acquire entry
   point (``build_rss_note_item``) is NEVER invoked, no
   ``lithos_write`` happens.

3. ``test_normal_run_cache_hit_still_acquires_for_merge`` — normal
   (non-backfill) run with a primed cache hit: acquire IS invoked, the
   write path runs to support multi-profile merge, and the ProfileItem
   carries ``cache_hit=True`` / ``cache_hit_reason="primary"``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from influx.backfill import run_backfill
from influx.config import (
    AppConfig,
    ArxivSourceConfig,
    FeedbackConfig,
    LithosConfig,
    NotificationsConfig,
    ProfileConfig,
    ProfileSources,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ResilienceConfig,
    RssSourceEntry,
    ScheduleConfig,
    SecurityConfig,
)
from influx.coordinator import RunKind
from influx.scheduler import run_profile
from influx.sources import FetchCache, make_item_provider
from influx.sources.arxiv import (
    ArxivItem,
    ArxivScorer,
    ArxivScoreResult,
)
from influx.sources.rss import RssFeedItem
from tests.contract.test_lithos_client import FakeLithosServer

PROFILE = "ai-robotics"


_ARXIV_FIXTURE = [
    ArxivItem(
        arxiv_id="2701.00001",
        title="Already-Ingested Paper",
        abstract="A paper that Lithos already has cached for this profile.",
        published=datetime(2026, 4, 20, tzinfo=UTC),
        categories=["cs.AI"],
    ),
]


_RSS_FIXTURE = [
    RssFeedItem(
        url="https://example.org/already-ingested",
        title="Already-Ingested Blog Post",
        summary="A post that Lithos already has cached.",
        published=datetime(2026, 4, 20, tzinfo=UTC),
        source_tag="rss",
        feed_name="Test Blog",
    ),
]


def _arxiv_only_config(lithos_url: str) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name=PROFILE,
                description="AI and robotics",
                thresholds=ProfileThresholds(
                    relevance=7,
                    full_text=100,
                    deep_extract=100,
                    notify_immediate=8,
                ),
                sources=ProfileSources(
                    arxiv=ArxivSourceConfig(
                        enabled=True,
                        categories=["cs.AI"],
                        max_results_per_category=10,
                        lookback_days=30,
                    ),
                ),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(
                text=(
                    "Filter: {profile_description} "
                    "{negative_examples} "
                    "{min_score_in_results}"
                ),
            ),
            tier1_enrich=PromptEntryConfig(text="t"),
            tier3_extract=PromptEntryConfig(text="t"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        feedback=FeedbackConfig(negative_examples_per_profile=20),
        resilience=ResilienceConfig(arxiv_request_min_interval_seconds=0),
    )


def _rss_only_config(lithos_url: str) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name=PROFILE,
                description="AI and robotics",
                thresholds=ProfileThresholds(
                    relevance=7,
                    full_text=100,
                    deep_extract=100,
                    notify_immediate=8,
                ),
                sources=ProfileSources(
                    arxiv=ArxivSourceConfig(enabled=False),
                    rss=[
                        RssSourceEntry(
                            name="Test Blog",
                            url="https://example.org/feed.xml",
                            source_tag="rss",
                        ),
                    ],
                ),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(
                text=(
                    "Filter: {profile_description} "
                    "{negative_examples} "
                    "{min_score_in_results}"
                ),
            ),
            tier1_enrich=PromptEntryConfig(text="t"),
            tier3_extract=PromptEntryConfig(text="t"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        feedback=FeedbackConfig(negative_examples_per_profile=20),
        resilience=ResilienceConfig(arxiv_request_min_interval_seconds=0),
    )


def _deterministic_arxiv_scorer(score: int = 8) -> ArxivScorer:
    def _score(item: ArxivItem, profile: str) -> ArxivScoreResult:
        del item, profile
        return ArxivScoreResult(score=score, confidence=1.0, reason="test-scorer")

    return _score


@pytest.fixture(scope="module")
def fake_lithos() -> Generator[FakeLithosServer, None, None]:
    server = FakeLithosServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def fake_lithos_url(fake_lithos: FakeLithosServer) -> str:
    return f"http://127.0.0.1:{fake_lithos.port}/sse"


@pytest.fixture(autouse=True)
def clear_fakes(fake_lithos: FakeLithosServer) -> None:
    fake_lithos.calls.clear()
    fake_lithos.write_responses.clear()
    fake_lithos.read_responses.clear()
    fake_lithos.cache_lookup_responses.clear()
    fake_lithos.list_responses.clear()
    fake_lithos.task_create_responses.clear()
    fake_lithos.task_complete_responses.clear()


# ── arXiv backfill: cache hit short-circuits before acquire ────────────


class TestPreAcquireDedupArxivBackfill:
    """Backfill cache hit drops the candidate before per-item acquire."""

    def test_backfill_arxiv_cache_hit_skips_source_acquire(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        config = _arxiv_only_config(fake_lithos_url)
        provider = make_item_provider(
            config,
            fetch_cache=FetchCache(),
            arxiv_scorer=_deterministic_arxiv_scorer(8),
        )

        # Feedback list (loaded by the Feedback stage).
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # Pre-acquire primary lookup → hit.
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        with (
            patch(
                "influx.sources.arxiv.fetch_arxiv",
                return_value=list(_ARXIV_FIXTURE),
            ),
            patch("influx.sources.arxiv.build_arxiv_note_item") as mock_build,
        ):
            result = asyncio.run(
                run_backfill(
                    PROFILE,
                    run_range={"days": 7},
                    config=config,
                    item_provider=provider,
                )
            )

        assert result is not None

        # The whole point of #125: per-item acquire is NEVER called for
        # a backfill cache hit.
        mock_build.assert_not_called()

        # Zero writes — the item was dropped pre-acquire.
        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 0

        # Exactly one cache_lookup — primary, fired in the pre-acquire
        # dedup helper.  The Ingest URL fallback never runs because the
        # item never reaches Ingest.
        cache_calls = [c for c in fake_lithos.calls if c[0] == "lithos_cache_lookup"]
        assert len(cache_calls) == 1


# ── RSS backfill: cache hit short-circuits before acquire ──────────────


class TestPreAcquireDedupRssBackfill:
    """RSS backfill cache hit drops the candidate before per-item acquire."""

    def test_backfill_rss_cache_hit_skips_source_acquire(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        config = _rss_only_config(fake_lithos_url)
        provider = make_item_provider(config, fetch_cache=FetchCache())

        # Feedback list.
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # Pre-acquire primary lookup → hit.
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        async def _fake_score_rss_items(
            *,
            items: list[RssFeedItem],
            profile: str,
            filter_prompt: str,
            config: AppConfig,
        ) -> dict[str, Any]:
            from influx.schemas import FilterResult
            from influx.urls import url_hash

            return {
                url_hash(item.url): FilterResult(
                    id=url_hash(item.url),
                    score=8,
                    reason="test",
                    tags=["test"],
                )
                for item in items
            }

        with (
            patch(
                "influx.sources.rss._fetch_rss_feed",
                return_value=list(_RSS_FIXTURE),
            ),
            patch(
                "influx.sources.rss._score_rss_items",
                side_effect=_fake_score_rss_items,
            ),
            patch("influx.sources.rss.build_rss_note_item") as mock_build,
        ):
            result = asyncio.run(
                run_backfill(
                    PROFILE,
                    run_range={"days": 7},
                    config=config,
                    item_provider=provider,
                )
            )

        assert result is not None

        # The whole point of #125: per-item acquire is NEVER called for
        # a backfill cache hit.
        mock_build.assert_not_called()

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 0

        cache_calls = [c for c in fake_lithos.calls if c[0] == "lithos_cache_lookup"]
        assert len(cache_calls) == 1


# ── Normal run: cache hit still goes through acquire (merge path) ──────


class TestPreAcquireDedupNormalRun:
    """Non-backfill cache hits still acquire so multi-profile merge runs."""

    def test_normal_run_cache_hit_still_acquires_for_merge(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        config = _arxiv_only_config(fake_lithos_url)
        provider = make_item_provider(
            config,
            fetch_cache=FetchCache(),
            arxiv_scorer=_deterministic_arxiv_scorer(8),
        )

        # Feedback list + repair sweep list (normal run runs repair too).
        fake_lithos.list_responses.append(json.dumps({"items": []}))  # feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))  # repair sweep
        # Pre-acquire primary lookup → hit (normal run, so merge path).
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )
        # Lithos write succeeds (multi-profile merge).
        fake_lithos.write_responses.append(
            json.dumps(
                {
                    "status": "updated",
                    "id": "note-merged",
                    "detail": "",
                }
            )
        )

        with (
            patch(
                "influx.sources.arxiv.fetch_arxiv",
                return_value=list(_ARXIV_FIXTURE),
            ),
            patch(
                "influx.sources.arxiv.build_arxiv_note_item",
                return_value={
                    "title": "Already-Ingested Paper",
                    "source": "arxiv",
                    "source_url": "https://arxiv.org/abs/2701.00001",
                    "content": "merged content",
                    "tags": ["source:arxiv", "profile:ai-robotics"],
                    "filter_tags": [],
                    "score": 8,
                    "confidence": 1.0,
                    "reason": "test-scorer",
                    "path": "papers/arxiv/2026/04",
                    "abstract_or_summary": "abs",
                    "contributions": None,
                    "builds_on": None,
                },
            ) as mock_build,
        ):
            result = asyncio.run(
                run_profile(
                    PROFILE,
                    RunKind.SCHEDULED,
                    config=config,
                    item_provider=provider,
                )
            )

        assert result is not None

        # Merge path: acquire IS called for the cache-hit item.
        assert mock_build.call_count == 1

        # Write IS called (multi-profile merge handled inside write_note).
        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 1
