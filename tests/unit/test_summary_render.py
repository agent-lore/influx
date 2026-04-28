"""Unit tests for ## Summary section rendering from Tier1Enrichment (US-013).

Covers:
- Structured ## Summary with all four sub-blocks when Tier1Enrichment provided
- ## Summary omitted when Tier1Enrichment absent (FR-ENR-6, AC-07-A)
- Sub-block ordering: ### Contributions, ### Method, ### Result, ### Relevance
- Backward compat: plain summary text still renders when tier1_enrichment=None
"""

from __future__ import annotations

from influx.notes import render_note
from influx.schemas import Tier1Enrichment

_BASE_TAGS = ["source:arxiv", "ingested-by:influx", "schema:1"]
_BASE_KWARGS = {
    "title": "Test Note",
    "source_url": "https://arxiv.org/abs/2601.00001",
    "tags": _BASE_TAGS,
    "confidence": 0.8,
    "archive_path": None,
    "summary": "",
    "keywords": [],
    "profile_entries": [],
}

_SAMPLE_T1 = Tier1Enrichment(
    contributions=[
        "Novel attention mechanism",
        "Improved training efficiency",
    ],
    method="Transformer with sparse attention and gradient checkpointing.",
    result="State-of-the-art on three benchmarks.",
    relevance="Directly advances efficient training research.",
)


class TestSummaryRenderedFromTier1:
    """## Summary with structured sub-blocks when Tier1Enrichment is provided."""

    def test_summary_heading_present(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=_SAMPLE_T1)
        assert "## Summary" in rendered

    def test_contributions_subheading(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=_SAMPLE_T1)
        assert "### Contributions" in rendered

    def test_contributions_bullets(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=_SAMPLE_T1)
        assert "- Novel attention mechanism" in rendered
        assert "- Improved training efficiency" in rendered

    def test_method_subheading_and_body(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=_SAMPLE_T1)
        assert "### Method" in rendered
        assert "Transformer with sparse attention" in rendered

    def test_result_subheading_and_body(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=_SAMPLE_T1)
        assert "### Result" in rendered
        assert "State-of-the-art on three benchmarks." in rendered

    def test_relevance_subheading_and_body(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=_SAMPLE_T1)
        assert "### Relevance" in rendered
        assert "Directly advances efficient training research." in rendered

    def test_single_contribution(self) -> None:
        t1 = Tier1Enrichment(
            contributions=["Only one contribution"],
            method="M",
            result="R",
            relevance="Rel",
        )
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=t1)
        assert "- Only one contribution" in rendered

    def test_six_contributions(self) -> None:
        t1 = Tier1Enrichment(
            contributions=[f"Contribution {i}" for i in range(1, 7)],
            method="M",
            result="R",
            relevance="Rel",
        )
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=t1)
        for i in range(1, 7):
            assert f"- Contribution {i}" in rendered


class TestSummaryOmittedWhenAbsent:
    """## Summary is omitted when Tier1Enrichment is absent (FR-ENR-6, AC-07-A)."""

    def test_omitted_when_tier1_none_and_summary_empty(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=None)
        assert "## Summary" not in rendered

    def test_omitted_when_default(self) -> None:
        rendered = render_note(**_BASE_KWARGS)
        assert "## Summary" not in rendered

    def test_no_placeholder_when_absent(self) -> None:
        """FR-ENR-6: no placeholder text — section simply absent."""
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=None)
        assert "### Contributions" not in rendered
        assert "### Method" not in rendered
        assert "### Result" not in rendered
        assert "### Relevance" not in rendered


class TestSummarySubBlockOrdering:
    """Sub-blocks in canonical order: Contributions, Method, Result, Relevance."""

    def test_subblock_order(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=_SAMPLE_T1)
        contributions_pos = rendered.index("### Contributions")
        method_pos = rendered.index("### Method")
        result_pos = rendered.index("### Result")
        relevance_pos = rendered.index("### Relevance")
        assert contributions_pos < method_pos < result_pos < relevance_pos

    def test_summary_before_user_notes(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=_SAMPLE_T1)
        summary_pos = rendered.index("## Summary")
        user_notes_pos = rendered.index("## User Notes")
        assert summary_pos < user_notes_pos

    def test_summary_before_profile_relevance(self) -> None:
        rendered = render_note(**_BASE_KWARGS, tier1_enrichment=_SAMPLE_T1)
        summary_pos = rendered.index("## Summary")
        profile_pos = rendered.index("## Profile Relevance")
        assert summary_pos < profile_pos

    def test_summary_after_archive(self) -> None:
        rendered = render_note(
            **{**_BASE_KWARGS, "archive_path": "arxiv/2026/01/test.pdf"},
            tier1_enrichment=_SAMPLE_T1,
        )
        archive_pos = rendered.index("## Archive")
        summary_pos = rendered.index("## Summary")
        assert archive_pos < summary_pos


class TestSummaryBackwardCompat:
    """Plain summary text still renders when tier1_enrichment is not provided."""

    def test_plain_summary_renders(self) -> None:
        kwargs = {**_BASE_KWARGS, "summary": "A plain-text summary."}
        rendered = render_note(**kwargs)
        assert "## Summary" in rendered
        assert "A plain-text summary." in rendered

    def test_plain_summary_with_keywords(self) -> None:
        kwargs = {
            **_BASE_KWARGS,
            "summary": "A plain-text summary.",
            "keywords": ["ml", "nlp"],
        }
        rendered = render_note(**kwargs)
        assert "## Summary" in rendered
        assert "Keywords: ml, nlp" in rendered

    def test_tier1_overrides_plain_summary(self) -> None:
        """When both tier1_enrichment and summary are present, tier1 wins."""
        kwargs = {**_BASE_KWARGS, "summary": "Old plain summary."}
        rendered = render_note(**kwargs, tier1_enrichment=_SAMPLE_T1)
        assert "### Contributions" in rendered
        assert "Old plain summary." not in rendered
