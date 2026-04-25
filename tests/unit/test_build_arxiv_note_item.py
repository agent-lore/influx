"""Tests for build_arxiv_note_item (US-014).

Covers: text:* tier tag selection, full-text tag, influx:repair-needed,
below-threshold skips extraction, note rendering with ## Full Text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

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
from influx.errors import ExtractionError
from influx.extraction.pipeline import ArxivExtractionResult
from influx.sources.arxiv import ArxivItem, build_arxiv_note_item


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
        config = _make_config(full_text=8)

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
        config = _make_config(full_text=8)

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
        assert "schema:v1" in result["tags"]

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
