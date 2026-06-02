"""RSS reuses the archived bytes for extraction — no second fetch (issue #200).

``build_rss_note_item`` archives ``item.url`` as HTML via ``download_archive``
and previously re-fetched the same URL through ``extract_article``.  These
tests pin the fetch-once behavior: when the archive download returns bytes,
extraction runs off those bytes via ``extract_article_from_html``; only when
there are no archived bytes does it fall back to a direct ``extract_article``
fetch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from influx.config import (
    AppConfig,
    ArchivePolicyConfig,
    ExtractionConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    SecurityConfig,
    StorageConfig,
)
from influx.sources.rss import RssFeedItem, build_rss_note_item
from influx.storage import ArchiveResult

_URL = "https://example.com/article"
_LONG_BODY = (
    "This is a substantial extracted article body, long enough to clear the "
    "default thin-summary and min-length thresholds without any trouble at all."
)


def _config() -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        storage=StorageConfig(
            archive_dir="/archive",
            archive_policy=ArchivePolicyConfig(include_defaults=False),
        ),
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
        extraction=ExtractionConfig(min_summary_chars=80),
    )


def _item() -> RssFeedItem:
    return RssFeedItem(
        title="Test Article",
        url=_URL,
        published=datetime(2026, 4, 25, tzinfo=UTC),
        summary="A short feed summary.",
        source_tag="rss",
        feed_name="example-feed",
    )


def _ok_archive_with_body(body: bytes) -> ArchiveResult:
    return ArchiveResult(
        ok=True,
        rel_posix_path="rss/example-feed/2026/04/abcd1234.html",
        error="",
        content_type="text/html; charset=utf-8",
        content_type_family="html",
        body=body,
    )


def _skipped_archive() -> ArchiveResult:
    return ArchiveResult(
        ok=False,
        rel_posix_path=None,
        error="missing_by_policy: skipped",
        failure_kind="missing_by_policy",
        policy_mode="skip",
    )


class _Extraction:
    def __init__(self, text: str) -> None:
        self.text = text


def test_reuses_archived_bytes_without_second_fetch() -> None:
    """A successful archive feeds its bytes to from-html extraction (no re-fetch)."""
    html = (
        b"<html><body><article>"
        + _LONG_BODY.encode("utf-8")
        + b"</article></body></html>"
    )
    with (
        patch(
            "influx.sources.rss.download_archive",
            return_value=_ok_archive_with_body(html),
        ),
        patch(
            "influx.sources.rss.extract_article_from_html",
            return_value=_Extraction(_LONG_BODY),
        ) as mock_from_html,
        patch("influx.sources.rss.extract_article") as mock_fetch_extract,
    ):
        result = build_rss_note_item(
            item=_item(), profile_name="ai-robotics", config=_config()
        )

    assert result is not None
    # Extraction ran off the archived bytes — the URL was not fetched again.
    mock_from_html.assert_called_once()
    assert mock_from_html.call_args.args[0] == html.decode("utf-8")
    mock_fetch_extract.assert_not_called()
    assert result["abstract_or_summary"] == _LONG_BODY


def test_falls_back_to_fetch_when_no_archived_bytes() -> None:
    """A policy-skipped archive (no bytes) still attempts a direct extraction."""
    with (
        patch(
            "influx.sources.rss.download_archive",
            return_value=_skipped_archive(),
        ),
        patch(
            "influx.sources.rss.extract_article",
            return_value=_Extraction(_LONG_BODY),
        ) as mock_fetch_extract,
        patch("influx.sources.rss.extract_article_from_html") as mock_from_html,
    ):
        result = build_rss_note_item(
            item=_item(), profile_name="ai-robotics", config=_config()
        )

    assert result is not None
    mock_fetch_extract.assert_called_once()
    assert mock_fetch_extract.call_args.args[0] == _URL
    mock_from_html.assert_not_called()
