"""Unit tests for the ``record_counted_failure`` operation (issue #53).

The repair_counters module owns the canonical four-step pattern that
the repair sweep repeats for tier 2, tier 3, and archive download:

1. parse the existing ``## Repair`` section
2. bump the per-stage counter
3. upsert the new counters back into the note body
4. add ``influx:<stage>-terminal`` when the cap is reached (idempotent)

These tests cover the combined ``record_counted_failure`` API plus the
read/advance/cap-check contract. Lower-level parse / render / upsert
behaviour is exercised in ``test_repair_self_repair.py``.
"""

from __future__ import annotations

import pytest

from influx.errors import ExtractionError, InfluxError, LCMAError, LithosError
from influx.repair_counters import (
    REPAIR_COUNTED_CAP,
    CountedFailureResult,
    RepairCounters,
    classify_failure,
    parse_repair_section,
    record_counted_failure,
    terminal_tag_for,
)

# A minimal note body with the placement landmarks ``upsert_repair_section``
# uses to insert the ``## Repair`` section in canonical position.
_BASE_NOTE = (
    "# Paper\n\n"
    "## Summary\nA paper.\n\n"
    "## Profile Relevance\n### research\nScore: 9/10\nReason\n\n"
    "## User Notes\n"
)
_BASE_TAGS = ["source:arxiv", "ingested-by:influx", "schema:1"]


# ── terminal_tag_for ───────────────────────────────────────────────


class TestTerminalTagFor:
    """``terminal_tag_for`` returns the canonical ``influx:<stage>-terminal``."""

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            ("tier2", "influx:tier2-terminal"),
            ("tier3", "influx:tier3-terminal"),
            ("archive", "influx:archive-terminal"),
        ],
    )
    def test_canonical_tag(self, stage: str, expected: str) -> None:
        # ``stage`` is typed as ``CountedStage`` but pytest parametrise
        # passes plain strings — cast at the call site.
        from influx.repair_counters import CountedStage

        cs: CountedStage = stage  # type: ignore[assignment]
        assert terminal_tag_for(cs) == expected


# ── attempts_for ───────────────────────────────────────────────────


class TestAttemptsFor:
    """``RepairCounters.attempts_for`` reads per-stage counter values."""

    def test_tier2_counter(self) -> None:
        c = RepairCounters(tier2_attempts=2, tier3_attempts=5, archive_attempts=1)
        assert c.attempts_for("tier2") == 2

    def test_tier3_counter(self) -> None:
        c = RepairCounters(tier2_attempts=2, tier3_attempts=5, archive_attempts=1)
        assert c.attempts_for("tier3") == 5

    def test_archive_counter(self) -> None:
        c = RepairCounters(tier2_attempts=2, tier3_attempts=5, archive_attempts=1)
        assert c.attempts_for("archive") == 1


# ── record_counted_failure: cap not reached ───────────────────────


class TestRecordCountedFailureBelowCap:
    """First counted failure: counter advances; no terminal tag added."""

    def test_first_tier2_failure_advances_counter(self) -> None:
        result = record_counted_failure(
            content=_BASE_NOTE,
            tags=list(_BASE_TAGS),
            stage="tier2",
            failure_stage="parse",
            failure_error="bad json",
        )
        assert result.attempts == 1
        assert result.cap_reached is False
        assert result.terminal_tag_added is False
        assert "influx:tier2-terminal" not in result.new_tags
        # Tags preserved verbatim
        assert result.new_tags == _BASE_TAGS
        assert result.counters.tier2_attempts == 1
        assert result.counters.tier2_last_stage == "parse"
        assert result.counters.tier2_last_error == "bad json"

    def test_repair_section_inserted_in_content(self) -> None:
        result = record_counted_failure(
            content=_BASE_NOTE,
            tags=list(_BASE_TAGS),
            stage="tier3",
            failure_stage="validate",
            failure_error="schema mismatch",
        )
        assert "## Repair\n" in result.new_content
        assert "tier3_attempts: 1" in result.new_content
        assert '- tier3_last_stage: "validate"' in result.new_content
        # Round-trips through parse
        roundtrip = parse_repair_section(result.new_content)
        assert roundtrip.tier3_attempts == 1
        assert roundtrip.tier3_last_stage == "validate"

    def test_existing_counter_advances_from_existing_state(self) -> None:
        content_with_two = (
            "# Paper\n\n## Repair\n"
            "- tier2_attempts: 2\n"
            '- tier2_last_stage: "parse"\n'
            "- tier3_attempts: 0\n"
            "- archive_attempts: 0\n\n"
            "## Profile Relevance\n### r\nScore: 9/10\nReason\n\n"
            "## User Notes\n"
        )
        result = record_counted_failure(
            content=content_with_two,
            tags=list(_BASE_TAGS),
            stage="tier2",
            failure_stage="validate",
            failure_error="oops",
        )
        # 2 + 1 = 3 → cap reached on this single advance
        assert result.attempts == 3
        assert result.cap_reached is True
        assert result.terminal_tag_added is True
        assert "influx:tier2-terminal" in result.new_tags


# ── record_counted_failure: cap-reach and idempotence ──────────────


