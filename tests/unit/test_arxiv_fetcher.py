"""Tests for arXiv Atom fetcher (US-010, FR-SRC-1, FR-SRC-2, FR-RES-1/2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from influx.config import ArxivSourceConfig, ResilienceConfig
from influx.errors import NetworkError
from influx.http_client import FetchResult
from influx.sources.arxiv import (
    ArxivItem,
    _extract_arxiv_id,
    _filter_by_lookback,
    _parse_atom,
    build_query_url,
    fetch_arxiv,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "arxiv"


def _load_fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _make_fetch_result(
    body: bytes,
    status_code: int = 200,
    content_type: str = "application/atom+xml",
    final_url: str = "https://export.arxiv.org/api/query",
) -> FetchResult:
    return FetchResult(
        body=body,
        status_code=status_code,
        content_type=content_type,
        final_url=final_url,
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
        """429 on all attempts raises NetworkError."""
        mock_fetch.side_effect = [
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
            _make_fetch_result(b"", status_code=429),
        ]

        resilience = ResilienceConfig(max_retries=3)
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
