"""Tests for RSS/Atom feed parser (US-001, FR-SRC-4)."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest

from influx.config import RssSourceEntry
from influx.sources.rss import RssFeedItem, parse_feed

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "rss"


def _load_fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _make_feed_entry(
    name: str = "test-feed",
    url: str = "https://example.com/feed",
    source_tag: Literal["rss", "blog"] = "rss",
) -> RssSourceEntry:
    return RssSourceEntry(name=name, url=url, source_tag=source_tag)


# ── Atom feed parsing ─────────────────────────────────────────────


class TestAtomParsing:
    """AC: Atom parsing yields the documented fields."""

    def test_atom_yields_documented_fields(self) -> None:
        content = _load_fixture("sample_atom.xml")
        entry = _make_feed_entry(name="ai-research", source_tag="blog")
        items = parse_feed(content, entry)

        assert len(items) == 2

        # Newest first (sorted by published)
        first = items[0]
        assert isinstance(first, RssFeedItem)
        assert first.title == "Advances in Transformer Architectures"
        assert first.url == "https://ai-research.example/posts/transformer-advances"
        assert first.published.year == 2026
        assert first.published.month == 4
        assert first.published.day == 20
        assert "transformer" in first.summary.lower()

    def test_atom_all_items_parsed(self) -> None:
        content = _load_fixture("sample_atom.xml")
        entry = _make_feed_entry(name="ai-research", source_tag="rss")
        items = parse_feed(content, entry)

        assert len(items) == 2
        titles = {it.title for it in items}
        assert "Advances in Transformer Architectures" in titles
        assert "Multi-Agent Reinforcement Learning Survey" in titles


# ── RSS 2.0 feed parsing ──────────────────────────────────────────


class TestRss2Parsing:
    """AC: RSS 2.0 parsing yields the documented fields."""

    def test_rss2_yields_documented_fields(self) -> None:
        content = _load_fixture("sample_rss2.xml")
        entry = _make_feed_entry(name="web-eng", source_tag="rss")
        items = parse_feed(content, entry)

        assert len(items) == 2

        first = items[0]
        assert isinstance(first, RssFeedItem)
        assert first.title == "Building Resilient Microservices"
        assert first.url == "https://webeng.example/posts/resilient-microservices"
        assert first.published.year == 2026
        assert first.published.month == 4
        assert first.published.day == 19
        assert "microservices" in first.summary.lower()

    def test_rss2_all_items_parsed(self) -> None:
        content = _load_fixture("sample_rss2.xml")
        entry = _make_feed_entry(name="web-eng", source_tag="blog")
        items = parse_feed(content, entry)

        assert len(items) == 2
        titles = {it.title for it in items}
        assert "Building Resilient Microservices" in titles
        assert "Edge Computing with WebAssembly" in titles


# ── source_tag passthrough (FR-SRC-4) ────────────────────────────


class TestSourceTagPassthrough:
    """AC: feed's configured source_tag flows through verbatim."""

    def test_rss_source_tag(self) -> None:
        """Feed configured with source_tag='rss' yields items tagged 'rss'."""
        content = _load_fixture("sample_rss2.xml")
        entry = _make_feed_entry(name="web-eng", source_tag="rss")
        items = parse_feed(content, entry)

        assert len(items) > 0
        for item in items:
            assert item.source_tag == "rss"

    def test_blog_source_tag(self) -> None:
        """Feed configured with source_tag='blog' yields items tagged 'blog'."""
        content = _load_fixture("sample_atom.xml")
        entry = _make_feed_entry(name="ai-blog", source_tag="blog")
        items = parse_feed(content, entry)

        assert len(items) > 0
        for item in items:
            assert item.source_tag == "blog"

    def test_source_tag_not_inferred(self) -> None:
        """Parser does not infer or override source_tag — same feed with
        different tags produces differently-tagged items."""
        content = _load_fixture("sample_atom.xml")

        items_rss = parse_feed(
            content, _make_feed_entry(name="feed-a", source_tag="rss")
        )
        items_blog = parse_feed(
            content, _make_feed_entry(name="feed-b", source_tag="blog")
        )

        assert all(it.source_tag == "rss" for it in items_rss)
        assert all(it.source_tag == "blog" for it in items_blog)


