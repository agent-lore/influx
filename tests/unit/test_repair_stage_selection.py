"""Tests for per-note stage selection across tag-combination matrix (US-006).

Verifies that ``select_stages()`` independently picks the correct retry
stages based on the current tag set, archive path, and profile score
thresholds.  Covers the FR-ST-4 / AC-06-A regression case and the
``influx:text-terminal`` suppression of abstract-only, Tier 2, and Tier 3.
"""

from __future__ import annotations

import pytest

from influx.repair import StageSelection, select_stages

# ── Defaults ──────────────────────────────────────────────────────────

# Standard thresholds matching config defaults.
FULL_TEXT_THRESHOLD = 8
DEEP_EXTRACT_THRESHOLD = 9

# Score values relative to thresholds.
LOW_SCORE = 5
HIGH_SCORE = 9  # >= both thresholds


def _select(
    tags: list[str],
    *,
    archive_path: str | None = None,
    archive_succeeded_this_pass: bool = False,
    max_profile_score: int = LOW_SCORE,
    full_text_threshold: int = FULL_TEXT_THRESHOLD,
    deep_extract_threshold: int = DEEP_EXTRACT_THRESHOLD,
) -> StageSelection:
    return select_stages(
        tags=tags,
        archive_path=archive_path,
        archive_succeeded_this_pass=archive_succeeded_this_pass,
        max_profile_score=max_profile_score,
        full_text_threshold=full_text_threshold,
        deep_extract_threshold=deep_extract_threshold,
    )


# ── Archive-missing only ─────────────────────────────────────────────


class TestArchiveMissingOnly:
    """Note tagged ``influx:archive-missing`` only (no text:* tag)."""

    def test_archive_retry_selected(self) -> None:
        s = _select(["influx:repair-needed", "influx:archive-missing"])
        assert s.archive_retry is True

    def test_text_extraction_also_selected(self) -> None:
        """No text:* tag present -> text-extraction retry selected."""
        s = _select(["influx:repair-needed", "influx:archive-missing"])
        assert s.text_extraction_retry is True

    def test_abstract_only_not_selected(self) -> None:
        s = _select(["influx:repair-needed", "influx:archive-missing"])
        assert s.abstract_only_reextraction is False


# ── text:abstract-only only (no archive-missing) ─────────────────────


class TestAbstractOnlyOnly:
    """Note tagged ``text:abstract-only`` (no ``influx:archive-missing``)."""

    def test_archive_retry_not_selected(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path="/some/path.pdf",
        )
        assert s.archive_retry is False

    def test_text_extraction_not_selected(self) -> None:
        """text:abstract-only is a text:* tag -> no text extraction retry."""
        s = _select(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path="/some/path.pdf",
        )
        assert s.text_extraction_retry is False

    def test_abstract_only_reextraction_with_archive_path(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path="/some/path.pdf",
        )
        assert s.abstract_only_reextraction is True

    def test_abstract_only_reextraction_without_archive_path(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path=None,
            archive_succeeded_this_pass=False,
        )
        assert s.abstract_only_reextraction is False


# ── archive-missing + text:abstract-only ─────────────────────────────


class TestArchiveMissingPlusAbstractOnly:
    """Note with both ``influx:archive-missing`` + ``text:abstract-only``."""

    def test_archive_retry_selected(self) -> None:
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
            ],
        )
        assert s.archive_retry is True

    def test_text_extraction_not_selected(self) -> None:
        """text:abstract-only is a text:* tag -> no text extraction."""
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
            ],
        )
        assert s.text_extraction_retry is False

    def test_abstract_only_reextraction_if_archive_succeeded(self) -> None:
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
            ],
            archive_succeeded_this_pass=True,
        )
        assert s.abstract_only_reextraction is True

    def test_abstract_only_reextraction_without_archive_success(self) -> None:
        """No stored path + archive not yet succeeded -> not eligible."""
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
            ],
            archive_path=None,
            archive_succeeded_this_pass=False,
        )
        assert s.abstract_only_reextraction is False


