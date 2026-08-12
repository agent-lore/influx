"""Tests for Tier3Extraction Pydantic model (FR-ENR-5, PRD 07 §5.3)."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from influx.schemas import TIER3_LIST_MAX, TIER3_SHORT_LIST_MAX, Tier3Extraction


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

    def test_claims_length_at_cap(self) -> None:
        """Issue #186: claims accepts up to TIER3_LIST_MAX items."""
        t = Tier3Extraction(**_valid(claims=[f"c{i}" for i in range(TIER3_LIST_MAX)]))
        assert len(t.claims) == TIER3_LIST_MAX

    @pytest.mark.parametrize("size", [11, 20, TIER3_LIST_MAX])
    def test_claims_over_10_now_parse(self, size: int) -> None:
        """Issue #186: >10 claims (observed up to 20) now parse and
        round-trip instead of discarding the whole extraction."""
        claims = [f"c{i}" for i in range(size)]
        t = Tier3Extraction(**_valid(claims=claims))
        assert t.claims == claims

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

    # Over-cap list lengths are no longer a rejection path — issue #288
    # moved them to truncation.  See ``TestTier3ListTruncation``.

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


class TestTier3ListTruncation:
    """Issue #288: over-long lists are truncated, not rejected.

    Rejecting the whole object for list length discarded otherwise-valid
    extractions.  Because ``validate`` is a counted repair stage, three
    such rejections applied ``influx:tier3-terminal`` and lost the
    extraction permanently — which is what happened to
    ``9eee59f3`` ("Context-Aware RL for Agentic and Multimodal LLMs").
    """

    @pytest.mark.parametrize("field", ["claims", "datasets", "builds_on"])
    def test_over_cap_truncates_to_cap(self, field: str) -> None:
        items = [f"{field}-{i}" for i in range(TIER3_LIST_MAX + 1)]
        t = Tier3Extraction(**_valid(**{field: items}))
        assert len(getattr(t, field)) == TIER3_LIST_MAX

    @pytest.mark.parametrize("field", ["claims", "datasets", "builds_on"])
    def test_truncation_keeps_the_leading_items_in_order(self, field: str) -> None:
        """The prompt asks for items 'prioritised by relevance', so the
        head of the list is the part worth keeping."""
        items = [f"{field}-{i}" for i in range(TIER3_LIST_MAX + 5)]
        t = Tier3Extraction(**_valid(**{field: items}))
        assert getattr(t, field) == items[:TIER3_LIST_MAX]

    @pytest.mark.parametrize(
        ("field", "size"),
        [
            ("datasets", 35),
            ("builds_on", 79),
            ("builds_on", 32),
            ("claims", 31),
        ],
    )
    def test_observed_production_overflows_now_survive(
        self, field: str, size: int
    ) -> None:
        """The four real overflows found in the prod corpus — every
        recorded ``tier3_last_stage: "validate"`` failure was one of
        these, none was a genuine schema violation."""
        items = [f"{field}-{i}" for i in range(size)]
        t = Tier3Extraction(**_valid(**{field: items}))
        assert getattr(t, field) == items[:TIER3_LIST_MAX]

    def test_issue_81_39_item_case_now_survives(self) -> None:
        """The 39-item datasets case from #81's bug report was the
        original reason the cap was raised 10 → 30; it no longer needs a
        cap large enough to contain it."""
        items = [f"d{i}" for i in range(39)]
        t = Tier3Extraction(**_valid(datasets=items))
        assert t.datasets == items[:TIER3_LIST_MAX]

    @pytest.mark.parametrize("field", ["open_questions", "potential_connections"])
    def test_short_capped_fields_truncate_too(self, field: str) -> None:
        items = [f"{field}-{i}" for i in range(TIER3_SHORT_LIST_MAX + 7)]
        t = Tier3Extraction(**_valid(**{field: items}))
        assert getattr(t, field) == items[:TIER3_SHORT_LIST_MAX]

    def test_truncation_is_logged_with_field_and_dropped_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silent truncation would trade one invisible failure for
        another; the drop must stay observable."""
        with caplog.at_level(logging.INFO, logger="influx.schemas"):
            Tier3Extraction(
                **_valid(datasets=[f"d{i}" for i in range(35)]),
            )
        records = [r for r in caplog.records if "truncat" in r.getMessage().lower()]
        assert len(records) == 1
        # Assert the log *arguments* rather than substrings of the
        # formatted message: "5" is a substring of "35", so a substring
        # check would pass even if the dropped count were wrong.
        assert records[0].args == ("datasets", 35, TIER3_LIST_MAX, 5)

    def test_no_log_when_within_cap(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="influx.schemas"):
            Tier3Extraction(**_valid(datasets=[f"d{i}" for i in range(TIER3_LIST_MAX)]))
        assert not [r for r in caplog.records if "truncat" in r.getMessage().lower()]

    def test_empty_claims_still_rejected(self) -> None:
        """AC-07-C is untouched: an empty extraction is a real failure.
        Truncation can never empty a non-empty list, so ``min_length``
        remains the meaningful bound."""
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(claims=[]))

    def test_invalid_element_within_the_cap_still_rejects(self) -> None:
        """Truncation must not become a way to smuggle malformed items
        past the element-level rules."""
        items = [f"c{i}" for i in range(TIER3_LIST_MAX + 5)]
        items[0] = "   "
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(claims=items))

    def test_invalid_element_beyond_the_cap_is_dropped_not_raised(self) -> None:
        """Capping happens before element validation, so junk in the
        discarded tail cannot fail an extraction we are keeping."""
        items: list[object] = [f"c{i}" for i in range(TIER3_LIST_MAX)]
        items.append({"claim": "structured instead of string"})
        t = Tier3Extraction(**_valid(claims=items))  # type: ignore[arg-type]
        assert len(t.claims) == TIER3_LIST_MAX

    def test_over_cap_tuple_is_truncated_like_a_list(self) -> None:
        """Pydantic coerces a tuple into ``list[str]``, so the cap has to
        apply to it too — otherwise a direct Python caller skips
        truncation and hits ``max_length``, the exact rejection this
        validator exists to avoid."""
        items = tuple(f"c{i}" for i in range(TIER3_LIST_MAX + 5))
        t = Tier3Extraction(**_valid(claims=items))  # type: ignore[arg-type]
        assert t.claims == list(items[:TIER3_LIST_MAX])

    def test_over_cap_set_is_truncated_not_rejected(self) -> None:
        """Sets have no meaningful head, but truncating beats rejecting."""
        items = {f"c{i}" for i in range(TIER3_LIST_MAX + 5)}
        t = Tier3Extraction(**_valid(claims=items))  # type: ignore[arg-type]
        assert len(t.claims) == TIER3_LIST_MAX

    def test_mapping_input_still_raises_validation_error(self) -> None:
        """Staging incident 2026-05-01: the failure must stay a
        ``ValidationError``, never a raw ``TypeError``."""
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(claims={"not": "a list"}))  # type: ignore[arg-type]

    def test_string_input_still_raises_validation_error(self) -> None:
        """A bare string must not be split into a list of characters."""
        with pytest.raises(ValidationError):
            Tier3Extraction(**_valid(claims="not a list"))  # type: ignore[arg-type]


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
        assert schema["properties"]["claims"]["maxItems"] == TIER3_LIST_MAX
        assert schema["properties"]["datasets"]["maxItems"] == TIER3_LIST_MAX
        assert schema["properties"]["builds_on"]["maxItems"] == TIER3_LIST_MAX
