"""Tests for the enrich stub (PRD 04 seam — replaced by PRD 07)."""

from __future__ import annotations

from influx.enrich import Tier1Result, tier1_enrich


class TestTier1Enrich:
    """tier1_enrich returns a canned Tier-1 shape from the abstract."""

    def test_returns_tier1_result(self) -> None:
        result = tier1_enrich(abstract="A study of neural scaling laws.")
        assert isinstance(result, Tier1Result)

    def test_summary_from_short_abstract(self) -> None:
        abstract = "A study of neural scaling laws."
        result = tier1_enrich(abstract=abstract)
        assert result.summary == abstract

    def test_summary_truncated_at_500_chars(self) -> None:
        abstract = "x" * 1000
        result = tier1_enrich(abstract=abstract)
        assert len(result.summary) == 500

    def test_summary_not_empty_for_nonempty_abstract(self) -> None:
        result = tier1_enrich(abstract="Some content here.")
        assert result.summary != ""

    def test_empty_abstract_returns_empty_summary(self) -> None:
        result = tier1_enrich(abstract="")
        assert result.summary == ""

    def test_keywords_is_list(self) -> None:
        result = tier1_enrich(abstract="Neural networks.")
        assert isinstance(result.keywords, list)

    def test_result_is_usable_for_summary_section(self) -> None:
        """The Tier-1 shape must provide enough data for ## Summary."""
        result = tier1_enrich(
            abstract=(
                "We present a novel approach to reinforcement learning "
                "that combines model-based planning with policy gradient "
                "methods for improved sample efficiency."
            )
        )
        assert result.summary
        assert isinstance(result.keywords, list)
