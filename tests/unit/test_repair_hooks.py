"""Tests for the repair worker hook protocol (US-003).

Verifies that the hook protocol types are importable, the
``re_extract_archive`` discriminator covers three outcomes, hooks
raise ``ExtractionError`` / ``LithosError`` as documented, and
hook implementations can be substituted by tests.
"""

from __future__ import annotations

import pytest

from influx.errors import ExtractionError, LithosError
from influx.repair import (
    ExtractionOutcome,
    ReExtractArchiveHook,
    ReExtractionResult,
    Tier2EnrichHook,
    Tier3ExtractHook,
)

# ── Fake hook implementations for substitution tests ─────────────────


def _fake_re_extract_upgrade(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    return ReExtractionResult(
        outcome=ExtractionOutcome.UPGRADE,
        upgraded_text_tag="text:html",
    )


def _fake_re_extract_terminal(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    return ReExtractionResult(
        outcome=ExtractionOutcome.TERMINAL,
    )


def _fake_re_extract_transient(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    return ReExtractionResult(
        outcome=ExtractionOutcome.TRANSIENT,
    )


def _fake_re_extract_raises_extraction_error(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    raise ExtractionError(
        "extraction failed",
        url="https://example.com/doc.pdf",
        stage="text-extract",
        detail="timeout",
    )


def _fake_re_extract_raises_lithos_error(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    raise LithosError(
        "lithos write failed",
        operation="write_note",
        detail="version_conflict",
    )


def _fake_tier2(note: dict[str, object]) -> None:
    pass


def _fake_tier2_raises(note: dict[str, object]) -> None:
    raise ExtractionError(
        "tier2 failed",
        stage="full-text",
    )


def _fake_tier3(note: dict[str, object]) -> None:
    pass


def _fake_tier3_raises(note: dict[str, object]) -> None:
    raise ExtractionError(
        "tier3 failed",
        stage="deep-extract",
    )


# ── ExtractionOutcome covers three variants ──────────────────────────


class TestExtractionOutcome:
    def test_three_variants_exist(self) -> None:
        assert ExtractionOutcome.UPGRADE.value == "upgrade"
        assert ExtractionOutcome.TERMINAL.value == "terminal"
        assert ExtractionOutcome.TRANSIENT.value == "transient"

    def test_exactly_three_variants(self) -> None:
        assert len(ExtractionOutcome) == 3


# ── ReExtractionResult discriminator ─────────────────────────────────


class TestReExtractionResult:
    def test_upgrade_carries_text_tag(self) -> None:
        r = ReExtractionResult(
            outcome=ExtractionOutcome.UPGRADE,
            upgraded_text_tag="text:pdf",
        )
        assert r.outcome is ExtractionOutcome.UPGRADE
        assert r.upgraded_text_tag == "text:pdf"

    def test_terminal_defaults_empty_tag(self) -> None:
        r = ReExtractionResult(outcome=ExtractionOutcome.TERMINAL)
        assert r.outcome is ExtractionOutcome.TERMINAL
        assert r.upgraded_text_tag == ""

    def test_transient_defaults_empty_tag(self) -> None:
        r = ReExtractionResult(outcome=ExtractionOutcome.TRANSIENT)
        assert r.outcome is ExtractionOutcome.TRANSIENT
        assert r.upgraded_text_tag == ""

    def test_frozen(self) -> None:
        r = ReExtractionResult(outcome=ExtractionOutcome.TERMINAL)
        with pytest.raises(AttributeError):
            r.outcome = ExtractionOutcome.UPGRADE  # type: ignore[misc]


# ── Hook substitution tests ──────────────────────────────────────────


class TestReExtractArchiveHookSubstitution:
    """Verify that test-provided callables satisfy the protocol."""

    def test_upgrade_callable(self) -> None:
        hook: ReExtractArchiveHook = _fake_re_extract_upgrade
        result = hook({"id": "n1"}, "arxiv/2025/01/123.pdf")
        assert result.outcome is ExtractionOutcome.UPGRADE
        assert result.upgraded_text_tag == "text:html"

    def test_terminal_callable(self) -> None:
        hook: ReExtractArchiveHook = _fake_re_extract_terminal
        result = hook({"id": "n1"}, "arxiv/2025/01/123.pdf")
        assert result.outcome is ExtractionOutcome.TERMINAL

    def test_transient_callable(self) -> None:
        hook: ReExtractArchiveHook = _fake_re_extract_transient
        result = hook({"id": "n1"}, "arxiv/2025/01/123.pdf")
        assert result.outcome is ExtractionOutcome.TRANSIENT

    def test_raises_extraction_error(self) -> None:
        hook: ReExtractArchiveHook = _fake_re_extract_raises_extraction_error
        with pytest.raises(ExtractionError):
            hook({"id": "n1"}, "arxiv/2025/01/123.pdf")

    def test_raises_lithos_error(self) -> None:
        hook: ReExtractArchiveHook = _fake_re_extract_raises_lithos_error
        with pytest.raises(LithosError):
            hook({"id": "n1"}, "arxiv/2025/01/123.pdf")


class TestTier2EnrichHookSubstitution:
    def test_success_callable(self) -> None:
        hook: Tier2EnrichHook = _fake_tier2
        hook({"id": "n1"})  # should not raise

    def test_raises_extraction_error(self) -> None:
        hook: Tier2EnrichHook = _fake_tier2_raises
        with pytest.raises(ExtractionError):
            hook({"id": "n1"})


class TestTier3ExtractHookSubstitution:
    def test_success_callable(self) -> None:
        hook: Tier3ExtractHook = _fake_tier3
        hook({"id": "n1"})  # should not raise

    def test_raises_extraction_error(self) -> None:
        hook: Tier3ExtractHook = _fake_tier3_raises
        with pytest.raises(ExtractionError):
            hook({"id": "n1"})