# ── AC-06-A regression: text:html + archive-missing ──────────────────


class TestAC06ARegression:
    """FR-ST-4 / AC-06-A: archive retry must be independent of text:* tag.

    A note tagged ``text:html`` AND ``influx:archive-missing`` selects
    the archive stage but NOT the text-extraction stage.
    """

    def test_archive_selected_despite_text_html(self) -> None:
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:html",
            ],
        )
        assert s.archive_retry is True

    def test_text_extraction_not_selected_with_text_html(self) -> None:
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:html",
            ],
        )
        assert s.text_extraction_retry is False

    def test_abstract_only_not_selected_with_text_html(self) -> None:
        """text:html != text:abstract-only -> no reextraction."""
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:html",
            ],
            archive_path="/some/path.pdf",
        )
        assert s.abstract_only_reextraction is False

    def test_with_text_pdf(self) -> None:
        """Same logic applies when text:pdf is present."""
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:pdf",
            ],
        )
        assert s.archive_retry is True
        assert s.text_extraction_retry is False


# ── High-score deep-extract eligible ─────────────────────────────────


class TestHighScoreEligible:
    """High-score note eligible for Tier 2 and Tier 3 retry."""

    def test_tier2_selected_when_score_at_threshold(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html"],
            max_profile_score=FULL_TEXT_THRESHOLD,
        )
        assert s.tier2_retry is True

    def test_tier2_not_selected_below_threshold(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html"],
            max_profile_score=FULL_TEXT_THRESHOLD - 1,
        )
        assert s.tier2_retry is False

    def test_tier2_not_selected_when_full_text_present(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html", "full-text"],
            max_profile_score=HIGH_SCORE,
        )
        assert s.tier2_retry is False

    def test_tier3_selected_when_score_at_threshold(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html"],
            max_profile_score=DEEP_EXTRACT_THRESHOLD,
        )
        assert s.tier3_retry is True

    def test_tier3_not_selected_below_threshold(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html"],
            max_profile_score=DEEP_EXTRACT_THRESHOLD - 1,
        )
        assert s.tier3_retry is False

    def test_tier3_not_selected_when_deep_extracted_present(self) -> None:
        s = _select(
            [
                "influx:repair-needed",
                "text:html",
                "influx:deep-extracted",
            ],
            max_profile_score=HIGH_SCORE,
        )
        assert s.tier3_retry is False

    def test_both_tiers_selected_at_high_score(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html"],
            max_profile_score=HIGH_SCORE,
        )
        assert s.tier2_retry is True
        assert s.tier3_retry is True


# ── influx:text-terminal present ─────────────────────────────────────


class TestTextTerminalPresent:
    """``influx:text-terminal`` suppresses abstract-only, Tier 2, Tier 3."""

    def test_tier2_not_selected_with_terminal(self) -> None:
        s = _select(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            max_profile_score=HIGH_SCORE,
        )
        assert s.tier2_retry is False

    def test_tier3_not_selected_with_terminal(self) -> None:
        s = _select(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            max_profile_score=HIGH_SCORE,
        )
        assert s.tier3_retry is False

    def test_abstract_only_reextraction_not_selected_with_terminal(
        self,
    ) -> None:
        s = _select(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path="/some/path.pdf",
        )
        assert s.abstract_only_reextraction is False

    def test_archive_retry_still_selected_with_terminal(self) -> None:
        """Archive retry is unaffected by influx:text-terminal."""
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
                "influx:text-terminal",
            ],
        )
        assert s.archive_retry is True

    def test_all_stages_suppressed_except_archive(self) -> None:
        """With terminal + archive-missing, only archive retry is active."""
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path="/some/path.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert s.archive_retry is True
        assert s.text_extraction_retry is False
        assert s.abstract_only_reextraction is False
        assert s.tier2_retry is False
        assert s.tier3_retry is False


