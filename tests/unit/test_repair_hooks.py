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


class TestNoteSourceUrlResolution:
    """Regression for #218.

    The repair sweep must read ``source_url`` from the doc-level
    ``lithos_read`` envelope (top-level, or nested under ``metadata``),
    NOT from a ``source_url:`` line embedded in the content body. The
    content-body shape was the pre-fix legacy zombie shape; reading it
    stranded every correctly-written ``influx:archive-missing`` note
    with ``ExtractionError: no source_url in frontmatter`` even though
    the URL was present at the doc level.
    """

    def test_reads_top_level_source_url(self) -> None:
        from influx.source import note_source_url

        note: dict[str, object] = {
            "id": "rss-some-item",
            "source_url": "https://example.com/article",
            "content": "# Title\n\n## Archive\n",  # no embedded frontmatter
        }
        assert note_source_url(note) == "https://example.com/article"

    def test_falls_back_to_metadata_nested_source_url(self) -> None:
        from influx.source import note_source_url

        note: dict[str, object] = {
            "id": "rss-some-item",
            "metadata": {"source_url": "https://example.com/nested"},
            "content": "# Title\n\n## Archive\n",
        }
        assert note_source_url(note) == "https://example.com/nested"

    def test_does_not_read_content_body_frontmatter(self) -> None:
        # The removed legacy shape: source_url ONLY in the content body.
        # The sweep must NOT resurrect it — doc-level is the only source.
        from influx.source import note_source_url

        note: dict[str, object] = {
            "id": "rss-some-item",
            "content": ("---\nsource_url: https://example.com/in-body\n---\n# Title\n"),
        }
        assert note_source_url(note) is None

    def test_returns_none_when_absent_everywhere(self) -> None:
        from influx.source import note_source_url

        note: dict[str, object] = {"id": "rss-x", "content": "# Title\n"}
        assert note_source_url(note) is None

    def test_ignores_non_string_source_url(self) -> None:
        from influx.source import note_source_url

        none_url: dict[str, object] = {"id": "rss-x", "source_url": None}
        empty_url: dict[str, object] = {"id": "rss-x", "source_url": ""}
        assert note_source_url(none_url) is None
        assert note_source_url(empty_url) is None