class TestRecordCountedFailureAtCap:
    """Cap-reach flips the terminal tag exactly once."""

    def test_cap_reach_adds_terminal_tag(self) -> None:
        content = (
            "# Paper\n\n## Repair\n"
            f"- tier3_attempts: {REPAIR_COUNTED_CAP - 1}\n\n"
            "## User Notes\n"
        )
        result = record_counted_failure(
            content=content,
            tags=list(_BASE_TAGS),
            stage="tier3",
            failure_stage="parse",
            failure_error="boom",
        )
        assert result.cap_reached is True
        assert result.terminal_tag_added is True
        assert result.new_tags[-1] == "influx:tier3-terminal"

    def test_already_terminal_does_not_re_add(self) -> None:
        """Idempotence: if ``influx:<stage>-terminal`` is already present,
        a subsequent counted failure does not duplicate the tag, and
        ``terminal_tag_added`` is False."""
        content = (
            "# Paper\n\n## Repair\n"
            f"- tier3_attempts: {REPAIR_COUNTED_CAP}\n\n"
            "## User Notes\n"
        )
        tags = [*_BASE_TAGS, "influx:tier3-terminal"]
        result = record_counted_failure(
            content=content,
            tags=tags,
            stage="tier3",
            failure_stage="parse",
            failure_error="still bad",
        )
        assert result.cap_reached is True
        assert result.terminal_tag_added is False  # NOT newly added
        assert result.new_tags.count("influx:tier3-terminal") == 1

    def test_archive_cap_uses_archive_terminal_tag(self) -> None:
        content = (
            "# Paper\n\n## Repair\n"
            f"- archive_attempts: {REPAIR_COUNTED_CAP - 1}\n\n"
            "## User Notes\n"
        )
        result = record_counted_failure(
            content=content,
            tags=list(_BASE_TAGS),
            stage="archive",
            failure_stage="oversize",
            failure_error="exceeds 100MB",
        )
        assert result.cap_reached is True
        assert result.terminal_tag_added is True
        assert "influx:archive-terminal" in result.new_tags
        # tier-specific terminal tags must NOT leak across stages
        assert "influx:tier2-terminal" not in result.new_tags
        assert "influx:tier3-terminal" not in result.new_tags

    def test_input_tags_not_mutated(self) -> None:
        """The input *tags* list is treated immutably."""
        original_tags = list(_BASE_TAGS)
        before = list(original_tags)
        content = (
            "# Paper\n\n## Repair\n"
            f"- tier2_attempts: {REPAIR_COUNTED_CAP - 1}\n\n"
            "## User Notes\n"
        )
        result = record_counted_failure(
            content=content,
            tags=original_tags,
            stage="tier2",
            failure_stage="parse",
            failure_error="boom",
        )
        assert original_tags == before
        # And the returned list is a fresh object
        assert result.new_tags is not original_tags


# ── record_counted_failure: stage isolation ────────────────────────


class TestStageIsolation:
    """Bumping one stage does not leak counters into the other stages."""

    def test_tier2_bump_does_not_advance_tier3_or_archive(self) -> None:
        result = record_counted_failure(
            content=_BASE_NOTE,
            tags=list(_BASE_TAGS),
            stage="tier2",
            failure_stage="parse",
            failure_error="boom",
        )
        assert result.counters.tier2_attempts == 1
        assert result.counters.tier3_attempts == 0
        assert result.counters.archive_attempts == 0


# ── classify_failure partition contract ────────────────────────────


class TestTransientCountedPartition:
    """Callers consult ``classify_failure`` BEFORE calling
    ``record_counted_failure`` — transient failures must not enter
    the counted path. These tests document the partition contract."""

    def test_transient_failures(self) -> None:
        transients: list[BaseException] = [
            LithosError("connection refused", operation="write"),
            LCMAError("timeout", model="extract", stage="http"),
            LCMAError("model slot missing", stage="resolve"),
            LCMAError("opaque failure"),  # no stage
            ExtractionError("io error", url="http://x", stage="archive_read"),
            InfluxError("generic"),
            ValueError("oops"),
        ]
        for exc in transients:
            assert classify_failure(exc) == "transient", (
                f"expected transient for {exc!r}"
            )

    def test_counted_failures(self) -> None:
        counted: list[BaseException] = [
            LCMAError("bad json", model="extract", stage="parse"),
            LCMAError("schema validation failed", stage="validate"),
            ExtractionError("no full text", url="http://x", stage="parse"),
            ExtractionError("too big", url="http://x", stage="oversize"),
        ]
        for exc in counted:
            assert classify_failure(exc) == "counted", f"expected counted for {exc!r}"


# ── Result dataclass shape ─────────────────────────────────────────


class TestCountedFailureResultShape:
    """``CountedFailureResult`` exposes the fields the sweep needs."""

    def test_has_expected_fields(self) -> None:
        result = record_counted_failure(
            content=_BASE_NOTE,
            tags=list(_BASE_TAGS),
            stage="tier2",
            failure_stage="parse",
            failure_error="boom",
        )
        assert isinstance(result, CountedFailureResult)
        assert isinstance(result.counters, RepairCounters)
        assert isinstance(result.new_content, str)
        assert isinstance(result.new_tags, list)
        assert isinstance(result.attempts, int)
        assert isinstance(result.cap_reached, bool)
        assert result.terminal_tag == "influx:tier2-terminal"
        assert isinstance(result.terminal_tag_added, bool)
