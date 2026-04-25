"""Tests for post-stage tag-clearing rules (US-008).

Verifies that ``compute_clearing()`` removes ``influx:archive-missing``
and ``influx:repair-needed`` exactly per PRD 06 §5.3, including the
high-score terminal exemption (AC-X-7) and the abstract-only-without-
terminal lock (AC-06-C).
"""

from __future__ import annotations

from influx.repair import ClearingDecision, compute_clearing

# ── Defaults ──────────────────────────────────────────────────────────

FULL_TEXT_THRESHOLD = 8
DEEP_EXTRACT_THRESHOLD = 9

LOW_SCORE = 5
HIGH_SCORE = 9  # >= both thresholds


def _clear(
    tags: list[str],
    *,
    archive_path: str | None = None,
    max_profile_score: int = LOW_SCORE,
    full_text_threshold: int = FULL_TEXT_THRESHOLD,
    deep_extract_threshold: int = DEEP_EXTRACT_THRESHOLD,
) -> ClearingDecision:
    return compute_clearing(
        tags=tags,
        archive_path=archive_path,
        max_profile_score=max_profile_score,
        full_text_threshold=full_text_threshold,
        deep_extract_threshold=deep_extract_threshold,
    )


# ── Archive-missing only ─────────────────────────────────────────────


