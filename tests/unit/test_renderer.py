"""Unit tests for the high-level Renderer facade (issue #52).

The Renderer module owns the canonical Markdown shape from spec §9.
``render(...)`` is the high-level entry point used by Source builders;
it wraps a single-profile :class:`ProfileRelevanceEntry` and delegates
to :func:`render_note`. These tests cover:

- Frontmatter shape (note_type, namespace, source_url, tags, confidence)
- Section ordering: ``## Archive``, ``## Summary``, ``## Full Text``,
  ``## Claims``, ``## Datasets & Benchmarks``, ``## Builds On``,
  ``## Open Questions``, ``## Profile Relevance``, ``## User Notes``
- Omitted-section rules (FR-ENR-6) for missing Tier 1 / Tier 2 / Tier 3
- Profile-relevance merge across rewrites (single-profile authority +
  multi-profile union)
- Byte-exact ``## User Notes`` preservation across parse/render cycles
"""

from __future__ import annotations

from influx.notes import parse_note
from influx.renderer import (
    ProfileRelevanceEntry,
    build_profile_relevance_for_rewrite,
    merge_profile_relevance_union,
    render,
    render_note,
)
from influx.schemas import Tier1Enrichment, Tier3Extraction

_BASE_TAGS = ["source:arxiv", "ingested-by:influx", "schema:1"]


def _render_minimal(**overrides: object) -> str:
    """Render a minimal note with sensible defaults; *overrides* override kwargs."""
    kwargs: dict[str, object] = {
        "title": "A Paper About Things",
        "source_url": "https://arxiv.org/abs/2601.00001",
        "tags": list(_BASE_TAGS),
        "confidence": 0.8,
        "archive_path": "papers/arxiv/2026/01/2601.00001.pdf",
        "summary": "An abstract.",
        "profile_name": "research",
        "score": 8,
        "reason": "Directly relevant.",
    }
    kwargs.update(overrides)
    return render(**kwargs)  # type: ignore[arg-type]


# ── Frontmatter ─────────────────────────────────────────────────────


class TestFrontmatter:
    """``render`` produces well-formed YAML frontmatter."""

    def test_frontmatter_fences(self) -> None:
        text = _render_minimal()
        assert text.startswith("---\n")
        assert "\n---\n" in text

    def test_frontmatter_fields_present(self) -> None:
        text = _render_minimal()
        head = text.split("\n---\n", 1)[0]
        assert "note_type: summary" in head
        assert "namespace: influx" in head
        assert "source_url: https://arxiv.org/abs/2601.00001" in head
        assert "confidence: 0.8" in head

    def test_frontmatter_tags_listed(self) -> None:
        text = _render_minimal(tags=["source:arxiv", "ingested-by:influx", "favourite"])
        head = text.split("\n---\n", 1)[0]
        assert "  - source:arxiv" in head
        assert "  - ingested-by:influx" in head
        assert "  - favourite" in head

    def test_confidence_normalised_to_decimal(self) -> None:
        text = _render_minimal(confidence=1.0)
        assert "confidence: 1.0" in text


# ── Section ordering ────────────────────────────────────────────────


class TestSectionOrdering:
    """Sections appear in the canonical order from spec §9."""

    def test_full_pipeline_order(self) -> None:
        tier1 = Tier1Enrichment(
            contributions=["c1"],
            method="m",
            result="r",
            relevance="rel",
        )
        tier3 = Tier3Extraction(
            claims=["claim"],
            datasets=["dataset"],
            builds_on=["prior"],
            open_questions=["q"],
            potential_connections=[],
        )
        text = _render_minimal(
            tier1_enrichment=tier1,
            full_text="extracted body",
            tier3_extraction=tier3,
        )

        headings = [
            "## Archive",
            "## Summary",
            "## Full Text",
            "## Claims",
            "## Datasets & Benchmarks",
            "## Builds On",
            "## Open Questions",
            "## Profile Relevance",
            "## User Notes",
        ]
        positions = [text.index(h) for h in headings]
        assert positions == sorted(positions), (
            f"Section ordering violated: {list(zip(headings, positions, strict=True))}"
        )

    def test_archive_path_rendered_in_section(self) -> None:
        text = _render_minimal(archive_path="papers/arxiv/2026/01/foo.pdf")
        assert "## Archive\npath: papers/arxiv/2026/01/foo.pdf\n" in text

    def test_archive_empty_body_when_no_path(self) -> None:
        text = _render_minimal(archive_path=None)
        # Empty Archive body: heading followed by blank line
        assert "## Archive\n\n" in text


# ── Omitted-section rules (FR-ENR-6) ────────────────────────────────


class TestOmittedSections:
    """Optional sections are omitted entirely when their data is absent."""

    def test_summary_omitted_when_blank_and_no_tier1(self) -> None:
        text = _render_minimal(summary="", tier1_enrichment=None)
        assert "## Summary" not in text

    def test_summary_present_when_summary_provided(self) -> None:
        text = _render_minimal(summary="A short abstract.")
        assert "## Summary\nA short abstract.\n" in text

    def test_full_text_omitted_when_none(self) -> None:
        text = _render_minimal(full_text=None)
        assert "## Full Text" not in text

    def test_full_text_omitted_when_empty(self) -> None:
        text = _render_minimal(full_text="")
        assert "## Full Text" not in text

    def test_full_text_present_when_provided(self) -> None:
        text = _render_minimal(full_text="extracted body")
        assert "## Full Text\nextracted body\n" in text

    def test_tier3_sections_all_omitted_when_none(self) -> None:
        text = _render_minimal(tier3_extraction=None)
        for heading in (
            "## Claims",
            "## Datasets & Benchmarks",
            "## Builds On",
            "## Open Questions",
        ):
            assert heading not in text

    def test_profile_relevance_always_present(self) -> None:
        text = _render_minimal()
        assert "## Profile Relevance" in text


