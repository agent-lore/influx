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
from typing import Any
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
    ResilienceConfig,
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


def _make_config(
    lithos_url: str,
    *,
    arxiv_min_interval: int = 0,
) -> AppConfig:
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
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        feedback=FeedbackConfig(negative_examples_per_profile=20),
        # Default to 0 pacing for the existing fast tests; the
        # finding-1 regression tests construct their own config and
        # override this when they assert pacing semantics.
        resilience=ResilienceConfig(
            arxiv_request_min_interval_seconds=arxiv_min_interval,
        ),
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
            arxiv_scorer=_deterministic_scorer(7),
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
            arxiv_scorer=_deterministic_scorer(7),
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
            arxiv_scorer=_deterministic_scorer(7),
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
        cache_calls = [c for c in fake_lithos.calls if c[0] == "lithos_cache_lookup"]
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
            arxiv_scorer=_deterministic_scorer(7),
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
            arxiv_scorer=_deterministic_scorer(7),
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
            arxiv_scorer=_deterministic_scorer(7),
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
            arxiv_scorer=_deterministic_scorer(7),
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
            arxiv_scorer=_deterministic_scorer(7),
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
        task_calls = [c for c in fake_lithos.calls if c[0] == "lithos_task_create"]
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
            arxiv_scorer=_deterministic_scorer(7),
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
        task_calls = [c for c in fake_lithos.calls if c[0] == "lithos_task_create"]
        assert len(task_calls) == 1
        tags = task_calls[0][1]["tags"]
        assert "influx:run" in tags
        assert f"profile:{PROFILE}" in tags
        assert "influx:backfill" not in tags


# ── Finding 1: backfill range threading + pacing ────────────────────


