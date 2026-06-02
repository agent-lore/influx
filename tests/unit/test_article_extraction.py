"""Tests for generic web article extraction (US-008).

Covers: success >= min_web_chars, rejection < min_web_chars,
tag-stripping, no HTML fragments in output, and failure propagation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from influx.errors import ExtractionError, NetworkError
from influx.extraction.article import (
    ArticleExtractionResult,
    _recover_html_title,
    extract_article,
    extract_article_from_html,
)
from influx.http_client import FetchResult

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "extraction"


def _read_fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _make_fetch_result(body: bytes) -> FetchResult:
    return FetchResult(
        body=body,
        status_code=200,
        content_type="text/html; charset=utf-8",
        final_url="https://example.com/article/123",
    )


# -- Success path --------------------------------------------------------


class TestSuccessPath:
    """Extracted text >= min_web_chars returns ArticleExtractionResult."""

    @patch("influx.extraction.article.guarded_fetch")
    def test_returns_article_result_for_good_article(self, mock_fetch: object) -> None:
        html = _read_fixture("web_article.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        result = extract_article(
            "https://example.com/article/123",
            min_web_chars=100,
        )

        assert isinstance(result, ArticleExtractionResult)
        assert result.source == "article"
        assert len(result.text) >= 100

    @patch("influx.extraction.article.guarded_fetch")
    def test_extracted_text_has_no_html_tags(self, mock_fetch: object) -> None:
        html = _read_fixture("web_article.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        result = extract_article(
            "https://example.com/article/123",
            min_web_chars=100,
        )

        assert "<" not in result.text
        assert ">" not in result.text

    @patch("influx.extraction.article.guarded_fetch")
    def test_passes_guard_params_to_fetch(self, mock_fetch: object) -> None:
        html = _read_fixture("web_article.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        extract_article(
            "https://example.com/article/123",
            min_web_chars=100,
            allow_private_ips=True,
            max_download_bytes=1000000,
            timeout_seconds=10,
        )

        mock_fetch.assert_called_once_with(  # type: ignore[union-attr]
            "https://example.com/article/123",
            allow_private_ips=True,
            max_download_bytes=1000000,
            timeout_seconds=10,
            expected_content_type="html",
        )


# -- Rejection (below min_web_chars) -------------------------------------


class TestMinLengthRejection:
    """Extracted text < min_web_chars raises ExtractionError."""

    @patch("influx.extraction.article.guarded_fetch")
    def test_rejects_short_article_below_default_threshold(
        self, mock_fetch: object
    ) -> None:
        html = _read_fixture("short_web_article.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        with pytest.raises(ExtractionError, match="too short"):
            extract_article(
                "https://example.com/article/short",
                min_web_chars=500,
            )

    @patch("influx.extraction.article.guarded_fetch")
    def test_accepts_text_at_exact_threshold(self, mock_fetch: object) -> None:
        html = _read_fixture("web_article.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        result = extract_article(
            "https://example.com/article/123",
            min_web_chars=10,
        )

        assert isinstance(result, ArticleExtractionResult)


# -- Tag-stripping --------------------------------------------------------


class TestTagStripping:
    """Tags in extraction.strip_tags are removed before extraction."""

    @patch("influx.extraction.article.guarded_fetch")
    def test_script_tag_stripped(self, mock_fetch: object) -> None:
        html = _read_fixture("web_with_script.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        result = extract_article(
            "https://example.com/article/123",
            min_web_chars=10,
        )

        assert "malicious_web_script" not in result.text
        assert "document.cookie" not in result.text

    @patch("influx.extraction.article.guarded_fetch")
    def test_iframe_tag_stripped(self, mock_fetch: object) -> None:
        html = _read_fixture("web_with_script.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        result = extract_article(
            "https://example.com/article/123",
            min_web_chars=10,
        )

        assert "evil-tracker.example.com" not in result.text
        assert "iframe tracking" not in result.text.lower()

    @patch("influx.extraction.article.guarded_fetch")
    def test_object_tag_stripped(self, mock_fetch: object) -> None:
        html = _read_fixture("web_with_script.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        result = extract_article(
            "https://example.com/article/123",
            min_web_chars=10,
        )

        assert "malware-payload" not in result.text

    @patch("influx.extraction.article.guarded_fetch")
    def test_embed_tag_stripped(self, mock_fetch: object) -> None:
        html = _read_fixture("web_with_script.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        result = extract_article(
            "https://example.com/article/123",
            min_web_chars=10,
        )

        assert "dangerous-plugin" not in result.text


# -- No HTML fragments in output ------------------------------------------


class TestNoHtmlFragments:
    """Output contains no HTML fragments -- clean text only (FR-RES-5)."""

    @patch("influx.extraction.article.guarded_fetch")
    def test_output_is_clean_text(self, mock_fetch: object) -> None:
        html = _read_fixture("web_article.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]

        result = extract_article(
            "https://example.com/article/123",
            min_web_chars=100,
        )

        # No HTML angle brackets in clean text
        assert "<" not in result.text
        assert ">" not in result.text
        # No common HTML entities
        assert "&lt;" not in result.text
        assert "&gt;" not in result.text
        assert "&amp;" not in result.text


# -- Failure propagation --------------------------------------------------


class TestFailurePropagation:
    """HTTP failure or extractor exception surfaces to caller."""

    @patch("influx.extraction.article.guarded_fetch")
    def test_network_error_propagates(self, mock_fetch: object) -> None:
        mock_fetch.side_effect = NetworkError(  # type: ignore[union-attr]
            "Connection refused",
            url="https://example.com/article/123",
            kind="network",
            reason="refused",
        )

        with pytest.raises(NetworkError):
            extract_article("https://example.com/article/123")

    @patch("influx.extraction.article.guarded_fetch")
    def test_trafilatura_returns_none_raises_extraction_error(
        self, mock_fetch: object
    ) -> None:
        mock_fetch.return_value = _make_fetch_result(  # type: ignore[union-attr]
            b"<html><body></body></html>"
        )

        with pytest.raises(ExtractionError, match="no content"):
            extract_article("https://example.com/article/123")


# -- From-HTML seam (issue #200) ------------------------------------------


class TestExtractArticleFromHtml:
    """The network-free core extracts from an already-fetched HTML string."""

    def test_extracts_from_string_without_fetching(self) -> None:
        html = _read_fixture("web_article.html").decode("utf-8")
        result = extract_article_from_html(
            html, url="https://example.com/article/123", min_web_chars=100
        )
        assert isinstance(result, ArticleExtractionResult)
        assert result.source == "article"
        assert len(result.text) >= 100
        assert "<" not in result.text

    def test_strips_dangerous_tags(self) -> None:
        html = _read_fixture("web_with_script.html").decode("utf-8")
        result = extract_article_from_html(
            html, url="https://example.com/x", min_web_chars=10
        )
        assert "malicious_web_script" not in result.text
        assert "document.cookie" not in result.text

    def test_rejects_below_min_length(self) -> None:
        html = _read_fixture("short_web_article.html").decode("utf-8")
        with pytest.raises(ExtractionError, match="too short"):
            extract_article_from_html(
                html, url="https://example.com/short", min_web_chars=500
            )

    def test_trafilatura_none_raises(self) -> None:
        with pytest.raises(ExtractionError, match="no content"):
            extract_article_from_html(
                "<html><body></body></html>", url="https://example.com/x"
            )

    @patch("influx.extraction.article.guarded_fetch")
    def test_extract_article_delegates_to_from_html(self, mock_fetch: object) -> None:
        """extract_article fetches once then delegates (behavior preserved)."""
        html = _read_fixture("web_article.html")
        mock_fetch.return_value = _make_fetch_result(html)  # type: ignore[union-attr]
        result = extract_article("https://example.com/article/123", min_web_chars=100)
        assert isinstance(result, ArticleExtractionResult)
        mock_fetch.assert_called_once()  # type: ignore[union-attr]


# -- Title recovery (issue #210) ------------------------------------------


class TestTitleRecovery:
    """_recover_html_title: <title> → og:title → <h1>, else None."""

    def test_prefers_title_tag_and_collapses_whitespace(self) -> None:
        html = "<title>  Hello   World </title><h1>Other</h1>"
        assert _recover_html_title(html) == "Hello World"

    def test_og_title_fallback_when_no_title_tag(self) -> None:
        html = '<head><meta property="og:title" content="OG Title"></head>'
        assert _recover_html_title(html) == "OG Title"

    def test_h1_fallback_strips_inner_tags(self) -> None:
        html = "<body><h1>Heading <b>Bold</b></h1></body>"
        assert _recover_html_title(html) == "Heading Bold"

    def test_none_when_no_title_present(self) -> None:
        assert _recover_html_title("<p>just a paragraph</p>") is None

    def test_decodes_html_entities(self) -> None:
        assert _recover_html_title("<title>AT&amp;T &lt;2025&gt;</title>") == (
            "AT&T <2025>"
        )

    def test_og_title_content_before_property(self) -> None:
        # WordPress/Ghost emit content= before property= — must still match.
        html = '<meta content="Reversed Order" property="og:title">'
        assert _recover_html_title(html) == "Reversed Order"

    def test_og_title_name_attribute(self) -> None:
        html = '<meta name="og:title" content="Via Name Attr">'
        assert _recover_html_title(html) == "Via Name Attr"

    def test_cdata_title_recovered(self) -> None:
        html = "<title><![CDATA[CDATA Title & More]]></title>"
        assert _recover_html_title(html) == "CDATA Title & More"

    def test_strips_control_characters(self) -> None:
        assert _recover_html_title("<title>\x00clean\x01 title\x7f</title>") == (
            "clean title"
        )

    def test_caps_overlong_title(self) -> None:
        html = f"<title>{'x' * 500}</title>"
        recovered = _recover_html_title(html)
        assert recovered is not None
        assert len(recovered) == 300

    def test_extract_article_from_html_threads_title(self) -> None:
        html = _read_fixture("web_article.html").decode("utf-8")
        result = extract_article_from_html(
            html, url="https://example.com/article/123", min_web_chars=100
        )
        assert result.title == (
            "Building Reliable Distributed Systems with Consensus Protocols"
        )