# ── Profile relevance ───────────────────────────────────────────────


class TestProfileRelevance:
    """``render`` builds a single-profile entry from name/score/reason."""

    def test_single_profile_entry_rendered(self) -> None:
        text = _render_minimal(
            profile_name="ai-robotics", score=9, reason="Builds on attention."
        )
        expected = (
            "## Profile Relevance\n### ai-robotics\nScore: 9/10\nBuilds on attention."
        )
        assert expected in text


# ── Profile-relevance merge across rewrites ─────────────────────────


class TestProfileRelevanceMergeForRewrite:
    """build_profile_relevance_for_rewrite resolves new-vs-old entries (FR-NOTE-6)."""

    def test_new_entry_replaces_old_for_same_profile(self) -> None:
        old = [ProfileRelevanceEntry("research", 5, "old reason")]
        new = [ProfileRelevanceEntry("research", 8, "new reason")]
        resolved = build_profile_relevance_for_rewrite(
            old_entries=old,
            new_entries=new,
            tags=["profile:research"],
        )
        assert resolved == [ProfileRelevanceEntry("research", 8, "new reason")]

    def test_rejected_profile_keeps_old_entry(self) -> None:
        old = [ProfileRelevanceEntry("research", 5, "kept")]
        new = [ProfileRelevanceEntry("research", 8, "ignored")]
        resolved = build_profile_relevance_for_rewrite(
            old_entries=old,
            new_entries=new,
            tags=["profile:research", "influx:rejected:research"],
        )
        assert resolved == [ProfileRelevanceEntry("research", 5, "kept")]

    def test_old_entry_for_non_current_profile_dropped(self) -> None:
        """Single-profile rewrite drops entries not in new and not rejected."""
        old = [ProfileRelevanceEntry("other", 7, "drop me")]
        new = [ProfileRelevanceEntry("research", 8, "keep")]
        resolved = build_profile_relevance_for_rewrite(
            old_entries=old,
            new_entries=new,
            tags=["profile:research"],
        )
        assert resolved == [ProfileRelevanceEntry("research", 8, "keep")]


class TestProfileRelevanceMergeUnion:
    """merge_profile_relevance_union preserves old entries on shared notes."""

    def test_old_entry_for_other_profile_preserved(self) -> None:
        old = [ProfileRelevanceEntry("other", 7, "shared note")]
        new = [ProfileRelevanceEntry("research", 8, "incoming")]
        merged = merge_profile_relevance_union(
            old_entries=old,
            new_entries=new,
            tags=["profile:research", "profile:other"],
        )
        names = [e.profile_name for e in merged]
        assert names == ["research", "other"]

    def test_rejected_profile_keeps_old_entry(self) -> None:
        old = [ProfileRelevanceEntry("research", 5, "kept")]
        new = [ProfileRelevanceEntry("research", 8, "ignored")]
        merged = merge_profile_relevance_union(
            old_entries=old,
            new_entries=new,
            tags=["profile:research", "influx:rejected:research"],
        )
        assert merged == [ProfileRelevanceEntry("research", 5, "kept")]


# ── Byte-exact ## User Notes preservation ──────────────────────────


class TestUserNotesPreservation:
    """The ``## User Notes`` region is preserved byte-exactly across rewrites."""

    def test_user_notes_passed_through_byte_exactly(self) -> None:
        user_notes = "## User Notes\n  Hand-written: keep me!  \n\nstill mine\n"
        text = _render_minimal(user_notes=user_notes)
        assert text.endswith(user_notes)

    def test_user_notes_with_crlf_preserved(self) -> None:
        user_notes = "## User Notes\r\nCRLF body\r\n"
        text = _render_minimal(user_notes=user_notes)
        assert text.endswith(user_notes)

    def test_empty_user_notes_heading_appended_when_missing(self) -> None:
        text = _render_minimal()  # user_notes=None default
        assert text.endswith("## User Notes\n")

    def test_round_trip_through_parse_preserves_user_notes(self) -> None:
        original_user_notes = (
            "## User Notes\n- bullet kept verbatim\n\n  trailing whitespace  \n"
        )
        first = _render_minimal(user_notes=original_user_notes)
        parsed = parse_note(first)
        assert parsed.user_notes == original_user_notes
        # Re-render with the parsed user_notes — bytes should match
        rerendered = _render_minimal(user_notes=parsed.user_notes)
        assert rerendered.endswith(original_user_notes)


# ── render(...) parity with render_note(...) ───────────────────────


class TestRenderFacadeParity:
    """render(...) produces the same bytes as a manual render_note call."""

    def test_render_matches_manual_render_note(self) -> None:
        kwargs = {
            "title": "Parity Paper",
            "source_url": "https://arxiv.org/abs/2601.00099",
            "tags": list(_BASE_TAGS),
            "confidence": 0.7,
            "archive_path": None,
            "summary": "An abstract.",
            "profile_name": "research",
            "score": 7,
            "reason": "Worth a look.",
        }
        via_facade = render(**kwargs)  # type: ignore[arg-type]
        via_render_note = render_note(
            title="Parity Paper",
            source_url="https://arxiv.org/abs/2601.00099",
            tags=list(_BASE_TAGS),
            confidence=0.7,
            archive_path=None,
            summary="An abstract.",
            keywords=[],
            profile_entries=[ProfileRelevanceEntry("research", 7, "Worth a look.")],
        )
        assert via_facade == via_render_note
