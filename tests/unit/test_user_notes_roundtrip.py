"""Round-trip tests for the full canonical renderer (US-007, AC-04-A).

Covers:
- Byte-exact User Notes preservation across parse → rewrite cycles
- Golden-file regression for section ordering and whitespace
- Profile Relevance rejection guard (FR-NOTE-6)
- ingested-by:influx tag presence (FR-RES-6)
- Confidence computation on initial creation and rewrite (FR-NOTE-3/8)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from influx.notes import (
    ArchiveInvariantError,
    ProfileRelevanceEntry,
    build_profile_relevance_for_rewrite,
    parse_note,
    parse_profile_relevance,
    recompute_confidence,
    render_note,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


# ── Golden-file regression ───────────────────────────────────────────


class TestGoldenFileRoundTrip:
    """Golden file backs the round-trip to catch section/whitespace regressions."""

    GOLDEN_PATH = FIXTURES_DIR / "golden_note.md"

    @pytest.fixture()
    def golden_text(self) -> str:
        return self.GOLDEN_PATH.read_text()

    def test_render_matches_golden_file(self, golden_text: str) -> None:
        rendered = render_note(
            title="Attention Is All You Need (Redux)",
            source_url="https://arxiv.org/abs/2601.12345",
            tags=[
                "source:arxiv",
                "arxiv-id:2601.12345",
                "cat:cs.AI",
                "cat:cs.RO",
                "ingested-by:influx",
                "schema:1",
                "profile:research",
                "favourite",
            ],
            confidence=0.8,
            archive_path="arxiv/2026/01/2601.12345.pdf",
            summary=(
                "This paper revisits the transformer architecture"
                " with novel insights."
            ),
            keywords=["transformer", "attention", "neural networks"],
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name="research",
                    score=8,
                    reason="Highly relevant to AI research interests.",
                ),
            ],
            user_notes=(
                "## User Notes\n"
                "My thoughts on this paper.\n"
                "\n"
                "Some more **markdown** and unicode:"
                " \u00e9\u00e0\u00fc\u00f1 \U0001f600\n"
                "\n"
                "### A nested heading in user notes\n"
                "With content below it.\n"
                "\n"
                "## Another H2 inside user notes\n"
                "This should be preserved, not parsed as an Influx section.\n"
            ),
        )
        assert rendered == golden_text

    def test_parse_rerender_matches_golden(self, golden_text: str) -> None:
        """Parse the golden file and re-render — output must match."""
        parsed = parse_note(golden_text)
        profile_entries = parse_profile_relevance(parsed)

        rendered = render_note(
            title=parsed.title,
            source_url="https://arxiv.org/abs/2601.12345",
            tags=[
                "source:arxiv",
                "arxiv-id:2601.12345",
                "cat:cs.AI",
                "cat:cs.RO",
                "ingested-by:influx",
                "schema:1",
                "profile:research",
                "favourite",
            ],
            confidence=0.8,
            archive_path="arxiv/2026/01/2601.12345.pdf",
            summary=(
                "This paper revisits the transformer architecture"
                " with novel insights."
            ),
            keywords=["transformer", "attention", "neural networks"],
            profile_entries=profile_entries,
            user_notes=parsed.user_notes,
        )
        assert rendered == golden_text


# ── AC-04-A: User Notes byte-exact preservation ─────────────────────


class TestUserNotesRoundTrip:
    """Parse → rewrite preserves User Notes byte-exactly (AC-04-A, R-5)."""

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

    ORIGINAL_NOTE = (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        "source_url: https://arxiv.org/abs/2601.99999\n"
        "tags:\n"
        "  - source:arxiv\n"
        "  - arxiv-id:2601.99999\n"
        "  - ingested-by:influx\n"
        "  - schema:1\n"
        "  - profile:research\n"
        "confidence: 0.7\n"
        "---\n"
        "# Complex User Notes Test\n"
        "\n"
        "## Archive\n"
        "path: arxiv/2026/01/2601.99999.pdf\n"
        "\n"
        "## Summary\n"
        "An interesting paper.\n"
        "\n"
        "## Profile Relevance\n"
        "### research\n"
        "Score: 7/10\n"
        "Old relevance text.\n"
        "\n"
        + USER_NOTES_CONTENT
    )

    def test_user_notes_survive_rewrite(self) -> None:
        """Non-trivial User Notes survive parse → rewrite cycle."""
        parsed = parse_note(self.ORIGINAL_NOTE)
        assert parsed.user_notes == self.USER_NOTES_CONTENT

        rewritten = render_note(
            title=parsed.title,
            source_url="https://arxiv.org/abs/2601.99999",
            tags=[
                "source:arxiv",
                "arxiv-id:2601.99999",
                "ingested-by:influx",
                "schema:1",
                "profile:research",
            ],
            confidence=0.8,
            archive_path="arxiv/2026/01/2601.99999.pdf",
            summary="Updated summary for the paper.",
            keywords=["new-keyword"],
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name="research",
                    score=8,
                    reason="Updated relevance.",
                ),
            ],
            user_notes=parsed.user_notes,
        )

        # Parse the rewritten note and check User Notes
        reparsed = parse_note(rewritten)
        assert reparsed.user_notes == self.USER_NOTES_CONTENT

    def test_unicode_preserved_in_roundtrip(self) -> None:
        parsed = parse_note(self.ORIGINAL_NOTE)
        rewritten = render_note(
            title=parsed.title,
            source_url="https://arxiv.org/abs/2601.99999",
            tags=["source:arxiv", "ingested-by:influx", "schema:1"],
            confidence=0.7,
            archive_path="arxiv/2026/01/2601.99999.pdf",
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=parsed.user_notes,
        )
        reparsed = parse_note(rewritten)
        assert reparsed.user_notes is not None
        assert "\u00e9\u00e0\u00fc\u00f1 \U0001f600" in reparsed.user_notes

    def test_blank_lines_preserved_in_roundtrip(self) -> None:
        parsed = parse_note(self.ORIGINAL_NOTE)
        rewritten = render_note(
            title=parsed.title,
            source_url="https://arxiv.org/abs/2601.99999",
            tags=["source:arxiv", "ingested-by:influx", "schema:1"],
            confidence=0.7,
            archive_path="arxiv/2026/01/2601.99999.pdf",
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=parsed.user_notes,
        )
        reparsed = parse_note(rewritten)
        assert reparsed.user_notes is not None
        assert "\n\n\nBlank lines above preserved." in reparsed.user_notes

    def test_nested_headings_preserved_in_roundtrip(self) -> None:
        parsed = parse_note(self.ORIGINAL_NOTE)
        rewritten = render_note(
            title=parsed.title,
            source_url="https://arxiv.org/abs/2601.99999",
            tags=["source:arxiv", "ingested-by:influx", "schema:1"],
            confidence=0.7,
            archive_path="arxiv/2026/01/2601.99999.pdf",
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=parsed.user_notes,
        )
        reparsed = parse_note(rewritten)
        assert reparsed.user_notes is not None
        assert "### Nested heading inside User Notes" in reparsed.user_notes
        assert "## Another H2 inside User Notes" in reparsed.user_notes

    def test_absent_user_notes_gets_empty_heading(self) -> None:
        """When User Notes is absent, renderer appends empty heading."""
        rendered = render_note(
            title="No User Notes Yet",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=["source:arxiv", "ingested-by:influx", "schema:1"],
            confidence=0.7,
            archive_path=None,
            summary="A summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        assert "## User Notes\n" in rendered
        parsed = parse_note(rendered)
        assert parsed.user_notes is not None
        assert parsed.user_notes.startswith("## User Notes")


# ── FR-NOTE-6: Rejected profiles not refreshed ──────────────────────


class TestRejectedProfileNotRefreshed:
    """On rewrite, rejected profile entries are NOT refreshed."""

    def test_rejected_profile_keeps_old_entry(self) -> None:
        old_entries = [
            ProfileRelevanceEntry(
                profile_name="research",
                score=7,
                reason="Old research relevance.",
            ),
            ProfileRelevanceEntry(
                profile_name="engineering",
                score=6,
                reason="Old engineering relevance.",
            ),
        ]
        new_entries = [
            ProfileRelevanceEntry(
                profile_name="research",
                score=9,
                reason="Updated research relevance.",
            ),
            ProfileRelevanceEntry(
                profile_name="engineering",
                score=8,
                reason="Updated engineering relevance.",
            ),
        ]
        tags = [
            "source:arxiv",
            "ingested-by:influx",
            "profile:research",
            "influx:rejected:engineering",
        ]
        resolved = build_profile_relevance_for_rewrite(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )

        by_name = {e.profile_name: e for e in resolved}
        # research is NOT rejected — uses new entry
        assert by_name["research"].score == 9
        assert by_name["research"].reason == "Updated research relevance."
        # engineering IS rejected — keeps old entry
        assert by_name["engineering"].score == 6
        assert by_name["engineering"].reason == "Old engineering relevance."

    def test_rejected_profile_in_rendered_note(self) -> None:
        """Full render with rejected profile preserves old entry text."""
        old_entries = [
            ProfileRelevanceEntry(
                profile_name="research",
                score=7,
                reason="Old research text.",
            ),
        ]
        new_entries = [
            ProfileRelevanceEntry(
                profile_name="research",
                score=9,
                reason="New research text.",
            ),
        ]
        tags = [
            "source:arxiv",
            "ingested-by:influx",
            "influx:rejected:research",
        ]
        resolved = build_profile_relevance_for_rewrite(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )
        rendered = render_note(
            title="Rejection Test",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=tags,
            confidence=0.7,
            archive_path=None,
            summary="Summary.",
            keywords=[],
            profile_entries=resolved,
            user_notes=None,
        )
        assert "Old research text." in rendered
        assert "New research text." not in rendered


# ── FR-RES-6: ingested-by:influx tag ────────────────────────────────


class TestIngestedByTag:
    """All Influx-authored notes carry ingested-by:influx."""

    def test_ingested_by_influx_in_rendered_note(self) -> None:
        tags = [
            "source:arxiv",
            "ingested-by:influx",
            "schema:1",
        ]
        rendered = render_note(
            title="Tag Test",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=tags,
            confidence=0.7,
            archive_path=None,
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        assert "  - ingested-by:influx" in rendered


# ── Confidence computation ───────────────────────────────────────────


class TestConfidenceInRenderer:
    """Confidence rendered correctly for initial and rewrite cases."""

    def test_initial_confidence(self) -> None:
        """Initial creation: confidence = max(profile_scores) / 10.0."""
        rendered = render_note(
            title="Initial",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=["source:arxiv", "ingested-by:influx", "schema:1"],
            confidence=8 / 10.0,
            archive_path=None,
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        assert "confidence: 0.8" in rendered

    def test_rewrite_confidence_increases(self) -> None:
        """Rewrite: confidence = max(existing, new_score / 10.0)."""
        new_confidence = recompute_confidence(
            existing_confidence=0.7,
            current_max_score=9,
        )
        rendered = render_note(
            title="Rewrite",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=["source:arxiv", "ingested-by:influx", "schema:1"],
            confidence=new_confidence,
            archive_path=None,
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        assert "confidence: 0.9" in rendered

    def test_rewrite_confidence_no_decrease(self) -> None:
        """Rewrite: confidence never decreases."""
        new_confidence = recompute_confidence(
            existing_confidence=0.9,
            current_max_score=7,
        )
        rendered = render_note(
            title="Rewrite",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=["source:arxiv", "ingested-by:influx", "schema:1"],
            confidence=new_confidence,
            archive_path=None,
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        assert "confidence: 0.9" in rendered


# ── Section ordering ─────────────────────────────────────────────────


class TestSectionOrdering:
    """Section ordering: title → Archive → Summary → Profile Relevance → User Notes."""

    def test_archive_before_summary(self) -> None:
        rendered = render_note(
            title="Order Test",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=["source:arxiv", "ingested-by:influx", "schema:1"],
            confidence=0.8,
            archive_path="arxiv/2026/01/2601.00001.pdf",
            summary="Summary text.",
            keywords=[],
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name="research",
                    score=8,
                    reason="Relevant.",
                ),
            ],
            user_notes=None,
        )
        archive_pos = rendered.index("## Archive")
        summary_pos = rendered.index("## Summary")
        profile_pos = rendered.index("## Profile Relevance")
        user_notes_pos = rendered.index("## User Notes")

        assert archive_pos < summary_pos
        assert summary_pos < profile_pos
        assert profile_pos < user_notes_pos

    def test_archive_immediately_after_title(self) -> None:
        rendered = render_note(
            title="Order Test",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=["source:arxiv", "ingested-by:influx", "schema:1"],
            confidence=0.8,
            archive_path="arxiv/2026/01/2601.00001.pdf",
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        title_pos = rendered.index("# Order Test")
        archive_pos = rendered.index("## Archive")
        # No other ## section between title and archive
        between = rendered[title_pos:archive_pos]
        assert "## " not in between.replace("# Order Test", "")


# ── Archive invariant enforced by renderer ───────────────────────────


class TestRendererArchiveInvariant:
    """Renderer enforces: never write path + influx:archive-missing."""

    def test_path_with_archive_missing_raises(self) -> None:
        with pytest.raises(ArchiveInvariantError):
            render_note(
                title="Invariant Test",
                source_url="https://arxiv.org/abs/2601.00001",
                tags=["source:arxiv", "influx:archive-missing"],
                confidence=0.7,
                archive_path="arxiv/2026/01/2601.00001.pdf",
                summary="Summary.",
                keywords=[],
                profile_entries=[],
                user_notes=None,
            )


# ── Frontmatter fields ──────────────────────────────────────────────


class TestFrontmatterFields:
    """Frontmatter includes required fields."""

    def test_note_type_summary(self) -> None:
        rendered = render_note(
            title="FM Test",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=["source:arxiv"],
            confidence=0.7,
            archive_path=None,
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        assert "note_type: summary" in rendered

    def test_namespace_influx(self) -> None:
        rendered = render_note(
            title="FM Test",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=["source:arxiv"],
            confidence=0.7,
            archive_path=None,
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        assert "namespace: influx" in rendered

    def test_source_url_present(self) -> None:
        rendered = render_note(
            title="FM Test",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=["source:arxiv"],
            confidence=0.7,
            archive_path=None,
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        assert "source_url: https://arxiv.org/abs/2601.00001" in rendered

    def test_empty_tags_renders_empty_list(self) -> None:
        rendered = render_note(
            title="FM Test",
            source_url="https://arxiv.org/abs/2601.00001",
            tags=[],
            confidence=0.7,
            archive_path=None,
            summary="Summary.",
            keywords=[],
            profile_entries=[],
            user_notes=None,
        )
        assert "tags: []" in rendered


# ── parse_profile_relevance ──────────────────────────────────────────


class TestParseProfileRelevance:
    """parse_profile_relevance extracts entries from parsed note."""

    NOTE_WITH_PROFILES = (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        "source_url: https://arxiv.org/abs/2601.00001\n"
        "tags:\n"
        "  - source:arxiv\n"
        "confidence: 0.8\n"
        "---\n"
        "# Test\n"
        "\n"
        "## Archive\n"
        "\n"
        "## Summary\n"
        "Summary.\n"
        "\n"
        "## Profile Relevance\n"
        "### research\n"
        "Score: 8/10\n"
        "Research reason.\n"
        "\n"
        "### engineering\n"
        "Score: 7/10\n"
        "Engineering reason.\n"
        "\n"
        "## User Notes\n"
        "Notes.\n"
    )

    def test_extracts_two_entries(self) -> None:
        parsed = parse_note(self.NOTE_WITH_PROFILES)
        entries = parse_profile_relevance(parsed)
        assert len(entries) == 2

    def test_first_entry(self) -> None:
        parsed = parse_note(self.NOTE_WITH_PROFILES)
        entries = parse_profile_relevance(parsed)
        assert entries[0].profile_name == "research"
        assert entries[0].score == 8
        assert entries[0].reason == "Research reason."

    def test_second_entry(self) -> None:
        parsed = parse_note(self.NOTE_WITH_PROFILES)
        entries = parse_profile_relevance(parsed)
        assert entries[1].profile_name == "engineering"
        assert entries[1].score == 7
        assert entries[1].reason == "Engineering reason."

    def test_no_profile_relevance_section(self) -> None:
        note = (
            "---\n"
            "note_type: summary\n"
            "---\n"
            "# Test\n"
            "\n"
            "## Summary\n"
            "Summary.\n"
        )
        parsed = parse_note(note)
        entries = parse_profile_relevance(parsed)
        assert entries == []
