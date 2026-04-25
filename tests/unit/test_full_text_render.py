"""Unit tests for ## Full Text section rendering (US-011).

Covers:
- Section rendered when full_text is provided
- Section omitted when full_text is absent (None)
- Section omitted when full_text is empty string
- Ordering: ## Full Text appears after ## Summary and before ## User Notes
"""

from __future__ import annotations

from influx.notes import render_note

_BASE_TAGS = ["source:arxiv", "ingested-by:influx", "schema:1"]
_BASE_KWARGS = {
    "title": "Test Note",
    "source_url": "https://arxiv.org/abs/2601.00001",
    "tags": _BASE_TAGS,
    "confidence": 0.8,
    "archive_path": None,
    "summary": "A short summary.",
    "keywords": [],
    "profile_entries": [],
}


class TestFullTextRendered:
    """## Full Text section is emitted when full_text is provided."""

    def test_full_text_section_present(self) -> None:
        rendered = render_note(**_BASE_KWARGS, full_text="Extracted article body.")
        assert "## Full Text" in rendered

    def test_full_text_body_content(self) -> None:
        text = "This is the full extracted article text spanning multiple paragraphs."
        rendered = render_note(**_BASE_KWARGS, full_text=text)
        assert text in rendered

    def test_full_text_multiline_body(self) -> None:
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        rendered = render_note(**_BASE_KWARGS, full_text=text)
        assert "## Full Text\n" in rendered
        assert "Paragraph one." in rendered
        assert "Paragraph three." in rendered


class TestFullTextOmitted:
    """## Full Text section is omitted when input is absent or empty (FR-ENR-6)."""

    def test_omitted_when_none(self) -> None:
        rendered = render_note(**_BASE_KWARGS, full_text=None)
        assert "## Full Text" not in rendered

    def test_omitted_when_default(self) -> None:
        rendered = render_note(**_BASE_KWARGS)
        assert "## Full Text" not in rendered

    def test_omitted_when_empty_string(self) -> None:
        rendered = render_note(**_BASE_KWARGS, full_text="")
        assert "## Full Text" not in rendered

    def test_no_placeholder_when_absent(self) -> None:
        """FR-ENR-6: no placeholder text on failure — section simply absent."""
        rendered = render_note(**_BASE_KWARGS, full_text=None)
        assert "Full Text" not in rendered


class TestFullTextOrdering:
    """## Full Text appears in the correct canonical position."""

    def test_after_summary_before_user_notes(self) -> None:
        rendered = render_note(**_BASE_KWARGS, full_text="Some body text.")
        summary_pos = rendered.index("## Summary")
        full_text_pos = rendered.index("## Full Text")
        user_notes_pos = rendered.index("## User Notes")
        assert summary_pos < full_text_pos < user_notes_pos

    def test_after_summary_before_profile_relevance(self) -> None:
        rendered = render_note(**_BASE_KWARGS, full_text="Some body text.")
        summary_pos = rendered.index("## Summary")
        full_text_pos = rendered.index("## Full Text")
        profile_pos = rendered.index("## Profile Relevance")
        assert summary_pos < full_text_pos < profile_pos

    def test_ordering_preserved_without_full_text(self) -> None:
        """Existing section order unchanged when full_text is absent."""
        rendered = render_note(**_BASE_KWARGS, full_text=None)
        summary_pos = rendered.index("## Summary")
        profile_pos = rendered.index("## Profile Relevance")
        user_notes_pos = rendered.index("## User Notes")
        assert summary_pos < profile_pos < user_notes_pos
