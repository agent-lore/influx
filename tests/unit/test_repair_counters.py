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


# ── parse_repair_section: section location ─────────────────────────


class TestParseRepairSectionLocation:
    """The read side locates ## Repair via canonical_note.extract_section_body."""

    def test_parses_counters_from_crlf_note(self) -> None:
        # CRLF regression: the legacy ^## Repair[ \t]*\n heading regex never
        # matched a "## Repair\r\n" heading and silently returned zero
        # counters; the canonical anchored matcher is CRLF-tolerant.
        content = (
            "# Paper\n\n## Repair\n"
            "- tier2_attempts: 2\n"
            '- tier2_last_stage: "parse"\n'
            "- tier3_attempts: 1\n\n"
            "## Profile Relevance\n### r\nScore: 9/10\nReason\n\n"
            "## User Notes\n"
        ).replace("\n", "\r\n")
        counters = parse_repair_section(content)
        assert counters.tier2_attempts == 2
        assert counters.tier2_last_stage == "parse"
        assert counters.tier3_attempts == 1

    def test_absent_section_returns_zero_defaults(self) -> None:
        counters = parse_repair_section(_BASE_NOTE)
        assert counters == RepairCounters()


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


# ── Archive-scoped counted stages (unsupported_source) ─────────────


class TestArchiveScopedCountedStages:
    """``unsupported_source`` is counted for the archive stage only.

    A note whose ``source:*`` tag has no registered reacquirer raises
    ``ExtractionError(stage="unsupported_source")`` from the archive
    hook on every pass.  Treated as transient it retries forever, which
    kept notes in the sweep set indefinitely.  It is definitionally
    permanent — unlike an HTTP 5xx, re-running changes nothing until
    code ships — so the archive stage counts it toward the cap.

    The text-extraction path must keep classifying it transient: it has
    its own terminal handling in
    ``repair._terminate_unsupported_text_source``, which flips
    ``influx:text-terminal`` directly rather than via the counter.
    """

    def test_unsupported_source_counted_for_archive_stage(self) -> None:
        exc = ExtractionError("source 'x' not supported", stage="unsupported_source")
        assert classify_failure(exc, repair_stage="archive") == "counted"

    def test_unsupported_source_transient_without_stage(self) -> None:
        """Default (text-extraction) classification is unchanged."""
        exc = ExtractionError("source 'x' not supported", stage="unsupported_source")
        assert classify_failure(exc) == "transient"

    def test_unsupported_source_transient_for_tier_stages(self) -> None:
        exc = ExtractionError("source 'x' not supported", stage="unsupported_source")
        assert classify_failure(exc, repair_stage="tier2") == "transient"
        assert classify_failure(exc, repair_stage="tier3") == "transient"

    def test_archive_stage_does_not_widen_other_transients(self) -> None:
        """The archive scope is an explicit list, not "anything archive-ish"."""
        for stage in ("http_5xx", "resolve", "archive_read"):
            exc = ExtractionError("boom", stage=stage)
            assert classify_failure(exc, repair_stage="archive") == "transient", stage

    def test_archive_stage_keeps_globally_counted_stages(self) -> None:
        for stage in ("parse", "validate", "oversize"):
            exc = ExtractionError("boom", stage=stage)
            assert classify_failure(exc, repair_stage="archive") == "counted", stage


# ── Archive-scoped counted stages (permanent HTTP — issue #282) ────


