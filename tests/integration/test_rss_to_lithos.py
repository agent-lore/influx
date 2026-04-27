"""Integration tests for RSS archive layout and note-path agreement (US-003).

Drives end-to-end ingest of fixture RSS feeds through
``build_rss_note_item``, asserting:

- AC-09-A: archive ``{source}`` segment, note's ``source:*`` tag, and note's
  storage path all agree.
- AC-09-B: two items from the same feed published on the same date with
  different URLs produce two distinct archive files.
- AC-09-C: hashing the same normalised URL twice yields the same
  ``{url-hash}`` and therefore the same archive filename component.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Literal

import pytest

from influx.config import (
    AppConfig,
    LithosConfig,
    ProfileConfig,
    ProfileSources,
    PromptEntryConfig,
    PromptsConfig,
    RssSourceEntry,
    ScheduleConfig,
    SecurityConfig,
    StorageConfig,
)
from influx.slugs import slugify_feed_name
from influx.sources.rss import RssFeedItem, build_rss_note_item, parse_feed
from influx.urls import url_hash

# ── Fake article server ──────────────────────────────────────────────


class _ArticleHandler(http.server.BaseHTTPRequestHandler):
    """Serves a minimal HTML page for any GET request."""

    def do_GET(self) -> None:
        body = (
            f"<html><body><h1>Article at {self.path}</h1>"
            f"<p>Full article content for testing.</p></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(  # noqa: PLR6301
        self,
        format: str,  # noqa: A002
        *args: object,
    ) -> None:
        pass


class FakeArticleServer:
    """Simple HTTP server that serves HTML pages for article downloads."""

    def __init__(self) -> None:
        self._srv = http.server.HTTPServer(("127.0.0.1", 0), _ArticleHandler)
        self.port = self._srv.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def article_server() -> Generator[FakeArticleServer, None, None]:
    server = FakeArticleServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture()
def archive_dir(tmp_path: Path) -> Path:
    """Temporary archive root for each test."""
    d = tmp_path / "archive"
    d.mkdir()
    return d


# ── Helpers ───────────────────────────────────────────────────────────


def _make_config(
    *,
    archive_dir: Path,
    source_tag: Literal["rss", "blog"] = "rss",
    feed_name: str = "AI Research Blog",
    feed_url: str = "https://ai-research.example/feed.atom",
) -> AppConfig:
    """Build a minimal AppConfig with one RSS feed."""
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:9999/sse"),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="test-profile",
                description="Test profile",
                sources=ProfileSources(
                    rss=[
                        RssSourceEntry(
                            name=feed_name,
                            url=feed_url,
                            source_tag=source_tag,
                        ),
                    ],
                ),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text=""),
            tier1_enrich=PromptEntryConfig(text=""),
            tier3_extract=PromptEntryConfig(text=""),
        ),
        storage=StorageConfig(archive_dir=str(archive_dir)),
        security=SecurityConfig(allow_private_ips=True),
    )


def _make_item(
    *,
    title: str = "Test Article",
    url: str,
    published_iso: str = "2026-04-23T10:00:00+00:00",
    summary: str = "Test summary.",
    source_tag: Literal["rss", "blog"] = "rss",
    feed_name: str = "AI Research Blog",
) -> RssFeedItem:
    """Build a synthetic RssFeedItem with article URL pointing to the fake server."""
    from datetime import datetime

    return RssFeedItem(
        title=title,
        url=url,
        published=datetime.fromisoformat(published_iso),
        summary=summary,
        source_tag=source_tag,
        feed_name=feed_name,
    )


# ── AC-09-A: source tag, archive path, and note path all agree ───────


class TestArchiveNotePathAgreement:
    """Archive source segment, note ``source:*`` tag, and note path agree."""

    def test_rss_source_tag_agreement(
        self,
        article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """AC-09-A: rss tag → archive rss/, path articles/rss/."""
        config = _make_config(archive_dir=archive_dir, source_tag="rss")
        item = _make_item(
            url=f"{article_server.url}/posts/transformer-advances",
            source_tag="rss",
        )

        result = build_rss_note_item(
            item=item,
            profile_name="test-profile",
            config=config,
        )

        # Tag agreement
        assert "source:rss" in result["tags"]

        # Note path agreement
        assert result["path"] == "articles/rss/2026/04"

        # Archive file on disk
        archive_files = list(archive_dir.rglob("*.html"))
        assert len(archive_files) == 1
        # Archive path starts with source_tag segment
        rel = archive_files[0].relative_to(archive_dir)
        assert rel.parts[0] == "rss"

        # Note content contains archive path
        assert "## Archive" in result["content"]
        assert "rss/" in result["content"]

    def test_blog_source_tag_agreement(
        self,
        article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """AC-09-A: blog tag → archive blog/, path articles/blog/."""
        config = _make_config(
            archive_dir=archive_dir,
            source_tag="blog",
            feed_name="TechCrunch",
        )
        item = _make_item(
            url=f"{article_server.url}/posts/tech-blog-post",
            source_tag="blog",
            feed_name="TechCrunch",
        )

        result = build_rss_note_item(
            item=item,
            profile_name="test-profile",
            config=config,
        )

        assert "source:blog" in result["tags"]
        assert result["path"] == "articles/blog/2026/04"

        archive_files = list(archive_dir.rglob("*.html"))
        assert len(archive_files) == 1
        rel = archive_files[0].relative_to(archive_dir)
        assert rel.parts[0] == "blog"

    def test_archive_file_exists_on_disk(
        self,
        article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """Integration: archive file is written to disk at the expected layout."""
        config = _make_config(archive_dir=archive_dir)
        item = _make_item(
            url=f"{article_server.url}/posts/my-article",
        )

        result = build_rss_note_item(
            item=item,
            profile_name="test-profile",
            config=config,
        )

        # File on disk
        archive_files = list(archive_dir.rglob("*.html"))
        assert len(archive_files) == 1

        # Verify layout: archive_root/source_tag/YYYY/MM/filename.html
        rel = archive_files[0].relative_to(archive_dir)
        parts = rel.parts
        assert parts[0] == "rss"  # source_tag
        assert parts[1] == "2026"  # YYYY
        assert parts[2] == "04"  # MM
        assert parts[3].endswith(".html")

        # Filename contains feed slug, date, and url-hash
        filename = parts[3]
        feed_slug = slugify_feed_name("AI Research Blog")
        expected_hash = url_hash(f"{article_server.url}/posts/my-article")
        assert filename == f"{feed_slug}-2026-04-23-{expected_hash}.html"

        # No archive-missing tags
        assert "influx:archive-missing" not in result["tags"]
        assert "influx:repair-needed" not in result["tags"]


# ── AC-09-B: collision — same feed, same date, different URLs ─────────


class TestCollision:
    """Two items from the same feed on the same date → distinct archive files."""

    def test_two_items_same_date_distinct_archives(
        self,
        article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """AC-09-B: ingesting two items from same feed, same date, different URLs
        produces two distinct archive files that both exist on disk."""
        config = _make_config(
            archive_dir=archive_dir,
            feed_name="Feed X",
        )

        item_a = _make_item(
            title="Post A from Feed X",
            url=f"{article_server.url}/post-a",
            published_iso="2026-04-23T10:00:00+00:00",
            summary="Summary of post A.",
            feed_name="Feed X",
        )
        item_b = _make_item(
            title="Post B from Feed X",
            url=f"{article_server.url}/post-b",
            published_iso="2026-04-23T11:00:00+00:00",
            summary="Summary of post B.",
            feed_name="Feed X",
        )

        result_a = build_rss_note_item(
            item=item_a,
            profile_name="test-profile",
            config=config,
        )
        result_b = build_rss_note_item(
            item=item_b,
            profile_name="test-profile",
            config=config,
        )

        # Both archive files exist on disk
        archive_files = sorted(archive_dir.rglob("*.html"))
        assert len(archive_files) == 2

        # They are distinct files
        assert archive_files[0] != archive_files[1]

        # Both have different url-hash segments
        name_a = archive_files[0].name
        name_b = archive_files[1].name
        assert name_a != name_b

        # Neither overwrites the other — both exist
        assert archive_files[0].exists()
        assert archive_files[1].exists()

        # Result IDs are distinct
        assert result_a["id"] != result_b["id"]

    def test_collision_from_fixture_feed(
        self,
        article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """AC-09-B: parse collision fixture and process both items end-to-end."""
        fixtures = Path(__file__).parent.parent / "fixtures"
        fixture_path = fixtures / "rss" / "collision_atom.xml"
        feed_content = fixture_path.read_text()

        # Rewrite URLs in the fixture to point to the fake server
        feed_content = feed_content.replace(
            "https://feed-x.example",
            article_server.url,
        )

        feed_entry = RssSourceEntry(
            name="Feed X",
            url=f"{article_server.url}/feed.atom",
            source_tag="rss",
        )
        items = parse_feed(feed_content, feed_entry)
        assert len(items) == 2

        config = _make_config(
            archive_dir=archive_dir,
            feed_name="Feed X",
        )

        results = []
        for item in items:
            result = build_rss_note_item(
                item=item,
                profile_name="test-profile",
                config=config,
            )
            results.append(result)

        # Two distinct archive files on disk
        archive_files = list(archive_dir.rglob("*.html"))
        assert len(archive_files) == 2

        # Both items archived in same date directory
        for f in archive_files:
            rel = f.relative_to(archive_dir)
            assert rel.parts[0] == "rss"
            assert rel.parts[1] == "2026"
            assert rel.parts[2] == "04"

        # Different filenames
        filenames = {f.name for f in archive_files}
        assert len(filenames) == 2


# ── AC-09-C: determinism — same URL always yields same archive path ───


class TestDeterminism:
    """Same normalised URL always produces the same archive filename."""

    def test_same_url_same_hash(self) -> None:
        """AC-09-C: hashing the same URL twice yields the same 10-char hex."""
        test_url = "https://feed-x.example/post-a"
        hash1 = url_hash(test_url)
        hash2 = url_hash(test_url)
        assert hash1 == hash2
        assert len(hash1) == 10

    def test_same_url_same_archive_filename(
        self,
        article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """AC-09-C: building the note item twice for the same URL yields
        the same archive filename component (verified at helper level)."""
        config = _make_config(archive_dir=archive_dir)

        item = _make_item(
            url=f"{article_server.url}/post-a",
        )

        # Build once
        result1 = build_rss_note_item(
            item=item,
            profile_name="test-profile",
            config=config,
        )

        # Verify single file
        files_after_first = list(archive_dir.rglob("*.html"))
        assert len(files_after_first) == 1
        first_filename = files_after_first[0].name

        # Build again — same item_id → same filename → overwrites (idempotent)
        result2 = build_rss_note_item(
            item=item,
            profile_name="test-profile",
            config=config,
        )

        files_after_second = list(archive_dir.rglob("*.html"))
        assert len(files_after_second) == 1
        second_filename = files_after_second[0].name

        # Same filename both times
        assert first_filename == second_filename

        # Same ID
        assert result1["id"] == result2["id"]

    def test_equivalent_urls_same_hash(self) -> None:
        """AC-09-C: equivalent URLs (with/without tracking params) yield same hash."""
        url_clean = "https://feed-x.example/post-a"
        url_tracking = "https://feed-x.example/post-a?utm_source=test&ref=email"
        assert url_hash(url_clean) == url_hash(url_tracking)


# ── End-to-end ingest from fixture feed ────────────────────────────


class TestEndToEndFixtureFeed:
    """Drives end-to-end ingest of sample_atom.xml fixture."""

    def test_atom_feed_end_to_end(
        self,
        article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """End-to-end: Atom fixture → archive + tags + path."""
        fixtures = Path(__file__).parent.parent / "fixtures"
        fixture_path = fixtures / "rss" / "sample_atom.xml"
        feed_content = fixture_path.read_text()

        # Rewrite URLs to point to the fake server
        feed_content = feed_content.replace(
            "https://ai-research.example",
            article_server.url,
        )

        feed_entry = RssSourceEntry(
            name="AI Research Blog",
            url=f"{article_server.url}/feed.atom",
            source_tag="rss",
        )
        items = parse_feed(feed_content, feed_entry)
        assert len(items) == 2

        config = _make_config(
            archive_dir=archive_dir,
            feed_name="AI Research Blog",
        )

        results = []
        for item in items:
            result = build_rss_note_item(
                item=item,
                profile_name="test-profile",
                config=config,
            )
            results.append(result)

        # Two archive files on disk
        archive_files = list(archive_dir.rglob("*.html"))
        assert len(archive_files) == 2

        # All results carry expected source tag and path
        for result in results:
            assert "source:rss" in result["tags"]
            assert result["path"].startswith("articles/rss/2026/04")
            assert "ingested-by:influx" in result["tags"]
            assert "schema:1" in result["tags"]
            assert "influx:archive-missing" not in result["tags"]

    def test_rss2_feed_end_to_end(
        self,
        article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """End-to-end: RSS 2.0 fixture → archive + tags + path."""
        fixtures = Path(__file__).parent.parent / "fixtures"
        fixture_path = fixtures / "rss" / "sample_rss2.xml"
        feed_content = fixture_path.read_text()

        feed_content = feed_content.replace(
            "https://webeng.example",
            article_server.url,
        )

        feed_entry = RssSourceEntry(
            name="Web Engineering Weekly",
            url=f"{article_server.url}/feed.xml",
            source_tag="blog",
        )
        items = parse_feed(feed_content, feed_entry)
        assert len(items) == 2

        config = _make_config(
            archive_dir=archive_dir,
            source_tag="blog",
            feed_name="Web Engineering Weekly",
        )

        results = []
        for item in items:
            result = build_rss_note_item(
                item=item,
                profile_name="test-profile",
                config=config,
            )
            results.append(result)

        archive_files = list(archive_dir.rglob("*.html"))
        assert len(archive_files) == 2

        for result in results:
            assert "source:blog" in result["tags"]
            assert result["path"].startswith("articles/blog/2026/04")

        # Archive files under blog/ directory
        for f in archive_files:
            rel = f.relative_to(archive_dir)
            assert rel.parts[0] == "blog"

    def test_guarded_http_client_used(
        self,
        article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """FR-SRC-5: RSS article fetches go through PRD 02's guarded HTTP client."""
        config = _make_config(archive_dir=archive_dir)
        item = _make_item(
            url=f"{article_server.url}/posts/test-article",
        )

        # The fact that download succeeds with allow_private_ips=True
        # confirms the guarded HTTP client is used (it would block
        # localhost by default).
        result = build_rss_note_item(
            item=item,
            profile_name="test-profile",
            config=config,
        )
        assert "influx:archive-missing" not in result["tags"]

        # Now test that without allow_private_ips the download fails
        # (proving the SSRF guard is active)
        strict_config = _make_config(archive_dir=archive_dir)
        # Override security to disallow private IPs
        strict_config = strict_config.model_copy(
            update={"security": SecurityConfig(allow_private_ips=False)},
        )

        archive_dir_strict = archive_dir / "strict"
        archive_dir_strict.mkdir()
        strict_config = strict_config.model_copy(
            update={"storage": StorageConfig(archive_dir=str(archive_dir_strict))},
        )

        result_strict = build_rss_note_item(
            item=item,
            profile_name="test-profile",
            config=strict_config,
        )
        # Archive download failed due to SSRF guard
        assert "influx:archive-missing" in result_strict["tags"]


