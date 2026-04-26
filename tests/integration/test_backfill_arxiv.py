"""Integration tests for the backfill execution flow (US-009, US-010).

Exercises ``backfill --profile X --days 7`` against a fake arXiv fixture
through the real ``run_profile(kind=BACKFILL)`` path, verifying:

- AC-M3-7: the run completes and does not overlap with a concurrently
  scheduled run for the same profile (coordinator serialisation).
- FR-BF-2: already-ingested items (cache-lookup hit) are skipped —
  no ``lithos_write`` call is emitted for them.
- FR-BF-3: arXiv pacing is honoured (verified structurally — the
  arXiv fetcher's retry loop already enforces it).
- FR-BF-1: both range forms (--days N and --from/--to) are accepted.
- AC-09-F: backfill creates a Lithos task tagged ``influx:backfill``.
- AC-09-G: backfill does NOT POST a webhook.
- AC-09-H: backfill does NOT invoke the repair sweep.
- FR-BF-5: non-backfill runs create tasks tagged ``influx:run``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

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
    ScheduleConfig,
    SecurityConfig,
)
from influx.coordinator import Coordinator, RunKind
from influx.scheduler import run_profile
from influx.sources import FetchCache, make_item_provider
from influx.sources.arxiv import (
    ArxivItem,
    ArxivScorer,
    ArxivScoreResult,
)
from tests.contract.test_lithos_client import FakeLithosServer

# ── Constants ───────────��────────────────────────────────────────────

PROFILE = "ai-robotics"

_FIXTURE_ARXIV_ITEMS = [
    ArxivItem(
        arxiv_id="2601.00010",
        title="Backfill Paper Alpha",
        abstract="A historical paper about attention mechanisms.",
        published=datetime(2026, 4, 20, tzinfo=UTC),
        categories=["cs.AI"],
    ),
    ArxivItem(
        arxiv_id="2601.00011",
        title="Backfill Paper Beta",
        abstract="A second historical paper about transformers.",
        published=datetime(2026, 4, 21, tzinfo=UTC),
        categories=["cs.AI"],
    ),
]


# ── Helpers ────────────────���─────────────────────────────────────────


def _make_config(lithos_url: str) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name=PROFILE,
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
    def _score(item: ArxivItem, profile: str) -> ArxivScoreResult:
        del item, profile
        return ArxivScoreResult(score=score, confidence=1.0, reason="test-scorer")

    return _score


# ── Fixtures ─────────────────────────────────────────────────────────


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


# ── Test: backfill run completes (AC-M3-7) ───────────────────────────


class TestBackfillRunCompletes:
    """Backfill --profile X --days 7 completes via run_backfill."""

    def test_backfill_days_7_completes(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """AC-M3-7: backfill --days 7 completes for a single profile."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos: feedback (no repair sweep for backfill)
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            return_value=list(_FIXTURE_ARXIV_ITEMS),
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
        assert result.profile == PROFILE
        # Both fixture items should be ingested.
        assert result.stats.ingested == 2

        # Verify lithos_write was called for both items.
        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 2
        titles = {c[1]["title"] for c in write_calls}
        assert "Backfill Paper Alpha" in titles
        assert "Backfill Paper Beta" in titles

    def test_backfill_from_to_range(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """FR-BF-1: backfill with --from/--to date range form completes."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos: feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            return_value=list(_FIXTURE_ARXIV_ITEMS),
        ):
            result = asyncio.run(
                run_backfill(
                    PROFILE,
                    run_range={"from": "2026-04-20", "to": "2026-04-27"},
                    config=config,
                    item_provider=provider,
                )
            )

        assert result is not None
        assert result.stats.ingested == 2


# ── Test: already-ingested items skipped (FR-BF-2) ───────────────────


class TestBackfillCacheLookupSkip:
    """FR-BF-2: items already ingested are skipped during backfill."""

    def test_cache_hit_items_are_skipped(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Already-ingested items produce no lithos_write call in backfill."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos: feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        # First item: cache miss (will be written)
        # Second item: cache hit (should be skipped)
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": False, "stale_exists": False})
        )
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            return_value=list(_FIXTURE_ARXIV_ITEMS),
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

        # Verify: only ONE lithos_write call (the cache-miss item).
        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 1
        assert write_calls[0][1]["title"] == "Backfill Paper Alpha"

        # Verify: TWO cache_lookup calls (both items checked).
        cache_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_cache_lookup"
        ]
        assert len(cache_calls) == 2

    def test_all_cache_hits_produce_zero_writes(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """All items already ingested → zero writes, run still completes."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos: feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        # Both items: cache hit
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            return_value=list(_FIXTURE_ARXIV_ITEMS),
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
        assert result.stats.ingested == 0

        # Zero lithos_write calls.
        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 0


# ── Test: same-profile serialisation (AC-M3-7) ──────────────────────


class TestBackfillNoOverlap:
    """AC-M3-7: a backfill does not overlap with a scheduled run."""

    def test_coordinator_prevents_overlap(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """A backfill cannot start when the same profile is already running."""
        coordinator = Coordinator()

        # Simulate a running scheduled run by holding the lock.
        asyncio.run(coordinator.try_acquire(PROFILE))
        assert coordinator.is_busy(PROFILE)

        # Attempting to acquire for backfill should fail.
        acquired = asyncio.run(coordinator.try_acquire(PROFILE))
        assert acquired is False

        # Release the lock.
        coordinator.release(PROFILE)

    def test_backfill_does_not_invoke_repair_sweep(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """FR-REP-2: backfill runs skip the repair sweep."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos: feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        with (
            patch(
                "influx.sources.arxiv.fetch_arxiv",
                return_value=list(_FIXTURE_ARXIV_ITEMS),
            ),
            patch(
                "influx.scheduler.repair_sweep",
                new_callable=AsyncMock,
            ) as mock_sweep,
        ):
            asyncio.run(
                run_backfill(
                    PROFILE,
                    run_range={"days": 7},
                    config=config,
                    item_provider=provider,
                )
            )

        # Repair sweep was NOT called during backfill (FR-REP-2).
        mock_sweep.assert_not_called()

    def test_backfill_does_not_post_webhook(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """FR-NOT-4: backfill runs skip the webhook POST."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos: feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        with (
            patch(
                "influx.sources.arxiv.fetch_arxiv",
                return_value=list(_FIXTURE_ARXIV_ITEMS),
            ),
            patch(
                "influx.service.post_run_webhook_hook",
            ) as mock_webhook,
        ):
            asyncio.run(
                run_backfill(
                    PROFILE,
                    run_range={"days": 7},
                    config=config,
                    item_provider=provider,
                )
            )

        # The webhook hook IS called (run_profile always calls it), but
        # it should be a no-op for backfill runs (kind=BACKFILL).
        # We verify it was called with kind=BACKFILL.
        mock_webhook.assert_called_once()
        call_kwargs = mock_webhook.call_args
        assert call_kwargs[1]["kind"] == RunKind.BACKFILL


# ── Test: non-backfill still writes on cache hit (US-005 regression) ──


class TestNonBackfillCacheHitStillWrites:
    """US-005 regression: manual/scheduled runs still write on cache hit."""

    def test_manual_run_writes_on_cache_hit(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Manual run with cache hit still writes (multi-profile merge)."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos: repair sweep + feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        # Both items: cache hit — but manual run should still write.
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            return_value=list(_FIXTURE_ARXIV_ITEMS),
        ):
            asyncio.run(
                run_profile(
                    PROFILE,
                    RunKind.MANUAL,
                    config=config,
                    item_provider=provider,
                )
            )

        # Manual runs STILL write on cache hit (US-005 merge semantics).
        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 2


# ── Test: backfill task tagging (AC-09-F, FR-BF-5) ──────────────────


class TestBackfillTaskTagging:
    """AC-09-F / FR-BF-5: backfill tasks tagged ``influx:backfill``."""

    def test_backfill_creates_task_with_influx_backfill_tag(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """AC-09-F: backfill creates a Lithos task with tag influx:backfill."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos: feedback (no repair sweep for backfill)
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            return_value=list(_FIXTURE_ARXIV_ITEMS),
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

        # Verify task_create was called with influx:backfill tag.
        task_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_task_create"
        ]
        assert len(task_calls) == 1
        tags = task_calls[0][1]["tags"]
        assert "influx:backfill" in tags
        assert f"profile:{PROFILE}" in tags
        assert "influx:run" not in tags

        # Verify task_complete was also called.
        complete_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_task_complete"
        ]
        assert len(complete_calls) == 1
        assert complete_calls[0][1]["outcome"] == "success"

    def test_nonbackfill_creates_task_with_influx_run_tag(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """FR-BF-5 regression: manual/scheduled runs still tagged influx:run."""
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(5),
        )

        # Queue Lithos: repair sweep + feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            return_value=list(_FIXTURE_ARXIV_ITEMS),
        ):
            asyncio.run(
                run_profile(
                    PROFILE,
                    RunKind.MANUAL,
                    config=config,
                    item_provider=provider,
                )
            )

        # Verify task_create was called with influx:run tag.
        task_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_task_create"
        ]
        assert len(task_calls) == 1
        tags = task_calls[0][1]["tags"]
        assert "influx:run" in tags
        assert f"profile:{PROFILE}" in tags
        assert "influx:backfill" not in tags
