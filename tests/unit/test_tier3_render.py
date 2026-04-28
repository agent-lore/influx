"""Unit tests for Tier 3 section rendering in notes.py (US-012).

Covers:
- All four sections rendered when Tier3Extraction provided
- All four sections omitted when Tier3Extraction absent
- Ordering: Tier 3 sections appear before ## User Notes (AC-M2-4)
- potential_connections is NOT rendered as a section
"""

from __future__ import annotations

from influx.notes import render_note
from influx.schemas import Tier3Extraction

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

_SAMPLE_T3 = Tier3Extraction(
    claims=["Claim A", "Claim B"],
    datasets=["Dataset X"],
    builds_on=["Prior work Y"],
    open_questions=["Question Z"],
    potential_connections=["Connection W"],
)


class TestTier3Rendered:
    """All four Tier 3 sections are emitted when tier3_extraction is provided."""

    def test_claims_section_present(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        assert "## Claims" in rendered

    def test_claims_bullets(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        assert "- Claim A" in rendered
        assert "- Claim B" in rendered

    def test_datasets_section_present(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        assert "## Datasets & Benchmarks" in rendered

    def test_datasets_bullets(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        assert "- Dataset X" in rendered

    def test_builds_on_section_present(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        assert "## Builds On" in rendered

    def test_builds_on_bullets(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        assert "- Prior work Y" in rendered

    def test_open_questions_section_present(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        assert "## Open Questions" in rendered

    def test_open_questions_bullets(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        assert "- Question Z" in rendered

    def test_empty_optional_lists(self) -> None:
        """Sections with empty lists render as headings without bullets."""
        t3 = Tier3Extraction(claims=["Only claim"])
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=t3)
        assert "## Claims" in rendered
        assert "- Only claim" in rendered
        assert "## Datasets & Benchmarks" in rendered
        assert "## Builds On" in rendered
        assert "## Open Questions" in rendered


class TestTier3Omitted:
    """All four Tier 3 sections are omitted when extraction is absent (FR-ENR-6)."""

    def test_omitted_when_none(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=None)
        assert "## Claims" not in rendered
        assert "## Datasets & Benchmarks" not in rendered
        assert "## Builds On" not in rendered
        assert "## Open Questions" not in rendered

    def test_omitted_when_default(self) -> None:
        rendered = render_note(**_BASE_KWARGS)
        assert "## Claims" not in rendered
        assert "## Datasets & Benchmarks" not in rendered

    def test_no_placeholder_when_absent(self) -> None:
        """FR-ENR-6: no placeholder text on failure — sections simply absent."""
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=None)
        assert "Claims" not in rendered
        assert "Datasets" not in rendered
        assert "Builds On" not in rendered
        assert "Open Questions" not in rendered


class TestTier3Ordering:
    """Tier 3 sections appear in canonical order before ## User Notes (AC-M2-4)."""

    def test_tier3_before_user_notes(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        claims_pos = rendered.index("## Claims")
        datasets_pos = rendered.index("## Datasets & Benchmarks")
        builds_pos = rendered.index("## Builds On")
        questions_pos = rendered.index("## Open Questions")
        user_notes_pos = rendered.index("## User Notes")
        assert claims_pos < datasets_pos < builds_pos < questions_pos < user_notes_pos

    def test_tier3_after_full_text(self) -> None:
        rendered = render_note(
            **_BASE_KWARGS, full_text="Some body.", tier3_extraction=_SAMPLE_T3
        )
        full_text_pos = rendered.index("## Full Text")
        claims_pos = rendered.index("## Claims")
        assert full_text_pos < claims_pos

    def test_tier3_after_summary(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        summary_pos = rendered.index("## Summary")
        claims_pos = rendered.index("## Claims")
        assert summary_pos < claims_pos

    def test_tier3_before_profile_relevance(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        questions_pos = rendered.index("## Open Questions")
        profile_pos = rendered.index("## Profile Relevance")
        assert questions_pos < profile_pos


class TestPotentialConnectionsNotRendered:
    """potential_connections is consumed by PRD 08 LCMA only — not rendered."""

    def test_potential_connections_not_in_output(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=_SAMPLE_T3)
        assert "## Potential Connections" not in rendered
        assert "Connection W" not in rendered

    def test_potential_connections_heading_absent(self) -> None:
        t3 = Tier3Extraction(
            claims=["A"],
            potential_connections=["Conn 1", "Conn 2", "Conn 3"],
        )
        rendered = render_note(**_BASE_KWARGS, tier3_extraction=t3)
        assert "Conn 1" not in rendered
        assert "Conn 2" not in rendered
        assert "Conn 3" not in rendered
