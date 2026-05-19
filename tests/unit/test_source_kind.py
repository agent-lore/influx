"""Tests for URL-derived source-kind classification (issue #160).

The classifier is purely syntactic and is consulted by
:func:`influx.storage.download_archive` to short-circuit the HTML
archive acquisition path when a feed-discovered URL points at a
non-HTML resource.  The taxonomy is intentionally small (``html`` /
``xml`` / ``pointer``) — these tests pin both the known-bad shapes
(RSS endpoints, HN ``/item`` pointers) and the default-``html``
fall-through for every URL that doesn't match a known pattern.
"""

from __future__ import annotations

import pytest

from influx.source_kind import classify_source_kind


class TestXmlClassification:
    """RSS / Atom / feed-flavoured URLs classify as ``"xml"``."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://csdb.dk/rss/upcomingevents.php",
            "https://csdb.dk/rss/latestadditions.php?type=release",
            "https://example.com/rss",
            "https://example.com/rss/",
            "https://example.com/feed/",
            "https://example.com/feed",
            "https://example.com/atom.xml",
            "https://example.com/posts/feed.xml",
            "https://example.com/feed.rss",
            "https://example.com/podcast.atom",
            "https://example.com/blog/atom",
        ],
    )
    def test_xml_urls(self, url: str) -> None:
        assert classify_source_kind(url) == "xml"


class TestPointerClassification:
    """HN discussion / aggregator pointer URLs classify as ``"pointer"``."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://news.ycombinator.com/item?id=48081266",
            "http://news.ycombinator.com/item?id=1",
            "https://NEWS.ycombinator.com/item?id=42",
        ],
    )
    def test_hn_item_pointer(self, url: str) -> None:
        assert classify_source_kind(url) == "pointer"

    def test_hn_non_item_path_is_html(self) -> None:
        # HN's submission landing pages render HTML article-like content
        # via the upstream link — they are NOT pointer URLs.  Only the
        # ``/item`` discussion-link shape collapses to pointer.
        assert classify_source_kind("https://news.ycombinator.com/newest") == "html"
        assert classify_source_kind("https://news.ycombinator.com/show") == "html"


class TestHtmlDefault:
    """URLs that don't match a known non-HTML shape fall through to ``"html"``."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/posts/article",
            "https://example.com/2026/05/post-title",
            "https://example.com/articles/feedback",  # contains "feedback", not "feed"
            "https://example.com/news/today",
            "https://example.com/",
            "https://example.com",
            "https://blog.example.com/post.html",
        ],
    )
    def test_default_html(self, url: str) -> None:
        assert classify_source_kind(url) == "html"


class TestEdgeCases:
    """Empty / malformed URLs collapse to the safe ``"html"`` default."""

    @pytest.mark.parametrize("url", ["", "not-a-url", "http://"])
    def test_empty_and_malformed(self, url: str) -> None:
        # Defensive default — the classifier is consulted on every
        # archive call, so a parse failure must not raise.
        assert classify_source_kind(url) == "html"
