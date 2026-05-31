"""Tests for the observe-only empty-source guard (issue #189).

The guard counts — but never drops — notes written with no usable source
(a blank ``source:`` tag AND a URL no source can be inferred from). It is
the belt-and-braces observability layer at ingest for the #187 zombie
class, deliberately shipped observe-only because the population it catches
is often legitimate content (a real article with a working non-arxiv link
whose feed-slug tag was never populated).

Verifies, at the RSS builder level, the wiring from
:func:`influx.sources.rss.build_rss_note_item` into
:func:`influx.repair_hooks.has_usable_source` and the per-note telemetry:

* Blank source tag + non-arxiv URL → note is **still written**; the
  contextvar counter / OTel metric / INFO log all fire.
* A present source tag → not counted (note written as today).
* Blank source tag + an arxiv URL → rescued by URL inference, not counted.

Plus a run-ledger test pinning the triage decision that an empty-source
write contributes **no degraded reason** (mirrors #166 thin-summary).

``extract_article`` is patched to raise so the feed summary is used
verbatim — no network IO — and a healthy archive is used so the
thin-summary suppression path never runs and the empty-source guard is
exercised in isolation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
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
from influx.run_ledger import _KNOWN_DEGRADED_REASONS, RunLedger
from influx.sources.rss import RssFeedItem, build_rss_note_item
from influx.storage import ArchiveResult
from influx.telemetry import current_empty_source_writes

_LONG_SUMMARY = (
    "This is a substantial article body discussing the subject in "
    "enough detail that no structural thin-summary rule should ever "
    "trip on it under default configuration."
)


def _make_config() -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        schedule=ScheduleConfig(),
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
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(),
    )


def _make_item(
    *,
    source_tag: str,
    url: str = "https://example.com/article",
    summary: str = _LONG_SUMMARY,
) -> RssFeedItem:
    return RssFeedItem(
        title="Test Article",
        url=url,
        published=datetime(2026, 4, 25, tzinfo=UTC),
        summary=summary,
        source_tag=source_tag,
        feed_name="example-feed",
    )


def _ok_archive() -> ArchiveResult:
    # Healthy archive so the #166 thin-summary path never runs — the
    # empty-source guard is exercised on its own.
    return ArchiveResult(
        ok=True,
        rel_posix_path="rss/example-feed/2026/04/abcd1234.html",
        error="",
    )


@pytest.fixture(autouse=True)
def _isolate_counter() -> object:
    """Each test gets its own ``current_empty_source_writes`` bucket."""
    bucket: list[int] = [0]
    token = current_empty_source_writes.set(bucket)
    try:
        yield bucket
    finally:
        current_empty_source_writes.reset(token)


@pytest.fixture(autouse=True)
def _force_extraction_failure() -> object:
    """Force ``extract_article`` to raise so the feed summary is used verbatim."""
    with patch(
        "influx.sources.rss.extract_article",
        side_effect=NetworkError(
            "test forced",
            url="https://example.com/article",
            kind="timeout",
        ),
    ) as patched:
        yield patched


class TestEmptySourceWriteCounted:
    """Blank source tag + non-arxiv URL → counted, logged, still written."""

    @patch("influx.sources.rss.download_archive")
    def test_blank_source_tag_non_arxiv_url_counts_and_writes(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_dl.return_value = _ok_archive()  # type: ignore[attr-defined]
        item = _make_item(source_tag="", url="https://example.com/article")

        with caplog.at_level(logging.INFO, logger="influx.sources.rss"):
            result = build_rss_note_item(
                item=item,
                profile_name="ai-robotics",
                config=_make_config(),
            )

        # Observe-only: the note is STILL written.
        assert result is not None
        assert _isolate_counter[0] == 1
        assert any(
            "empty-source write source=rss" in record.message
            and "reason=no_usable_source" in record.message
            for record in caplog.records
        )

    @patch("influx.sources.rss.metrics.empty_source_writes")
    @patch("influx.sources.rss.download_archive")
    def test_blank_source_tag_bumps_metric(
        self,
        mock_dl: object,
        mock_metric: object,
        _isolate_counter: list[int],
    ) -> None:
        mock_dl.return_value = _ok_archive()  # type: ignore[attr-defined]
        item = _make_item(source_tag="")

        result = build_rss_note_item(
            item=item,
            profile_name="ai-robotics",
            config=_make_config(),
        )

        assert result is not None
        mock_metric.return_value.add.assert_called_once()  # type: ignore[attr-defined]


class TestEmptySourceWriteNotCounted:
    """The guard must NOT fire when a usable source exists."""

    @patch("influx.sources.rss.metrics.empty_source_writes")
    @patch("influx.sources.rss.download_archive")
    def test_present_source_tag_not_counted(
        self,
        mock_dl: object,
        mock_metric: object,
        _isolate_counter: list[int],
    ) -> None:
        mock_dl.return_value = _ok_archive()  # type: ignore[attr-defined]
        item = _make_item(source_tag="rss-blog")

        result = build_rss_note_item(
            item=item,
            profile_name="ai-robotics",
            config=_make_config(),
        )

        assert result is not None
        assert _isolate_counter[0] == 0
        mock_metric.return_value.add.assert_not_called()  # type: ignore[attr-defined]

    @patch("influx.sources.rss.metrics.empty_source_writes")
    @patch("influx.sources.rss.download_archive")
    def test_blank_source_tag_arxiv_url_is_rescued(
        self,
        mock_dl: object,
        mock_metric: object,
        _isolate_counter: list[int],
    ) -> None:
        # No source tag, but an arxiv URL → has_usable_source infers the
        # source, so the guard does not fire (URL-rescue).
        mock_dl.return_value = _ok_archive()  # type: ignore[attr-defined]
        item = _make_item(source_tag="", url="https://arxiv.org/abs/2401.00001")

        result = build_rss_note_item(
            item=item,
            profile_name="ai-robotics",
            config=_make_config(),
        )

        assert result is not None
        assert _isolate_counter[0] == 0
        mock_metric.return_value.add.assert_not_called()  # type: ignore[attr-defined]


class TestEmptySourceWriteNoDegradedReason:
    """Triage decision (#189, mirrors #166): an empty-source write is a
    quality/observability signal and must never degrade the run."""

    def test_not_a_known_degraded_reason(self) -> None:
        assert "empty_source_writes" not in _KNOWN_DEGRADED_REASONS
        assert "empty_source" not in _KNOWN_DEGRADED_REASONS

    def test_empty_source_writes_do_not_degrade_run(self, tmp_path: Path) -> None:
        ledger = RunLedger(tmp_path / "state")
        ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
        reasons = ledger.complete(
            run_id="r-1",
            sources_checked=5,
            ingested=3,
            empty_source_writes_total=5,
        )

        assert reasons == []
        entry = ledger.recent()[0]
        assert entry["empty_source_writes_total"] == 5
        assert entry["degraded"] is False
        assert entry["degradation_severity"] == "success"

    def test_field_defaults_none_in_flight(self, tmp_path: Path) -> None:
        ledger = RunLedger(tmp_path / "state")
        ledger.start(run_id="r-2", profile="p", kind="scheduled", run_range=None)
        active = cast(list[dict[str, object]], ledger.active_runs())
        assert active[0]["empty_source_writes_total"] is None
