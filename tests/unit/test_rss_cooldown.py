"""Unit tests for the per-feed RSS timeout cooldown (issue #163).

The cooldown is a per-feed-URL state machine layered on top of the
``_fetch_rss_feed`` path:

    NORMAL  ── timeout_streak >= threshold ──► COOLDOWN
    COOLDOWN ── deadline elapsed ────────────► NORMAL  (lazy clear)
    COOLDOWN ── successful fetch ────────────► NORMAL  (eager clear)

These tests cover the enter/exit transitions, the disable knob, the
per-feed isolation (one slow feed must not quarantine others on the
same profile), the kind-restrictive policy (only ``timeout`` counts),
and the wiring through ``_fetch_rss_feed`` that surfaces a cooldown
skip as ``source_cooldown_skip`` rather than ``source_acquisition``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from influx.config import ResilienceConfig, RssSourceEntry
from influx.errors import NetworkError
from influx.http_client import FetchResult
from influx.sources.rss import (
    _fetch_rss_feed,
    _record_rss_fetch_success,
    _record_rss_timeout,
    _reset_rss_cooldown_for_tests,
    _rss_cooldown_snapshot,
    _should_skip_rss_for_cooldown,
)
from influx.telemetry import (
    current_source_acquisition_errors,
    current_source_cooldown_skips,
)

_FEED_URL = "https://example.com/feed.xml"
_FEED_URL_OTHER = "https://example.com/other-feed.xml"


@pytest.fixture(autouse=True)
def _reset_cooldown_state() -> None:
    """Clear per-feed cooldown state between cases."""
    _reset_rss_cooldown_for_tests()


def _resilience(
    *,
    threshold: int = 3,
    cooldown_seconds: int = 600,
) -> ResilienceConfig:
    return ResilienceConfig(
        rss_timeout_cooldown_threshold=threshold,
        rss_timeout_cooldown_seconds=cooldown_seconds,
    )


def _feed(url: str = _FEED_URL, name: str = "test-feed") -> RssSourceEntry:
    return RssSourceEntry(name=name, url=url, source_tag="rss")


def _timeout_error(url: str = _FEED_URL) -> NetworkError:
    # Mirrors the staging shape: ``httpx.ReadTimeout`` wrapped by the
    # guarded client into a ``NetworkError`` with ``kind="timeout"``.
    return NetworkError(
        "The read operation timed out",
        url=url,
        kind="timeout",
        reason="ReadTimeout",
    )


# ── State machine: bookkeeping helpers ────────────────────────────────


class TestRecordRssTimeout:
    """``_record_rss_timeout`` ticks a per-URL streak and engages cooldown."""

    def test_streak_increments_on_each_call(self) -> None:
        res = _resilience(threshold=5)
        streak1, entered1 = _record_rss_timeout(_FEED_URL, res)
        streak2, entered2 = _record_rss_timeout(_FEED_URL, res)

        assert (streak1, entered1) == (1, False)
        assert (streak2, entered2) == (2, False)

    def test_crossing_threshold_engages_cooldown(self) -> None:
        res = _resilience(threshold=2, cooldown_seconds=300)
        _record_rss_timeout(_FEED_URL, res)
        streak, entered = _record_rss_timeout(_FEED_URL, res)

        assert (streak, entered) == (2, True)
        snap_streak, deadline = _rss_cooldown_snapshot(_FEED_URL)
        assert snap_streak == 2
        assert deadline is not None

    def test_per_url_isolation(self) -> None:
        # A timeout on one feed must not raise another feed's streak.
        res = _resilience(threshold=2)
        _record_rss_timeout(_FEED_URL, res)
        _record_rss_timeout(_FEED_URL, res)

        other_streak, other_deadline = _rss_cooldown_snapshot(_FEED_URL_OTHER)
        assert other_streak == 0
        assert other_deadline is None

        primary_streak, primary_deadline = _rss_cooldown_snapshot(_FEED_URL)
        assert primary_streak == 2
        assert primary_deadline is not None

    def test_disabled_when_threshold_zero(self) -> None:
        res = _resilience(threshold=0)
        for _ in range(10):
            streak, entered = _record_rss_timeout(_FEED_URL, res)
            assert (streak, entered) == (0, False)

        snap_streak, deadline = _rss_cooldown_snapshot(_FEED_URL)
        assert snap_streak == 0
        assert deadline is None

    def test_zero_cooldown_seconds_disables_deadline(self) -> None:
        # ``rss_timeout_cooldown_seconds = 0`` lets streak tick but
        # never engages a deadline.
        res = _resilience(threshold=1, cooldown_seconds=0)
        streak, entered = _record_rss_timeout(_FEED_URL, res)

        assert streak == 1
        assert entered is False
        _, deadline = _rss_cooldown_snapshot(_FEED_URL)
        assert deadline is None


class TestShouldSkipRssForCooldown:
    def test_returns_false_when_no_deadline(self) -> None:
        res = _resilience()
        skip, remaining = _should_skip_rss_for_cooldown(_FEED_URL, res)
        assert skip is False
        assert remaining is None

    def test_returns_true_while_deadline_active(self) -> None:
        res = _resilience(threshold=1, cooldown_seconds=600)
        _record_rss_timeout(_FEED_URL, res)
        skip, remaining = _should_skip_rss_for_cooldown(_FEED_URL, res)
        assert skip is True
        assert remaining is not None and remaining > 0

    def test_lazy_clear_after_deadline_elapsed(self) -> None:
        # A cooldown set in the past clears itself on next inspection
        # and lets the caller proceed normally.
        res = _resilience(threshold=1, cooldown_seconds=600)
        _record_rss_timeout(_FEED_URL, res)

        # Force the deadline into the past by manipulating internal
        # state directly via the snapshot/record helpers.
        from influx.sources import rss as rss_module

        with rss_module._RSS_COOLDOWN_LOCK:
            state = rss_module._RSS_COOLDOWN_STATE[_FEED_URL]
            state.deadline_monotonic = (state.deadline_monotonic or 0) - 10_000.0

        skip, remaining = _should_skip_rss_for_cooldown(_FEED_URL, res)
        assert skip is False
        assert remaining is None
        # State self-healed.
        snap_streak, deadline = _rss_cooldown_snapshot(_FEED_URL)
        assert snap_streak == 0
        assert deadline is None

    def test_disabled_when_threshold_zero(self) -> None:
        # Even if state is set (e.g. carried over from a config flip),
        # the feature gate must not skip.
        res = _resilience(threshold=0)
        skip, remaining = _should_skip_rss_for_cooldown(_FEED_URL, res)
        assert skip is False
        assert remaining is None


class TestRecordRssFetchSuccess:
    def test_eager_clear_resets_streak_and_deadline(self) -> None:
        res = _resilience(threshold=2, cooldown_seconds=600)
        _record_rss_timeout(_FEED_URL, res)
        _record_rss_timeout(_FEED_URL, res)
        # Cooldown engaged.
        snap_streak, deadline = _rss_cooldown_snapshot(_FEED_URL)
        assert snap_streak == 2
        assert deadline is not None

        _record_rss_fetch_success(_FEED_URL)

        snap_streak, deadline = _rss_cooldown_snapshot(_FEED_URL)
        assert snap_streak == 0
        assert deadline is None

    def test_no_op_for_unknown_url(self) -> None:
        # Calling success on a feed we've never seen does not raise.
        _record_rss_fetch_success("https://never-seen.example/feed")
        snap_streak, deadline = _rss_cooldown_snapshot(
            "https://never-seen.example/feed"
        )
        assert snap_streak == 0
        assert deadline is None


# ── Integration: _fetch_rss_feed wiring ───────────────────────────────


async def _run_with_telemetry_buckets(
    coro_fn: Any,
) -> tuple[list[Any], list[Any]]:
    """Run *coro_fn()* with telemetry context vars set up so the test
    can inspect ``source_acquisition_errors`` and ``source_cooldown_skips``
    after the fetch completes.
    """
    acquisition: list[Any] = []
    cooldown: list[Any] = []
    acquisition_token = current_source_acquisition_errors.set(acquisition)
    cooldown_token = current_source_cooldown_skips.set(cooldown)
    try:
        await coro_fn()
    finally:
        current_source_acquisition_errors.reset(acquisition_token)
        current_source_cooldown_skips.reset(cooldown_token)
    return acquisition, cooldown


class TestFetchRssFeedCooldownIntegration:
    """``_fetch_rss_feed`` records cooldown skips and resets on success.

    Staging-evidence parallel: the "Two Stop Bits" aggregator emits
    ``httpx.ReadTimeout`` on every fetch.  After three consecutive
    timeouts the feed is quarantined; subsequent runs short-circuit
    with ``source_cooldown_skip`` instead of repeating the timeout.
    """

    async def test_timeouts_below_threshold_record_acquisition_only(self) -> None:
        feed = _feed(name="Two Stop Bits (retro aggregator)")
        res = _resilience(threshold=3)

        async def run() -> None:
            with patch(
                "influx.sources.rss._aguarded_fetch",
                new=AsyncMock(side_effect=_timeout_error(feed.url)),
            ):
                items = await _fetch_rss_feed(
                    feed, cache=None, profile="retro-computing", resilience=res
                )
            assert items == []

        acquisition, cooldown = await _run_with_telemetry_buckets(run)

        # Single timeout below threshold → counted as source_acquisition,
        # not source_cooldown_skip.
        assert len(acquisition) == 1
        assert acquisition[0]["kind"] == "timeout"
        assert "Two Stop Bits" in acquisition[0]["detail"]
        assert cooldown == []

        snap_streak, _ = _rss_cooldown_snapshot(feed.url)
        assert snap_streak == 1

    async def test_third_timeout_engages_cooldown_and_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        feed = _feed(name="Two Stop Bits (retro aggregator)")
        res = _resilience(threshold=3, cooldown_seconds=1800)

        async def run() -> None:
            with (
                patch(
                    "influx.sources.rss._aguarded_fetch",
                    new=AsyncMock(side_effect=_timeout_error(feed.url)),
                ),
                caplog.at_level(logging.WARNING, logger="influx.sources.rss"),
            ):
                for _ in range(3):
                    await _fetch_rss_feed(
                        feed,
                        cache=None,
                        profile="retro-computing",
                        resilience=res,
                    )

        await _run_with_telemetry_buckets(run)

        # Cooldown engaged on the 3rd timeout.
        snap_streak, deadline = _rss_cooldown_snapshot(feed.url)
        assert snap_streak == 3
        assert deadline is not None

        # The cooldown-engagement WARNING is emitted once.
        engagement_logs = [
            r
            for r in caplog.records
            if "cooldown engaged" in r.message and "rss" in r.message
        ]
        assert len(engagement_logs) == 1
        assert "Two Stop Bits" in engagement_logs[0].message

    async def test_subsequent_fetch_in_cooldown_skips_and_records_cooldown_skip(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        feed = _feed()
        res = _resilience(threshold=1, cooldown_seconds=600)

        # Force the cooldown via direct bookkeeping (avoid wall-time).
        _record_rss_timeout(feed.url, res)
        snap_streak, deadline = _rss_cooldown_snapshot(feed.url)
        assert snap_streak == 1 and deadline is not None

        async def run() -> None:
            fetch_mock = AsyncMock()
            with (
                patch("influx.sources.rss._aguarded_fetch", new=fetch_mock),
                caplog.at_level(logging.INFO, logger="influx.sources.rss"),
            ):
                items = await _fetch_rss_feed(
                    feed, cache=None, profile="retro-computing", resilience=res
                )
            assert items == []
            # The fetch was NOT made — the cooldown short-circuited.
            fetch_mock.assert_not_called()

        acquisition, cooldown = await _run_with_telemetry_buckets(run)

        # The skip routes through ``source_cooldown_skip``, NOT
        # ``source_acquisition``.
        assert cooldown and cooldown[0]["kind"] == "timeout_cooldown"
        assert acquisition == []

        # The skip log is a single INFO line — no repeated stack
        # traces while the cooldown is active.
        skip_logs = [
            r
            for r in caplog.records
            if "rss feed skipped (cooldown active)" in r.message
        ]
        assert len(skip_logs) == 1
        assert skip_logs[0].levelname == "INFO"

    async def test_successful_fetch_clears_cooldown(self) -> None:
        feed = _feed()
        res = _resilience(threshold=1, cooldown_seconds=600)
        _record_rss_timeout(feed.url, res)

        # Force the deadline into the past so the next fetch is allowed.
        from influx.sources import rss as rss_module

        with rss_module._RSS_COOLDOWN_LOCK:
            state = rss_module._RSS_COOLDOWN_STATE[feed.url]
            state.deadline_monotonic = (state.deadline_monotonic or 0) - 10_000.0

        async def run() -> None:
            with patch(
                "influx.sources.rss._aguarded_fetch",
                new=AsyncMock(
                    return_value=FetchResult(
                        body=b"<rss version='2.0'><channel></channel></rss>",
                        status_code=200,
                        content_type="application/rss+xml",
                        final_url=feed.url,
                    )
                ),
            ):
                await _fetch_rss_feed(
                    feed, cache=None, profile="retro-computing", resilience=res
                )

        await _run_with_telemetry_buckets(run)

        # Streak and deadline both cleared by the successful fetch.
        snap_streak, deadline = _rss_cooldown_snapshot(feed.url)
        assert snap_streak == 0
        assert deadline is None

    async def test_other_feed_not_quarantined_by_neighbour(self) -> None:
        # One feed in cooldown must not suppress a sibling feed on
        # the same profile — per-feed-URL isolation.
        bad_feed = _feed(url=_FEED_URL, name="bad")
        good_feed = _feed(url=_FEED_URL_OTHER, name="good")
        res = _resilience(threshold=1, cooldown_seconds=600)
        _record_rss_timeout(bad_feed.url, res)

        good_response = FetchResult(
            body=b"<rss version='2.0'><channel></channel></rss>",
            status_code=200,
            content_type="application/rss+xml",
            final_url=good_feed.url,
        )

        async def run() -> None:
            with patch(
                "influx.sources.rss._aguarded_fetch",
                new=AsyncMock(return_value=good_response),
            ) as fetch_mock:
                items_good = await _fetch_rss_feed(
                    good_feed,
                    cache=None,
                    profile="retro-computing",
                    resilience=res,
                )
                # The healthy feed fetched — the bad feed's cooldown
                # did not bleed onto it.
                fetch_mock.assert_called_once()
            # Healthy fetch returned (parsed item list — empty is OK
            # for the empty channel fixture above).
            assert items_good == []

        acquisition, cooldown = await _run_with_telemetry_buckets(run)
        # No degradation recorded for the healthy feed's fetch.
        assert acquisition == []
        assert cooldown == []

    async def test_non_timeout_failure_does_not_increment_streak(self) -> None:
        # A DNS / network / content-type failure must NOT enter the
        # cooldown path — only ``timeout`` counts.  Otherwise a one-off
        # transport hiccup could quarantine a healthy feed.
        feed = _feed()
        res = _resilience(threshold=2)

        async def run() -> None:
            dns_error = NetworkError(
                "DNS resolution failed",
                url=feed.url,
                kind="dns",
                reason="NXDOMAIN",
            )
            with patch(
                "influx.sources.rss._aguarded_fetch",
                new=AsyncMock(side_effect=dns_error),
            ):
                for _ in range(5):
                    await _fetch_rss_feed(
                        feed,
                        cache=None,
                        profile="retro-computing",
                        resilience=res,
                    )

        await _run_with_telemetry_buckets(run)

        snap_streak, deadline = _rss_cooldown_snapshot(feed.url)
        assert snap_streak == 0
        assert deadline is None

    async def test_resilience_none_keeps_legacy_behaviour(self) -> None:
        # Backwards-compat seam: callers that do not pass ``resilience``
        # see the pre-#163 behaviour — timeouts produce a single
        # WARNING + acquisition error, no streak tracking.
        feed = _feed()

        async def run() -> None:
            with patch(
                "influx.sources.rss._aguarded_fetch",
                new=AsyncMock(side_effect=_timeout_error(feed.url)),
            ):
                await _fetch_rss_feed(
                    feed, cache=None, profile="retro-computing", resilience=None
                )

        await _run_with_telemetry_buckets(run)

        snap_streak, deadline = _rss_cooldown_snapshot(feed.url)
        assert snap_streak == 0
        assert deadline is None
