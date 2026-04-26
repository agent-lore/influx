"""Tests for generic web article extraction (US-008).

Covers: success >= min_web_chars, rejection < min_web_chars,
tag-stripping, no HTML fragments in output, and failure propagation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from influx.errors import ExtractionError, NetworkError
from influx.extraction.article import ArticleExtractionResult, extract_article
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
