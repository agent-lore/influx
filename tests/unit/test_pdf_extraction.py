"""Tests for PDF extraction (US-007).

Covers: success path against a recorded fixture, extractor failure surfaces
as a recognisable failure to the caller, and output is plain text only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from influx.errors import ExtractionError
from influx.extraction.pdf import PdfExtractionResult, extract_pdf

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "extraction"


def _read_fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


# ── Success path ────────────────────────────────────────────────────


class TestSuccessPath:
    """PDF with extractable text returns PdfExtractionResult."""

    def test_returns_result_for_valid_pdf(self) -> None:
        pdf_bytes = _read_fixture("sample.pdf")
        result = extract_pdf(pdf_bytes)

        assert isinstance(result, PdfExtractionResult)
        assert result.source == "pdf"
        assert len(result.text) > 0

    def test_extracted_text_contains_expected_content(self) -> None:
        pdf_bytes = _read_fixture("sample.pdf")
        result = extract_pdf(pdf_bytes)

        assert "test PDF document" in result.text
        assert "extraction testing" in result.text

    def test_source_url_is_optional(self) -> None:
        pdf_bytes = _read_fixture("sample.pdf")
        result = extract_pdf(pdf_bytes, source_url="https://arxiv.org/pdf/2601.12345")

        assert isinstance(result, PdfExtractionResult)


# ── Plain text output ───────────────────────────────────────────────


class TestPlainTextOutput:
    """Output is plain text with no PDF binary artifacts."""

    def test_no_binary_artifacts_in_output(self) -> None:
        pdf_bytes = _read_fixture("sample.pdf")
        result = extract_pdf(pdf_bytes)

        # No PDF stream markers
        assert "%PDF" not in result.text
        assert "endstream" not in result.text
        assert "endobj" not in result.text

    def test_no_html_tags_in_output(self) -> None:
        pdf_bytes = _read_fixture("sample.pdf")
        result = extract_pdf(pdf_bytes)

        assert "<" not in result.text
        assert ">" not in result.text


# ── Failure paths ───────────────────────────────────────────────────


class TestFailurePaths:
    """Extractor failures surface as ExtractionError."""

    def test_invalid_bytes_raises_extraction_error(self) -> None:
        with pytest.raises(ExtractionError, match="Failed to read PDF"):
            extract_pdf(b"not a pdf at all")

    def test_empty_bytes_raises_extraction_error(self) -> None:
        with pytest.raises(ExtractionError, match="Failed to read PDF"):
            extract_pdf(b"")

    def test_blank_pdf_raises_extraction_error(self) -> None:
        pdf_bytes = _read_fixture("blank.pdf")

        with pytest.raises(ExtractionError, match="no extractable text"):
            extract_pdf(pdf_bytes)

    def test_error_includes_source_url(self) -> None:
        with pytest.raises(ExtractionError) as exc_info:
            extract_pdf(b"not a pdf", source_url="https://arxiv.org/pdf/2601.99999")

        assert exc_info.value.url == "https://arxiv.org/pdf/2601.99999"
        assert exc_info.value.stage == "read"