# ── feed_name passthrough ─────────────────────────────────────────


class TestFeedName:
    def test_feed_name_carried(self) -> None:
        content = _load_fixture("sample_rss2.xml")
        entry = _make_feed_entry(name="web-eng", source_tag="rss")
        items = parse_feed(content, entry)

        for item in items:
            assert item.feed_name == "web-eng"


# ── No cross-bucket leakage ───────────────────────────────────────


class TestNoCrossBucketLeakage:
    """AC: No feed contributes items to more than one source:* bucket."""

    def test_single_source_tag_per_feed(self) -> None:
        content = _load_fixture("sample_atom.xml")
        entry = _make_feed_entry(name="test-feed", source_tag="blog")
        items = parse_feed(content, entry)

        tags = {it.source_tag for it in items}
        assert len(tags) == 1
        assert tags == {"blog"}


# ── Published date UTC handling (review finding 1) ─────────────────


class TestPublishedDateUtc:
    """Review finding 1: feedparser tuples are UTC; convert without local-time shift.

    A timestamp like ``2026-04-23 00:30 UTC`` must stay on April 23 in
    UTC regardless of the host's local timezone.  Using
    :func:`time.mktime` would silently apply ``TZ=local`` and shift the
    entry into a different ``{YYYY-MM-DD}`` archive bucket on any host
    not running in UTC.  Using :func:`calendar.timegm` keeps the
    conversion UTC-safe.
    """

    def test_near_midnight_utc_preserves_day(self) -> None:
        from time import struct_time

        from influx.sources.rss import _parse_published

        # 00:30 on 2026-04-23 (UTC) — within 1h of midnight on either
        # side of UTC for any reasonable host TZ.
        utc_tuple = struct_time((2026, 4, 23, 0, 30, 0, 0, 0, 0))

        class _Entry:
            def get(self, key: str) -> Any:
                if key == "published_parsed":
                    return utc_tuple
                return None

        published = _parse_published(_Entry())

        assert published.tzinfo is UTC
        assert published.year == 2026
        assert published.month == 4
        assert published.day == 23
        assert published.hour == 0
        assert published.minute == 30

    def test_near_midnight_utc_does_not_shift_day(self) -> None:
        """``2026-04-23 00:30 UTC`` must NOT roll back to April 22.

        On hosts running in eastern timezones, ``mktime`` would convert
        the tuple as local time and yield a UTC timestamp earlier than
        the input — landing on April 22.
        """
        from time import struct_time

        from influx.sources.rss import _parse_published

        utc_tuple = struct_time((2026, 4, 23, 0, 30, 0, 0, 0, 0))

        class _Entry:
            def get(self, key: str) -> Any:
                return utc_tuple if key == "published_parsed" else None

        published = _parse_published(_Entry())

        # Day MUST be 23, not 22 — regression guard for finding 1.
        assert (published.year, published.month, published.day) == (2026, 4, 23)


# ── Storage tunables threaded through (review finding 1, AC-X-1) ─────