class TestBackfillRangePropagation:
    """Finding 1 regression: backfill range and pacing must reach the fetcher.

    The ``backfill --days N`` flow has to actually request items from the
    historical window and apply ``arxiv_request_min_interval_seconds``
    spacing — otherwise it behaves like an ordinary scheduled run.
    """

    def test_backfill_range_threaded_into_fetch_arxiv(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """``backfill --days 7`` splits into per-day fetches with pacing.

        Review finding 2: a multi-day backfill must split into per-day
        windows so the run actually realizes the
        ``days × len(categories) × max_results_per_category`` contract
        and applies pacing between requests.  The previous single-call
        shape capped a 7-day run at ``max_results_per_category`` items
        regardless of how many days were requested.
        """
        from datetime import timedelta

        from influx.sources.arxiv import BackfillRange

        config = _make_config(lithos_url=fake_lithos_url, arxiv_min_interval=3)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(7),
        )

        # Queue Lithos: feedback (no repair sweep for backfill)
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        all_calls: list[dict[str, Any]] = []

        def capturing_fetch(**kwargs: Any) -> list[ArxivItem]:
            all_calls.append(dict(kwargs))
            return list(_FIXTURE_ARXIV_ITEMS)

        with (
            patch(
                "influx.sources.arxiv.fetch_arxiv",
                side_effect=capturing_fetch,
            ),
            patch("influx.sources.arxiv._sleep") as mock_sleep,
        ):
            asyncio.run(
                run_backfill(
                    PROFILE,
                    run_range={"days": 7},
                    config=config,
                    item_provider=provider,
                )
            )

        # One fetch per day in the backfill window (review finding 2).
        assert len(all_calls) == 7

        # Each call MUST carry a 1-day BackfillRange.
        per_day_ranges: list[BackfillRange] = []
        for call_kwargs in all_calls:
            r = call_kwargs.get("backfill_range")
            assert isinstance(r, BackfillRange)
            assert r.days == 1
            per_day_ranges.append(r)

        # The 7 ranges together cover the requested 7-day window with
        # no gaps and no overlap.
        per_day_ranges.sort(key=lambda r: r.date_from)
        for i in range(1, len(per_day_ranges)):
            assert per_day_ranges[i].date_from == (
                per_day_ranges[i - 1].date_from + timedelta(days=1)
            )
        full_span = per_day_ranges[-1].date_to - per_day_ranges[0].date_from
        assert full_span == timedelta(days=7)

        # Each call also carries a widened ``max_results_per_category`` so
        # the per-day OR-joined query can return up to the full estimator
        # budget for that day (Q-3 / FR-BF-6).
        n_categories = 1  # cs.AI only in the test config
        for call_kwargs in all_calls:
            arxiv_cfg = call_kwargs.get("arxiv_config")
            assert arxiv_cfg is not None
            assert arxiv_cfg.max_results_per_category == 10 * n_categories

        # Pacing sleep called with arxiv_request_min_interval_seconds
        # at least once per day-fetch (FR-BF-3).
        assert mock_sleep.call_count >= 7
        for sleep_call in mock_sleep.call_args_list:
            assert sleep_call[0][0] == float(
                config.resilience.arxiv_request_min_interval_seconds
            )

    def test_backfill_from_to_threaded_into_fetch_arxiv(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """The ``--from``/``--to`` form propagates per-day bounds to fetch_arxiv."""
        from datetime import date, timedelta

        from influx.sources.arxiv import BackfillRange

        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(7),
        )

        fake_lithos.list_responses.append(json.dumps({"items": []}))

        all_calls: list[dict[str, Any]] = []

        def capturing_fetch(**kwargs: Any) -> list[ArxivItem]:
            all_calls.append(dict(kwargs))
            return list(_FIXTURE_ARXIV_ITEMS)

        with (
            patch(
                "influx.sources.arxiv.fetch_arxiv",
                side_effect=capturing_fetch,
            ),
            patch("influx.sources.arxiv._sleep"),
        ):
            asyncio.run(
                run_backfill(
                    PROFILE,
                    run_range={"from": "2026-04-20", "to": "2026-04-27"},
                    config=config,
                    item_provider=provider,
                )
            )

        # 7 per-day calls covering [2026-04-20, 2026-04-27).
        assert len(all_calls) == 7
        starts = sorted(
            call_kwargs["backfill_range"].date_from for call_kwargs in all_calls
        )
        assert starts[0] == date(2026, 4, 20)
        assert starts[-1] == date(2026, 4, 26)
        for i in range(1, len(starts)):
            assert starts[i] == starts[i - 1] + timedelta(days=1)
        for call_kwargs in all_calls:
            r = call_kwargs["backfill_range"]
            assert isinstance(r, BackfillRange)
            assert r.days == 1

    def test_nonbackfill_does_not_pass_range_or_pace(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Scheduled / manual runs must NOT set ``backfill_range`` and
        must NOT incur the per-fetch pacing sleep that backfill needs.
        """
        config = _make_config(lithos_url=fake_lithos_url)
        fetch_cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=fetch_cache,
            arxiv_scorer=_deterministic_scorer(7),
        )

        # Queue Lithos: repair sweep + feedback for the manual run.
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        captured_kwargs: dict[str, Any] = {}

        def capturing_fetch(**kwargs: Any) -> list[ArxivItem]:
            captured_kwargs.update(kwargs)
            return list(_FIXTURE_ARXIV_ITEMS)

        with (
            patch(
                "influx.sources.arxiv.fetch_arxiv",
                side_effect=capturing_fetch,
            ),
            patch("influx.sources.arxiv._sleep") as mock_sleep,
        ):
            asyncio.run(
                run_profile(
                    PROFILE,
                    RunKind.MANUAL,
                    config=config,
                    item_provider=provider,
                )
            )

        # Manual run: no backfill_range, no pacing sleep.
        assert captured_kwargs.get("backfill_range") is None
        mock_sleep.assert_not_called()


# ── Finding 3: real /backfills endpoint integration ─────────────────


def _wait_for_idle(
    coordinator: Coordinator,
    profile: str,
    timeout: float = 10.0,
) -> None:
    """Poll until *profile* is no longer held by the coordinator."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not coordinator.is_busy(profile):
            return
        time.sleep(0.05)
    raise TimeoutError(f"Profile {profile!r} still busy after {timeout}s")


class TestBackfillEndpointEndToEnd:
    """Review finding 3: drive ``POST /backfills`` through the real app.

    The earlier tests in this module call ``run_backfill`` directly and
    mock ``post_run_webhook_hook`` / ``repair_sweep``.  Those tests
    cannot prove that ``kind="backfill"`` propagates from the HTTP layer
    down to task tagging, that the webhook gate prevents a real outbound
    HTTP call, or that the repair-sweep gate prevents a real
    ``lithos_list(tags=["influx:repair-needed", ...])`` call.  This
    class drives ``POST /backfills`` on the real
    :func:`influx.service.create_app` factory with a fake arXiv fixture
    and asserts the post-conditions on the *real* side effects (Lithos
    calls + webhook sender).
    """

    def test_backfills_endpoint_drives_kind_backfill_end_to_end(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """``POST /backfills`` propagates kind=backfill through the full path.

        Asserts the four end-to-end post-conditions for finding 3:

        - AC-09-F: ``lithos_task_create`` carries ``influx:backfill`` tag.
        - AC-09-G: no webhook HTTP call was emitted (the real
          ``send_digest`` path is not reached).
        - AC-09-H: no ``lithos_list(tags=["influx:repair-needed", ...])``
          call was recorded against the real Lithos fake.
        - AC-M3-7 sibling: the coordinator releases the profile lock
          when the request completes.
        """
        from fastapi.testclient import TestClient

        from influx.service import create_app

        # Use a *real-looking* webhook URL so any leakage would actually
        # try to POST and be observable through the patched sender.
        config = _make_config(lithos_url=fake_lithos_url)
        config.notifications.webhook_url = "https://example.invalid/hook"

        # Pre-queue feedback list response.  No repair-sweep response is
        # queued — if the gate fails and the sweep runs, the next
        # ``lithos_list`` call would still receive the fake's default
        # ``{"items": []}`` and we would see the call recorded with
        # ``influx:repair-needed`` in the tags, which is what we assert
        # against below.
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        app = create_app(
            config,
            arxiv_scorer=_deterministic_scorer(7),
        )

        with (
            patch(
                "influx.sources.arxiv.fetch_arxiv",
                return_value=list(_FIXTURE_ARXIV_ITEMS),
            ),
            patch("influx.sources.arxiv._sleep"),
            # ``send_digest`` lazily imports ``guarded_post_json`` from
            # ``influx.http_client``, so the patch must target the
            # source module rather than ``influx.notifications``.
            patch(
                "influx.http_client.guarded_post_json",
                return_value=200,
            ) as mock_webhook_post,
            TestClient(app) as client,
        ):
            resp = client.post(
                "/backfills",
                json={"profile": PROFILE, "days": 1, "confirm": True},
            )
            assert resp.status_code == 202, resp.text
            body = resp.json()
            assert body["kind"] == "backfill"
            assert body["scope"] == PROFILE

            # Wait for the spawned background backfill to finish.
            _wait_for_idle(app.state.coordinator, PROFILE, timeout=15.0)

        # ── AC-09-F: task tagged ``influx:backfill`` ──────────────────
        task_calls = [c for c in fake_lithos.calls if c[0] == "lithos_task_create"]
        assert len(task_calls) == 1, fake_lithos.calls
        task_tags = task_calls[0][1]["tags"]
        assert "influx:backfill" in task_tags
        assert "influx:run" not in task_tags

        # ── AC-09-G: no webhook HTTP call ─────────────────────────────
        # ``send_digest`` short-circuits for ``kind=BACKFILL`` so the
        # real ``guarded_post_json`` sender must never be invoked.
        mock_webhook_post.assert_not_called()

        # ── AC-09-H: no repair-sweep ``lithos_list`` call ─────────────
        repair_list_calls = [
            c
            for c in fake_lithos.calls
            if c[0] == "lithos_list"
            and "influx:repair-needed" in (c[1].get("tags") or [])
        ]
        assert repair_list_calls == [], (
            "Repair sweep must not be invoked during backfill (FR-REP-2 / AC-09-H); "
            f"recorded calls: {fake_lithos.calls}"
        )

    def test_backfills_endpoint_does_not_overlap_concurrent_run(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """AC-M3-7: a concurrently scheduled run blocks ``POST /backfills``.

        The coordinator owns same-profile serialisation.  This test
        holds the profile lock as if a scheduled run were in flight,
        then exercises the real endpoint and verifies the production
        path returns 409 with ``reason="profile_busy"`` rather than
        racing into a second ingest cycle.
        """
        from fastapi.testclient import TestClient

        from influx.service import create_app

        config = _make_config(lithos_url=fake_lithos_url)
        app = create_app(
            config,
            arxiv_scorer=_deterministic_scorer(7),
        )

        # Hold the profile lock to simulate a concurrent scheduled run.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(app.state.coordinator.try_acquire(PROFILE))
            try:
                with TestClient(app) as client:
                    resp = client.post(
                        "/backfills",
                        json={"profile": PROFILE, "days": 1, "confirm": True},
                    )
                assert resp.status_code == 409
                assert resp.json()["reason"] == "profile_busy"
            finally:
                app.state.coordinator.release(PROFILE)
        finally:
            loop.close()

    def test_nonbackfill_endpoint_creates_influx_run_task(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """FR-BF-5 regression: ``POST /runs`` still creates ``influx:run`` tasks.

        Drives the symmetric non-backfill path through the real
        :func:`create_app` factory so the run-kind switch in
        ``run_profile`` is verified end-to-end (i.e. backfill does NOT
        leak the new ``influx:backfill`` tag onto scheduled / manual
        runs).
        """
        from fastapi.testclient import TestClient

        from influx.service import create_app

        config = _make_config(lithos_url=fake_lithos_url)
        # Repair-sweep + feedback list responses for the manual run.
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        app = create_app(
            config,
            arxiv_scorer=_deterministic_scorer(7),
        )

        with (
            patch(
                "influx.sources.arxiv.fetch_arxiv",
                return_value=list(_FIXTURE_ARXIV_ITEMS),
            ),
            TestClient(app) as client,
        ):
            resp = client.post("/runs", json={"profile": PROFILE})
            assert resp.status_code == 202, resp.text
            _wait_for_idle(app.state.coordinator, PROFILE, timeout=15.0)

        task_calls = [c for c in fake_lithos.calls if c[0] == "lithos_task_create"]
        assert len(task_calls) == 1
        task_tags = task_calls[0][1]["tags"]
        assert "influx:run" in task_tags
        assert "influx:backfill" not in task_tags
