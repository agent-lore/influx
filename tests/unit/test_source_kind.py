"""Tests for URL-derived source-kind classification (issue #160).

The classifier is purely syntactic and is consulted by
:func:`influx.storage.download_archive` to short-circuit the HTML
archive acquisition path when a feed-discovered URL points at a
non-HTML resource.  The taxonomy is intentionally small (``html`` /
``xml`` / ``pointer``).

These tests pin the *narrow* known-bad shapes (file extensions, CSDB-
style CGI feed endpoints, HN ``/item`` pointers) plus an explicit
false-positive guard for article URLs that look superficially like
feed URLs but are not.  PR #167 review feedback called out that a
false positive is permanent (the note carries
``influx:archive-terminal``), so the guard cases are first-class
coverage rather than incidental.
"""

from __future__ import annotations

import pytest

from influx.source_kind import classify_source_kind


class TestXmlClassification:
    """File-extension and CGI-feed URLs classify as ``"xml"``."""

    @pytest.mark.parametrize(
        "url",
        [
            # Staging-observed CSDB CGI feed shape (issue #160 evidence).
            "https://csdb.dk/rss/upcomingevents.php",
            "https://csdb.dk/rss/latestadditions.php?type=release",
            # Other CGI-style feed scripts under the same shape.
            "https://example.com/atom/recent.cgi",
            "https://example.com/feed/today.aspx",
            "https://example.com/rss/podcast.jsp",
            # File-extension feeds.
            "https://example.com/atom.xml",
            "https://example.com/posts/feed.xml",
            "https://example.com/feed.rss",
            "https://example.com/podcast.atom",
            "https://example.com/some/path/document.xml",
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


class TestNarrowingRegressionGuards:
    """Pin the cases the PR #167 review specifically warned about.

    A false positive in the classifier short-circuits archive
    acquisition and stamps the note ``influx:archive-terminal`` — the
    repair sweep then refuses to retry.  These cases must stay
    classified as ``"html"`` so the broad pattern that previously
    matched any ``/feed``, ``/rss``, ``/atom`` segment cannot return
    silently.
    """

    @pytest.mark.parametrize(
        "url",
        [
            # Bare directory segments: WordPress / Ghost / Substack
            # feed roots are NOT children inside another feed (they ARE
            # the feed), but article URLs that pass through these
            # segments are common in the wild.
            "https://example.com/feed/",
            "https://example.com/feed",
            "https://example.com/rss/",
            "https://example.com/rss",
            "https://example.com/atom/",
            "https://example.com/atom",
            # Article URLs whose path passes through a ``feed`` /
            # ``rss`` / ``atom`` segment.  These are the high-cost
            # false-positive surface.
            "https://example.com/news/feed/breaking-story",
            "https://example.com/rss/2026/spring-release-notes",
            "https://example.com/atom/post-title",
            "https://example.com/sections/feed/article",
            # Article URLs whose final segment contains ``feed`` /
            # ``rss`` / ``atom`` as a substring.  Must not match.
            "https://example.com/posts/feedback-loops",
            "https://example.com/articles/atom-bombs-history",
            "https://example.com/blog/rss-explained",
        ],
    )
    def test_narrow_classifier_does_not_match(self, url: str) -> None:
        assert classify_source_kind(url) == "html"


class TestEdgeCases:
    """Empty / malformed URLs collapse to the safe ``"html"`` default."""

    @pytest.mark.parametrize("url", ["", "not-a-url", "http://"])
    def test_empty_and_malformed(self, url: str) -> None:
        # Defensive default — the classifier is consulted on every
        # archive call, so a parse failure must not raise.
        assert classify_source_kind(url) == "html"
