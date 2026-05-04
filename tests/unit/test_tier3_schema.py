"""Tests for Tier3Extraction Pydantic model (FR-ENR-5, PRD 07 §5.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from influx.schemas import TIER3_LIST_MAX, Tier3Extraction


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

    def test_datasets_length_at_cap(self) -> None:
        """Issue #81: datasets accepts up to TIER3_LIST_MAX items."""
        t = Tier3Extraction(**_valid(datasets=[f"d{i}" for i in range(TIER3_LIST_MAX)]))
        assert len(t.datasets) == TIER3_LIST_MAX

    def test_builds_on_length_0(self) -> None:
        t = Tier3Extraction(**_valid(builds_on=[]))
        assert len(t.builds_on) == 0

    def test_builds_on_length_10(self) -> None:
        t = Tier3Extraction(**_valid(builds_on=[f"b{i}" for i in range(10)]))
        assert len(t.builds_on) == 10

    def test_builds_on_length_at_cap(self) -> None:
        """Issue #81: builds_on accepts up to TIER3_LIST_MAX items."""
        t = Tier3Extraction(
            **_valid(builds_on=[f"b{i}" for i in range(TIER3_LIST_MAX)])
        )
        assert len(t.builds_on) == TIER3_LIST_MAX

    @pytest.mark.parametrize("size", [13, 14, 23, 30])
    def test_observed_failure_sizes_now_parse(self, size: int) -> None:
        """Issue #81: 13-, 14-, 23-, 30-item datasets/builds_on now parse."""
        t = Tier3Extraction(
            **_valid(
                datasets=[f"d{i}" for i in range(size)],
                builds_on=[f"b{i}" for i in range(size)],
            )
        )
        assert len(t.datasets) == size
        assert len(t.builds_on) == size

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

    def test_datasets_length_over_cap(self) -> None:
        """Issue #81: datasets length TIER3_LIST_MAX + 1 is still rejected."""
        with pytest.raises(ValidationError):
            Tier3Extraction(
                **_valid(datasets=[f"d{i}" for i in range(TIER3_LIST_MAX + 1)])
            )

    def test_builds_on_length_over_cap(self) -> None:
        """Issue #81: builds_on length TIER3_LIST_MAX + 1 is still rejected."""
        with pytest.raises(ValidationError):
            Tier3Extraction(
                **_valid(builds_on=[f"b{i}" for i in range(TIER3_LIST_MAX + 1)])
            )

    def test_observed_39_item_case_still_rejected(self) -> None:
        """Issue #81: 39-item datasets case from the bug report still fails."""
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(datasets=[f"d{i}" for i in range(39)]))

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

    def test_dict_element_in_claims_raises_validation_error(self) -> None:
        """Non-string list elements (e.g. dict per item from a model that
        emitted structured output) must raise ``ValidationError`` rather
        than ``AttributeError`` so callers see the standard Pydantic
        contract.  Staging incident 2026-05-01.
        """
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(claims=[{"claim": "x", "score": 0.8}]))  # type: ignore[arg-type]

    def test_dict_element_in_builds_on_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(
                **_valid(builds_on=[{"title": "FooNet", "arxiv_id": "2412.12345"}])  # type: ignore[arg-type]
            )

    def test_dict_element_in_potential_connections_raises_validation_error(
        self,
    ) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(
                **_valid(potential_connections=[{"title": "Foo"}])  # type: ignore[arg-type]
            )

    def test_missing_claims(self) -> None:
        with pytest.raises(ValidationError):
            Tier3Extraction(
                datasets=["d"],
                builds_on=["b"],
                open_questions=["q"],
                potential_connections=["p"],
            )  # type: ignore[call-arg]


class TestTier3ListMaxConstant:
    """Issue #81: TIER3_LIST_MAX is the single source of truth for the cap."""

    def test_constant_value(self) -> None:
        """The cap is 30 today (raised from 10 per issue #81)."""
        assert TIER3_LIST_MAX == 30

    def test_schema_max_length_reads_from_constant(self) -> None:
        """The schema's ``max_length`` for the bumped fields must equal
        ``TIER3_LIST_MAX`` so a future bump propagates without code drift.
        """
        schema = Tier3Extraction.model_json_schema()
        assert schema["properties"]["datasets"]["maxItems"] == TIER3_LIST_MAX
        assert schema["properties"]["builds_on"]["maxItems"] == TIER3_LIST_MAX
