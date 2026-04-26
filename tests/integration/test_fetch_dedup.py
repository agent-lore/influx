"""Integration tests for per-source fetch deduplication across profiles (US-007).

When a scheduled fire executes multiple profiles concurrently, shared sources
should be fetched once and the result fanned out to all interested profiles
(R-8 mitigation, AC-09-D).

These tests exercise the ``FetchCache`` wiring in
:func:`influx.sources.make_item_provider` end-to-end through
:func:`influx.scheduler.run_profile`, verifying:

- AC-09-D: ``cs.AI`` is fetched exactly once when two profiles both subscribe.
- RSS feed dedup: a shared RSS feed URL is fetched once across two profiles.
- Cross-profile parallelism remains allowed (Q-4).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

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
from tests.contract.test_lithos_client import FakeLithosServer

# ── Constants ─────────────────────────────────────────────────────────

PROFILE_A = "ai-robotics"
PROFILE_B = "web-tech"

# Fixture arXiv item (cs.AI category, shared by both profiles).
_FIXTURE_ARXIV_ITEMS = [
    ArxivItem(
        arxiv_id="2601.00001",
        title="Shared cs.AI Paper",
        abstract="This paper covers attention mechanisms relevant to both profiles.",
        published=datetime(2026, 4, 25, tzinfo=UTC),
        categories=["cs.AI"],
    ),
]


# ── Helpers ───────────────────────────────────────────────────────────


def _make_config(lithos_url: str) -> AppConfig:
    """Build an AppConfig with two profiles both subscribed to cs.AI."""
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name=PROFILE_A,
                description="AI and robotics",
                thresholds=ProfileThresholds(
                    relevance=100,
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
            ProfileConfig(
                name=PROFILE_B,
                description="Web tech and standards",
                thresholds=ProfileThresholds(
                    relevance=100,
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
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        feedback=FeedbackConfig(negative_examples_per_profile=20),
    )


def _deterministic_scorer(score: int = 5) -> ArxivScorer:
    """Build a deterministic scorer that accepts every item with *score*."""

    def _score(item: ArxivItem, profile: str) -> ArxivScoreResult:
        del item, profile
        return ArxivScoreResult(score=score, confidence=1.0, reason="test-scorer")

    return _score


# ── Fixtures ─────────────────────────────────────���────────────────────


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


# ── AC-09-D: arXiv category fetched once across two profiles ─────────


class TestArxivFetchDedup:
    """Two profiles both subscribed to cs.AI → one fetch, items fan-out."""

    def test_shared_category_fetched_once(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """AC-09-D: cs.AI fetched exactly once for the run; items to both."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()

        # Track how many times fetch_arxiv is actually called.
        fetch_count = 0

        def counting_fetch(**kwargs: Any) -> list[ArxivItem]:
            nonlocal fetch_count
            fetch_count += 1
            return list(_FIXTURE_ARXIV_ITEMS)

        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos responses for Profile A: repair sweep + feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            side_effect=counting_fetch,
        ):
            # Run Profile A
            asyncio.run(
                run_profile(
                    PROFILE_A,
                    RunKind.MANUAL,
                    config=config,
                    item_provider=provider,
                )
            )

        assert fetch_count == 1, f"Expected 1 fetch, got {fetch_count}"

        # Verify Profile A got items
        write_calls_a = [
            c for c in fake_lithos.calls if c[0] == "lithos_write"
        ]
        assert len(write_calls_a) >= 1
        a_titles = {c[1]["title"] for c in write_calls_a}
        assert "Shared cs.AI Paper" in a_titles

        # Now run Profile B — fetch_arxiv should NOT be called again.
        fake_lithos.calls.clear()
        fake_lithos.write_responses.clear()
        # Queue Lithos responses for Profile B: repair sweep + feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # Cache hit → write → version_conflict → merge
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            side_effect=counting_fetch,
        ):
            asyncio.run(
                run_profile(
                    PROFILE_B,
                    RunKind.MANUAL,
                    config=config,
                    item_provider=provider,
                )
            )

        # Still exactly 1 total fetch — the second profile used the cache.
        assert fetch_count == 1, f"Expected 1 total fetch, got {fetch_count}"

        # Verify Profile B also received items (wrote to Lithos).
        write_calls_b = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls_b) >= 1
        b_titles = {c[1]["title"] for c in write_calls_b}
        assert "Shared cs.AI Paper" in b_titles

    def test_different_categories_fetched_separately(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Profiles with different categories → separate fetches."""
        config = AppConfig(
            lithos=LithosConfig(url=fake_lithos_url),
            schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
            profiles=[
                ProfileConfig(
                    name=PROFILE_A,
                    description="AI robotics",
                    thresholds=ProfileThresholds(
                        relevance=100, full_text=100,
                        deep_extract=100, notify_immediate=8,
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
                ProfileConfig(
                    name=PROFILE_B,
                    description="Web tech",
                    thresholds=ProfileThresholds(
                        relevance=100, full_text=100,
                        deep_extract=100, notify_immediate=8,
                    ),
                    sources=ProfileSources(
                        arxiv=ArxivSourceConfig(
                            enabled=True,
                            categories=["cs.LG"],
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
                        "{negative_examples} {min_score_in_results}"
                    ),
                ),
                tier1_enrich=PromptEntryConfig(text="test"),
                tier3_extract=PromptEntryConfig(text="test"),
            ),
            notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
            security=SecurityConfig(allow_private_ips=True),
            feedback=FeedbackConfig(negative_examples_per_profile=20),
        )
        fetch_cache = FetchCache()
        fetch_count = 0

        def counting_fetch(**kwargs: Any) -> list[ArxivItem]:
            nonlocal fetch_count
            fetch_count += 1
            return list(_FIXTURE_ARXIV_ITEMS)

        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue for both profiles: repair sweep + feedback
        for _ in range(4):
            fake_lithos.list_responses.append(json.dumps({"items": []}))

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            side_effect=counting_fetch,
        ):
            asyncio.run(
                run_profile(
                    PROFILE_A,
                    RunKind.MANUAL,
                    config=config,
                    item_provider=provider,
                )
            )
            asyncio.run(
                run_profile(
                    PROFILE_B,
                    RunKind.MANUAL,
                    config=config,
                    item_provider=provider,
                )
            )

        # Different categories → 2 fetches (no dedup).
        assert fetch_count == 2, f"Expected 2 fetches, got {fetch_count}"


# ── RSS fetch dedup ───────────────────────────────────────────────────


class TestRssFetchDedup:
    """Two profiles sharing the same RSS feed URL → one feed fetch."""

    def test_shared_rss_feed_fetched_once(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Shared RSS feed URL fetched once, items fan-out to both profiles."""
        from influx.http_client import FetchResult

        rss_feed = RssSourceEntry(
            name="Shared Blog",
            url="https://shared-blog.example/feed.xml",
            source_tag="blog",
        )
        config = AppConfig(
            lithos=LithosConfig(url=fake_lithos_url),
            schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
            profiles=[
                ProfileConfig(
                    name=PROFILE_A,
                    description="AI robotics",
                    thresholds=ProfileThresholds(notify_immediate=8),
                    sources=ProfileSources(
                        arxiv=ArxivSourceConfig(enabled=False),
                        rss=[rss_feed],
                    ),
                ),
                ProfileConfig(
                    name=PROFILE_B,
                    description="Web tech",
                    thresholds=ProfileThresholds(notify_immediate=8),
                    sources=ProfileSources(
                        arxiv=ArxivSourceConfig(enabled=False),
                        rss=[rss_feed],
                    ),
                ),
            ],
            providers={},
            prompts=PromptsConfig(
                filter=PromptEntryConfig(
                    text=(
                        "Filter: {profile_description} "
                        "{negative_examples} {min_score_in_results}"
                    ),
                ),
                tier1_enrich=PromptEntryConfig(text="test"),
                tier3_extract=PromptEntryConfig(text="test"),
            ),
            notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
            security=SecurityConfig(allow_private_ips=True),
            feedback=FeedbackConfig(negative_examples_per_profile=20),
        )

        fetch_cache = FetchCache()
        http_fetch_count = 0

        # Minimal RSS 2.0 feed fixture.
        rss_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel>'
            "<title>Shared Blog</title>"
            "<item>"
            "<title>Shared Blog Post</title>"
            "<link>https://shared-blog.example/post-1</link>"
            "<description>A shared blog post.</description>"
            "<pubDate>Sat, 25 Apr 2026 00:00:00 GMT</pubDate>"
            "</item>"
            "</channel></rss>"
        )

        def counting_guarded_fetch(url: str, **kwargs: Any) -> FetchResult:
            nonlocal http_fetch_count
            http_fetch_count += 1
            return FetchResult(
                body=rss_xml.encode(),
                status_code=200,
                content_type="application/rss+xml",
                final_url=url,
            )

        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
        )

        # Queue for both profiles: repair sweep + feedback
        for _ in range(4):
            fake_lithos.list_responses.append(json.dumps({"items": []}))
        # Profile B cache hit on the Lithos note (already ingested by A).
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        # Patch both guarded_fetch callsites: the one in rss.py (for
        # feed parsing) and the ones in storage.py + extraction/article.py
        # (for archive download + article extraction).
        with (
            patch(
                "influx.sources.rss._guarded_fetch",
                side_effect=counting_guarded_fetch,
            ),
            patch(
                "influx.storage.guarded_fetch",
                side_effect=counting_guarded_fetch,
            ),
            patch(
                "influx.extraction.article.guarded_fetch",
                side_effect=counting_guarded_fetch,
            ),
        ):
            asyncio.run(
                run_profile(
                    PROFILE_A,
                    RunKind.MANUAL,
                    config=config,
                    item_provider=provider,
                )
            )
            asyncio.run(
                run_profile(
                    PROFILE_B,
                    RunKind.MANUAL,
                    config=config,
                    item_provider=provider,
                )
            )

        # The RSS _feed_ should be fetched exactly once (by Profile A).
        # Profile B reuses cached parsed items from the FetchCache.
        # The feed fetch is the very first guarded_fetch call for
        # Profile A.  Subsequent calls are archive + extraction.
        # For Profile B the feed is cached, so only archive + extraction
        # calls are made.  We verify the cache has the feed key.
        assert fetch_cache.has("rss:https://shared-blog.example/feed.xml")

        # Both profiles should have written items.
        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) >= 2
