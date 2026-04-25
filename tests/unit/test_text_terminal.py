"""Unit tests for influx:text-terminal three-outcome semantics (US-010).

Exercises ``apply_abstract_only_reextraction()`` for all three outcomes
(Upgrade / Terminal / Transient) and verifies AC-M2-3: influx:text-terminal
is NEVER set on a note's initial write — only via the Terminal outcome.
"""

from __future__ import annotations

from influx.errors import ExtractionError
from influx.repair import (
    ExtractionOutcome,
    ReExtractionResult,
    apply_abstract_only_reextraction,
)

# ── Fake hooks ────────────────────────────────────────────────────────


def _hook_upgrade(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    return ReExtractionResult(
        outcome=ExtractionOutcome.UPGRADE,
        upgraded_text_tag="text:html",
    )


def _hook_upgrade_pdf(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    return ReExtractionResult(
        outcome=ExtractionOutcome.UPGRADE,
        upgraded_text_tag="text:pdf",
    )


def _hook_terminal(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    return ReExtractionResult(outcome=ExtractionOutcome.TERMINAL)


def _hook_transient(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    return ReExtractionResult(outcome=ExtractionOutcome.TRANSIENT)


def _hook_raises_extraction_error(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    raise ExtractionError(
        "extraction failed",
        url="https://example.com/doc.pdf",
        stage="text-extract",
    )


# ── Helpers ───────────────────────────────────────────────────────────

_NOTE: dict[str, object] = {"id": "n1"}
_PATH = "arxiv/2025/01/123.pdf"


def _apply(
    tags: list[str],
    hook: object,
) -> list[str]:
    return apply_abstract_only_reextraction(
        tags=tags,
        note=_NOTE,
        archive_path=_PATH,
        hook=hook,  # type: ignore[arg-type]
    )


# ── Upgrade outcome ──────────────────────────────────────────────────


class TestUpgradeOutcome:
    """Fake hook returns Upgrade — text:abstract-only replaced."""

    def test_replaces_abstract_only_with_html(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
            "profile:ai",
        ]
        result = _apply(tags, _hook_upgrade)
        assert "text:html" in result
        assert "text:abstract-only" not in result

    def test_replaces_abstract_only_with_pdf(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
            "profile:ai",
        ]
        result = _apply(tags, _hook_upgrade_pdf)
        assert "text:pdf" in result
        assert "text:abstract-only" not in result

    def test_no_text_terminal_added(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
        ]
        result = _apply(tags, _hook_upgrade)
        assert "influx:text-terminal" not in result

    def test_other_tags_preserved(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
            "profile:ai",
            "influx:archive-missing",
        ]
        result = _apply(tags, _hook_upgrade)
        assert "influx:repair-needed" in result
        assert "profile:ai" in result
        assert "influx:archive-missing" in result


# ── Terminal outcome ─────────────────────────────────────────────────


class TestTerminalOutcome:
    """Fake hook returns Terminal — abstract-only kept, terminal added."""

    def test_keeps_abstract_only(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
            "profile:ai",
        ]
        result = _apply(tags, _hook_terminal)
        assert "text:abstract-only" in result

    def test_adds_text_terminal(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
        ]
        result = _apply(tags, _hook_terminal)
        assert "influx:text-terminal" in result

    def test_idempotent_if_terminal_already_present(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
            "influx:text-terminal",
        ]
        result = _apply(tags, _hook_terminal)
        assert result.count("influx:text-terminal") == 1

    def test_other_tags_preserved(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
            "profile:ai",
        ]
        result = _apply(tags, _hook_terminal)
        assert "influx:repair-needed" in result
        assert "profile:ai" in result


# ── Transient failure outcome ────────────────────────────────────────


class TestTransientOutcome:
    """Fake hook returns Transient — tags unchanged."""

    def test_keeps_abstract_only(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
        ]
        result = _apply(tags, _hook_transient)
        assert "text:abstract-only" in result

    def test_keeps_repair_needed(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
        ]
        result = _apply(tags, _hook_transient)
        assert "influx:repair-needed" in result

    def test_no_text_terminal_added(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
        ]
        result = _apply(tags, _hook_transient)
        assert "influx:text-terminal" not in result

    def test_tags_unchanged(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
            "profile:ai",
        ]
        result = _apply(tags, _hook_transient)
        assert sorted(result) == sorted(tags)


# ── ExtractionError treated as Transient ─────────────────────────────


class TestExtractionErrorAsTransient:
    """Hook raises ExtractionError — treated as Transient outcome."""

    def test_keeps_abstract_only(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
        ]
        result = _apply(tags, _hook_raises_extraction_error)
        assert "text:abstract-only" in result

    def test_keeps_repair_needed(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
        ]
        result = _apply(tags, _hook_raises_extraction_error)
        assert "influx:repair-needed" in result

    def test_no_text_terminal_added(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
        ]
        result = _apply(tags, _hook_raises_extraction_error)
        assert "influx:text-terminal" not in result

    def test_tags_unchanged(self) -> None:
        tags = [
            "influx:repair-needed",
            "text:abstract-only",
            "profile:ai",
        ]
        result = _apply(tags, _hook_raises_extraction_error)
        assert sorted(result) == sorted(tags)


# ── AC-M2-3: initial write never carries influx:text-terminal ────────


class TestInitialWriteNeverTerminal:
    """A note that has never been through re-extraction does NOT carry
    influx:text-terminal (AC-M2-3).

    influx:text-terminal is only set by the Terminal outcome of
    apply_abstract_only_reextraction(). This test verifies that
    the tag is not spuriously introduced by Upgrade or Transient
    outcomes, and that a fresh tag set without text:abstract-only
    never gains it.
    """

    def test_upgrade_does_not_introduce_terminal(self) -> None:
        tags = ["text:abstract-only", "influx:repair-needed"]
        result = _apply(tags, _hook_upgrade)
        assert "influx:text-terminal" not in result

    def test_transient_does_not_introduce_terminal(self) -> None:
        tags = ["text:abstract-only", "influx:repair-needed"]
        result = _apply(tags, _hook_transient)
        assert "influx:text-terminal" not in result

    def test_extraction_error_does_not_introduce_terminal(self) -> None:
        tags = ["text:abstract-only", "influx:repair-needed"]
        result = _apply(tags, _hook_raises_extraction_error)
        assert "influx:text-terminal" not in result

    def test_fresh_note_tags_no_terminal(self) -> None:
        """A newly-written note's tag set has no text-terminal."""
        fresh_tags = [
            "influx:repair-needed",
            "influx:archive-missing",
            "profile:ai",
        ]
        assert "influx:text-terminal" not in fresh_tags

    def test_abstract_only_note_before_reextraction_no_terminal(
        self,
    ) -> None:
        """A note tagged text:abstract-only before any re-extraction
        attempt does not carry influx:text-terminal."""
        tags = [
            "text:abstract-only",
            "influx:repair-needed",
            "profile:ai",
        ]
        assert "influx:text-terminal" not in tags


# ── Input immutability ───────────────────────────────────────────────


class TestInputImmutability:
    """apply_abstract_only_reextraction() must not mutate the input."""

    def test_upgrade_does_not_mutate_input(self) -> None:
        tags = ["text:abstract-only", "influx:repair-needed"]
        original = list(tags)
        _apply(tags, _hook_upgrade)
        assert tags == original

    def test_terminal_does_not_mutate_input(self) -> None:
        tags = ["text:abstract-only", "influx:repair-needed"]
        original = list(tags)
        _apply(tags, _hook_terminal)
        assert tags == original

    def test_transient_does_not_mutate_input(self) -> None:
        tags = ["text:abstract-only", "influx:repair-needed"]
        original = list(tags)
        _apply(tags, _hook_transient)
        assert tags == original