class TestArchiveScopedPermanentHttpStages:
    """Permanently-failing HTTP statuses are counted for archive only.

    Before #282 the archive hook flattened every HTTP failure to a bare
    ``"http"``, so a paywalled URL (403) was indistinguishable from a
    recoverable 503.  ``archive_attempts`` never advanced,
    ``influx:archive-terminal`` was never applied, and the note was
    re-selected and rewritten by every sweep — the production instance
    reached v43 at two versions per day.

    The partition is by *permanence*, not by "is it an error": a 403
    paywall, a 404, a 410 Gone, and an operator-declared ``blocked``
    domain all return the same answer however many times we ask.  A 429,
    a 5xx, or a 408 do not.
    """

    PERMANENT = ("http_403", "http_404", "http_410", "blocked")
    RECOVERABLE = ("http_429", "rate_limited", "http_4xx", "http_5xx", "network")

    @pytest.mark.parametrize("stage", PERMANENT)
    def test_permanent_kinds_counted_for_archive_stage(self, stage: str) -> None:
        exc = ExtractionError("archive_download retry failed", stage=stage)
        assert classify_failure(exc, repair_stage="archive") == "counted"

    @pytest.mark.parametrize("stage", PERMANENT)
    def test_permanent_kinds_transient_elsewhere(self, stage: str) -> None:
        """Stage-scoped: the tier and text paths are untouched.

        A 403 on a text-extraction fetch has its own terminal handling
        and must not be pulled into the global counted set.
        """
        exc = ExtractionError("archive_download retry failed", stage=stage)
        assert classify_failure(exc) == "transient"
        assert classify_failure(exc, repair_stage="tier2") == "transient"
        assert classify_failure(exc, repair_stage="tier3") == "transient"

    @pytest.mark.parametrize("stage", RECOVERABLE)
    def test_recoverable_kinds_stay_transient_everywhere(self, stage: str) -> None:
        exc = ExtractionError("archive_download retry failed", stage=stage)
        assert classify_failure(exc, repair_stage="archive") == "transient"
        assert classify_failure(exc, repair_stage="tier2") == "transient"
        assert classify_failure(exc) == "transient"

    def test_permanent_http_reaches_the_cap(self) -> None:
        """Three recorded 403s flip ``influx:archive-terminal``."""

        def record(content: str, tags: list[str]) -> CountedFailureResult:
            return record_counted_failure(
                content=content,
                tags=tags,
                stage="archive",
                failure_stage="http_403",
                failure_error="HTTP 403 for https://www.ft.com/content/x",
            )

        result = record(_BASE_NOTE, list(_BASE_TAGS))
        assert result.attempts == 1
        assert result.cap_reached is False

        result = record(result.new_content, result.new_tags)
        assert result.attempts == 2
        assert result.cap_reached is False

        result = record(result.new_content, result.new_tags)
        assert result.attempts == REPAIR_COUNTED_CAP
        assert result.cap_reached is True
        assert result.terminal_tag_added is True
        assert "influx:archive-terminal" in result.new_tags
        # The richer discriminator is persisted for the operator.
        assert result.counters.archive_last_kind == "http_403"


class TestArchiveScopedSetIntegrity:
    """Guards the archive scope against silent drift.

    The counted set is written as string literals in the code, in the
    tests above, and in ``docs/SPECIFICATION.md`` / ``runbook.md``.
    Nothing fails if they disagree: an unrecognised discriminator simply
    never matches, and the note quietly goes back to churning.

    So rather than re-assert the set, walk the *whole* public
    ``ArchiveFailureKind`` taxonomy and pin the classification of every
    member. Adding a kind to the taxonomy without deciding whether it is
    permanent fails here, which set-equality against a private constant
    would not catch.
    """

    # Permanent for the archive stage specifically.  ``unsupported_source``
    # is not an ArchiveFailureKind — the repair hook raises it before any
    # download — but it shares the scope, so it belongs in this table.
    PERMANENT_FOR_ARCHIVE = frozenset(
        {"http_403", "http_404", "http_410", "blocked", "unsupported_source"}
    )
    # Permanent everywhere, so counted regardless of stage.
    GLOBALLY_COUNTED = frozenset({"oversize"})

    @staticmethod
    def _all_kinds() -> frozenset[str]:
        from typing import get_args

        from influx.archive_policy import ArchiveFailureKind

        return frozenset(get_args(ArchiveFailureKind)) | {"unsupported_source"}

    def test_every_failure_kind_has_a_decided_classification(self) -> None:
        counted = self.PERMANENT_FOR_ARCHIVE | self.GLOBALLY_COUNTED
        for kind in sorted(self._all_kinds()):
            exc = ExtractionError("archive_download retry failed", stage=kind)
            expected = "counted" if kind in counted else "transient"
            assert classify_failure(exc, repair_stage="archive") == expected, kind

    def test_no_failure_kind_is_counted_outside_the_archive_stage(self) -> None:
        """The scope really is a scope — tier stages see none of this."""
        for kind in sorted(self._all_kinds() - self.GLOBALLY_COUNTED):
            exc = ExtractionError("archive_download retry failed", stage=kind)
            assert classify_failure(exc, repair_stage="tier2") == "transient", kind
            assert classify_failure(exc, repair_stage="tier3") == "transient", kind
            assert classify_failure(exc) == "transient", kind
