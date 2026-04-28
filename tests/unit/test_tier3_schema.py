"""Tests for Tier3Extraction Pydantic model (FR-ENR-5, PRD 07 §5.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from influx.schemas import Tier3Extraction


def _valid(**overrides: list[str]) -> dict[str, list[str]]:
    """Return a minimal valid Tier3Extraction payload, with overrides."""
    base: dict[str, list[str]] = {
        "claims": ["claim1"],
        "datasets": [],
        "builds_on": [],
        "open_questions": [],
        "potential_connections": [],
    }
    base.update(overrides)
    return base


class TestTier3ExtractionPositive:
    """Well-formed Tier3Extraction payloads parse correctly."""

    def test_claims_length_1(self) -> None:
        t = Tier3Extraction(**_valid(claims=["c1"]))
        assert len(t.claims) == 1

    def test_claims_length_10(self) -> None:
        t = Tier3Extraction(**_valid(claims=[f"c{i}" for i in range(10)]))
        assert len(t.claims) == 10

    def test_datasets_length_0(self) -> None:
        t = Tier3Extraction(**_valid(datasets=[]))
        assert len(t.datasets) == 0

    def test_datasets_length_10(self) -> None:
        t = Tier3Extraction(**_valid(datasets=[f"d{i}" for i in range(10)]))
        assert len(t.datasets) == 10

    def test_builds_on_length_0(self) -> None:
        t = Tier3Extraction(**_valid(builds_on=[]))
        assert len(t.builds_on) == 0

    def test_builds_on_length_10(self) -> None:
        t = Tier3Extraction(**_valid(builds_on=[f"b{i}" for i in range(10)]))
        assert len(t.builds_on) == 10

    def test_open_questions_length_0(self) -> None:
        t = Tier3Extraction(**_valid(open_questions=[]))
        assert len(t.open_questions) == 0

    def test_open_questions_length_10(self) -> None:
        t = Tier3Extraction(**_valid(open_questions=[f"q{i}" for i in range(10)]))
        assert len(t.open_questions) == 10

    def test_potential_connections_length_0(self) -> None:
        t = Tier3Extraction(**_valid(potential_connections=[]))
        assert len(t.potential_connections) == 0

    def test_potential_connections_length_10(self) -> None:
        t = Tier3Extraction(
            **_valid(potential_connections=[f"p{i}" for i in range(10)])
        )
        assert len(t.potential_connections) == 10

    def test_all_fields_stored(self) -> None:
        t = Tier3Extraction(
            claims=["claim"],
            datasets=["dataset"],
            builds_on=["paper"],
            open_questions=["question"],
            potential_connections=["connection"],
        )
        assert t.claims == ["claim"]
        assert t.datasets == ["dataset"]
        assert t.builds_on == ["paper"]
        assert t.open_questions == ["question"]
        assert t.potential_connections == ["connection"]

    def test_defaults_for_optional_lists(self) -> None:
        t = Tier3Extraction(claims=["c1"])
        assert t.datasets == []
        assert t.builds_on == []
        assert t.open_questions == []
        assert t.potential_connections == []


class TestTier3ExtractionNegative:
    """Invalid Tier3Extraction payloads raise ValidationError."""

    def test_claims_length_0(self) -> None:
        """AC-07-C: claims must have at least 1 element."""
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(claims=[]))

    def test_claims_length_11(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(claims=[f"c{i}" for i in range(11)]))

    def test_datasets_length_11(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(datasets=[f"d{i}" for i in range(11)]))

    def test_builds_on_length_11(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(builds_on=[f"b{i}" for i in range(11)]))

    def test_open_questions_length_11(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(open_questions=[f"q{i}" for i in range(11)]))

    def test_potential_connections_length_11(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(
                **_valid(potential_connections=[f"p{i}" for i in range(11)])
            )

    def test_empty_string_in_claims(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(claims=[""]))

    def test_whitespace_only_in_claims(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(claims=["  "]))

    def test_empty_string_in_datasets(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(datasets=[""]))

    def test_whitespace_only_in_builds_on(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(builds_on=["  \t "]))

    def test_empty_string_in_open_questions(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(open_questions=[""]))

    def test_empty_string_in_potential_connections(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(potential_connections=[""]))

    def test_missing_claims(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(
                datasets=["d"],
                builds_on=["b"],
                open_questions=["q"],
                potential_connections=["p"],
            )  # type: ignore[call-arg]