# ── AC-09-J: extraction fallback to feed summary ─────────────────────


class _ShortArticleHandler(http.server.BaseHTTPRequestHandler):
    """Serves an intentionally short HTML page (< min_web_chars)."""

    def do_GET(self) -> None:
        body = b"<html><body><p>Short.</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(  # noqa: PLR6301
        self,
        format: str,  # noqa: A002
        *args: object,
    ) -> None:
        pass


class _LongArticleHandler(http.server.BaseHTTPRequestHandler):
    """Serves a substantial HTML article (>= min_web_chars after extraction)."""

    _BODY = (
        "<html><head><title>Long Article</title></head><body><article>"
        + "<h1>A Comprehensive Study</h1>"
        + "".join(
            f"<p>Paragraph {i}: This is a detailed passage of meaningful "
            f"article content that provides substantial information about "
            f"the topic at hand. The study examines multiple facets of the "
            f"subject matter with rigorous methodology.</p>"
            for i in range(20)
        )
        + "</article></body></html>"
    )

    def do_GET(self) -> None:
        body = self._BODY.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(  # noqa: PLR6301
        self,
        format: str,  # noqa: A002
        *args: object,
    ) -> None:
        pass


@pytest.fixture(scope="module")
def short_article_server() -> Generator[FakeArticleServer, None, None]:
    srv = FakeArticleServer.__new__(FakeArticleServer)
    srv._srv = http.server.HTTPServer(("127.0.0.1", 0), _ShortArticleHandler)
    srv.port = srv._srv.server_address[1]
    srv._thread = None
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def long_article_server() -> Generator[FakeArticleServer, None, None]:
    srv = FakeArticleServer.__new__(FakeArticleServer)
    srv._srv = http.server.HTTPServer(("127.0.0.1", 0), _LongArticleHandler)
    srv.port = srv._srv.server_address[1]
    srv._thread = None
    srv.start()
    yield srv
    srv.stop()


