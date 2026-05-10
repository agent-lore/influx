"""Tests for arXiv Atom fetcher (US-010, FR-SRC-1, FR-SRC-2, FR-RES-1/2)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from influx.config import ArxivSourceConfig, ResilienceConfig
from influx.errors import NetworkError
from influx.http_client import FetchResult
from influx.sources.arxiv import (
    ArxivItem,
    BackfillRange,
    _apply_min_interval,
    _extract_arxiv_id,
    _filter_by_lookback,
    _parse_atom,
    _reset_fetch_pacing_for_tests,
    build_query_url,
    fetch_arxiv,
    resolve_backfill_range,
)


@pytest.fixture(autouse=True)
def _reset_arxiv_pacing() -> None:
    """Clear the module-level cross-fetch pacing state between tests.

    ``_fetch_with_retry`` now applies ``arxiv_request_min_interval_seconds``
    against a process-wide last-fetch timestamp (issue #129).  Without
    this fixture, the second test in a pytest run would pick up the
    timestamp the first test left behind and trigger an unexpected
    ``_sleep`` call that breaks ``mock_sleep.assert_called_once_with``.
    """
    _reset_fetch_pacing_for_tests()


_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "arxiv"


def _load_fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _make_fetch_result(
    body: bytes,
    status_code: int = 200,
    content_type: str = "application/atom+xml",
    final_url: str = "https://export.arxiv.org/api/query",
    headers: dict[str, str] | None = None,
) -> FetchResult:
    return FetchResult(
        body=body,
        status_code=status_code,
        content_type=content_type,
        final_url=final_url,
        headers=headers or {},
    )


def _default_resilience() -> ResilienceConfig:
    return ResilienceConfig()


# ── Query URL construction ─────────────────────────────────────────


class TestBuildQueryUrl:
    def test_single_category(self) -> None:
        url = build_query_url(categories=["cs.AI"], max_results=100)
        assert "search_query=cat:cs.AI" in url
        assert "sortBy=submittedDate" in url
        assert "sortOrder=descending" in url
        assert "max_results=100" in url

    def test_multiple_categories_or_joined(self) -> None:
        url = build_query_url(
            categories=["cs.AI", "cs.RO", "cs.MA"],
            max_results=200,
        )
        assert "search_query=cat:cs.AI+OR+cat:cs.RO+OR+cat:cs.MA" in url

    def test_base_url(self) -> None:
        url = build_query_url(categories=["cs.AI"], max_results=50)
        assert url.startswith("https://export.arxiv.org/api/query?")

    def test_max_results_from_config(self) -> None:
        url = build_query_url(categories=["cs.CL"], max_results=42)
        assert "max_results=42" in url

    def test_backfill_range_adds_submitted_date_clause(self) -> None:
        """Finding 1: ``backfill --days N`` must constrain the URL.

        Review finding 2: the BackfillRange is half-open
        ``[date_from, date_to)`` so ``days=N`` covers exactly N
        calendar days.  The arXiv ``submittedDate:[... TO ...]`` query
        is inclusive on both endpoints, so the upper bound is emitted
        as the last minute of the day BEFORE ``date_to``.
        """
        from datetime import date

        rng = BackfillRange(
            date_from=date(2026, 4, 20),
            date_to=date(2026, 4, 27),
        )
        url = build_query_url(
            categories=["cs.AI"],
            max_results=200,
            backfill_range=rng,
        )
        # 7-day window (Apr 20..Apr 26 inclusive); upper bound is
        # ``date_to - 1 day`` at 2359 because the arXiv query is
        # inclusive on both endpoints.
        assert "submittedDate:[202604200000+TO+202604262359]" in url
        assert "(cat:cs.AI)+AND+submittedDate" in url

    def test_backfill_range_zero_day_window_emits_empty_range(self) -> None:
        """Zero-day window (``date_from == date_to``) emits an empty range
        rather than an inverted query that would server-error.
        """
        from datetime import date

        rng = BackfillRange(
            date_from=date(2026, 4, 20),
            date_to=date(2026, 4, 20),
        )
        url = build_query_url(
            categories=["cs.AI"],
            max_results=200,
            backfill_range=rng,
        )
        # Both bounds collapse to the same minute — server returns
        # no items.
        assert "submittedDate:[202604200000+TO+202604200000]" in url


# ── Backfill range resolver ────────────────────────────────────────


class TestResolveBackfillRange:
    def test_none_yields_none(self) -> None:
        assert resolve_backfill_range(None) is None

    def test_days_form(self) -> None:
        from datetime import timedelta

        now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
        rng = resolve_backfill_range({"days": 7}, now=now)
        assert rng is not None
        assert rng.date_to == now.date()
        assert rng.date_from == now.date() - timedelta(days=7)
        assert rng.days == 7

    def test_from_to_form(self) -> None:
        from datetime import date

        rng = resolve_backfill_range({"from": "2026-04-01", "to": "2026-04-08"})
        assert rng is not None
        assert rng.date_from == date(2026, 4, 1)
        assert rng.date_to == date(2026, 4, 8)
        assert rng.days == 7


# ── Atom parsing ───────────────────────────────────────────────────


class TestParseAtom:
    def test_recent_two_entries(self) -> None:
        body = _load_fixture("recent_two.atom")
        items = _parse_atom(body)
        assert len(items) == 2

    def test_extracts_arxiv_id(self) -> None:
        body = _load_fixture("recent_two.atom")
        items = _parse_atom(body)
        assert items[0].arxiv_id == "2604.11111"
        assert items[1].arxiv_id == "2604.22222"

    def test_extracts_title(self) -> None:
        body = _load_fixture("recent_two.atom")
        items = _parse_atom(body)
        assert items[0].title == (
            "Emergent Planning in Multi-Agent Reinforcement Learning"
        )

    def test_extracts_abstract(self) -> None:
        body = _load_fixture("recent_two.atom")
        items = _parse_atom(body)
        assert "multi-agent" in items[0].abstract.lower()
        assert "reinforcement learning" in items[0].abstract.lower()

    def test_extracts_published_as_utc(self) -> None:
        body = _load_fixture("recent_two.atom")
        items = _parse_atom(body)
        expected = datetime(2026, 4, 23, 18, 0, 0, tzinfo=UTC)
        assert items[0].published == expected

    def test_extracts_categories(self) -> None:
        body = _load_fixture("recent_two.atom")
        items = _parse_atom(body)
        assert "cs.AI" in items[0].categories
        assert "cs.MA" in items[0].categories

    def test_multiple_categories_on_entry(self) -> None:
        body = _load_fixture("single_entry.atom")
        items = _parse_atom(body)
        assert len(items) == 1
        assert set(items[0].categories) == {
            "cs.NE",
            "cs.LG",
            "cs.AI",
        }

    def test_empty_feed(self) -> None:
        body = _load_fixture("empty_feed.atom")
        items = _parse_atom(body)
        assert items == []

    def test_mixed_dates_four_entries(self) -> None:
        body = _load_fixture("mixed_dates.atom")
        items = _parse_atom(body)
        assert len(items) == 4

    def test_strips_version_suffix(self) -> None:
        body = _load_fixture("mixed_dates.atom")
        items = _parse_atom(body)
        # 2604.44444v2 → 2604.44444
        ids = [i.arxiv_id for i in items]
        assert "2604.44444" in ids

    def test_title_whitespace_collapsed(self) -> None:
        """Multi-line title in XML is collapsed to single line."""
        body = _load_fixture("recent_two.atom")
        items = _parse_atom(body)
        # Second entry has a multi-line title in the fixture
        assert "\n" not in items[1].title
        assert "  " not in items[1].title


# ── Extract arXiv ID ───────────────────────────────────────────────


class TestExtractArxivId:
    def test_http_url_with_version(self) -> None:
        assert _extract_arxiv_id("http://arxiv.org/abs/2604.11111v1") == "2604.11111"

    def test_https_url_with_version(self) -> None:
        assert _extract_arxiv_id("https://arxiv.org/abs/2604.11111v2") == "2604.11111"

    def test_bare_id_no_version(self) -> None:
        assert _extract_arxiv_id("2604.11111") == "2604.11111"

    def test_bare_id_with_version(self) -> None:
        assert _extract_arxiv_id("2604.11111v3") == "2604.11111"


# ── Date filtering ─────────────────────────────────────────────────


class TestFilterByLookback:
    def test_drops_old_items(self) -> None:
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        body = _load_fixture("mixed_dates.atom")
        items = _parse_atom(body)
        filtered = _filter_by_lookback(items, lookback_days=1, now=now)
        # Only items from 2026-04-23 and later should survive
        assert len(filtered) == 1
        assert filtered[0].arxiv_id == "2604.33333"

    def test_wider_lookback_keeps_more(self) -> None:
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        body = _load_fixture("mixed_dates.atom")
        items = _parse_atom(body)
        filtered = _filter_by_lookback(items, lookback_days=7, now=now)
        # Items from 2026-04-17 and later: 2604.33333 (Apr 23),
        # 2604.44444 (Apr 20)
        assert len(filtered) == 2

    def test_lookback_filters_all_when_very_narrow(self) -> None:
        now = datetime(2026, 4, 24, 20, 0, 0, tzinfo=UTC)
        body = _load_fixture("mixed_dates.atom")
        items = _parse_atom(body)
        # With lookback_days=0 and now at 20:00 Apr 24,
        # cutoff = Apr 24 20:00 — only items >= that time survive
        filtered = _filter_by_lookback(items, lookback_days=0, now=now)
        assert len(filtered) == 0

    def test_large_lookback_keeps_all(self) -> None:
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        body = _load_fixture("mixed_dates.atom")
        items = _parse_atom(body)
        filtered = _filter_by_lookback(items, lookback_days=365, now=now)
        assert len(filtered) == 4


# ── Fetch with retry ──────────────────────────────────────────────


class TestFetchArxiv:
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_successful_fetch_and_filter(self, mock_fetch: MagicMock) -> None:
        body = _load_fixture("recent_two.atom")
        mock_fetch.return_value = _make_fetch_result(body)

        cfg = ArxivSourceConfig(
            categories=["cs.AI", "cs.RO"],
            max_results_per_category=200,
            lookback_days=1,
        )
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(
            arxiv_config=cfg,
            resilience=_default_resilience(),
            now=now,
        )
        assert len(items) == 2
        assert all(isinstance(i, ArxivItem) for i in items)

    @patch("influx.sources.arxiv.guarded_fetch")
    def test_query_url_passed_to_fetch(self, mock_fetch: MagicMock) -> None:
        body = _load_fixture("empty_feed.atom")
        mock_fetch.return_value = _make_fetch_result(body)

        cfg = ArxivSourceConfig(
            categories=["cs.CL", "cs.LO"],
            max_results_per_category=50,
        )
        fetch_arxiv(
            arxiv_config=cfg,
            resilience=_default_resilience(),
        )
        url_arg = mock_fetch.call_args[0][0]
        assert "search_query=cat:cs.CL+OR+cat:cs.LO" in url_arg
        assert "max_results=50" in url_arg

    @patch("influx.sources.arxiv.guarded_fetch")
    def test_lookback_filtering_applied(self, mock_fetch: MagicMock) -> None:
        body = _load_fixture("mixed_dates.atom")
        mock_fetch.return_value = _make_fetch_result(body)

        cfg = ArxivSourceConfig(
            categories=["cs.CL"],
            lookback_days=1,
        )
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(
            arxiv_config=cfg,
            resilience=_default_resilience(),
            now=now,
        )
        # Only 2604.33333 (Apr 23) should survive with 1-day lookback
        assert len(items) == 1
        assert items[0].arxiv_id == "2604.33333"


class TestFetchRetry:
    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_429_backoff_honoured(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """HTTP 429 triggers arxiv_429_backoff_seconds sleep (FR-RES-2)."""
        body = _load_fixture("recent_two.atom")
        mock_fetch.side_effect = [
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(body),
        ]

        resilience = ResilienceConfig(
            arxiv_429_backoff_seconds=10,
            max_retries=3,
        )
        cfg = ArxivSourceConfig(
            categories=["cs.AI"],
            lookback_days=30,
        )
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(
            arxiv_config=cfg,
            resilience=resilience,
            now=now,
        )
        assert len(items) == 2
        mock_sleep.assert_called_once_with(10)

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_429_exhausts_retries(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """429 on every attempt of the 429 retry budget raises NetworkError."""
        del mock_sleep
        mock_fetch.side_effect = [
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
        ]

        # Issue #129: the 429 retry budget defaults to 5; pin both
        # budgets to 3 so the existing 4-element side_effect still
        # exercises "exhaust all 429 retries" exactly.
        resilience = ResilienceConfig(max_retries=3, arxiv_429_max_retries=3)
        cfg = ArxivSourceConfig(categories=["cs.AI"])
        with pytest.raises(NetworkError, match="429"):
            fetch_arxiv(
                arxiv_config=cfg,
                resilience=resilience,
            )

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_network_error_exponential_backoff(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """NetworkError triggers exponential backoff (FR-RES-1)."""
        body = _load_fixture("empty_feed.atom")
        mock_fetch.side_effect = [
            NetworkError("timeout", url="http://x", kind="timeout"),
            NetworkError("timeout", url="http://x", kind="timeout"),
            _make_fetch_result(body),
        ]

        resilience = ResilienceConfig(
            backoff_base_seconds=1,
            max_retries=3,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"], lookback_days=365)
        fetch_arxiv(arxiv_config=cfg, resilience=resilience)
        # Expect exponential backoff: 1*2^0=1, 1*2^1=2
        assert mock_sleep.call_args_list == [
            call(1),
            call(2),
        ]

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_network_error_exhausts_retries(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """All retries exhausted raises the last NetworkError."""
        mock_fetch.side_effect = NetworkError("dns fail", url="http://x", kind="dns")

        resilience = ResilienceConfig(max_retries=2)
        cfg = ArxivSourceConfig(categories=["cs.AI"])
        with pytest.raises(NetworkError, match="dns fail"):
            fetch_arxiv(arxiv_config=cfg, resilience=resilience)
        # 3 attempts total (initial + 2 retries)
        assert mock_fetch.call_count == 3

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_5xx_retried_with_exponential_backoff(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """5xx HTTP errors are retried with exponential backoff
        (FR-RES-1); a transient 500 followed by 200 succeeds."""
        body = _load_fixture("recent_two.atom")
        mock_fetch.side_effect = [
            _make_fetch_result(b"", status_code=500),
            _make_fetch_result(body),
        ]

        resilience = ResilienceConfig(
            backoff_base_seconds=1,
            max_retries=3,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"], lookback_days=30)
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(
            arxiv_config=cfg,
            resilience=resilience,
            now=now,
        )
        assert len(items) == 2
        assert mock_fetch.call_count == 2
        # First attempt is attempt=0 → delay = base * 2^0 = 1s
        mock_sleep.assert_called_once_with(1)

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_5xx_exhausts_retries(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Persistent 5xx exhausts retries and raises NetworkError."""
        mock_fetch.side_effect = [
            _make_fetch_result(b"", status_code=503),
            _make_fetch_result(b"", status_code=503),
            _make_fetch_result(b"", status_code=503),
        ]

        resilience = ResilienceConfig(
            backoff_base_seconds=1,
            max_retries=2,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"])
        with pytest.raises(NetworkError, match="503"):
            fetch_arxiv(arxiv_config=cfg, resilience=resilience)
        # 3 attempts total (initial + 2 retries)
        assert mock_fetch.call_count == 3
        # Exponential backoff between retries: 1*2^0, 1*2^1
        assert mock_sleep.call_args_list == [call(1), call(2)]

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_4xx_non_429_raises_immediately(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Non-retryable 4xx (not 429) raises NetworkError without retry."""
        mock_fetch.return_value = _make_fetch_result(b"", status_code=400)

        cfg = ArxivSourceConfig(categories=["cs.AI"])
        with pytest.raises(NetworkError, match="400"):
            fetch_arxiv(
                arxiv_config=cfg,
                resilience=_default_resilience(),
            )
        assert mock_fetch.call_count == 1
        mock_sleep.assert_not_called()

    @patch("influx.sources.arxiv.guarded_fetch")
    def test_content_type_not_passed_to_guarded_fetch(
        self, mock_fetch: MagicMock
    ) -> None:
        """Fetcher does NOT pass expected_content_type to guarded_fetch.

        Content-type validation happens locally after status handling so
        429/5xx with non-XML bodies route through the retry/backoff paths
        rather than being raised as content-type errors first.
        """
        body = _load_fixture("empty_feed.atom")
        mock_fetch.return_value = _make_fetch_result(body)

        cfg = ArxivSourceConfig(categories=["cs.AI"])
        fetch_arxiv(
            arxiv_config=cfg,
            resilience=_default_resilience(),
        )
        _, kwargs = mock_fetch.call_args
        assert "expected_content_type" not in kwargs

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_429_non_xml_content_type_still_backs_off(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """FR-RES-2: 429 with non-XML content-type still triggers
        arxiv_429_backoff_seconds (not generic exponential backoff).

        Regression guard: the earlier implementation passed
        expected_content_type='xml' to guarded_fetch, so any 429 with a
        text/plain or text/html body was raised as a content-type
        mismatch and fell into the generic backoff path.
        """
        body = _load_fixture("recent_two.atom")
        mock_fetch.side_effect = [
            _make_fetch_result(
                b"Too many requests",
                status_code=429,
                content_type="text/plain; charset=utf-8",
            ),
            _make_fetch_result(body),
        ]

        resilience = ResilienceConfig(
            arxiv_429_backoff_seconds=12,
            backoff_base_seconds=1,
            max_retries=3,
        )
        cfg = ArxivSourceConfig(
            categories=["cs.AI"],
            lookback_days=30,
        )
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(
            arxiv_config=cfg,
            resilience=resilience,
            now=now,
        )
        assert len(items) == 2
        # Must be the fixed 429 backoff, NOT base * 2**0 = 1s.
        mock_sleep.assert_called_once_with(12)

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_429_numeric_retry_after_honoured(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        body = _load_fixture("recent_two.atom")
        mock_fetch.side_effect = [
            _make_fetch_result(
                b"Too many requests",
                status_code=429,
                headers={"Retry-After": "42"},
            ),
            _make_fetch_result(body),
        ]

        resilience = ResilienceConfig(
            arxiv_429_backoff_seconds=10,
            arxiv_request_min_interval_seconds=3,
            max_retries=3,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"], lookback_days=30)
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(arxiv_config=cfg, resilience=resilience, now=now)

        assert len(items) == 2
        mock_sleep.assert_called_once_with(42)

    @patch("influx.sources.arxiv.time.time", return_value=1_778_068_800.0)
    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_429_http_date_retry_after_honoured(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
        mock_time: MagicMock,
    ) -> None:
        del mock_time
        body = _load_fixture("recent_two.atom")
        mock_fetch.side_effect = [
            _make_fetch_result(
                b"Too many requests",
                status_code=429,
                headers={"Retry-After": "Wed, 06 May 2026 12:01:30 GMT"},
            ),
            _make_fetch_result(body),
        ]

        resilience = ResilienceConfig(
            arxiv_429_backoff_seconds=10,
            arxiv_request_min_interval_seconds=3,
            max_retries=3,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"], lookback_days=30)
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(arxiv_config=cfg, resilience=resilience, now=now)

        assert len(items) == 2
        mock_sleep.assert_called_once_with(90)

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_429_malformed_retry_after_uses_default_backoff(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        body = _load_fixture("recent_two.atom")
        mock_fetch.side_effect = [
            _make_fetch_result(
                b"Too many requests",
                status_code=429,
                headers={"Retry-After": "not a date"},
            ),
            _make_fetch_result(body),
        ]

        resilience = ResilienceConfig(
            arxiv_429_backoff_seconds=12,
            max_retries=3,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"], lookback_days=30)
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(arxiv_config=cfg, resilience=resilience, now=now)

        assert len(items) == 2
        mock_sleep.assert_called_once_with(12)

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_429_retry_after_is_clamped(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Issue #129: ``Retry-After`` cannot exceed
        ``arxiv_429_backoff_max_seconds``.  ``Retry-After`` is first
        clamped to the parser's RFC ceiling (300s) and then to the
        config's progressive-backoff cap so a misbehaving upstream
        cannot extend a single retry beyond a known wall-clock bound.
        """
        body = _load_fixture("recent_two.atom")
        mock_fetch.side_effect = [
            _make_fetch_result(
                b"Too many requests",
                status_code=429,
                headers={"Retry-After": "999"},
            ),
            _make_fetch_result(body),
        ]

        resilience = ResilienceConfig(
            arxiv_429_backoff_seconds=10,
            arxiv_429_backoff_max_seconds=120,
            max_retries=3,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"], lookback_days=30)
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(arxiv_config=cfg, resilience=resilience, now=now)

        assert len(items) == 2
        mock_sleep.assert_called_once_with(120)

    @patch("influx.sources.arxiv.guarded_fetch")
    def test_successful_non_xml_content_type_raises(
        self, mock_fetch: MagicMock
    ) -> None:
        """A 200 response with non-XML content-type still fails."""
        mock_fetch.return_value = _make_fetch_result(
            b"<html>not xml</html>",
            content_type="text/html",
        )

        cfg = ArxivSourceConfig(categories=["cs.AI"])
        with pytest.raises(NetworkError, match="Content-type"):
            fetch_arxiv(
                arxiv_config=cfg,
                resilience=_default_resilience(),
            )

    @patch("influx.sources.arxiv.guarded_fetch")
    def test_storage_tunables_threaded_to_guarded_fetch(
        self, mock_fetch: MagicMock
    ) -> None:
        """Review finding 1: ``fetch_arxiv`` forwards
        ``max_download_bytes`` / ``timeout_seconds`` to ``guarded_fetch``
        so the loaded ``[storage]`` config actually shapes outbound
        download safety on the arXiv API path (US-011 AC-X-1)."""
        body = _load_fixture("empty_feed.atom")
        mock_fetch.return_value = _make_fetch_result(body)

        cfg = ArxivSourceConfig(categories=["cs.AI"])
        fetch_arxiv(
            arxiv_config=cfg,
            resilience=_default_resilience(),
            max_download_bytes=4321,
            timeout_seconds=42,
        )
        kwargs = mock_fetch.call_args.kwargs
        assert kwargs.get("max_download_bytes") == 4321
        assert kwargs.get("timeout_seconds") == 42


class TestArxivHardening:
    """Issue #129 — production-cadence hardening tests.

    Cover progressive 429 backoff, the separate 429 retry budget,
    cross-fetch pacing, and the run-ledger retry-count linkage.
    """

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_429_backoff_doubles_per_attempt_until_cap(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Progressive 429 backoff: each successive 429 in one fetch
        waits twice the previous delay, capped at
        ``arxiv_429_backoff_max_seconds``.
        """
        body = _load_fixture("recent_two.atom")
        mock_fetch.side_effect = [
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(body),
        ]
        resilience = ResilienceConfig(
            arxiv_429_backoff_seconds=10,
            arxiv_429_backoff_max_seconds=60,
            arxiv_429_max_retries=5,
            max_retries=5,
            arxiv_request_min_interval_seconds=0,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"], lookback_days=30)
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        fetch_arxiv(arxiv_config=cfg, resilience=resilience, now=now)
        # Attempts 0..3 each pre-retry sleep: 10, 20, 40, 60 (capped).
        assert mock_sleep.call_args_list == [
            call(10.0),
            call(20.0),
            call(40.0),
            call(60.0),
        ]

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_429_uses_separate_retry_budget(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """``arxiv_429_max_retries`` exceeds ``max_retries`` and 429s
        keep retrying past the network/5xx budget — the more generous
        soft-failure budget is what gets the run home.
        """
        del mock_sleep
        body = _load_fixture("recent_two.atom")
        mock_fetch.side_effect = [
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(body),
        ]
        resilience = ResilienceConfig(
            arxiv_429_backoff_seconds=1,
            arxiv_429_backoff_max_seconds=10,
            max_retries=2,  # would only allow 3 attempts total for 5xx
            arxiv_429_max_retries=5,  # but 429s keep going
            arxiv_request_min_interval_seconds=0,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"], lookback_days=30)
        now = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)
        items = fetch_arxiv(arxiv_config=cfg, resilience=resilience, now=now)
        assert len(items) == 2
        assert mock_fetch.call_count == 5

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_network_path_still_uses_max_retries(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Issue #129 hardening must not loosen the network/5xx budget.
        ``max_retries=2`` means 3 attempts total before raising even
        when ``arxiv_429_max_retries`` is far higher.
        """
        del mock_sleep
        mock_fetch.side_effect = NetworkError("boom", url="http://x", kind="network")
        resilience = ResilienceConfig(
            max_retries=2,
            arxiv_429_max_retries=10,
            arxiv_request_min_interval_seconds=0,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"])
        with pytest.raises(NetworkError, match="boom"):
            fetch_arxiv(arxiv_config=cfg, resilience=resilience)
        assert mock_fetch.call_count == 3

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_cross_fetch_pacing_applies_min_interval(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Two consecutive fetches in the same process pace themselves.

        The first fetch records the timestamp without sleeping; the
        second sees the recent timestamp and sleeps the remaining
        interval.  The per-day backfill loop already paced itself with
        the same interval, but the scheduled-tick path did not — this
        is the new behaviour that absorbs back-to-back profile fetches
        in the same tick (issue #129).
        """
        body = _load_fixture("empty_feed.atom")
        mock_fetch.return_value = _make_fetch_result(body)
        resilience = ResilienceConfig(
            arxiv_request_min_interval_seconds=3,
            max_retries=0,
            arxiv_429_max_retries=0,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"])
        fetch_arxiv(arxiv_config=cfg, resilience=resilience)
        # First call records the timestamp; no pacing sleep yet.
        assert mock_sleep.call_count == 0
        fetch_arxiv(arxiv_config=cfg, resilience=resilience)
        # Second call, still within 3s wall-clock of the first, must
        # have slept to bring the gap up to ~3 seconds.  Allow a
        # generous lower bound to avoid flakiness on slow CI.
        assert mock_sleep.call_count == 1
        slept = mock_sleep.call_args_list[0].args[0]
        assert 0 < slept <= 3

    @patch("influx.sources.arxiv._sleep")
    @patch("influx.sources.arxiv.guarded_fetch")
    def test_record_source_retry_called_per_retry_decision(
        self,
        mock_fetch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Each retry — 429 or network — calls ``record_source_retry``
        with the correct kind so the run-ledger entry can surface
        recovered-retry counts (#129).
        """
        del mock_sleep
        body = _load_fixture("empty_feed.atom")
        mock_fetch.side_effect = [
            NetworkError("t/o", url="http://x", kind="timeout"),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(body),
        ]
        resilience = ResilienceConfig(
            arxiv_429_backoff_seconds=1,
            arxiv_429_backoff_max_seconds=2,
            backoff_base_seconds=1,
            max_retries=3,
            arxiv_429_max_retries=3,
            arxiv_request_min_interval_seconds=0,
        )
        cfg = ArxivSourceConfig(categories=["cs.AI"])
        with patch("influx.sources.arxiv.record_source_retry") as mock_record:
            fetch_arxiv(arxiv_config=cfg, resilience=resilience)
        kinds = [c.kwargs["kind"] for c in mock_record.call_args_list]
        sources = [c.kwargs["source"] for c in mock_record.call_args_list]
        assert kinds == ["timeout", "rate_limit"]
        assert sources == ["arxiv", "arxiv"]


class TestApplyMinInterval:
    """Direct tests for the cross-fetch pacing slot allocator (#129).

    Review feedback: the original implementation released the lock
    before sleeping, so two concurrent callers could observe the same
    ``last`` timestamp, compute the same wait, and then both start
    their HTTP fetch together.  The slot-allocator version below
    serialises *slot allocation* under the lock — concurrent callers
    therefore receive *different* slots and pace themselves correctly.
    """

    @patch("influx.sources.arxiv._sleep")
    def test_first_call_does_not_sleep(self, mock_sleep: MagicMock) -> None:
        _apply_min_interval(3.0)
        mock_sleep.assert_not_called()

    @patch("influx.sources.arxiv._sleep")
    def test_zero_interval_is_no_op(self, mock_sleep: MagicMock) -> None:
        """``min_interval=0`` short-circuits and never touches the slot
        state, so a subsequent paced call sees a clean baseline."""
        _apply_min_interval(0.0)
        _apply_min_interval(3.0)
        # First call: short-circuit. Second call: clean baseline → no
        # waiting slot, sleeps zero.
        mock_sleep.assert_not_called()

    @patch("influx.sources.arxiv._sleep")
    def test_three_back_to_back_callers_get_distinct_slots(
        self, mock_sleep: MagicMock
    ) -> None:
        """Three calls in rapid succession (``_sleep`` mocked, so wall-
        clock barely advances) each claim a slot
        ``min_interval`` apart: the first sleeps zero, the second
        sleeps ~``min_interval``, the third sleeps ~``2 * min_interval``.

        This is the property that cross-profile pacing depends on:
        even if profiles A, B, and C all reach
        ``_fetch_with_retry`` at the same wall-clock instant (the
        scheduled-tick concurrency case the original review flagged),
        each gets a unique slot under the lock and they hit arXiv
        ``min_interval`` apart on the wire — not all together.
        """
        _apply_min_interval(3.0)
        _apply_min_interval(3.0)
        _apply_min_interval(3.0)
        # The first call short-circuits the sleep guard with wait=0.
        # The remaining two each call ``_sleep`` with their full slot
        # offset because ``_sleep`` is mocked and so wall-clock time
        # barely advances between calls.
        assert mock_sleep.call_count == 2
        slept_first = mock_sleep.call_args_list[0].args[0]
        slept_second = mock_sleep.call_args_list[1].args[0]
        # Generous lower bounds to absorb the few microseconds that
        # *do* advance during the test; tight upper bounds to verify
        # the slot allocator is not over-spacing.
        assert 2.9 < slept_first <= 3.0
        assert 5.9 < slept_second <= 6.0

    @patch("influx.sources.arxiv._sleep")
    def test_call_after_slot_already_passed_does_not_sleep(
        self, mock_sleep: MagicMock
    ) -> None:
        """When the previously claimed slot is already in the past, a
        fresh call starts immediately at ``now`` — pacing only kicks in
        when calls cluster within a single interval window."""
        _apply_min_interval(0.001)  # claims a slot ~1 ms in the future
        # Sleep so the previously-claimed slot is firmly in the past.
        time.sleep(0.01)
        mock_sleep.reset_mock()
        _apply_min_interval(3.0)
        mock_sleep.assert_not_called()
