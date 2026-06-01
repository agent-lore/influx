"""Unit tests for the inbox acquisition + note builder (Inbox v1 slice 1).

Covers :func:`influx.sources.inbox.acquire_inbox_bytes` (fixed archive
subtree, content-type branch) and
:func:`influx.sources.inbox.build_inbox_note_item` (title fallback,
ProfileItem key set, tags, thin-summary suppression).  ``download_archive``
and ``extract_article`` / ``extract_pdf`` are patched — no network IO.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import patch

from influx.config import (
    AppConfig,
    ExtractionConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    SecurityConfig,
    StorageConfig,
)
from influx.errors import NetworkError
from influx.sources.inbox import (
    INBOX_SOURCE,
    acquire_inbox_bytes,
    build_inbox_note_item,
)
from influx.storage import ArchiveResult
from influx.urls import url_hash

_URL = "https://example.com/article"
_LONG_BODY = (
    "This is a substantial article body discussing the subject in enough "
    "detail that no structural thin-summary rule should trip on it under "
    "default configuration thresholds."
)


def _make_config(*, min_summary_chars: int = 80) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        storage=StorageConfig(archive_dir="/archive"),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="AI and robotics research",
                thresholds=ProfileThresholds(
                    relevance=7, full_text=100, deep_extract=100
                ),
            )
        ],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(min_summary_chars=min_summary_chars),
    )


def _ok_html_archive(url: str = _URL) -> ArchiveResult:
    return ArchiveResult(
        ok=True,
        rel_posix_path=f"inbox/2026/06/{url_hash(url)}.html",
        error="",
    )


def _failed_archive() -> ArchiveResult:
    return ArchiveResult(
        ok=False,
        rel_posix_path=None,
        error="HTTP 404",
        failure_kind=cast("str", "http_404"),
    )


class _Extraction:
    def __init__(self, text: str) -> None:
        self.text = text


# ── acquire_inbox_bytes ─────────────────────────────────────────────


def test_acquire_archives_into_fixed_inbox_subtree() -> None:
    """HTML URLs archive under inbox/YYYY/MM/<url_hash>.html (§13.1)."""
    config = _make_config()
    with (
        patch(
            "influx.sources.inbox.download_archive", return_value=_ok_html_archive()
        ) as mock_dl,
        patch(
            "influx.sources.inbox.extract_article",
            return_value=_Extraction(_LONG_BODY),
        ),
    ):
        acquired = acquire_inbox_bytes(_URL, config=config)

    kwargs = mock_dl.call_args.kwargs
    assert kwargs["source"] == INBOX_SOURCE
    assert kwargs["item_id"] == url_hash(_URL)
    assert kwargs["ext"] == ".html"
    assert kwargs["expected_content_type"] == "html"
    assert acquired.archive_path == f"inbox/2026/06/{url_hash(_URL)}.html"
    assert acquired.extracted_text == _LONG_BODY
    assert acquired.text_flavour == "html"


def test_acquire_pdf_url_uses_pdf_branch() -> None:
    """A .pdf URL archives with a .pdf ext and extracts via extract_pdf."""
    config = _make_config()
    pdf_url = "https://arxiv.org/pdf/2401.12345.pdf"
    ok_pdf = ArchiveResult(
        ok=True, rel_posix_path=f"inbox/2026/06/{url_hash(pdf_url)}.pdf", error=""
    )
    with (
        patch("influx.sources.inbox.download_archive", return_value=ok_pdf) as mock_dl,
        patch("pathlib.Path.read_bytes", return_value=b"%PDF-1.4 ..."),
        patch(
            "influx.sources.inbox.extract_pdf", return_value=_Extraction(_LONG_BODY)
        ) as mock_pdf,
    ):
        acquired = acquire_inbox_bytes(pdf_url, config=config)

    assert mock_dl.call_args.kwargs["ext"] == ".pdf"
    assert mock_dl.call_args.kwargs["expected_content_type"] == "pdf"
    mock_pdf.assert_called_once()
    assert acquired.text_flavour == "pdf"
    assert acquired.extracted_text == _LONG_BODY


def test_acquire_falls_back_to_summary_hint_on_extraction_failure() -> None:
    config = _make_config()
    with (
        patch("influx.sources.inbox.download_archive", return_value=_failed_archive()),
        patch(
            "influx.sources.inbox.extract_article",
            side_effect=NetworkError("boom", url=_URL, kind="timeout"),
        ),
    ):
        acquired = acquire_inbox_bytes(_URL, config=config, summary_hint="a hint")

    assert acquired.extracted_text is None
    assert acquired.summary == "a hint"
    assert acquired.archive_missing is True


# ── build_inbox_note_item ───────────────────────────────────────────


def _acquire_ok(config: AppConfig) -> object:
    with (
        patch("influx.sources.inbox.download_archive", return_value=_ok_html_archive()),
        patch(
            "influx.sources.inbox.extract_article",
            return_value=_Extraction(_LONG_BODY),
        ),
    ):
        return acquire_inbox_bytes(_URL, config=config)


def test_build_item_has_full_profile_item_key_set() -> None:
    config = _make_config()
    acquired = _acquire_ok(config)
    item = build_inbox_note_item(
        acquired=acquired,  # type: ignore[arg-type]
        profile_name="ai-robotics",
        score=8,
        confidence=0.9,
        reason="relevant",
        filter_tags=("ml",),
        source_tag="inbox",
        submitted_by="daily-report:ai-news",
        title_hint="Some Title",
        config=config,
    )
    assert item is not None
    expected_keys = {
        "id",
        "title",
        "source",
        "source_url",
        "content",
        "tags",
        "filter_tags",
        "score",
        "confidence",
        "reason",
        "path",
        "abstract_or_summary",
        "contributions",
        "builds_on",
    }
    assert expected_keys <= set(item)
    assert item["source"] == "inbox"
    assert item["source_url"] == _URL
    assert item["title"] == "Some Title"
    assert "profile:ai-robotics" in item["tags"]
    assert "source:inbox" in item["tags"]
    assert "submitter:daily-report:ai-news" in item["tags"]


def test_build_item_title_falls_back_to_url() -> None:
    config = _make_config()
    acquired = _acquire_ok(config)
    item = build_inbox_note_item(
        acquired=acquired,  # type: ignore[arg-type]
        profile_name="ai-robotics",
        score=8,
        confidence=0.9,
        reason="relevant",
        filter_tags=(),
        source_tag="inbox",
        submitted_by="x",
        title_hint=None,
        config=config,
    )
    assert item is not None
    assert item["title"] == _URL


def test_build_item_thin_summary_suppressed() -> None:
    """No body extracted + thin fallback summary → None (suppressed)."""
    config = _make_config()
    with (
        patch("influx.sources.inbox.download_archive", return_value=_failed_archive()),
        patch(
            "influx.sources.inbox.extract_article",
            side_effect=NetworkError("boom", url=_URL, kind="timeout"),
        ),
    ):
        acquired = acquire_inbox_bytes(_URL, config=config, summary_hint="tiny")

    item = build_inbox_note_item(
        acquired=acquired,
        profile_name="ai-robotics",
        score=8,
        confidence=0.9,
        reason="relevant",
        filter_tags=(),
        source_tag="inbox",
        submitted_by="x",
        title_hint="Title",
        config=config,
    )
    assert item is None
