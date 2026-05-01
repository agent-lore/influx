"""Tests for build_arxiv_note_item (US-014 + US-015).

Covers: text:* tier tag selection, full-text tag, influx:repair-needed,
below-threshold skips extraction, note rendering with ## Full Text,
Tier 1 + Tier 3 enrichment wiring with per-tier failure independence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ExtractionConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.errors import ExtractionError, LCMAError
from influx.extraction.pipeline import ArxivExtractionResult
from influx.schemas import Tier1Enrichment, Tier3Extraction
from influx.sources.arxiv import ArxivItem, build_arxiv_note_item
from influx.storage import ArchiveResult


@pytest.fixture(autouse=True)
def _archive_success() -> object:
    """Keep unit tests focused on note-building unless they override archive IO."""
    with patch(
        "influx.sources.arxiv.download_archive",
        return_value=ArchiveResult(
            ok=True,
            rel_posix_path="arxiv/2026/04/2601.12345.pdf",
            error="",
        ),
    ) as patched:
        yield patched


def _make_config(
    *,
    full_text: int = 8,
    relevance: int = 7,
    deep_extract: int = 9,
) -> AppConfig:
    """Build a minimal AppConfig with configurable thresholds."""
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="AI and robotics research",
                thresholds=ProfileThresholds(
                    relevance=relevance,
                    full_text=full_text,
                    deep_extract=deep_extract,
                ),
            )
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(),
    )


def _make_item(arxiv_id: str = "2601.12345") -> ArxivItem:
    return ArxivItem(
        arxiv_id=arxiv_id,
        title="Test Paper Title",
        abstract="This is the abstract of the test paper.",
        published=datetime(2026, 4, 25, tzinfo=UTC),
        categories=["cs.AI", "cs.RO"],
    )


# ── HTML success (text:html + full-text + ## Full Text) ───────────


class TestHTMLSuccess:
    """Score >= full_text threshold + HTML extraction succeeds."""

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_text_html_tag_on_html_success(self, mock_extract: object) -> None:
        mock_extract.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="Extracted HTML content " * 100, source_tag="text:html"
        )
        config = _make_config(full_text=8)
        item = _make_item()

        result = build_arxiv_note_item(
            item=item,
            score=9,
            confidence=0.9,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "text:html" in result["tags"]

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_full_text_tag_on_html_success(self, mock_extract: object) -> None:
        mock_extract.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="Extracted content " * 100, source_tag="text:html"
        )
        config = _make_config(full_text=8)
        item = _make_item()

        result = build_arxiv_note_item(
            item=item,
            score=9,
            confidence=0.9,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "full-text" in result["tags"]

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_full_text_section_in_content(self, mock_extract: object) -> None:
        mock_extract.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="The full extracted article text", source_tag="text:html"
        )
        config = _make_config(full_text=8)
        item = _make_item()

        result = build_arxiv_note_item(
            item=item,
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Full Text" in result["content"]
        assert "The full extracted article text" in result["content"]

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_no_repair_needed_on_success(self, mock_extract: object) -> None:
        mock_extract.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="content", source_tag="text:html"
        )
        # Enrichment thresholds set high — this test covers extraction-only repair.
        config = _make_config(full_text=8, relevance=100, deep_extract=100)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:repair-needed" not in result["tags"]


# ── PDF fallback (text:pdf + full-text) ───────────────────────────


class TestPDFFallback:
    """Score >= full_text threshold + PDF extraction succeeds."""

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_text_pdf_tag_on_pdf_success(self, mock_extract: object) -> None:
        mock_extract.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="PDF text", source_tag="text:pdf"
        )
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "text:pdf" in result["tags"]
        assert "full-text" in result["tags"]
        assert "## Full Text" in result["content"]


# ── Both fail (text:abstract-only + influx:repair-needed) ────────


class TestBothFail:
    """Score >= full_text threshold + both extractions fail."""

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_abstract_only_tag_when_both_fail(self, mock_extract: object) -> None:
        mock_extract.side_effect = ExtractionError(  # type: ignore[union-attr]
            "both fail", url="x", stage="cascade"
        )
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "text:abstract-only" in result["tags"]

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_repair_needed_tag_when_both_fail(self, mock_extract: object) -> None:
        mock_extract.side_effect = ExtractionError(  # type: ignore[union-attr]
            "both fail", url="x", stage="cascade"
        )
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:repair-needed" in result["tags"]

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_no_full_text_section_when_both_fail(self, mock_extract: object) -> None:
        mock_extract.side_effect = ExtractionError(  # type: ignore[union-attr]
            "both fail", url="x", stage="cascade"
        )
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Full Text" not in result["content"]
        assert "full-text" not in result["tags"]

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_no_text_terminal_on_initial_write(self, mock_extract: object) -> None:
        """AC-M2-3: influx:text-terminal NOT set on initial write."""
        mock_extract.side_effect = ExtractionError(  # type: ignore[union-attr]
            "both fail", url="x", stage="cascade"
        )
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:text-terminal" not in result["tags"]


# ── Below full_text threshold ─────────────────────────────────────


class TestBelowThreshold:
    """Score below full_text threshold → no extraction, no full-text."""

    def test_no_extraction_below_threshold(self) -> None:
        """Extraction should not be attempted below threshold."""
        # Enrichment thresholds set high — this test covers extraction threshold.
        config = _make_config(full_text=8, relevance=100, deep_extract=100)

        with patch("influx.sources.arxiv.extract_arxiv_text") as mock_extract:
            result = build_arxiv_note_item(
                item=_make_item(),
                score=7,
                confidence=0.7,
                reason="OK",
                profile_name="ai-robotics",
                config=config,
            )
            mock_extract.assert_not_called()  # type: ignore[union-attr]

        assert "text:abstract-only" in result["tags"]
        assert "full-text" not in result["tags"]
        assert "## Full Text" not in result["content"]
        assert "influx:repair-needed" not in result["tags"]

    def test_no_full_text_section_below_threshold(self) -> None:
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=6,
            confidence=0.6,
            reason="Low",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Full Text" not in result["content"]


# ── Item shape & metadata ─────────────────────────────────────────


class TestItemShape:
    """ProfileItem dict has required fields and correct metadata."""

    def test_item_has_required_fields(self) -> None:
        config = _make_config()
        item = _make_item("2601.99999")

        result = build_arxiv_note_item(
            item=item,
            score=5,
            confidence=0.5,
            reason="Test",
            profile_name="ai-robotics",
            config=config,
        )

        assert result["title"] == "Test Paper Title"
        assert result["source_url"] == "https://arxiv.org/abs/2601.99999"
        assert result["score"] == 5
        assert result["confidence"] == 0.5
        assert result["abstract_or_summary"] == item.abstract
        assert "content" in result
        assert isinstance(result["tags"], list)

    def test_tags_include_profile_arxiv_source(self) -> None:
        config = _make_config()

        result = build_arxiv_note_item(
            item=_make_item(),
            score=5,
            confidence=0.5,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "profile:ai-robotics" in result["tags"]
        assert "arxiv-id:2601.12345" in result["tags"]
        assert "source:arxiv" in result["tags"]
        assert "ingested-by:influx" in result["tags"]
        assert "schema:1" in result["tags"]

    def test_tags_include_category_tags(self) -> None:
        config = _make_config()
        item = _make_item()

        result = build_arxiv_note_item(
            item=item,
            score=5,
            confidence=0.5,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "cat:cs.AI" in result["tags"]
        assert "cat:cs.RO" in result["tags"]

    def test_path_derived_from_published_date(self) -> None:
        config = _make_config()
        item = _make_item()

        result = build_arxiv_note_item(
            item=item,
            score=5,
            confidence=0.5,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert result["path"] == "papers/arxiv/2026/04"

    def test_note_content_rendered_with_title(self) -> None:
        config = _make_config()
        item = _make_item()

        result = build_arxiv_note_item(
            item=item,
            score=5,
            confidence=0.5,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "# Test Paper Title" in result["content"]

    def test_note_content_has_profile_relevance(self) -> None:
        config = _make_config()

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="Highly relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Profile Relevance" in result["content"]
        assert "ai-robotics" in result["content"]
        assert "9/10" in result["content"]

    def test_explicit_thresholds_override_config(self) -> None:
        """Explicit thresholds param takes priority over profile config."""
        config = _make_config(full_text=8)
        custom_thresholds = ProfileThresholds(full_text=100)

        with patch("influx.sources.arxiv.extract_arxiv_text") as mock_extract:
            result = build_arxiv_note_item(
                item=_make_item(),
                score=9,
                confidence=0.9,
                reason="R",
                profile_name="ai-robotics",
                config=config,
                thresholds=custom_thresholds,
            )
            mock_extract.assert_not_called()  # type: ignore[union-attr]

        # Score 9 < threshold 100, so no extraction
        assert "text:abstract-only" in result["tags"]


# ══════════════════════════════════════════════════════════════════════
# US-015: Tier 1 + Tier 3 enrichment wiring on initial-write pipeline
# ══════════════════════════════════════════════════════════════════════


def _make_tier1(**overrides: object) -> Tier1Enrichment:
    """Build a valid Tier1Enrichment with optional overrides."""
    data: dict[str, object] = {
        "contributions": ["Novel approach to task X"],
        "method": "Uses technique Y with modification Z",
        "result": "Achieves state-of-the-art on benchmark B",
        "relevance": "Directly applicable to robotics planning",
    }
    data.update(overrides)
    return Tier1Enrichment.model_validate(data)


def _make_tier3(**overrides: object) -> Tier3Extraction:
    """Build a valid Tier3Extraction with optional overrides."""
    data: dict[str, object] = {
        "claims": ["Outperforms baseline by 15%"],
        "datasets": ["ImageNet-1k"],
        "builds_on": ["Prior work A"],
        "open_questions": ["Scalability to larger inputs"],
        "potential_connections": ["Related to approach C"],
    }
    data.update(overrides)
    return Tier3Extraction.model_validate(data)


# ── Tier 1 enrichment success ─────────────────────────────────────


class TestTier1EnrichmentSuccess:
    """Score >= relevance + tier1_enrich succeeds → structured ## Summary."""

    @patch("influx.sources.arxiv.tier1_enrich")
    def test_summary_section_rendered_from_tier1(self, mock_t1: object) -> None:
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        config = _make_config(relevance=7, deep_extract=100)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Summary" in result["content"]
        assert "### Contributions" in result["content"]
        assert "### Method" in result["content"]
        assert "### Result" in result["content"]
        assert "### Relevance" in result["content"]
        assert "Novel approach to task X" in result["content"]

    @patch("influx.sources.arxiv.tier1_enrich")
    def test_tier1_called_with_profile_summary(self, mock_t1: object) -> None:
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        config = _make_config(relevance=7, deep_extract=100)

        build_arxiv_note_item(
            item=_make_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        mock_t1.assert_called_once()  # type: ignore[union-attr]
        call_kwargs = mock_t1.call_args.kwargs  # type: ignore[union-attr]
        assert call_kwargs["title"] == "Test Paper Title"
        assert call_kwargs["abstract"] == "This is the abstract of the test paper."
        assert call_kwargs["profile_summary"] == "AI and robotics research"

    @patch("influx.sources.arxiv.tier1_enrich")
    def test_no_repair_needed_when_tier1_succeeds(self, mock_t1: object) -> None:
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        config = _make_config(relevance=7, deep_extract=100)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:repair-needed" not in result["tags"]

    @patch("influx.sources.arxiv.tier1_enrich")
    def test_tier1_not_called_below_relevance(self, mock_t1: object) -> None:
        config = _make_config(relevance=7)

        build_arxiv_note_item(
            item=_make_item(),
            score=6,
            confidence=0.6,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        mock_t1.assert_not_called()  # type: ignore[union-attr]

    def test_plain_summary_below_relevance(self) -> None:
        """Below relevance threshold → plain abstract as ## Summary."""
        config = _make_config(relevance=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Summary" in result["content"]
        assert "This is the abstract of the test paper." in result["content"]


# ── Tier 1 enrichment failure ────────────────────────────────────


class TestTier1EnrichmentFailure:
    """Score >= relevance + tier1_enrich fails → no ## Summary + repair-needed."""

    @patch("influx.sources.arxiv.tier1_enrich")
    def test_no_summary_on_tier1_failure(self, mock_t1: object) -> None:
        """AC-07-A: note written WITHOUT ## Summary on tier1 failure."""
        mock_t1.side_effect = LCMAError(  # type: ignore[union-attr]
            "validation failed", model="enrich", stage="validate"
        )
        config = _make_config(relevance=7, deep_extract=100)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Summary" not in result["content"]

    @patch("influx.sources.arxiv.tier1_enrich")
    def test_repair_needed_on_tier1_failure(self, mock_t1: object) -> None:
        mock_t1.side_effect = LCMAError(  # type: ignore[union-attr]
            "validation failed", model="enrich", stage="validate"
        )
        config = _make_config(relevance=7, deep_extract=100)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:repair-needed" in result["tags"]

    @patch("influx.sources.arxiv.tier1_enrich")
    def test_no_placeholder_text_on_tier1_failure(self, mock_t1: object) -> None:
        """FR-ENR-6: no placeholder text inserted on failure."""
        mock_t1.side_effect = LCMAError(  # type: ignore[union-attr]
            "error", model="enrich", stage="validate"
        )
        config = _make_config(relevance=7, deep_extract=100)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        # No summary placeholder, abstract not rendered as summary
        assert "## Summary" not in result["content"]
        assert "### Contributions" not in result["content"]


# ── Tier 3 enrichment success ─────────────────────────────────────


class TestTier3ExtractionSuccess:
    """Score >= deep_extract + extraction succeeds + tier3 succeeds."""

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_tier3_sections_rendered(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="Full text body", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.return_value = _make_tier3()  # type: ignore[union-attr]
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Claims" in result["content"]
        assert "## Datasets & Benchmarks" in result["content"]
        assert "## Builds On" in result["content"]
        assert "## Open Questions" in result["content"]

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_deep_extracted_tag_on_tier3_success(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="Full text body", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.return_value = _make_tier3()  # type: ignore[union-attr]
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:deep-extracted" in result["tags"]

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_no_repair_needed_all_succeed(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="Full text body", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.return_value = _make_tier3()  # type: ignore[union-attr]
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:repair-needed" not in result["tags"]

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_tier3_not_called_without_extracted_text(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        """Tier 3 requires extracted full text — not called on abstract-only."""
        mock_ext.side_effect = ExtractionError(  # type: ignore[union-attr]
            "fail", url="x", stage="cascade"
        )
        mock_t1.side_effect = LCMAError(  # type: ignore[union-attr]
            "fail", model="enrich", stage="validate"
        )
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        mock_t3.assert_not_called()  # type: ignore[union-attr]

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_tier3_not_called_below_deep_extract_threshold(
        self, mock_ext: object, mock_t3: object
    ) -> None:
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text", source_tag="text:html"
        )
        config = _make_config(full_text=8, deep_extract=9, relevance=100)

        build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        mock_t3.assert_not_called()  # type: ignore[union-attr]


# ── Tier 3 extraction failure ────────────────────────────────────


class TestTier3ExtractionFailure:
    """Score >= deep_extract + tier3_extract fails → no Tier 3 + repair-needed."""

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_no_tier3_sections_on_failure(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.side_effect = LCMAError(  # type: ignore[union-attr]
            "fail", model="extract", stage="validate"
        )
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Claims" not in result["content"]
        assert "## Datasets & Benchmarks" not in result["content"]
        assert "## Builds On" not in result["content"]
        assert "## Open Questions" not in result["content"]

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_no_deep_extracted_tag_on_tier3_failure(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.side_effect = LCMAError(  # type: ignore[union-attr]
            "fail", model="extract", stage="validate"
        )
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:deep-extracted" not in result["tags"]

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_repair_needed_on_tier3_failure(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.side_effect = LCMAError(  # type: ignore[union-attr]
            "fail", model="extract", stage="validate"
        )
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:repair-needed" in result["tags"]

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_unexpected_tier3_exception_degrades_to_repair(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        """A non-LCMAError raised by tier3_extract (e.g. an AttributeError
        from a validator that bypassed Pydantic's ValidationError wrapper)
        must degrade to ``influx:repair-needed`` for this paper, not abort
        the run with an uncaught exception (staging incident 2026-05-01).
        """
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.side_effect = AttributeError(  # type: ignore[union-attr]
            "'dict' object has no attribute 'strip'"
        )
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        # Must not raise.
        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:repair-needed" in result["tags"]
        assert "influx:deep-extracted" not in result["tags"]
        assert "## Claims" not in result["content"]

    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_unexpected_tier1_exception_degrades_to_repair(
        self, mock_ext: object, mock_t1: object
    ) -> None:
        """Same defence-in-depth for Tier 1 — an unexpected exception must
        not abort the run.
        """
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text", source_tag="text:html"
        )
        mock_t1.side_effect = TypeError("bad shape")  # type: ignore[union-attr]
        config = _make_config(relevance=7, deep_extract=100)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=7,
            confidence=0.7,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:repair-needed" in result["tags"]
        assert "## Summary" not in result["content"]


# ── Per-tier failure independence (AC-07-D) ──────────────────────


class TestPerTierFailureIndependence:
    """Tier 1 success + Tier 3 failure → ## Summary present, no Tier 3."""

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_tier1_success_tier3_failure(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        """AC-07-D: Tier 1 ok + Tier 3 fail → summary yes, Tier 3 no, repair-needed."""
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="Full text", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.side_effect = LCMAError(  # type: ignore[union-attr]
            "fail", model="extract", stage="validate"
        )
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        # Tier 1 succeeded → ## Summary present
        assert "## Summary" in result["content"]
        assert "### Contributions" in result["content"]
        # Tier 3 failed → no Tier 3 sections
        assert "## Claims" not in result["content"]
        assert "influx:deep-extracted" not in result["tags"]
        # repair-needed from Tier 3 failure
        assert "influx:repair-needed" in result["tags"]

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_tier1_failure_tier3_success(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        """Tier 1 fail + Tier 3 ok → no summary, Tier 3 yes, repair-needed."""
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="Full text", source_tag="text:html"
        )
        mock_t1.side_effect = LCMAError(  # type: ignore[union-attr]
            "fail", model="enrich", stage="validate"
        )
        mock_t3.return_value = _make_tier3()  # type: ignore[union-attr]
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        # Tier 1 failed → no ## Summary
        assert "## Summary" not in result["content"]
        # Tier 3 succeeded → sections present
        assert "## Claims" in result["content"]
        assert "influx:deep-extracted" in result["tags"]
        # repair-needed from Tier 1 failure
        assert "influx:repair-needed" in result["tags"]

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_extraction_fail_tier1_success(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        """Extraction fail + Tier 1 ok → summary yes, no full text, no Tier 3."""
        mock_ext.side_effect = ExtractionError(  # type: ignore[union-attr]
            "fail", url="x", stage="cascade"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        # Tier 1 succeeded
        assert "## Summary" in result["content"]
        assert "### Contributions" in result["content"]
        # Extraction failed → no full text, no Tier 3
        assert "## Full Text" not in result["content"]
        assert "## Claims" not in result["content"]
        # Tier 3 not called (no extracted text)
        mock_t3.assert_not_called()  # type: ignore[union-attr]
        # repair-needed from extraction failure
        assert "influx:repair-needed" in result["tags"]


# ── Tag derivation invariants (US-015 AC-5) ──────────────────────


class TestTagDerivationInvariants:
    """Section-iff-tag invariant on initial write."""

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_full_text_tag_iff_section(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        """full-text tag present iff ## Full Text body is non-empty."""
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text body", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.return_value = _make_tier3()  # type: ignore[union-attr]
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        has_section = "## Full Text" in result["content"]
        has_tag = "full-text" in result["tags"]
        assert has_section == has_tag

    @patch("influx.sources.arxiv.tier3_extract")
    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_deep_extracted_tag_iff_tier3_sections(
        self, mock_ext: object, mock_t1: object, mock_t3: object
    ) -> None:
        """influx:deep-extracted iff all Tier 3 sections exist."""
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text body", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        mock_t3.return_value = _make_tier3()  # type: ignore[union-attr]
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        has_sections = all(
            h in result["content"]
            for h in [
                "## Claims",
                "## Datasets & Benchmarks",
                "## Builds On",
                "## Open Questions",
            ]
        )
        has_tag = "influx:deep-extracted" in result["tags"]
        assert has_sections == has_tag

    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_no_deep_extracted_without_tier3(
        self, mock_ext: object, mock_t1: object
    ) -> None:
        """No influx:deep-extracted when Tier 3 not attempted."""
        mock_ext.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text", source_tag="text:html"
        )
        mock_t1.return_value = _make_tier1()  # type: ignore[union-attr]
        # deep_extract=100 → Tier 3 not called
        config = _make_config(relevance=7, full_text=8, deep_extract=100)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:deep-extracted" not in result["tags"]
        assert "## Claims" not in result["content"]

    @patch("influx.sources.arxiv.tier1_enrich")
    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_repair_needed_set_exactly_once(
        self, mock_ext: object, mock_t1: object
    ) -> None:
        """influx:repair-needed appears at most once even with multiple failures."""
        mock_ext.side_effect = ExtractionError(  # type: ignore[union-attr]
            "fail", url="x", stage="cascade"
        )
        mock_t1.side_effect = LCMAError(  # type: ignore[union-attr]
            "fail", model="enrich", stage="validate"
        )
        config = _make_config(relevance=7, full_text=8, deep_extract=9)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )

        assert result["tags"].count("influx:repair-needed") == 1


class TestFilterTagsPropagation:
    """LLM filter-result tags carried through the ``ProfileItem`` dict.

    Distinct from persisted note / provenance tags — ``filter_tags`` is
    what scheduler.record_filter_result() consumes for FR-OBS-5
    rejection-rate computation (US-008).
    """

    @patch("influx.sources.arxiv.extract_arxiv_text")
    def test_filter_tags_passed_through(self, mock_extract: object) -> None:
        mock_extract.return_value = ArxivExtractionResult(  # type: ignore[union-attr]
            text="text", source_tag="text:html"
        )
        config = _make_config(full_text=100, relevance=100, deep_extract=100)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=5,
            confidence=0.5,
            reason="R",
            profile_name="ai-robotics",
            config=config,
            filter_tags=["topic:robotics", "topic:ai"],
        )

        assert result["filter_tags"] == ["topic:robotics", "topic:ai"]
        # Filter-result tags are not mixed into persisted note tags.
        assert "topic:robotics" not in result["tags"]

    def test_filter_tags_default_empty(self) -> None:
        config = _make_config(full_text=100, relevance=100, deep_extract=100)
        result = build_arxiv_note_item(
            item=_make_item(),
            score=1,
            confidence=0.1,
            reason="R",
            profile_name="ai-robotics",
            config=config,
        )
        assert result["filter_tags"] == []
