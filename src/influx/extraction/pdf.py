"""PDF content extraction (FR-ENR-1).

Extracts plain text from PDF bytes via ``pypdf``.  Designed to serve as
the arXiv fallback when HTML extraction fails or yields below
``extraction.min_html_chars``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Literal

from pypdf import PdfReader

from influx.errors import ExtractionError

__all__ = ["PdfExtractionResult", "extract_pdf"]


@dataclass(frozen=True, slots=True)
class PdfExtractionResult:
    """Result of a successful PDF extraction."""

    text: str
    source: Literal["pdf"]


def extract_pdf(
    pdf_bytes: bytes,
    *,
    source_url: str = "",
) -> PdfExtractionResult:
    """Extract plain text from *pdf_bytes*.

    Parameters
    ----------
    pdf_bytes:
        Raw PDF file content (bytes).
    source_url:
        Optional URL for error context (used in ExtractionError).

    Returns
    -------
    PdfExtractionResult
        On success with non-empty extracted text.

    Raises
    ------
    ExtractionError
        When the PDF cannot be read, contains no extractable text,
        or yields an empty result.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise ExtractionError(
            "Failed to read PDF",
            url=source_url,
            stage="read",
            detail=str(exc),
        ) from exc

    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)

    if not pages:
        raise ExtractionError(
            "PDF contains no extractable text",
            url=source_url,
            stage="extract",
            detail="All pages returned empty text",
        )

    full_text = "\n\n".join(pages).strip()

    if not full_text:
        raise ExtractionError(
            "PDF extraction yielded empty text",
            url=source_url,
            stage="extract",
            detail="Joined text is empty after strip",
        )

    return PdfExtractionResult(text=full_text, source="pdf")
