"""Tests for FilterResult and FilterResponse Pydantic models (FR-FLT-3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from influx.schemas import (
    FilterResponse,
    FilterResult,
    Tier1Enrichment,
    Tier3Extraction,
)


class TestFilterResultPositive:
    """Well-formed FilterResult payloads parse correctly."""

    def test_minimal_valid(self) -> None:
        r = FilterResult(id="2601.12345", score=5, tags=[], reason="ok")
        assert r.id == "2601.12345"
        assert r.score == 5
        assert r.tags == []
        assert r.reason == "ok"

    def test_max_tags(self) -> None:
        tags = ["a", "b", "c", "d", "e"]
        r = FilterResult(id="x", score=10, tags=tags, reason="r")
        assert len(r.tags) == 5

    def test_extra_tags_are_trimmed_to_contract_limit(self) -> None:
        r = FilterResult(
            id="x",
            score=5,
            tags=["a", "b", "c", "d", "e", "f"],
            reason="r",
        )

        assert r.tags == ["a", "b", "c", "d", "e"]

    def test_score_boundary_low(self) -> None:
        r = FilterResult(id="x", score=1, tags=[], reason="r")
        assert r.score == 1

    def test_score_boundary_high(self) -> None:
        r = FilterResult(id="x", score=10, tags=[], reason="r")
        assert r.score == 10

    def test_tags_default_empty(self) -> None:
        r = FilterResult(id="x", score=5, reason="r")
        assert r.tags == []


class TestFilterResultNegative:
    """Invalid FilterResult payloads raise ValidationError."""

    def test_score_below_minimum(self) -> None:
        with pytest.raises(ValidationError):
            FilterResult(id="x", score=0, tags=[], reason="r")

    def test_score_above_maximum(self) -> None:
        with pytest.raises(ValidationError):
            FilterResult(id="x", score=11, tags=[], reason="r")

    def test_missing_id(self) -> None:
        with pytest.raises(ValidationError):
            FilterResult(score=5, tags=[], reason="r")  # type: ignore[call-arg]

    def test_missing_reason(self) -> None:
        with pytest.raises(ValidationError):
            FilterResult(id="x", score=5, tags=[])  # type: ignore[call-arg]

    def test_missing_score(self) -> None:
        with pytest.raises(ValidationError):
            FilterResult(id="x", tags=[], reason="r")  # type: ignore[call-arg]


class TestFilterResponsePositive:
    """Well-formed FilterResponse JSON parses correctly."""

    def test_from_json_payload(self) -> None:
        payload = {
            "results": [
                {"id": "2601.001", "score": 8, "tags": ["ml"], "reason": "relevant"},
                {"id": "2601.002", "score": 3, "tags": [], "reason": "off-topic"},
            ]
        }
        resp = FilterResponse.model_validate(payload)
        assert len(resp.results) == 2
        assert resp.results[0].id == "2601.001"
        assert resp.results[0].score == 8
        assert resp.results[0].tags == ["ml"]
        assert resp.results[1].score == 3

    def test_empty_results(self) -> None:
        resp = FilterResponse.model_validate({"results": []})
        assert resp.results == []


class TestFilterResponseNegative:
    """Invalid FilterResponse payloads raise ValidationError."""

    def test_missing_results(self) -> None:
        with pytest.raises(ValidationError):
            FilterResponse.model_validate({})

    def test_nested_invalid_score(self) -> None:
        payload = {
            "results": [
                {"id": "x", "score": 99, "tags": [], "reason": "r"},
            ]
        }
        with pytest.raises(ValidationError):
            FilterResponse.model_validate(payload)


class TestTier1Enrichment:
    """Tier-1 enrichment schema validation (FR-ENR-4)."""

    def test_minimal_valid(self) -> None:
        t = Tier1Enrichment(
            contributions=["c1"],
            method="m",
            result="r",
            relevance="rel",
        )
        assert t.contributions == ["c1"]

    def test_max_contributions(self) -> None:
        t = Tier1Enrichment(
            contributions=["a", "b", "c", "d", "e", "f"],
            method="m",
            result="r",
            relevance="rel",
        )
        assert len(t.contributions) == 6

    def test_too_many_contributions(self) -> None:
        with pytest.raises(ValidationError):
            Tier1Enrichment(
                contributions=["a"] * 7, method="m", result="r", relevance="rel"
            )

    def test_empty_contributions(self) -> None:
        with pytest.raises(ValidationError):
            Tier1Enrichment(contributions=[], method="m", result="r", relevance="rel")


class TestTier3Extraction:
    """Tier-3 deep extraction schema validation (FR-ENR-5)."""

    def test_minimal_valid(self) -> None:
        t = Tier3Extraction(claims=["claim1"])
        assert t.claims == ["claim1"]
        assert t.datasets == []
        assert t.builds_on == []

    def test_full_payload(self) -> None:
        t = Tier3Extraction(
            claims=["c1", "c2"],
            datasets=["d1"],
            builds_on=["b1"],
            open_questions=["o1"],
            potential_connections=["p1"],
        )
        assert t.builds_on == ["b1"]

    def test_trim_and_truncate_long_strings(self) -> None:
        long_text = "x" * 800
        t = Tier3Extraction(claims=[long_text])
        assert len(t.claims[0]) == 500

    def test_whitespace_trimmed(self) -> None:
        t = Tier3Extraction(claims=["  hello  "])
        assert t.claims == ["hello"]

    def test_empty_after_trim_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(claims=["   "])

    def test_too_many_claims(self) -> None:
        t = Tier3Extraction(claims=[f"c{i}" for i in range(11)])
        assert t.claims == [f"c{i}" for i in range(10)]

    def test_no_claims_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(claims=[])

    def test_too_many_datasets(self) -> None:
        t = Tier3Extraction(claims=["c"], datasets=[f"d{i}" for i in range(11)])
        assert t.datasets == [f"d{i}" for i in range(10)]