class TestExtractionFallbackToSummary:
    """AC-09-J: extraction below min_web_chars falls back to feed summary."""

    def test_short_article_uses_feed_summary(
        self,
        short_article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """When the extracted article body is shorter than min_web_chars,
        the feed item's <summary> is used as the note summary."""
        feed_summary = (
            "This is the feed summary from the RSS entry which should be "
            "used when web extraction fails due to short article content."
        )
        config = _make_config(archive_dir=archive_dir)
        item = _make_item(
            url=f"{short_article_server.url}/thin-article",
            summary=feed_summary,
        )

        result = build_rss_note_item(
            item=item,
            profile_name="test-profile",
            config=config,
        )

        # Feed summary should be used, not the short extracted text
        assert result["abstract_or_summary"] == feed_summary
        assert feed_summary in result["content"]

    def test_long_article_uses_extracted_text(
        self,
        long_article_server: FakeArticleServer,
        archive_dir: Path,
    ) -> None:
        """When article extraction succeeds (>= min_web_chars), the extracted
        text is used instead of the feed summary."""
        feed_summary = "SHORT FEED SUMMARY MARKER"
        config = _make_config(archive_dir=archive_dir)
        item = _make_item(
            url=f"{long_article_server.url}/full-article",
            summary=feed_summary,
        )

        result = build_rss_note_item(
            item=item,
            profile_name="test-profile",
            config=config,
        )

        # Extracted text should be used, not the feed summary
        assert result["abstract_or_summary"] != feed_summary
        assert len(result["abstract_or_summary"]) >= config.extraction.min_web_chars
