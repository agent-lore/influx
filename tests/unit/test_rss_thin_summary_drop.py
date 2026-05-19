"""Tests for the thin-summary suppression on the RSS adapter (issue #166).

Verifies the wiring from :func:`influx.sources.rss.build_rss_note_item`
into :func:`influx.thin_summary.is_thin_summary` and the per-drop
telemetry:

* Thin summary + failed archive  → ``build_rss_note_item`` returns
  ``None``; the contextvar counter / OTel metric / INFO log all fire.
* Thin summary + successful archive → note is still written (the rule
  only fires when no body arrived).
* Non-thin summary + failed archive → note is written as today (with
  the existing ``influx:archive-missing`` tag).
* ``non_html_source`` / ``unsupported`` failures with a thin summary
  → dropped (the user picked the broader scope so these short-circuits
  benefit too).
* ``min_summary_chars = 0`` override disables length-rule suppression
  but title-equality + boilerplate continue to fire.

``extract_article`` is patched to raise ``NetworkError`` in every test
so the feed summary is the value the rule actually sees — no real
network IO, no implicit extraction-fallback noise.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import cast
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ArchivePolicyConfig,
    ExtractionConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
    StorageConfig,
)
from influx.errors import NetworkError
from influx.sources.rss import RssFeedItem, build_rss_note_item
from influx.storage import ArchiveResult
from influx.telemetry import current_summary_thin_drops

# A meaningful summary well above the default 80-char threshold so any
# test that wants a "non-thin" baseline can re-use it.
_LONG_SUMMARY = (
    "This is a substantial article body discussing the subject in "
    "enough detail that no structural thin-summary rule should ever "
    "trip on it under default configuration."
)


def _make_config(
    *,
    min_summary_chars: int = 80,
    archive_dir: str = "/archive",
) -> AppConfig:
    """Build a minimal AppConfig with a tunable thin-summary threshold."""
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        schedule=ScheduleConfig(),
        storage=StorageConfig(
            archive_dir=archive_dir,
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
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(min_summary_chars=min_summary_chars),
    )


def _make_item(
    *,
    summary: str = "A summary of the article.",
    title: str = "Test Article",
    url: str = "https://example.com/article",
) -> RssFeedItem:
    return RssFeedItem(
        title=title,
        url=url,
        published=datetime(2026, 4, 25, tzinfo=UTC),
        summary=summary,
        source_tag="rss",
        feed_name="example-feed",
    )


def _failed_archive(
    *,
    failure_kind: str = "http_404",
) -> ArchiveResult:
    return ArchiveResult(
        ok=False,
        rel_posix_path=None,
        error=f"HTTP 404 for url ({failure_kind})",
        failure_kind=cast(  # type: ignore[arg-type]
            "str",
            failure_kind,
        ),
    )


def _ok_archive() -> ArchiveResult:
    return ArchiveResult(
        ok=True,
        rel_posix_path="rss/example-feed/2026/04/abcd1234.html",
        error="",
    )


@pytest.fixture(autouse=True)
def _isolate_counter() -> object:
    """Each test gets its own ``current_summary_thin_drops`` bucket."""
    bucket: list[int] = [0]
    token = current_summary_thin_drops.set(bucket)
    try:
        yield bucket
    finally:
        current_summary_thin_drops.reset(token)


@pytest.fixture(autouse=True)
def _force_extraction_failure() -> object:
    """Force ``extract_article`` to raise so the test sees the feed summary verbatim."""
    with patch(
        "influx.sources.rss.extract_article",
        side_effect=NetworkError(
            "test forced",
            url="https://example.com/article",
            kind="timeout",
        ),
    ) as patched:
        yield patched


class TestThinSummaryDrop:
    """Thin summary + failed archive → drop with metric + counter + INFO log."""

    @patch("influx.sources.rss.download_archive")
    def test_pointer_summary_drops_item(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # "Discussion (47 points)" is 22 chars — well under the default
        # 80-char threshold, so the length rule fires first.
        mock_dl.return_value = _failed_archive(failure_kind="http_404")  # type: ignore[attr-defined]
        item = _make_item(summary="Discussion (47 points)")

        with caplog.at_level(logging.INFO, logger="influx.sources.rss"):
            result = build_rss_note_item(
                item=item,
                profile_name="ai-robotics",
                config=_make_config(),
            )

        assert result is None
        assert _isolate_counter[0] == 1
        assert any(
            "thin-summary drop source=rss" in record.message
            and "rule=length" in record.message
            and "failure_kind=http_404" in record.message
            for record in caplog.records
        )

    @patch("influx.sources.rss.download_archive")
    def test_title_equality_drops_item_at_min_chars_zero(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # min_summary_chars=0 disables the length rule, so this proves
        # the title-equality check fires independently.
        mock_dl.return_value = _failed_archive(failure_kind="timeout")  # type: ignore[attr-defined]
        item = _make_item(
            title="The Title Verbatim",
            summary="THE TITLE VERBATIM!",
        )

        with caplog.at_level(logging.INFO, logger="influx.sources.rss"):
            result = build_rss_note_item(
                item=item,
                profile_name="ai-robotics",
                config=_make_config(min_summary_chars=0),
            )

        assert result is None
        assert _isolate_counter[0] == 1
        assert any(
            "rule=title_equality" in record.message
            and "failure_kind=timeout" in record.message
            for record in caplog.records
        )

    @patch("influx.sources.rss.download_archive")
    def test_boilerplate_summary_drops_item_at_min_chars_zero(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_dl.return_value = _failed_archive(failure_kind="http_5xx")  # type: ignore[attr-defined]
        item = _make_item(summary="Read more at example.com")

        with caplog.at_level(logging.INFO, logger="influx.sources.rss"):
            result = build_rss_note_item(
                item=item,
                profile_name="ai-robotics",
                config=_make_config(min_summary_chars=0),
            )

        assert result is None
        assert _isolate_counter[0] == 1
        assert any("rule=boilerplate" in record.message for record in caplog.records)


class TestThinSummaryNoDrop:
    """The rule must NOT fire outside its trigger scope."""

    @patch("influx.sources.rss.download_archive")
    def test_long_summary_still_writes_note(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
    ) -> None:
        mock_dl.return_value = _failed_archive(failure_kind="http_404")  # type: ignore[attr-defined]
        item = _make_item(summary=_LONG_SUMMARY)

        result = build_rss_note_item(
            item=item,
            profile_name="ai-robotics",
            config=_make_config(),
        )

        assert result is not None
        # Existing behaviour preserved: archive failure still produces the
        # archive-missing tag on the rendered note.
        tags = cast(list[str], result["tags"])
        assert "influx:archive-missing" in tags
        assert _isolate_counter[0] == 0

    @patch("influx.sources.rss.download_archive")
    def test_thin_summary_with_archive_success_writes_note(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
    ) -> None:
        # Archive succeeded → suppression check does not even run.
        mock_dl.return_value = _ok_archive()  # type: ignore[attr-defined]
        item = _make_item(summary="Discussion (47 points)")

        result = build_rss_note_item(
            item=item,
            profile_name="ai-robotics",
            config=_make_config(),
        )

        assert result is not None
        tags = cast(list[str], result["tags"])
        assert "influx:archive-missing" not in tags
        assert _isolate_counter[0] == 0


class TestThinSummaryScopeBroaderFailureKinds:
    """User decision: also fire on ``non_html_source`` / ``unsupported``."""

    @patch("influx.sources.rss.download_archive")
    def test_non_html_source_with_thin_summary_drops(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # ``non_html_source`` (issue #160) short-circuits the download.
        # Today it writes a note WITHOUT archive-missing; with #166
        # the thin-summary rule extends to this case too.
        mock_dl.return_value = _failed_archive(failure_kind="non_html_source")  # type: ignore[attr-defined]
        item = _make_item(summary="Comments")

        with caplog.at_level(logging.INFO, logger="influx.sources.rss"):
            result = build_rss_note_item(
                item=item,
                profile_name="ai-robotics",
                config=_make_config(min_summary_chars=0),
            )

        assert result is None
        assert _isolate_counter[0] == 1
        assert any(
            "failure_kind=non_html_source" in record.message
            for record in caplog.records
        )

    @patch("influx.sources.rss.download_archive")
    def test_unsupported_with_thin_summary_drops(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # ``unsupported`` (issue #161) — domain policy says no archive.
        mock_dl.return_value = _failed_archive(failure_kind="unsupported")  # type: ignore[attr-defined]
        item = _make_item(summary="Continue reading")

        with caplog.at_level(logging.INFO, logger="influx.sources.rss"):
            result = build_rss_note_item(
                item=item,
                profile_name="ai-robotics",
                config=_make_config(min_summary_chars=0),
            )

        assert result is None
        assert _isolate_counter[0] == 1
        assert any(
            "failure_kind=unsupported" in record.message for record in caplog.records
        )

    @patch("influx.sources.rss.download_archive")
    def test_non_html_source_with_long_summary_writes_note(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
    ) -> None:
        # A non-thin summary on a non_html_source URL keeps today's
        # behaviour: note written with the dedicated tag.  This pins
        # the rule's *narrowness* — it does NOT broaden to "drop every
        # non_html_source / unsupported outcome".
        mock_dl.return_value = _failed_archive(failure_kind="non_html_source")  # type: ignore[attr-defined]
        item = _make_item(summary=_LONG_SUMMARY)

        result = build_rss_note_item(
            item=item,
            profile_name="ai-robotics",
            config=_make_config(),
        )

        assert result is not None
        tags = cast(list[str], result["tags"])
        assert "influx:archive-non-html-source" in tags
        assert _isolate_counter[0] == 0


class TestConfigOverride:
    """``min_summary_chars`` propagates from the config."""

    @patch("influx.sources.rss.download_archive")
    def test_zero_threshold_disables_length_rule_only(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
    ) -> None:
        # 30-char summary that doesn't match boilerplate / title-equality.
        # With min_summary_chars=0 the length rule is off, so the item
        # passes and a note is written.
        mock_dl.return_value = _failed_archive(failure_kind="http_404")  # type: ignore[attr-defined]
        item = _make_item(summary="Just a brief but real summary.")  # 30 chars

        result = build_rss_note_item(
            item=item,
            profile_name="ai-robotics",
            config=_make_config(min_summary_chars=0),
        )

        assert result is not None
        assert _isolate_counter[0] == 0

    @patch("influx.sources.rss.download_archive")
    def test_high_threshold_drops_medium_summaries(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
    ) -> None:
        # A 30-char summary that is fine at the default threshold is
        # dropped when an operator raises the bar to 200 chars.
        mock_dl.return_value = _failed_archive(failure_kind="http_404")  # type: ignore[attr-defined]
        item = _make_item(summary="A short factual summary string.")

        result = build_rss_note_item(
            item=item,
            profile_name="ai-robotics",
            config=_make_config(min_summary_chars=200),
        )

        assert result is None
        assert _isolate_counter[0] == 1