# ── Per-stage tier{2,3}-terminal suppression ─────────────────────────


class TestTier2TerminalPresent:
    """``influx:tier2-terminal`` suppresses Tier 2 only — Tier 3 still runs."""

    def test_tier2_not_selected_with_tier2_terminal(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html", "influx:tier2-terminal"],
            max_profile_score=HIGH_SCORE,
        )
        assert s.tier2_retry is False

    def test_tier3_still_selected_with_only_tier2_terminal(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html", "influx:tier2-terminal"],
            max_profile_score=HIGH_SCORE,
        )
        assert s.tier3_retry is True


class TestTier3TerminalPresent:
    """``influx:tier3-terminal`` suppresses Tier 3 only — Tier 2 still runs."""

    def test_tier3_not_selected_with_tier3_terminal(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html", "influx:tier3-terminal"],
            max_profile_score=HIGH_SCORE,
        )
        assert s.tier3_retry is False

    def test_tier2_still_selected_with_only_tier3_terminal(self) -> None:
        s = _select(
            ["influx:repair-needed", "text:html", "influx:tier3-terminal"],
            max_profile_score=HIGH_SCORE,
        )
        assert s.tier2_retry is True


class TestArchiveTerminalPresent:
    """``influx:archive-terminal`` caps the archive_retry stage so a
    permanently-oversize PDF stops being retried every sweep.
    """

    def test_archive_retry_not_selected_with_archive_terminal(self) -> None:
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "influx:archive-terminal",
                "text:abstract-only",
            ],
        )
        assert s.archive_retry is False

    def test_other_stages_still_eligible_with_only_archive_terminal(self) -> None:
        """Tier 2 / Tier 3 are independent of archive-terminal."""
        s = _select(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "influx:archive-terminal",
                "text:html",
            ],
            max_profile_score=HIGH_SCORE,
        )
        assert s.archive_retry is False
        assert s.tier2_retry is True
        assert s.tier3_retry is True


# ── Abstract-only re-extraction with stored archive path ─────────────


class TestAbstractOnlyWithStoredArchivePath:
    """AC: abstract-only IS selected when archive path already stored."""

    def test_selected_with_stored_path_no_archive_stage(self) -> None:
        """Even if no archive stage runs this pass, stored path suffices."""
        s = _select(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path="/archives/doc.pdf",
            archive_succeeded_this_pass=False,
        )
        assert s.abstract_only_reextraction is True

    def test_selected_with_archive_success_no_stored_path(self) -> None:
        """Archive succeeded this pass -> eligible even without path."""
        s = _select(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path=None,
            archive_succeeded_this_pass=True,
        )
        assert s.abstract_only_reextraction is True


# ── Edge cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases for stage selection."""

    def test_no_relevant_tags_at_all(self) -> None:
        """Note with only influx:repair-needed and no other relevant tags."""
        s = _select(["influx:repair-needed"])
        assert s.archive_retry is False
        assert s.text_extraction_retry is True  # no text:* tag
        assert s.abstract_only_reextraction is False
        assert s.tier2_retry is False
        assert s.tier3_retry is False

    def test_fully_repaired_note(self) -> None:
        """A note with all repairs complete selects no stages."""
        s = _select(
            [
                "influx:repair-needed",
                "text:html",
                "full-text",
                "influx:deep-extracted",
            ],
            archive_path="/some/path.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert s.archive_retry is False
        assert s.text_extraction_retry is False
        assert s.abstract_only_reextraction is False
        assert s.tier2_retry is False
        assert s.tier3_retry is False

    @pytest.mark.parametrize(
        "text_tag",
        ["text:html", "text:pdf", "text:abstract-only"],
    )
    def test_text_extraction_not_selected_for_any_text_tag(
        self,
        text_tag: str,
    ) -> None:
        """Any text:* tag suppresses text-extraction retry."""
        s = _select(["influx:repair-needed", text_tag])
        assert s.text_extraction_retry is False