class TestStorageTunablesThreaded:
    """``_fetch_rss_feed`` forwards storage tunables to ``guarded_fetch``.

    Regression guard for review finding 1: the RSS feed fetch path must
    forward ``config.storage.max_download_bytes`` and
    ``config.storage.download_timeout_seconds`` so the loaded
    ``influx.toml`` actually shapes outbound download safety on the RSS
    feed-bytes path (US-011 AC-X-1).
    """

    @patch("influx.sources.rss._aguarded_fetch")
    @pytest.mark.asyncio
    async def test_fetch_rss_feed_threads_storage_tunables(
        self, mock_fetch: MagicMock
    ) -> None:
        from influx.http_client import FetchResult
        from influx.sources.rss import _fetch_rss_feed

        # ``_aguarded_fetch`` is async; the mock must return a coroutine.
        async def _stub(*_args: object, **_kwargs: object) -> FetchResult:
            return FetchResult(
                body=b"<rss/>",
                status_code=200,
                content_type="application/rss+xml",
                final_url="https://example.com/feed.xml",
            )

        mock_fetch.side_effect = _stub

        feed_entry = _make_feed_entry(
            name="x", url="https://example.com/feed.xml", source_tag="rss"
        )
        await _fetch_rss_feed(
            feed_entry,
            cache=None,
            max_download_bytes=4321,
            timeout_seconds=42,
        )
        kwargs = mock_fetch.call_args.kwargs
        assert kwargs.get("max_download_bytes") == 4321
        assert kwargs.get("timeout_seconds") == 42


# ── Issue #131: pre-acquire URL validation ─────────────────────────


_MIXED_FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Mixed Feed</title>
  <link>https://example.com/</link>
  <description>One good link, one localhost link, one private link.</description>
  <item>
    <title>Good Public Article</title>
    <link>https://example.com/good</link>
    <description>Public article.</description>
    <pubDate>Wed, 06 May 2026 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Localhost Leak</title>
    <link>http://localhost:5174/leak</link>
    <description>Upstream-malformed link.</description>
    <pubDate>Wed, 06 May 2026 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Private Network Link</title>
    <link>http://192.168.1.10/internal</link>
    <description>RFC1918 link.</description>
    <pubDate>Wed, 06 May 2026 12:00:00 +0000</pubDate>
  </item>
</channel>
</rss>
"""


class TestParseFeedRejectsInvalidUrls:
    """Issue #131: ``parse_feed`` rejects loopback/private/malformed URLs."""

    def test_default_drops_loopback_and_private_items(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        entry = _make_feed_entry(name="mixed", source_tag="rss")
        with caplog.at_level("WARNING", logger="influx.sources.rss"):
            items = parse_feed(_MIXED_FEED_XML, entry, profile="p")

        assert [it.title for it in items] == ["Good Public Article"]
        # Two WARNINGs emitted: one for localhost, one for 192.168.x.x
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        rejected_messages = [r.getMessage() for r in warnings]
        assert any("localhost:5174" in msg for msg in rejected_messages)
        assert any("192.168.1.10" in msg for msg in rejected_messages)
        assert any("reason=loopback" in msg for msg in rejected_messages)
        assert any("reason=private" in msg for msg in rejected_messages)

    def test_allow_private_ips_keeps_local_items(self) -> None:
        entry = _make_feed_entry(name="mixed", source_tag="rss")
        items = parse_feed(_MIXED_FEED_XML, entry, allow_private_ips=True)

        assert {it.title for it in items} == {
            "Good Public Article",
            "Localhost Leak",
            "Private Network Link",
        }

    def test_increments_run_counter(self) -> None:
        """Rejections bump ``current_invalid_url_rejections`` (issue #131)."""
        from influx.telemetry import current_invalid_url_rejections

        counter: list[int] = [0]
        token = current_invalid_url_rejections.set(counter)
        try:
            entry = _make_feed_entry(name="mixed", source_tag="rss")
            parse_feed(_MIXED_FEED_XML, entry, profile="p")
        finally:
            current_invalid_url_rejections.reset(token)

        # Localhost + 192.168.x.x = 2 rejections.
        assert counter[0] == 2

    def test_counter_no_op_outside_run_context(self) -> None:
        """``record_invalid_url_rejection`` is safe when ContextVar is unset."""
        from influx.telemetry import current_invalid_url_rejections

        # Default value is None — no run context.
        assert current_invalid_url_rejections.get() is None

        entry = _make_feed_entry(name="mixed", source_tag="rss")
        items = parse_feed(_MIXED_FEED_XML, entry)

        # Validator still runs; only the public item passes.
        assert [it.title for it in items] == ["Good Public Article"]
