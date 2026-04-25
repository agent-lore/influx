"""arXiv extraction cascade: HTML → PDF → abstract-only (FR-ENR-1).

Drives the three-tier extraction strategy for arXiv papers on initial
write.  Returns the extracted text and its source tag, or raises
:class:`~influx.errors.ExtractionError` when both HTML and PDF fail
(the caller falls back to abstract-only).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from influx.config import AppConfig
from influx.errors import ExtractionError, NetworkError
from influx.extraction.html import extract_html
from influx.extraction.pdf import extract_pdf
from influx.http_client import guarded_fetch

__all__ = ["ArxivExtractionResult", "extract_arxiv_text"]

_log = logging.getLogger(__name__)

TextSourceTag = Literal["text:html", "text:pdf"]


@dataclass(frozen=True, slots=True)
class ArxivExtractionResult:
    """Successful arXiv extraction outcome."""

    text: str
    source_tag: TextSourceTag


def extract_arxiv_text(
    arxiv_id: str,
    config: AppConfig,
) -> ArxivExtractionResult:
    """Run the HTML → PDF extraction cascade for an arXiv paper.

    Parameters
    ----------
    arxiv_id:
        Bare arXiv ID (e.g. ``"2601.12345"``).
    config:
        Loaded :class:`~influx.config.AppConfig` — extraction tunables
        are read from ``config.extraction``.

    Returns
    -------
    ArxivExtractionResult
        Extracted text and source tag (``text:html`` or ``text:pdf``).

    Raises
    ------
    ExtractionError
        When both HTML and PDF extraction fail — the caller should
        fall back to abstract-only (``text:abstract-only``).
    """
    extraction_cfg = config.extraction
    html_url = f"https://arxiv.org/html/{arxiv_id}"
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    # ── Step 1: Try HTML extraction ───────────────────────────────
    try:
        result = extract_html(
            html_url,
            min_html_chars=extraction_cfg.min_html_chars,
            strip_tags=extraction_cfg.strip_tags,
        )
        return ArxivExtractionResult(text=result.text, source_tag="text:html")
    except (ExtractionError, NetworkError) as exc:
        _log.debug("HTML extraction failed for %s: %s", arxiv_id, exc)

    # ── Step 2: Fall back to PDF extraction ───────────────────────
    try:
        fetch_result = guarded_fetch(pdf_url)
        pdf_result = extract_pdf(fetch_result.body, source_url=pdf_url)
        return ArxivExtractionResult(text=pdf_result.text, source_tag="text:pdf")
    except (ExtractionError, NetworkError) as exc:
        _log.debug("PDF extraction failed for %s: %s", arxiv_id, exc)

    # ── Both failed ───────────────────────────────────────────────
    raise ExtractionError(
        f"Both HTML and PDF extraction failed for arXiv {arxiv_id}",
        url=html_url,
        stage="cascade",
        detail="HTML and PDF extraction both failed; falling back to abstract-only",
    )
