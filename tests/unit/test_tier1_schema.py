"""Tests for Tier1Enrichment Pydantic model (FR-ENR-4, PRD 07 §5.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from influx.schemas import Tier1Enrichment


class TestTier1EnrichmentPositive:
    """Well-formed Tier1Enrichment payloads parse correctly."""

    def test_contributions_length_1(self) -> None:
        t = Tier1Enrichment(
            contributions=["c1"],
            method="m",
            result="r",
            relevance="rel",
        )
        assert len(t.contributions) == 1

    def test_contributions_length_6(self) -> None:
        t = Tier1Enrichment(
            contributions=["c1", "c2", "c3", "c4", "c5", "c6"],
            method="m",
            result="r",
            relevance="rel",
        )
        assert len(t.contributions) == 6

    def test_all_fields_stored(self) -> None:
        t = Tier1Enrichment(
            contributions=["a"],
            method="gradient descent",
            result="SOTA on X",
            relevance="high",
        )
        assert t.contributions == ["a"]
        assert t.method == "gradient descent"
        assert t.result == "SOTA on X"
        assert t.relevance == "high"


class TestTier1EnrichmentNegative:
    """Invalid Tier1Enrichment payloads raise ValidationError."""

    def test_contributions_length_0(self) -> None:
        with pytest.raises(ValidationError):
            Tier1Enrichment(
                contributions=[],
                method="m",
                result="r",
                relevance="rel",
            )

    def test_contributions_length_7(self) -> None:
        with pytest.raises(ValidationError):
            Tier1Enrichment(
                contributions=["c1", "c2", "c3", "c4", "c5", "c6", "c7"],
                method="m",
                result="r",
                relevance="rel",
            )

    def test_missing_contributions(self) -> None:
        with pytest.raises(ValidationError):
            Tier1Enrichment(
                method="m",
                result="r",
                relevance="rel",
            )  # type: ignore[call-arg]

    def test_missing_method(self) -> None:
        with pytest.raises(ValidationError):
            Tier1Enrichment(
                contributions=["c1"],
                result="r",
                relevance="rel",
            )  # type: ignore[call-arg]

    def test_missing_result(self) -> None:
        with pytest.raises(ValidationError):
            Tier1Enrichment(
                contributions=["c1"],
                method="m",
                relevance="rel",
            )  # type: ignore[call-arg]

    def test_missing_relevance(self) -> None:
        with pytest.raises(ValidationError):
            Tier1Enrichment(
                contributions=["c1"],
                method="m",
                result="r",
            )  # type: ignore[call-arg]
