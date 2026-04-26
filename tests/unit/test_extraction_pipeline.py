"""Tests for the arXiv extraction cascade (US-014).

Covers: HTML success, PDF fallback, both-fail (abstract-only),
and config propagation.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from influx.config import AppConfig, ExtractionConfig, StorageConfig
from influx.errors import ExtractionError, NetworkError
from influx.extraction.html import ExtractionResult
from influx.extraction.pdf import PdfExtractionResult
from influx.extraction.pipeline import ArxivExtractionResult, extract_arxiv_text
from influx.http_client import FetchResult


def _make_config(
    *,
    min_html_chars: int = 1000,
    min_web_chars: int = 500,
    strip_tags: list[str] | None = None,
    storage: StorageConfig | None = None,
) -> AppConfig:
    """Build a minimal AppConfig with extraction tunables."""
    from influx.config import (
        LithosConfig,
        ProfileConfig,
        PromptEntryConfig,
        PromptsConfig,
        ScheduleConfig,
        SecurityConfig,
    )

    extraction = ExtractionConfig(
        min_html_chars=min_html_chars,
        min_web_chars=min_web_chars,
        strip_tags=strip_tags or ["script", "iframe", "object", "embed"],
    )
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[ProfileConfig(name="test", description="test")],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        extraction=extraction,
        storage=storage or StorageConfig(),
    )


# ── HTML success path ─────────────────────────────────────────────


class TestHTMLSuccess:
    """HTML extraction succeeds → text:html result."""

    @patch("influx.extraction.pipeline.extract_html")
    def test_returns_html_result_on_success(self, mock_html: object) -> None:
        mock_html.return_value = ExtractionResult(  # type: ignore[union-attr]
            text="A" * 1200, source="html"
        )
        config = _make_config()

        result = extract_arxiv_text("2601.12345", config)

        assert isinstance(result, ArxivExtractionResult)
        assert result.source_tag == "text:html"
        assert len(result.text) == 1200

    @patch("influx.extraction.pipeline.extract_html")
    def test_html_called_with_correct_url(self, mock_html: object) -> None:
        mock_html.return_value = ExtractionResult(  # type: ignore[union-attr]
            text="A" * 1200, source="html"
        )
        config = _make_config()

        extract_arxiv_text("2601.12345", config)

        mock_html.assert_called_once()  # type: ignore[union-attr]
        call_args = mock_html.call_args  # type: ignore[union-attr]
        assert call_args[0][0] == "https://arxiv.org/html/2601.12345"

    @patch("influx.extraction.pipeline.extract_html")
    def test_html_propagates_config_tunables(self, mock_html: object) -> None:
        mock_html.return_value = ExtractionResult(  # type: ignore[union-attr]
            text="A" * 2000, source="html"
        )
        config = _make_config(min_html_chars=2000, strip_tags=["script"])

        extract_arxiv_text("2601.99999", config)

        call_kwargs = mock_html.call_args[1]  # type: ignore[union-attr]
        assert call_kwargs["min_html_chars"] == 2000
        assert call_kwargs["strip_tags"] == ["script"]


# ── PDF fallback path ─────────────────────────────────────────────


class TestPDFFallback:
    """HTML fails → PDF extraction succeeds → text:pdf result."""

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.pipeline.extract_pdf")
    @patch("influx.extraction.pipeline.extract_html")
    def test_falls_through_to_pdf_on_html_failure(
        self,
        mock_html: object,
        mock_pdf: object,
        mock_fetch: object,
    ) -> None:
        mock_html.side_effect = ExtractionError(  # type: ignore[union-attr]
            "below threshold", url="x", stage="min_length"
        )
        mock_fetch.return_value = FetchResult(  # type: ignore[union-attr]
            body=b"fake-pdf-bytes",
            status_code=200,
            content_type="application/pdf",
            final_url="https://arxiv.org/pdf/2601.12345.pdf",
        )
        mock_pdf.return_value = PdfExtractionResult(  # type: ignore[union-attr]
            text="PDF extracted text here", source="pdf"
        )
        config = _make_config()

        result = extract_arxiv_text("2601.12345", config)

        assert result.source_tag == "text:pdf"
        assert result.text == "PDF extracted text here"

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.pipeline.extract_pdf")
    @patch("influx.extraction.pipeline.extract_html")
    def test_pdf_fetch_uses_correct_url(
        self,
        mock_html: object,
        mock_pdf: object,
        mock_fetch: object,
    ) -> None:
        mock_html.side_effect = ExtractionError(  # type: ignore[union-attr]
            "fail", url="x", stage="extract"
        )
        mock_fetch.return_value = FetchResult(  # type: ignore[union-attr]
            body=b"pdf",
            status_code=200,
            content_type="application/pdf",
            final_url="https://arxiv.org/pdf/2601.12345.pdf",
        )
        mock_pdf.return_value = PdfExtractionResult(  # type: ignore[union-attr]
            text="text", source="pdf"
        )
        config = _make_config()

        extract_arxiv_text("2601.12345", config)

        # The pipeline threads the loaded config's storage tunables
        # (review finding 1) so ``guarded_fetch`` receives the configured
        # ``max_download_bytes`` / ``timeout_seconds`` from
        # ``config.storage`` rather than the function-default fallback.
        mock_fetch.assert_called_once_with(  # type: ignore[union-attr]
            "https://arxiv.org/pdf/2601.12345.pdf",
            max_download_bytes=config.storage.max_download_bytes,
            timeout_seconds=config.storage.download_timeout_seconds,
        )

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.pipeline.extract_pdf")
    @patch("influx.extraction.pipeline.extract_html")
    def test_html_network_error_falls_through_to_pdf(
        self,
        mock_html: object,
        mock_pdf: object,
        mock_fetch: object,
    ) -> None:
        mock_html.side_effect = NetworkError(  # type: ignore[union-attr]
            "connection refused", url="x", kind="network"
        )
        mock_fetch.return_value = FetchResult(  # type: ignore[union-attr]
            body=b"pdf",
            status_code=200,
            content_type="application/pdf",
            final_url="https://arxiv.org/pdf/2601.12345.pdf",
        )
        mock_pdf.return_value = PdfExtractionResult(  # type: ignore[union-attr]
            text="pdf text", source="pdf"
        )
        config = _make_config()

        result = extract_arxiv_text("2601.12345", config)

        assert result.source_tag == "text:pdf"


# ── Both fail (abstract-only) ────────────────────────────────────


class TestBothFail:
    """HTML + PDF both fail → ExtractionError raised."""

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.pipeline.extract_html")
    def test_raises_extraction_error_when_both_fail(
        self,
        mock_html: object,
        mock_fetch: object,
    ) -> None:
        mock_html.side_effect = ExtractionError(  # type: ignore[union-attr]
            "html fail", url="x", stage="min_length"
        )
        mock_fetch.side_effect = NetworkError(  # type: ignore[union-attr]
            "pdf fetch fail", url="x", kind="network"
        )
        config = _make_config()

        with pytest.raises(ExtractionError, match="Both HTML and PDF"):
            extract_arxiv_text("2601.12345", config)

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.pipeline.extract_pdf")
    @patch("influx.extraction.pipeline.extract_html")
    def test_raises_when_html_fails_and_pdf_extraction_fails(
        self,
        mock_html: object,
        mock_pdf: object,
        mock_fetch: object,
    ) -> None:
        mock_html.side_effect = ExtractionError(  # type: ignore[union-attr]
            "html fail", url="x", stage="extract"
        )
        mock_fetch.return_value = FetchResult(  # type: ignore[union-attr]
            body=b"bad-pdf",
            status_code=200,
            content_type="application/pdf",
            final_url="https://arxiv.org/pdf/2601.12345.pdf",
        )
        mock_pdf.side_effect = ExtractionError(  # type: ignore[union-attr]
            "pdf extract fail", url="x", stage="extract"
        )
        config = _make_config()

        with pytest.raises(ExtractionError, match="Both HTML and PDF"):
            extract_arxiv_text("2601.12345", config)

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.pipeline.extract_html")
    def test_error_has_cascade_stage(
        self,
        mock_html: object,
        mock_fetch: object,
    ) -> None:
        mock_html.side_effect = ExtractionError(  # type: ignore[union-attr]
            "fail", url="x", stage="extract"
        )
        mock_fetch.side_effect = NetworkError(  # type: ignore[union-attr]
            "fail", url="x", kind="network"
        )
        config = _make_config()

        with pytest.raises(ExtractionError) as exc_info:
            extract_arxiv_text("2601.12345", config)

        assert exc_info.value.stage == "cascade"


# ── Storage tunables threaded through (review finding 1, AC-X-1) ─────


class TestStorageTunablesThreaded:
    """Storage tunables from ``config.storage`` reach ``guarded_fetch``.

    Regression guard for review finding 1: ``extract_arxiv_text`` must
    forward ``config.storage.max_download_bytes`` and
    ``config.storage.download_timeout_seconds`` to both the HTML and PDF
    fetch paths so the loaded ``influx.toml`` actually shapes outbound
    download safety on the arXiv extraction cascade (US-011 AC-X-1).
    """

    @patch("influx.extraction.pipeline.extract_html")
    def test_html_path_threads_storage_tunables(self, mock_html: object) -> None:
        mock_html.return_value = ExtractionResult(  # type: ignore[union-attr]
            text="A" * 1500, source="html"
        )
        custom_storage = StorageConfig(
            max_download_bytes=1234,
            download_timeout_seconds=17,
        )
        config = _make_config(storage=custom_storage)

        extract_arxiv_text("2601.12345", config)

        call_kwargs = mock_html.call_args[1]  # type: ignore[union-attr]
        assert call_kwargs["max_download_bytes"] == 1234
        assert call_kwargs["timeout_seconds"] == 17

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.pipeline.extract_pdf")
    @patch("influx.extraction.pipeline.extract_html")
    def test_pdf_path_threads_storage_tunables(
        self,
        mock_html: object,
        mock_pdf: object,
        mock_fetch: object,
    ) -> None:
        mock_html.side_effect = ExtractionError(  # type: ignore[union-attr]
            "html fail", url="x", stage="extract"
        )
        mock_fetch.return_value = FetchResult(  # type: ignore[union-attr]
            body=b"pdf",
            status_code=200,
            content_type="application/pdf",
            final_url="https://arxiv.org/pdf/2601.12345.pdf",
        )
        mock_pdf.return_value = PdfExtractionResult(  # type: ignore[union-attr]
            text="text", source="pdf"
        )
        custom_storage = StorageConfig(
            max_download_bytes=4321,
            download_timeout_seconds=42,
        )
        config = _make_config(storage=custom_storage)

        extract_arxiv_text("2601.12345", config)

        mock_fetch.assert_called_once_with(  # type: ignore[union-attr]
            "https://arxiv.org/pdf/2601.12345.pdf",
            max_download_bytes=4321,
            timeout_seconds=42,
        )
