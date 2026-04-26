"""Tests for RSS/Atom feed parser (US-001, FR-SRC-4)."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any, Literal

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