class TestArchiveMissingOnly:
    """Note tagged ``influx:archive-missing`` only (no text:* tag)."""

    def test_archive_missing_cleared_when_path_stored(self) -> None:
        d = _clear(
            ["influx:repair-needed", "influx:archive-missing"],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_archive_missing is True

    def test_archive_missing_not_cleared_without_path(self) -> None:
        d = _clear(
            ["influx:repair-needed", "influx:archive-missing"],
            archive_path=None,
        )
        assert d.clear_archive_missing is False

    def test_repair_needed_not_cleared_without_text_tag(self) -> None:
        """No text:* tag -> condition (b) fails -> not cleared."""
        d = _clear(
            ["influx:repair-needed", "influx:archive-missing"],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_repair_needed is False

    def test_repair_needed_not_cleared_without_archive_path(self) -> None:
        """No archive path -> condition (a) fails -> not cleared."""
        d = _clear(
            ["influx:repair-needed", "influx:archive-missing"],
            archive_path=None,
        )
        assert d.clear_repair_needed is False


# ── text:abstract-only only (no archive-missing) ─────────────────────


class TestAbstractOnlyOnly:
    """Note tagged ``text:abstract-only`` without ``influx:text-terminal``."""

    def test_archive_missing_cleared_when_path_stored(self) -> None:
        d = _clear(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_archive_missing is True

    def test_repair_needed_not_cleared_without_terminal(self) -> None:
        """AC-06-C: abstract-only without terminal is NEVER cleared."""
        d = _clear(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_repair_needed is False

    def test_repair_needed_not_cleared_without_archive(self) -> None:
        d = _clear(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path=None,
        )
        assert d.clear_repair_needed is False


# ── archive-missing + text:abstract-only ─────────────────────────────


class TestArchiveMissingPlusAbstractOnly:
    """Note with both ``influx:archive-missing`` + ``text:abstract-only``."""

    def test_archive_missing_cleared_when_path_stored(self) -> None:
        d = _clear(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
            ],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_archive_missing is True

    def test_repair_needed_not_cleared_without_terminal(self) -> None:
        """AC-06-C: even with archive path, abstract-only blocks clearing."""
        d = _clear(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
            ],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_repair_needed is False

    def test_neither_cleared_without_archive(self) -> None:
        d = _clear(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
            ],
            archive_path=None,
        )
        assert d.clear_archive_missing is False
        assert d.clear_repair_needed is False


# ── High-score deep-extract eligible ─────────────────────────────────


class TestHighScoreEligible:
    """High-score note with ``text:html`` — Tier 2/3 requirements apply."""

    def test_cleared_when_all_tiers_present(self) -> None:
        """All conditions met: archive + text:html + full-text + deep-extracted."""
        d = _clear(
            [
                "influx:repair-needed",
                "text:html",
                "full-text",
                "influx:deep-extracted",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert d.clear_repair_needed is True

    def test_not_cleared_missing_full_text(self) -> None:
        """Score >= full_text threshold but full-text tag missing."""
        d = _clear(
            [
                "influx:repair-needed",
                "text:html",
                "influx:deep-extracted",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert d.clear_repair_needed is False

    def test_not_cleared_missing_deep_extracted(self) -> None:
        """Score >= deep_extract threshold but deep-extracted tag missing."""
        d = _clear(
            [
                "influx:repair-needed",
                "text:html",
                "full-text",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert d.clear_repair_needed is False

    def test_cleared_when_score_below_thresholds(self) -> None:
        """Low score: Tier 2/3 not required -> text:html + archive clears."""
        d = _clear(
            ["influx:repair-needed", "text:html"],
            archive_path="/archives/doc.pdf",
            max_profile_score=LOW_SCORE,
        )
        assert d.clear_repair_needed is True

    def test_tier2_required_at_exact_threshold(self) -> None:
        """Score == full_text_threshold: full-text required."""
        d = _clear(
            ["influx:repair-needed", "text:html"],
            archive_path="/archives/doc.pdf",
            max_profile_score=FULL_TEXT_THRESHOLD,
        )
        assert d.clear_repair_needed is False

    def test_tier2_not_required_below_threshold(self) -> None:
        """Score one below threshold: full-text not required."""
        d = _clear(
            ["influx:repair-needed", "text:html"],
            archive_path="/archives/doc.pdf",
            max_profile_score=FULL_TEXT_THRESHOLD - 1,
        )
        assert d.clear_repair_needed is True

    def test_tier3_required_at_exact_threshold(self) -> None:
        """Score == deep_extract_threshold: deep-extracted required."""
        d = _clear(
            ["influx:repair-needed", "text:html", "full-text"],
            archive_path="/archives/doc.pdf",
            max_profile_score=DEEP_EXTRACT_THRESHOLD,
        )
        assert d.clear_repair_needed is False

    def test_text_pdf_also_satisfies_text_condition(self) -> None:
        """text:pdf is equally valid for condition (b)."""
        d = _clear(
            [
                "influx:repair-needed",
                "text:pdf",
                "full-text",
                "influx:deep-extracted",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert d.clear_repair_needed is True


# ── influx:text-terminal present ─────────────────────────────────────


class TestTextTerminalPresent:
    """``influx:text-terminal`` triggers high-score terminal exemption."""

    def test_repair_needed_cleared_with_terminal_and_archive(self) -> None:
        """AC-X-7: terminal abstract-only + archive path -> cleared."""
        d = _clear(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_repair_needed is True

    def test_high_score_terminal_exemption(self) -> None:
        """AC-X-7 positive: high score + terminal -> Tier 2/3 waived."""
        d = _clear(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert d.clear_repair_needed is True

    def test_archive_missing_still_cleared_with_terminal(self) -> None:
        d = _clear(
            [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_archive_missing is True

    def test_not_cleared_without_archive_path(self) -> None:
        """Even with terminal, condition (a) must hold."""
        d = _clear(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path=None,
        )
        assert d.clear_repair_needed is False


# ── AC-06-B: archive + abstract-only upgrade in one pass ─────────────


class TestAC06BOnePassUpgrade:
    """Archive download + abstract-only Upgrade in one pass clears BOTH.

    After a successful upgrade, the note carries ``text:html`` (or
    ``text:pdf``) instead of ``text:abstract-only``.  With no other
    outstanding-stage tag, BOTH ``influx:archive-missing`` and
    ``influx:repair-needed`` should be cleared.
    """

    def test_both_cleared_after_upgrade_to_html(self) -> None:
        d = _clear(
            ["influx:repair-needed", "influx:archive-missing", "text:html"],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_archive_missing is True
        assert d.clear_repair_needed is True

    def test_both_cleared_after_upgrade_to_pdf(self) -> None:
        d = _clear(
            ["influx:repair-needed", "influx:archive-missing", "text:pdf"],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_archive_missing is True
        assert d.clear_repair_needed is True

    def test_repair_needed_not_cleared_if_tier2_outstanding(self) -> None:
        """High score + missing full-text -> repair-needed stays."""
        d = _clear(
            ["influx:repair-needed", "influx:archive-missing", "text:html"],
            archive_path="/archives/doc.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert d.clear_archive_missing is True
        assert d.clear_repair_needed is False


# ── AC-06-C: abstract-only WITHOUT terminal is NEVER cleared ─────────


class TestAC06CAbstractOnlyNeverCleared:
    """``text:abstract-only`` without ``influx:text-terminal`` blocks clearing.

    Even if archive + Tier 2 + Tier 3 all succeed, the abstract-only
    state must first be resolved via Upgrade or Terminal before
    ``influx:repair-needed`` can be cleared.
    """

    def test_not_cleared_with_all_tiers_present(self) -> None:
        d = _clear(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "full-text",
                "influx:deep-extracted",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert d.clear_repair_needed is False

    def test_not_cleared_with_archive_only(self) -> None:
        d = _clear(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_repair_needed is False

    def test_not_cleared_at_low_score(self) -> None:
        """Even when Tier 2/3 are not required by score."""
        d = _clear(
            ["influx:repair-needed", "text:abstract-only"],
            archive_path="/archives/doc.pdf",
            max_profile_score=LOW_SCORE,
        )
        assert d.clear_repair_needed is False


# ── AC-X-7 high-score-terminal positive: full exemption ──────────────


class TestACX7HighScoreTerminal:
    """A terminal-abstract-only note with high score clears ``repair-needed``.

    Seeds: score=9 (>= deep_extract), ``text:abstract-only`` +
    ``influx:text-terminal``, archive path stored, no other outstanding
    tag.  Should clear ``influx:repair-needed`` even without ``full-text``
    or ``influx:deep-extracted``.
    """

    def test_cleared_without_full_text_or_deep_extracted(self) -> None:
        d = _clear(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert d.clear_repair_needed is True

    def test_cleared_at_deep_extract_threshold_exact(self) -> None:
        d = _clear(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=DEEP_EXTRACT_THRESHOLD,
        )
        assert d.clear_repair_needed is True

    def test_cleared_at_full_text_threshold_exact(self) -> None:
        """Even at FULL_TEXT_THRESHOLD (below DEEP_EXTRACT) -> cleared."""
        d = _clear(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=FULL_TEXT_THRESHOLD,
        )
        assert d.clear_repair_needed is True

    def test_cleared_at_low_score_with_terminal(self) -> None:
        """Terminal exemption applies regardless of score magnitude."""
        d = _clear(
            [
                "influx:repair-needed",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            archive_path="/archives/doc.pdf",
            max_profile_score=LOW_SCORE,
        )
        assert d.clear_repair_needed is True


# ── Partial repair: unsatisfied tags remain ──────────────────────────


class TestPartialRepair:
    """Partially-completed repair keeps ``influx:repair-needed``."""

    def test_archive_only_complete(self) -> None:
        """Archive fixed but no text:* tag -> not cleared."""
        d = _clear(
            ["influx:repair-needed", "influx:archive-missing"],
            archive_path="/archives/doc.pdf",
        )
        assert d.clear_archive_missing is True
        assert d.clear_repair_needed is False

    def test_text_only_complete_no_archive(self) -> None:
        """text:html present but no archive path -> not cleared."""
        d = _clear(
            ["influx:repair-needed", "text:html"],
            archive_path=None,
        )
        assert d.clear_repair_needed is False

    def test_full_text_but_no_deep_extract_at_high_score(self) -> None:
        """full-text present but deep-extracted missing at high score."""
        d = _clear(
            ["influx:repair-needed", "text:html", "full-text"],
            archive_path="/archives/doc.pdf",
            max_profile_score=HIGH_SCORE,
        )
        assert d.clear_repair_needed is False
