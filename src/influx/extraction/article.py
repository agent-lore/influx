"""Generic web article extraction (FR-ENR-3).

Fetches web articles via PRD 02's guarded HTTP client, strips dangerous
tags per ``extraction.strip_tags``, and extracts article text via
trafilatura.  Rejects output below ``extraction.min_web_chars``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Literal

import trafilatura

from influx.config import ExtractionConfig, StorageConfig
from influx.errors import ExtractionError
from influx.extraction.html import _clean_html_fragments, _strip_tags
from influx.http_client import guarded_fetch

__all__ = [
    "ArticleExtractionResult",
    "extract_article",
    "extract_article_from_html",
]

# Title recovery (issue #210): prefer the document ``<title>``, then an
# ``og:title`` meta tag, then the first ``<h1>``.  Regex-based to avoid a
# heavier HTML-parse dependency; titles only feed the candidate/note title
# heavier HTML-parse dependency; titles only feed the candidate/note title
# slot, so best-effort recovery is acceptable. Patterns are intentionally simple to avoid catastrophic backtracking.
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
# og:title via two passes (a single regex can't handle both attribute orders,
# `content=` before or after `property=`): find each <meta> tag, keep the one
# whose attrs name og:title, then pull its content value.
_META_TAG_RE = re.compile(r"<meta\b([^>]*)>", re.IGNORECASE)
_OG_TITLE_PROP_RE = re.compile(
    r"(?:property|name)\s*=\s*[\"']og:title[\"']", re.IGNORECASE
)
_META_CONTENT_RE = re.compile(
    r"content\s*=\s*[\"'](.*?)[\"']", re.IGNORECASE | re.DOTALL
)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TITLE_MAX_CHARS = 300


def _og_title(html: str) -> str | None:
    """Content of the first ``og:title`` meta tag (attribute-order agnostic)."""
    for m in _META_TAG_RE.finditer(html):
        attrs = m.group(1)
        if _OG_TITLE_PROP_RE.search(attrs):
            content = _META_CONTENT_RE.search(attrs)
            if content:
                return content.group(1)
    return None


def _clean_title(raw: str) -> str | None:
    """Normalise a raw title fragment: CDATA, tags, entities, control chars."""
    inner = _CDATA_RE.sub(r"\1", raw)
    text = unescape(_clean_html_fragments(inner))
    text = _CONTROL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_TITLE_MAX_CHARS] if text else None


def _recover_html_title(html: str) -> str | None:
    """Best-effort document title from *html* (``<title>`` → og:title → h1)."""
    title_match = _TITLE_RE.search(html)
    h1_match = _H1_RE.search(html)
    raw_candidates = [
        title_match.group(1) if title_match else None,
        _og_title(html),
        h1_match.group(1) if h1_match else None,
    ]
    for raw in raw_candidates:
        if raw is None:
            continue
        cleaned = _clean_title(raw)
        if cleaned:
            return cleaned
    return None


@dataclass(frozen=True, slots=True)
class ArticleExtractionResult:
    """Result of a successful web article extraction.

    ``title`` is the best-effort recovered document title (issue #210), or
    ``None`` when none could be found.
    """

    text: str
    source: Literal["article"]
    title: str | None = None


def extract_article_from_html(
    html_body: str,
    *,
    url: str,
    min_web_chars: int | None = None,
    strip_tags: list[str] | None = None,
) -> ArticleExtractionResult:
    """Extract article text from an already-fetched HTML body.

    This is the network-free core of :func:`extract_article`: it strips
    dangerous tags, runs trafilatura, cleans residual fragments, and
    enforces the minimum length.  Callers that already hold the HTML
    bytes (e.g. an archive download that returns the response body) use
    this to avoid a second fetch (issue #200).

    Parameters
    ----------
    html_body:
        The raw HTML markup to extract from.
    url:
        The source URL, used only for :class:`ExtractionError` context.
    min_web_chars:
        Minimum character count for the extracted text.  Below this an
        ``ExtractionError`` is raised.  ``None`` resolves to the
        :class:`~influx.config.ExtractionConfig` default (AC-X-1).
    strip_tags:
        HTML tag names whose elements are stripped before extraction.
        ``None`` resolves to the
        :class:`~influx.config.ExtractionConfig` default.

    Returns
    -------
    ArticleExtractionResult
        On success with extracted text >= *min_web_chars*.

    Raises
    ------
    ExtractionError
        When extraction fails or extracted text is below *min_web_chars*.
    """
    if min_web_chars is None or strip_tags is None:
        _extraction_defaults = ExtractionConfig()
        if min_web_chars is None:
            min_web_chars = _extraction_defaults.min_web_chars
        if strip_tags is None:
            strip_tags = list(_extraction_defaults.strip_tags)

    # Recover the title from the original markup before tag-stripping.
    title = _recover_html_title(html_body)

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

    return ArticleExtractionResult(text=extracted, source="article", title=title)


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

    return extract_article_from_html(
        html_body,
        url=url,
        min_web_chars=min_web_chars,
        strip_tags=strip_tags,
    )
