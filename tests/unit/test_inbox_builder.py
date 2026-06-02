"""Unit tests for the inbox acquisition + note builder (Inbox v1 slice 1).

Covers :func:`influx.sources.inbox.acquire_inbox_bytes` (fixed archive
subtree, real-Content-Type routing — issue #200) and
:func:`influx.sources.inbox.build_inbox_note_item` (title fallback,
ProfileItem key set, tags, thin-summary suppression).
``download_archive_autodetect`` and ``extract_article_from_html`` /
``extract_pdf`` are patched — no network IO.
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
from influx.http_client import FetchResult
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


def _make_config(
    *, min_summary_chars: int = 80, archive_dir: str = "/archive"
) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        storage=StorageConfig(archive_dir=archive_dir),
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


def _ok_html_archive(
    url: str = _URL, *, body: bytes = b"<html>article</html>"
) -> ArchiveResult:
    return ArchiveResult(
        ok=True,
        rel_posix_path=f"inbox/2026/06/{url_hash(url)}.html",
        error="",
        content_type="text/html; charset=utf-8",
        content_type_family="html",
        body=body,
    )


def _ok_pdf_archive(url: str, *, body: bytes = b"%PDF-1.4 ...") -> ArchiveResult:
    return ArchiveResult(
        ok=True,
        rel_posix_path=f"inbox/2026/06/{url_hash(url)}.pdf",
        error="",
        content_type="application/pdf",
        content_type_family="pdf",
        body=body,
    )


def _failed_archive() -> ArchiveResult:
    return ArchiveResult(
        ok=False,
        rel_posix_path=None,
        error="HTTP 404",
        failure_kind=cast("str", "http_404"),
    )


class _Extraction:
    def __init__(self, text: str, title: str | None = None) -> None:
        self.text = text
        self.title = title


# ── acquire_inbox_bytes ─────────────────────────────────────────────


def test_acquire_archives_into_fixed_inbox_subtree() -> None:
    """HTML responses archive under inbox/YYYY/MM/<url_hash>.html (§13.1)."""
    config = _make_config()
    with (
        patch(
            "influx.sources.inbox.download_archive_autodetect",
            return_value=_ok_html_archive(),
        ) as mock_dl,
        patch(
            "influx.sources.inbox.extract_article_from_html",
            return_value=_Extraction(_LONG_BODY),
        ),
    ):
        acquired = acquire_inbox_bytes(_URL, config=config)

    kwargs = mock_dl.call_args.kwargs
    assert kwargs["source"] == INBOX_SOURCE
    assert kwargs["item_id"] == url_hash(_URL)
    # No ext / expected_content_type is passed any more — the extension is
    # derived from the response Content-Type by the autodetect helper.
    assert "ext" not in kwargs
    assert "expected_content_type" not in kwargs
    assert acquired.archive_path == f"inbox/2026/06/{url_hash(_URL)}.html"
    assert acquired.extracted_text == _LONG_BODY
    assert acquired.text_flavour == "html"


def test_acquire_routes_pdf_on_content_type_not_url_shape() -> None:
    """A PDF served at a NON-.pdf URL still extracts as a PDF (issue #200 AC-1)."""
    config = _make_config()
    # URL has no .pdf suffix — only the response Content-Type marks it PDF.
    pdf_url = "https://example.com/download?doc=42"
    with (
        patch(
            "influx.sources.inbox.download_archive_autodetect",
            return_value=_ok_pdf_archive(pdf_url, body=b"%PDF-1.4 bytes"),
        ),
        patch(
            "influx.sources.inbox.extract_pdf",
            return_value=_Extraction(_LONG_BODY),
        ) as mock_pdf,
        patch("influx.sources.inbox.extract_article_from_html") as mock_html,
    ):
        acquired = acquire_inbox_bytes(pdf_url, config=config)

    mock_pdf.assert_called_once()
    # The PDF bytes from the fetch are what get extracted (not re-read).
    assert mock_pdf.call_args.args[0] == b"%PDF-1.4 bytes"
    mock_html.assert_not_called()
    assert acquired.text_flavour == "pdf"
    assert acquired.extracted_text == _LONG_BODY


def test_acquire_non_extractable_type_uses_summary_fallback(
    caplog: object,
) -> None:
    """A fetched body whose type is neither HTML nor PDF falls back + logs."""
    import logging

    config = _make_config()
    mismatch = ArchiveResult(
        ok=False,
        rel_posix_path=None,
        error="content_type_mismatch: 'application/xml'",
        failure_kind=cast("str", "content_type_mismatch"),
        content_type="application/xml",
        content_type_family="xml",
        body=b"<rss></rss>",
    )
    with (
        patch(
            "influx.sources.inbox.download_archive_autodetect",
            return_value=mismatch,
        ),
        caplog.at_level(logging.INFO, logger="influx.sources.inbox"),  # type: ignore[attr-defined]
    ):
        acquired = acquire_inbox_bytes(_URL, config=config, summary_hint="a hint")

    assert acquired.extracted_text is None
    assert acquired.text_flavour == "summary-fallback"
    assert acquired.summary == "a hint"
    assert any(
        "content-type not extractable" in r.message
        for r in caplog.records  # type: ignore[attr-defined]
    )


def test_acquire_falls_back_to_summary_hint_on_archive_failure() -> None:
    config = _make_config()
    with patch(
        "influx.sources.inbox.download_archive_autodetect",
        return_value=_failed_archive(),
    ):
        acquired = acquire_inbox_bytes(_URL, config=config, summary_hint="a hint")

    assert acquired.extracted_text is None
    assert acquired.summary == "a hint"
    assert acquired.archive_missing is True
    assert acquired.text_flavour == "summary-fallback"


def test_acquire_html_item_fetches_url_exactly_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An HTML inbox item triggers exactly one network fetch (issue #200 AC-2).

    The bytes that get archived and the bytes that get extracted come from
    the *same* response — there is no second fetch in the extraction step.
    """
    config = _make_config(archive_dir=str(tmp_path))
    html = (
        "<html><body><article><p>" + _LONG_BODY + "</p></article></body></html>"
    ).encode("utf-8")
    fetched = FetchResult(
        body=html,
        status_code=200,
        content_type="text/html; charset=utf-8",
        final_url=_URL,
    )
    seen: list[str] = []

    with (
        # Patch the fetch where the autodetect helper imported it.
        patch("influx.storage.guarded_fetch", return_value=fetched) as mock_fetch,
        # Capture what the extractor receives without invoking trafilatura.
        patch(
            "influx.sources.inbox.extract_article_from_html",
            side_effect=lambda body, **_: seen.append(body) or _Extraction(_LONG_BODY),
        ),
    ):
        acquired = acquire_inbox_bytes(_URL, config=config)

    assert mock_fetch.call_count == 1
    # The extractor saw exactly the fetched (decoded) bytes — same response.
    assert seen == [html.decode("utf-8")]
    assert acquired.text_flavour == "html"
    assert acquired.archive_missing is False


# ── build_inbox_note_item ───────────────────────────────────────────


def _acquire_ok(config: AppConfig) -> object:
    with (
        patch(
            "influx.sources.inbox.download_archive_autodetect",
            return_value=_ok_html_archive(),
        ),
        patch(
            "influx.sources.inbox.extract_article_from_html",
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


def _acquire_with_title(config: AppConfig, title: str | None) -> object:
    with (
        patch(
            "influx.sources.inbox.download_archive_autodetect",
            return_value=_ok_html_archive(),
        ),
        patch(
            "influx.sources.inbox.extract_article_from_html",
            return_value=_Extraction(_LONG_BODY, title=title),
        ),
    ):
        return acquire_inbox_bytes(_URL, config=config)


def test_acquire_captures_extracted_title() -> None:
    """The HTML branch threads the recovered title onto InboxAcquisition (#210)."""
    acquired = _acquire_with_title(_make_config(), "Recovered Title")
    assert acquired.extracted_title == "Recovered Title"  # type: ignore[attr-defined]


def test_build_item_uses_extracted_title_when_no_hint() -> None:
    config = _make_config()
    acquired = _acquire_with_title(config, "Recovered Title")
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
    assert item["title"] == "Recovered Title"


def test_build_item_hint_wins_over_extracted_title() -> None:
    config = _make_config()
    acquired = _acquire_with_title(config, "Recovered Title")
    item = build_inbox_note_item(
        acquired=acquired,  # type: ignore[arg-type]
        profile_name="ai-robotics",
        score=8,
        confidence=0.9,
        reason="relevant",
        filter_tags=(),
        source_tag="inbox",
        submitted_by="x",
        title_hint="Submitter Title",
        config=config,
    )
    assert item is not None
    assert item["title"] == "Submitter Title"


def test_build_item_thin_summary_suppressed() -> None:
    """No body extracted + thin fallback summary → None (suppressed)."""
    config = _make_config()
    with patch(
        "influx.sources.inbox.download_archive_autodetect",
        return_value=_failed_archive(),
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
