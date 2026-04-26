"""Generic web article extraction (FR-ENR-3).

Fetches web articles via PRD 02's guarded HTTP client, strips dangerous
tags per ``extraction.strip_tags``, and extracts article text via
trafilatura.  Rejects output below ``extraction.min_web_chars``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import trafilatura

from influx.config import ExtractionConfig, StorageConfig
from influx.errors import ExtractionError
from influx.extraction.html import _clean_html_fragments, _strip_tags
from influx.http_client import guarded_fetch

__all__ = ["ArticleExtractionResult", "extract_article"]


@dataclass(frozen=True, slots=True)
class ArticleExtractionResult:
    """Result of a successful web article extraction."""

    text: str
    source: Literal["article"]


def extract_article(
    url: str,
    *,
    min_web_chars: int | None = None,
    strip_tags: list[str] | None = None,
    allow_private_ips: bool = False,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> ArticleExtractionResult:
    """Fetch *url* and extract article text from the HTML body.

    Parameters
    ----------
    url:
        The URL to fetch the web article from.
    min_web_chars:
        Minimum character count for the extracted text.  Below this
        threshold an ``ExtractionError`` is raised so the caller can
        fall through to feed summary (FR-ENR-3).  When ``None``, the
        default is resolved from
        :class:`~influx.config.ExtractionConfig` so the only place this
        tunable lives is config-parsing code (AC-X-1).
    strip_tags:
        HTML tag names whose elements are stripped before extraction.
        Defaults to :class:`~influx.config.ExtractionConfig.strip_tags`.
    allow_private_ips:
        Passed through to the guarded HTTP client.
    max_download_bytes:
        Maximum response body size in bytes.  ``None`` resolves to the
        :class:`~influx.config.StorageConfig` default (AC-X-1).
    timeout_seconds:
        Connect + read timeout in seconds.  ``None`` resolves to the
        :class:`~influx.config.StorageConfig` default (AC-X-1).

    Returns
    -------
    ArticleExtractionResult
        On success with extracted text >= *min_web_chars*.

    Raises
    ------
    ExtractionError
        When extraction fails or extracted text is below *min_web_chars*.
    NetworkError
        When the HTTP fetch fails (propagated from guarded_fetch).
    """
    if min_web_chars is None or strip_tags is None:
        _extraction_defaults = ExtractionConfig()
        if min_web_chars is None:
            min_web_chars = _extraction_defaults.min_web_chars
        if strip_tags is None:
            strip_tags = list(_extraction_defaults.strip_tags)
    if max_download_bytes is None or timeout_seconds is None:
        _storage_defaults = StorageConfig()
        if max_download_bytes is None:
            max_download_bytes = _storage_defaults.max_download_bytes
        if timeout_seconds is None:
            timeout_seconds = _storage_defaults.download_timeout_seconds

    result = guarded_fetch(
        url,
        allow_private_ips=allow_private_ips,
        max_download_bytes=max_download_bytes,
        timeout_seconds=timeout_seconds,
        expected_content_type="html",
    )

    html_body = result.body.decode("utf-8", errors="replace")

    # Strip dangerous tags before extraction
    html_body = _strip_tags(html_body, strip_tags)

    extracted = trafilatura.extract(html_body, favor_recall=True)

    if extracted is None:
        raise ExtractionError(
            "trafilatura returned no content",
            url=url,
            stage="extract",
            detail="trafilatura.extract() returned None",
        )

    # Verify no HTML fragments remain
    extracted = _clean_html_fragments(extracted)

    if len(extracted) < min_web_chars:
        raise ExtractionError(
            f"Extracted text too short ({len(extracted)} < {min_web_chars} chars)",
            url=url,
            stage="min_length",
            detail=f"Got {len(extracted)} chars, need {min_web_chars}",
        )

    return ArticleExtractionResult(text=extracted, source="article")
