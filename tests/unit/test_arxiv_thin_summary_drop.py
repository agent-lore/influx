"""Tests for the thin-summary suppression on the arXiv adapter (issue #166).

arXiv abstracts are typically several hundred characters of real
content so the default 80-char threshold rarely fires in the wild.
These tests force the conditions where the rule must / must not fire
so the per-source behaviour is provably consistent with the RSS path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ExtractionConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.sources.arxiv import ArxivItem, build_arxiv_note_item
from influx.storage import ArchiveResult
from influx.telemetry import current_summary_thin_drops

# A realistic ~250-char arXiv abstract: comfortably above 80 chars,
# matches no boilerplate, distinct from any title we synthesise here.
_REAL_ABSTRACT = (
    "We present a novel architecture for self-supervised learning on "
    "graph-structured data that improves downstream node classification "
    "accuracy by 4.7% on benchmark datasets while reducing training "
    "time by an order of magnitude over prior work."
)


def _make_config(*, min_summary_chars: int = 80) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        schedule=ScheduleConfig(),
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
    abstract: str = _REAL_ABSTRACT,
    title: str = "Self-Supervised Graph Learning",
    arxiv_id: str = "2604.12345",
) -> ArxivItem:
    return ArxivItem(
        arxiv_id=arxiv_id,
        title=title,
        abstract=abstract,
        published=datetime(2026, 4, 25, tzinfo=UTC),
        categories=["cs.AI"],
    )


def _failed_archive(failure_kind: str = "http_404") -> ArchiveResult:
    return ArchiveResult(
        ok=False,
        rel_posix_path=None,
        error=f"forced ({failure_kind})",
        failure_kind=failure_kind,  # type: ignore[arg-type]
    )


def _ok_archive() -> ArchiveResult:
    return ArchiveResult(
        ok=True,
        rel_posix_path="arxiv/2026/04/2604.12345.pdf",
        error="",
    )


@pytest.fixture(autouse=True)
def _isolate_counter() -> object:
    bucket: list[int] = [0]
    token = current_summary_thin_drops.set(bucket)
    try:
        yield bucket
    finally:
        current_summary_thin_drops.reset(token)


class TestArxivThinSummaryDrop:
    """Thin abstract + failed PDF archive → drop with telemetry."""

    @patch("influx.sources.arxiv.download_archive")
    def test_pointer_abstract_drops_item(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_dl.return_value = _failed_archive("http_404")  # type: ignore[attr-defined]
        item = _make_item(abstract="Comments")

        with caplog.at_level(logging.INFO, logger="influx.sources.arxiv"):
            result = build_arxiv_note_item(
                item=item,
                score=8,
                confidence=0.8,
                reason="R",
                profile_name="ai-robotics",
                config=_make_config(min_summary_chars=0),
            )

        assert result is None
        assert _isolate_counter[0] == 1
        assert any(
            "thin-summary drop source=arxiv" in record.message
            and "rule=boilerplate" in record.message
            and "failure_kind=http_404" in record.message
            for record in caplog.records
        )

    @patch("influx.sources.arxiv.download_archive")
    def test_short_abstract_drops_at_default_threshold(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
    ) -> None:
        mock_dl.return_value = _failed_archive("timeout")  # type: ignore[attr-defined]
        # 30-char abstract — below default 80-char threshold.
        item = _make_item(abstract="Short stub abstract for paper.")

        result = build_arxiv_note_item(
            item=item,
            score=8,
            confidence=0.8,
            reason="R",
            profile_name="ai-robotics",
            config=_make_config(),
        )

        assert result is None
        assert _isolate_counter[0] == 1


class TestArxivThinSummaryNoDrop:
    """Realistic abstracts pass; archive success bypasses the rule entirely."""

    @patch("influx.sources.arxiv.download_archive")
    def test_real_abstract_with_failed_archive_writes_note(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
    ) -> None:
        mock_dl.return_value = _failed_archive("http_404")  # type: ignore[attr-defined]
        item = _make_item(abstract=_REAL_ABSTRACT)

        with patch(
            "influx.sources.arxiv.extract_arxiv_text",
            side_effect=Exception("extraction not the focus"),
        ):
            result = build_arxiv_note_item(
                item=item,
                score=4,  # below full_text threshold so extraction is skipped
                confidence=0.4,
                reason="R",
                profile_name="ai-robotics",
                config=_make_config(),
            )

        assert result is not None
        assert "influx:archive-missing" in result["tags"]
        assert _isolate_counter[0] == 0

    @patch("influx.sources.arxiv.download_archive")
    def test_thin_abstract_with_archive_success_writes_note(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
    ) -> None:
        # Archive OK — the suppression check never runs even with a
        # pointer-shaped abstract.
        mock_dl.return_value = _ok_archive()  # type: ignore[attr-defined]
        item = _make_item(abstract="Comments")

        result = build_arxiv_note_item(
            item=item,
            score=4,
            confidence=0.4,
            reason="R",
            profile_name="ai-robotics",
            config=_make_config(min_summary_chars=0),
        )

        assert result is not None
        assert "influx:archive-missing" not in result["tags"]
        assert _isolate_counter[0] == 0


class TestMetricsDeferralPastSuppression:
    """Issue #166 review: archive_missing / archive_policy_failures must
    NOT bump for items the thin-summary rule drops, since those items
    never receive the corresponding tags on a written note.
    """

    @patch("influx.sources.arxiv.metrics.archive_policy_failures")
    @patch("influx.sources.arxiv.metrics.archive_missing")
    @patch("influx.sources.arxiv.download_archive")
    def test_thin_summary_drop_skips_archive_missing_bump_on_terminal(
        self,
        mock_dl: object,
        mock_am: object,
        mock_apf: object,
        _isolate_counter: list[int],
    ) -> None:
        # Terminal-flipped path: archive_missing would have bumped
        # eagerly under the original wiring.  Drop must skip the bump.
        from influx.telemetry import current_archive_terminal_arxiv_ids

        item = _make_item(abstract="Comments")
        token = current_archive_terminal_arxiv_ids.set(frozenset({item.arxiv_id}))
        try:
            result = build_arxiv_note_item(
                item=item,
                score=4,
                confidence=0.4,
                reason="R",
                profile_name="ai-robotics",
                config=_make_config(min_summary_chars=0),
            )
        finally:
            current_archive_terminal_arxiv_ids.reset(token)

        assert result is None
        assert _isolate_counter[0] == 1
        # ``mock_am``/``mock_apf`` are the helper-getter mocks; their
        # ``.return_value`` is the per-call counter mock — assert no
        # ``.add`` invocation happened.
        mock_am.return_value.add.assert_not_called()  # type: ignore[attr-defined]
        mock_apf.return_value.add.assert_not_called()  # type: ignore[attr-defined]
        # Sanity: download_archive was NOT called because terminal
        # short-circuits download.
        mock_dl.assert_not_called()  # type: ignore[attr-defined]

    @patch("influx.sources.arxiv.metrics.archive_policy_failures")
    @patch("influx.sources.arxiv.metrics.archive_missing")
    @patch("influx.sources.arxiv.download_archive")
    def test_thin_summary_drop_skips_archive_metrics_on_generic_failure(
        self,
        mock_dl: object,
        mock_am: object,
        mock_apf: object,
        _isolate_counter: list[int],
    ) -> None:
        # Generic 404 path: both archive_missing AND
        # archive_policy_failures would have bumped eagerly.  Drop must
        # skip both.
        mock_dl.return_value = _failed_archive("http_404")  # type: ignore[attr-defined]
        item = _make_item(abstract="...")

        result = build_arxiv_note_item(
            item=item,
            score=4,
            confidence=0.4,
            reason="R",
            profile_name="ai-robotics",
            config=_make_config(min_summary_chars=0),
        )

        assert result is None
        assert _isolate_counter[0] == 1
        mock_am.return_value.add.assert_not_called()  # type: ignore[attr-defined]
        mock_apf.return_value.add.assert_not_called()  # type: ignore[attr-defined]

    @patch("influx.sources.arxiv.metrics.archive_policy_failures")
    @patch("influx.sources.arxiv.metrics.archive_missing")
    @patch("influx.sources.arxiv.download_archive")
    def test_non_thin_summary_still_bumps_archive_metrics(
        self,
        mock_dl: object,
        mock_am: object,
        mock_apf: object,
        _isolate_counter: list[int],
    ) -> None:
        # Non-thin abstract + failed archive: item is written, so both
        # counters must bump (deferred but still hit).
        mock_dl.return_value = _failed_archive("http_404")  # type: ignore[attr-defined]
        item = _make_item(abstract=_REAL_ABSTRACT)

        with patch(
            "influx.sources.arxiv.extract_arxiv_text",
            side_effect=Exception("extraction not the focus"),
        ):
            result = build_arxiv_note_item(
                item=item,
                score=4,
                confidence=0.4,
                reason="R",
                profile_name="ai-robotics",
                config=_make_config(),
            )

        assert result is not None
        assert _isolate_counter[0] == 0
        mock_am.return_value.add.assert_called_once()  # type: ignore[attr-defined]
        mock_apf.return_value.add.assert_called_once()  # type: ignore[attr-defined]


class TestArxivUnsupportedScope:
    """The broader trigger scope decision also applies to arXiv."""

    @patch("influx.sources.arxiv.download_archive")
    def test_unsupported_with_thin_abstract_drops(
        self,
        mock_dl: object,
        _isolate_counter: list[int],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_dl.return_value = _failed_archive("unsupported")  # type: ignore[attr-defined]
        item = _make_item(abstract="...")

        with caplog.at_level(logging.INFO, logger="influx.sources.arxiv"):
            result = build_arxiv_note_item(
                item=item,
                score=4,
                confidence=0.4,
                reason="R",
                profile_name="ai-robotics",
                config=_make_config(min_summary_chars=0),
            )

        assert result is None
        assert _isolate_counter[0] == 1
        assert any(
            "failure_kind=unsupported" in record.message for record in caplog.records
        )
