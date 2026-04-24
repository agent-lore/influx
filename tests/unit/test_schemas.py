"""Tests for FilterResult and FilterResponse Pydantic models (FR-FLT-3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from influx.schemas import FilterResponse, FilterResult


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

    def test_tags_exceeds_max_length(self) -> None:
        with pytest.raises(ValidationError):
            FilterResult(
                id="x",
                score=5,
                tags=["a", "b", "c", "d", "e", "f"],
                reason="r",
            )

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
