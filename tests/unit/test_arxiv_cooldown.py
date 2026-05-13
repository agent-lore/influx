"""Unit tests for the adaptive arXiv 429 cooldown (issue #146).

The cooldown is a process-local state machine layered on top of the
in-fetch 429 retry loop:

    NORMAL  ── failure_streak >= threshold ──► COOLDOWN
    COOLDOWN ── cooldown deadline elapsed ──► NORMAL  (lazy clear)
    COOLDOWN ── successful fetch ────────────► NORMAL  (eager clear)

These tests cover the enter/exit transitions, the disable knob, the
process-local helpers, and the wiring through ``_fetch_with_retry`` so
a subsequent fetch is skipped quickly with the dedicated
``cooldown_active`` ``NetworkError.kind``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from influx.config import ResilienceConfig
from influx.errors import NetworkError
from influx.http_client import FetchResult
from influx.sources.arxiv import (
    ArxivCooldownError,
    _arxiv_cooldown_snapshot,
    _fetch_with_retry,
    _record_429_final_failure,
    _record_arxiv_fetch_success,
    _reset_arxiv_cooldown_for_tests,
    _reset_fetch_pacing_for_tests,
    _should_skip_for_cooldown,
)


@pytest.fixture(autouse=True)
def _reset_cooldown_state() -> None:
    """Clear process-local cooldown + pacing state between cases."""
    _reset_arxiv_cooldown_for_tests()
    _reset_fetch_pacing_for_tests()


def _resilience(
    *,
    threshold: int = 3,
    cooldown_seconds: int = 600,
    arxiv_429_max_retries: int = 0,
    max_retries: int = 0,
    arxiv_request_min_interval_seconds: int = 0,
) -> ResilienceConfig:
    """Resilience config with very tight budgets so 429s are *final* fast."""
    return ResilienceConfig(
        max_retries=max_retries,
        arxiv_429_max_retries=arxiv_429_max_retries,
        arxiv_429_cooldown_threshold=threshold,
        arxiv_429_cooldown_seconds=cooldown_seconds,
        arxiv_request_min_interval_seconds=arxiv_request_min_interval_seconds,
    )


def _fetch_result_429() -> FetchResult:
    return FetchResult(
        body=b"Rate exceeded.",
        status_code=429,
        content_type="text/html",
        final_url="https://arxiv.org/api/query",
        headers={"Retry-After": "5"},
    )


def _fetch_result_ok() -> FetchResult:
    return FetchResult(
        body=b"<feed xmlns='http://www.w3.org/2005/Atom'></feed>",
        status_code=200,
        content_type="application/atom+xml",
        final_url="https://arxiv.org/api/query",
        headers={},
    )


class TestRecord429FinalFailure:
    def test_streak_increments_on_each_call(self) -> None:
        res = _resilience(threshold=5)
        streak1, entered1 = _record_429_final_failure(res, classification="local")
        streak2, entered2 = _record_429_final_failure(res, classification="local")

        assert (streak1, entered1) == (1, False)
        assert (streak2, entered2) == (2, False)

    def test_crossing_threshold_engages_cooldown(self) -> None:
        res = _resilience(threshold=2, cooldown_seconds=300)
        _record_429_final_failure(res, classification="upstream_capacity")
        streak, entered = _record_429_final_failure(
            res, classification="upstream_capacity"
        )

        assert (streak, entered) == (2, True)
        snapshot_streak, deadline, classification = _arxiv_cooldown_snapshot()
        assert snapshot_streak == 2
        assert deadline is not None  # deadline set
        assert classification == "upstream_capacity"

    def test_disabled_when_threshold_zero(self) -> None:
        res = _resilience(threshold=0)
        for _ in range(10):
            streak, entered = _record_429_final_failure(res, classification="x")
            assert (streak, entered) == (0, False)

        snapshot_streak, deadline, classification = _arxiv_cooldown_snapshot()
        assert snapshot_streak == 0
        assert deadline is None
        assert classification is None

    def test_zero_cooldown_seconds_disables_engagement(self) -> None:
        """``arxiv_429_cooldown_seconds = 0`` keeps streak ticking but
        never sets a deadline — useful for "report skips but never
        actually pause" probing.
        """
        res = _resilience(threshold=1, cooldown_seconds=0)
        streak, entered = _record_429_final_failure(res, classification="local")

        assert streak == 1
        assert entered is False
        _, deadline, _ = _arxiv_cooldown_snapshot()
        assert deadline is None


class TestShouldSkipForCooldown:
    def test_returns_false_when_no_deadline(self) -> None:
        res = _resilience(threshold=2)
        skip, remaining, classification = _should_skip_for_cooldown(res)

        assert skip is False
        assert remaining is None
        assert classification is None

    def test_returns_true_while_deadline_pending(self) -> None:
        res = _resilience(threshold=1, cooldown_seconds=600)
        _record_429_final_failure(res, classification="rate_limit_local")

        skip, remaining, classification = _should_skip_for_cooldown(res)

        assert skip is True
        assert remaining is not None and remaining > 0
        assert classification == "rate_limit_local"

    def test_disabled_short_circuits_without_state_mutation(self) -> None:
        """When threshold is 0 the helper must not even consult state."""
        res_enabled = _resilience(threshold=1, cooldown_seconds=600)
        _record_429_final_failure(res_enabled, classification="x")
        # State engaged.  Now reading with threshold=0 must not skip
        # and must not mutate the deadline.
        res_disabled = _resilience(threshold=0)
        skip, remaining, _ = _should_skip_for_cooldown(res_disabled)
        assert skip is False
        assert remaining is None
        # The deadline left by the engaged state is still there.
        _, deadline, _ = _arxiv_cooldown_snapshot()
        assert deadline is not None

    def test_elapsed_deadline_clears_state_lazily(self) -> None:
        """When the deadline has elapsed, the next snapshot transitions
        back to NORMAL by clearing both the deadline and the streak.
        """
        res = _resilience(threshold=1, cooldown_seconds=600)
        _record_429_final_failure(res, classification="rate_limit_local")

        # Fast-forward by patching monotonic to return "deadline + 10".
        snapshot_streak, deadline, _ = _arxiv_cooldown_snapshot()
        assert deadline is not None

        with patch("influx.sources.arxiv.time.monotonic", return_value=deadline + 10):
            skip, remaining, _ = _should_skip_for_cooldown(res)
        assert skip is False
        assert remaining is None

        snapshot_streak, snapshot_deadline, _ = _arxiv_cooldown_snapshot()
        assert snapshot_streak == 0
        assert snapshot_deadline is None


class TestRecordArxivFetchSuccess:
    def test_eagerly_clears_active_cooldown(self) -> None:
        res = _resilience(threshold=1, cooldown_seconds=600)
        _record_429_final_failure(res, classification="rate_limit_local")
        _, deadline, _ = _arxiv_cooldown_snapshot()
        assert deadline is not None

        _record_arxiv_fetch_success()

        streak, deadline, _ = _arxiv_cooldown_snapshot()
        assert streak == 0
        assert deadline is None

    def test_resets_streak_below_threshold(self) -> None:
        res = _resilience(threshold=3)
        _record_429_final_failure(res, classification="rate_limit_local")
        _record_429_final_failure(res, classification="rate_limit_local")
        streak, _, _ = _arxiv_cooldown_snapshot()
        assert streak == 2

        _record_arxiv_fetch_success()

        streak, _, _ = _arxiv_cooldown_snapshot()
        assert streak == 0


class TestFetchWithRetryCooldownIntegration:
    def test_skipped_fetch_raises_cooldown_error_without_calling_upstream(self) -> None:
        res = _resilience(threshold=1, cooldown_seconds=600)
        _record_429_final_failure(res, classification="rate_limit_upstream_capacity")

        mock_guarded = MagicMock()
        with (
            patch("influx.sources.arxiv.guarded_fetch", mock_guarded),
            pytest.raises(ArxivCooldownError) as exc_info,
        ):
            _fetch_with_retry(url="https://x", resilience=res)

        assert exc_info.value.kind == "cooldown_active"
        assert exc_info.value.reason is not None
        assert "rate_limit_upstream_capacity" in exc_info.value.reason
        # Critically, upstream was never called.
        mock_guarded.assert_not_called()

    def test_first_429_does_not_skip_when_threshold_not_crossed(self) -> None:
        res = _resilience(threshold=3, cooldown_seconds=600)
        # No prior streak — the call must hit guarded_fetch.
        mock_guarded = MagicMock(return_value=_fetch_result_429())
        with (
            patch("influx.sources.arxiv.guarded_fetch", mock_guarded),
            pytest.raises(NetworkError),
        ):
            _fetch_with_retry(url="https://x", resilience=res)
        # The single 429 final failure bumped the streak to 1 only.
        assert mock_guarded.call_count == 1
        streak, deadline, _ = _arxiv_cooldown_snapshot()
        assert streak == 1
        assert deadline is None  # below threshold, no deadline set

    def test_429_burst_eventually_engages_cooldown(self) -> None:
        """N=threshold final 429 failures engage the cooldown; the next
        request is skipped before reaching the wire.
        """
        res = _resilience(threshold=2, cooldown_seconds=600)
        mock_guarded = MagicMock(return_value=_fetch_result_429())
        with patch("influx.sources.arxiv.guarded_fetch", mock_guarded):
            with pytest.raises(NetworkError):
                _fetch_with_retry(url="https://x", resilience=res)
            with pytest.raises(NetworkError):
                _fetch_with_retry(url="https://x", resilience=res)
        assert mock_guarded.call_count == 2

        # Streak crossed threshold; subsequent request skips.
        mock_guarded.reset_mock()
        with (
            patch("influx.sources.arxiv.guarded_fetch", mock_guarded),
            pytest.raises(ArxivCooldownError),
        ):
            _fetch_with_retry(url="https://x", resilience=res)
        mock_guarded.assert_not_called()

    def test_successful_fetch_clears_cooldown_and_next_call_proceeds(self) -> None:
        """A successful fetch resets the streak; the call after it does
        not skip, even when the prior streak was above threshold.
        """
        res = _resilience(threshold=1, cooldown_seconds=600)
        # Engage cooldown.
        _record_429_final_failure(res, classification="rate_limit_local")
        _, deadline, _ = _arxiv_cooldown_snapshot()
        assert deadline is not None

        # Simulate operator widening the cooldown duration to 0 so we
        # can prove that *successful fetch* clears state, not deadline
        # expiry.  We bypass the skip check by calling success directly
        # (it's the same path ``_fetch_with_retry`` follows after a 2xx).
        _record_arxiv_fetch_success()

        # The next call goes to the wire and returns OK without
        # raising ArxivCooldownError.
        mock_guarded = MagicMock(return_value=_fetch_result_ok())
        with patch("influx.sources.arxiv.guarded_fetch", mock_guarded):
            body = _fetch_with_retry(url="https://x", resilience=res)
        assert b"feed" in body
        mock_guarded.assert_called_once()

    def test_disable_via_threshold_zero_never_skips_fetch(self) -> None:
        """With the disable knob (threshold = 0) the cooldown is a
        no-op even after many final 429 failures — operators can opt
        out wholesale without touching the rest of the retry path.
        """
        res = _resilience(threshold=0, cooldown_seconds=600)
        mock_guarded = MagicMock(return_value=_fetch_result_429())
        with patch("influx.sources.arxiv.guarded_fetch", mock_guarded):
            for _ in range(5):
                with pytest.raises(NetworkError):
                    _fetch_with_retry(url="https://x", resilience=res)
        # All 5 calls hit the wire — no skip.
        assert mock_guarded.call_count == 5

    def test_successful_fetch_from_wire_clears_cooldown(self) -> None:
        """End-to-end through ``_fetch_with_retry``: a 200 response
        clears the cooldown deadline via the success path inside the
        retry loop, not via the test seam.
        """
        res = _resilience(threshold=3, cooldown_seconds=600)
        _record_429_final_failure(res, classification="rate_limit_local")
        _record_429_final_failure(res, classification="rate_limit_local")
        streak, _, _ = _arxiv_cooldown_snapshot()
        assert streak == 2  # below threshold; no deadline

        mock_guarded = MagicMock(return_value=_fetch_result_ok())
        with patch("influx.sources.arxiv.guarded_fetch", mock_guarded):
            _fetch_with_retry(url="https://x", resilience=res)

        streak, deadline, _ = _arxiv_cooldown_snapshot()
        assert streak == 0
        assert deadline is None
