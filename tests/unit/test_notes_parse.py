"""Unit tests for src/influx/notes.py parser (US-004).

Covers:
- Well-formed note with all sections
- Note missing ``## User Notes``
- Note with only title + ``## User Notes``
- Note whose ``## User Notes`` contains blank lines, unicode, nested headings
- All preserved byte-exactly
"""

from __future__ import annotations

import pytest

from influx.notes import NoteParseError, parse_note


class TestParseNoteWellFormed:
    """Well-formed note with frontmatter, title, sections, and User Notes."""

    WELL_FORMED = (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        "source_url: https://arxiv.org/abs/2601.12345\n"
        "tags:\n"
        "  - source:arxiv\n"
        "  - arxiv-id:2601.12345\n"
        "confidence: 0.8\n"
        "---\n"
        "# A Great Paper Title\n"
        "\n"
        "## Archive\n"
        "path: arxiv/2026/01/2601.12345.pdf\n"
        "\n"
        "## Summary\n"
        "This paper is about interesting things.\n"
        "\n"
        "## Profile Relevance\n"
        "Relevant to research profile.\n"
        "\n"
        "## User Notes\n"
        "My personal thoughts here.\n"
    )

    def test_frontmatter_parsed(self) -> None:
        result = parse_note(self.WELL_FORMED)
        assert "note_type: summary" in result.frontmatter_raw
        assert "source_url: https://arxiv.org/abs/2601.12345" in result.frontmatter_raw

    def test_title_parsed(self) -> None:
        result = parse_note(self.WELL_FORMED)
        assert result.title == "A Great Paper Title"

    def test_sections_parsed(self) -> None:
        result = parse_note(self.WELL_FORMED)
        headings = [s.heading for s in result.sections]
        assert headings == ["Archive", "Summary", "Profile Relevance"]

    def test_archive_section_body(self) -> None:
        result = parse_note(self.WELL_FORMED)
        archive = next(s for s in result.sections if s.heading == "Archive")
        assert "path: arxiv/2026/01/2601.12345.pdf" in archive.body

    def test_summary_section_body(self) -> None:
        result = parse_note(self.WELL_FORMED)
        summary = next(s for s in result.sections if s.heading == "Summary")
        assert "This paper is about interesting things." in summary.body

    def test_user_notes_present(self) -> None:
        result = parse_note(self.WELL_FORMED)
        assert result.user_notes is not None
        assert result.user_notes.startswith("## User Notes")
        assert "My personal thoughts here.\n" in result.user_notes


class TestParseNoteMissingUserNotes:
    """Note without ``## User Notes`` section."""

    NO_USER_NOTES = (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        "---\n"
        "# A Paper Without User Notes\n"
        "\n"
        "## Archive\n"
        "path: arxiv/2026/01/2601.99999.pdf\n"
        "\n"
        "## Summary\n"
        "Summary content here.\n"
    )

    def test_user_notes_is_none(self) -> None:
        result = parse_note(self.NO_USER_NOTES)
        assert result.user_notes is None

    def test_sections_parsed(self) -> None:
        result = parse_note(self.NO_USER_NOTES)
        headings = [s.heading for s in result.sections]
        assert headings == ["Archive", "Summary"]

    def test_title_parsed(self) -> None:
        result = parse_note(self.NO_USER_NOTES)
        assert result.title == "A Paper Without User Notes"


class TestParseNoteOnlyTitleAndUserNotes:
    """Minimal note: frontmatter + title + User Notes only."""

    MINIMAL = (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        "---\n"
        "# Minimal Note\n"
        "\n"
        "## User Notes\n"
        "Just user notes, no Influx sections.\n"
    )

    def test_title_parsed(self) -> None:
        result = parse_note(self.MINIMAL)
        assert result.title == "Minimal Note"

    def test_no_influx_sections(self) -> None:
        result = parse_note(self.MINIMAL)
        assert result.sections == ()

    def test_user_notes_present(self) -> None:
        result = parse_note(self.MINIMAL)
        assert result.user_notes is not None
        assert "Just user notes, no Influx sections.\n" in result.user_notes


class TestUserNotesBytePreservation:
    """User Notes region is preserved byte-exactly."""

    USER_NOTES_CONTENT = (
        "## User Notes\n"
        "Line 1 with unicode: \u00e9\u00e0\u00fc\u00f1 \U0001f600\n"
        "\n"
        "\n"
        "Blank lines above preserved.\n"
        "\n"
        "### Nested heading inside User Notes\n"
        "Content under nested heading.\n"
        "\n"
        "## Another H2 inside User Notes\n"
        "This is NOT parsed as an Influx section.\n"
        "\n"
        "    Indented code block\n"
        "    with multiple lines\n"
    )

    NOTE = (
        "---\n"
        "note_type: summary\n"
        "---\n"
        "# Unicode & Nested Test\n"
        "\n"
        "## Summary\n"
        "A summary.\n"
        "\n"
        + USER_NOTES_CONTENT
    )

    def test_user_notes_exact_bytes(self) -> None:
        result = parse_note(self.NOTE)
        assert result.user_notes is not None
        assert result.user_notes == self.USER_NOTES_CONTENT

    def test_unicode_preserved(self) -> None:
        result = parse_note(self.NOTE)
        assert result.user_notes is not None
        assert "\u00e9\u00e0\u00fc\u00f1 \U0001f600" in result.user_notes

    def test_blank_lines_preserved(self) -> None:
        result = parse_note(self.NOTE)
        assert result.user_notes is not None
        assert "\n\n\nBlank lines above preserved." in result.user_notes

    def test_nested_headings_preserved(self) -> None:
        result = parse_note(self.NOTE)
        assert result.user_notes is not None
        assert "### Nested heading inside User Notes" in result.user_notes
        assert "## Another H2 inside User Notes" in result.user_notes

    def test_influx_sections_not_polluted(self) -> None:
        """H2s inside User Notes must NOT appear as Influx sections."""
        result = parse_note(self.NOTE)
        headings = [s.heading for s in result.sections]
        assert "Another H2 inside User Notes" not in headings
        assert headings == ["Summary"]


class TestParseNoteErrors:
    """Error cases for malformed notes."""

    def test_no_frontmatter_fence(self) -> None:
        with pytest.raises(NoteParseError, match="frontmatter fence"):
            parse_note("# Title\nContent\n")

    def test_no_closing_fence(self) -> None:
        with pytest.raises(NoteParseError, match="closing frontmatter fence"):
            parse_note("---\nnote_type: summary\n# Title\n")

    def test_no_title(self) -> None:
        with pytest.raises(NoteParseError, match="title heading"):
            parse_note("---\nnote_type: summary\n---\nNo heading here.\n")


class TestFrontmatterContent:
    """Frontmatter raw content is captured correctly."""

    NOTE = (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        "tags:\n"
        "  - source:arxiv\n"
        "  - profile:research\n"
        "  - favourite\n"
        "confidence: 0.9\n"
        "---\n"
        "# Test Title\n"
    )

    def test_frontmatter_contains_tags(self) -> None:
        result = parse_note(self.NOTE)
        assert "- source:arxiv" in result.frontmatter_raw
        assert "- profile:research" in result.frontmatter_raw
        assert "- favourite" in result.frontmatter_raw

    def test_frontmatter_contains_confidence(self) -> None:
        result = parse_note(self.NOTE)
        assert "confidence: 0.9" in result.frontmatter_raw

    def test_frontmatter_excludes_fences(self) -> None:
        result = parse_note(self.NOTE)
        assert not result.frontmatter_raw.startswith("---")
        assert not result.frontmatter_raw.endswith("---")
