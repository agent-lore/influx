"""Unit tests for Layer 2 self-repair helpers.

Covers:

* ``classify_failure`` — stage-driven transient-vs-counted partition that
  decides whether a sweep failure should advance the cap counter.
* ``parse_repair_section`` / ``render_repair_section`` /
  ``upsert_repair_section`` — parser and serializer for the per-note
  ``## Repair`` section that holds the per-stage attempt counter.

The cap-flip integration (counter bump + ``influx:tier{2,3}-terminal``
tag) is exercised in ``test_repair_sweep.py``.
"""

from __future__ import annotations

from influx.errors import ExtractionError, InfluxError, LCMAError, LithosError
from influx.repair import (
    RepairCounters,
    classify_failure,
    parse_repair_section,
    render_repair_section,
    upsert_repair_section,
)

# ── Classifier ───────────────────────────────────────────────────────


class TestClassifyFailure:
    """Counted = persistent (advances cap). Transient = retry forever."""

    def test_lithos_error_is_transient(self) -> None:
        exc = LithosError(
            "connection refused", operation="write", detail="econnrefused"
        )
        assert classify_failure(exc) == "transient"

    def test_lcma_http_stage_is_transient(self) -> None:
        assert (
            classify_failure(LCMAError("timeout", model="extract", stage="http"))
            == "transient"
        )

    def test_lcma_resolve_stage_is_transient(self) -> None:
        assert (
            classify_failure(LCMAError("model slot missing", stage="resolve"))
            == "transient"
        )

    def test_lcma_unspecified_stage_is_transient(self) -> None:
        # No stage info means we can't say it's persistent — keep retrying.
        assert classify_failure(LCMAError("opaque failure")) == "transient"

    def test_lcma_validate_stage_is_counted(self) -> None:
        assert (
            classify_failure(
                LCMAError(
                    "schema validation failed",
                    model="extract",
                    stage="validate",
                    detail="missing field",
                )
            )
            == "counted"
        )

    def test_lcma_parse_stage_is_counted(self) -> None:
        assert (
            classify_failure(LCMAError("bad json", model="extract", stage="parse"))
            == "counted"
        )

    def test_extraction_parse_stage_is_counted(self) -> None:
        assert (
            classify_failure(
                ExtractionError("no full text", url="http://x", stage="parse")
            )
            == "counted"
        )

    def test_extraction_archive_read_stage_is_transient(self) -> None:
        # Filesystem-style failures are transient.
        assert (
            classify_failure(
                ExtractionError("cannot read", url="/tmp/x", stage="archive_read")
            )
            == "transient"
        )

    def test_unknown_exception_is_transient(self) -> None:
        # Conservative: never permanently terminate on something we don't recognise.
        assert classify_failure(InfluxError("generic")) == "transient"
        assert classify_failure(ValueError("oops")) == "transient"


# ── Parser ───────────────────────────────────────────────────────────


class TestParseRepairSection:
    """``parse_repair_section`` returns zero-defaults for missing sections
    and round-trips structured counters for present ones.
    """

    def test_no_section_returns_zero_counters(self) -> None:
        c = parse_repair_section("# Title\n\n## Summary\nbody\n")
        assert c == RepairCounters()
        assert c.tier2_attempts == 0
        assert c.tier3_attempts == 0
        assert c.tier2_last_stage == ""
        assert c.tier3_last_stage == ""

    def test_parses_known_keys(self) -> None:
        body = (
            "# Title\n\n## Repair\n"
            "- tier2_attempts: 1\n"
            '- tier2_last_stage: "http"\n'
            "- tier3_attempts: 3\n"
            '- tier3_last_stage: "validate"\n'
            '- tier3_last_error: "schema mismatch"\n\n'
            "## Profile Relevance\n"
        )
        c = parse_repair_section(body)
        assert c.tier2_attempts == 1
        assert c.tier2_last_stage == "http"
        assert c.tier3_attempts == 3
        assert c.tier3_last_stage == "validate"
        assert c.tier3_last_error == "schema mismatch"

    def test_partial_section_uses_defaults(self) -> None:
        body = "## Repair\n- tier3_attempts: 2\n"
        c = parse_repair_section(body)
        assert c.tier3_attempts == 2
        assert c.tier2_attempts == 0
        assert c.tier3_last_stage == ""

    def test_garbage_count_is_treated_as_zero(self) -> None:
        body = "## Repair\n- tier3_attempts: not-a-number\n"
        c = parse_repair_section(body)
        assert c.tier3_attempts == 0


# ── Serializer ───────────────────────────────────────────────────────


class TestRenderRepairSection:
    def test_renders_all_keys(self) -> None:
        rendered = render_repair_section(
            RepairCounters(
                tier2_attempts=1,
                tier2_last_stage="http",
                tier3_attempts=2,
                tier3_last_stage="validate",
                tier3_last_error="bad json",
            )
        )
        assert rendered.startswith("## Repair\n")
        assert "- tier2_attempts: 1" in rendered
        assert '- tier2_last_stage: "http"' in rendered
        assert "- tier3_attempts: 2" in rendered
        assert '- tier3_last_stage: "validate"' in rendered
        assert '- tier3_last_error: "bad json"' in rendered
        # Archive fields are always rendered, even when zero, so the
        # bullet schema stays stable across notes.
        assert "- archive_attempts: 0" in rendered
        assert '- archive_last_kind: ""' in rendered

    def test_renders_archive_fields(self) -> None:
        rendered = render_repair_section(
            RepairCounters(
                archive_attempts=2,
                archive_last_kind="oversize",
                archive_last_error="exceeds 100000000 bytes",
            )
        )
        assert "- archive_attempts: 2" in rendered
        assert '- archive_last_kind: "oversize"' in rendered
        assert '- archive_last_error: "exceeds 100000000 bytes"' in rendered

    def test_strips_newlines_in_last_error(self) -> None:
        rendered = render_repair_section(
            RepairCounters(tier3_last_error="line1\nline2\nline3")
        )
        # Multi-line errors must collapse to a single line so the markdown
        # bullet list parses round-trippably.
        repair_lines = [ln for ln in rendered.splitlines() if "tier3_last_error" in ln]
        assert len(repair_lines) == 1
        assert "\n" not in repair_lines[0]


# ── Upsert ───────────────────────────────────────────────────────────


class TestUpsertRepairSection:
    def test_inserts_when_absent(self) -> None:
        content = (
            "# Paper\n\n## Summary\nA paper.\n\n"
            "## Profile Relevance\n### r\nScore: 9/10\nReason\n\n"
            "## User Notes\n"
        )
        out = upsert_repair_section(
            content,
            RepairCounters(tier3_attempts=1, tier3_last_stage="validate"),
        )
        # Section appears before ## Profile Relevance.
        assert "## Repair\n" in out
        assert out.index("## Repair") < out.index("## Profile Relevance")
        assert "tier3_attempts: 1" in out

    def test_replaces_when_present(self) -> None:
        content = (
            "# Paper\n\n## Summary\nA.\n\n## Repair\n- tier3_attempts: 1\n\n"
            "## Profile Relevance\n### r\nScore: 9/10\nReason\n\n## User Notes\n"
        )
        out = upsert_repair_section(content, RepairCounters(tier3_attempts=2))
        assert out.count("## Repair\n") == 1
        # The old "1" must be gone.
        assert "tier3_attempts: 1" not in out
        assert "tier3_attempts: 2" in out

    def test_round_trip(self) -> None:
        original = "# Paper\n\n## Summary\nA.\n\n## User Notes\n"
        counters = RepairCounters(
            tier2_attempts=2,
            tier2_last_stage="parse",
            tier3_attempts=1,
            tier3_last_stage="validate",
            tier3_last_error="bad",
            archive_attempts=2,
            archive_last_kind="oversize",
            archive_last_error="too big",
        )
        roundtripped = parse_repair_section(upsert_repair_section(original, counters))
        assert roundtripped == counters

    def test_archive_fields_round_trip_through_parse(self) -> None:
        body = (
            "## Repair\n"
            "- tier2_attempts: 0\n"
            "- tier3_attempts: 0\n"
            "- archive_attempts: 2\n"
            '- archive_last_kind: "oversize"\n'
            '- archive_last_error: "exceeds 100000000 bytes"\n'
        )
        c = parse_repair_section(body)
        assert c.archive_attempts == 2
        assert c.archive_last_kind == "oversize"
        assert c.archive_last_error == "exceeds 100000000 bytes"


# ── Counter mutation ─────────────────────────────────────────────────


class TestRepairCountersBump:
    """``RepairCounters`` is immutable — the bump helper returns a new instance."""

    def test_bump_tier2_returns_new_instance_with_incremented_count(self) -> None:
        c = RepairCounters(tier2_attempts=1)
        c2 = c.bump_tier2(stage="parse", error="boom")
        assert c is not c2
        assert c.tier2_attempts == 1
        assert c2.tier2_attempts == 2
        assert c2.tier2_last_stage == "parse"
        assert c2.tier2_last_error == "boom"

    def test_bump_tier3_returns_new_instance_with_incremented_count(self) -> None:
        c = RepairCounters(tier3_attempts=2)
        c2 = c.bump_tier3(stage="validate", error="mismatch")
        assert c2.tier3_attempts == 3
        assert c2.tier3_last_stage == "validate"
        assert c2.tier3_last_error == "mismatch"

    def test_bump_archive_returns_new_instance_with_incremented_count(self) -> None:
        c = RepairCounters(archive_attempts=1)
        c2 = c.bump_archive(kind="oversize", error="exceeds 100000000 bytes")
        assert c is not c2
        assert c.archive_attempts == 1
        assert c2.archive_attempts == 2
        assert c2.archive_last_kind == "oversize"
        assert c2.archive_last_error.startswith("exceeds")


class TestClassifyArchiveFailure:
    """``classify_failure`` treats oversize archive failures as counted —
    a 200 MB PDF will not shrink on retry, so the cap should engage.
    """

    def test_extraction_oversize_stage_is_counted(self) -> None:
        assert (
            classify_failure(
                ExtractionError("too big", url="http://x", stage="oversize")
            )
            == "counted"
        )

    def test_extraction_archive_read_stage_is_transient(self) -> None:
        # archive_read covers transient filesystem / IO errors that may
        # heal on retry — must NOT bump the cap.
        assert (
            classify_failure(
                ExtractionError("io error", url="http://x", stage="archive_read")
            )
            == "transient"
        )

    def test_lithos_error_during_archive_is_transient(self) -> None:
        assert (
            classify_failure(
                LithosError("transport flake", operation="archive_download")
            )
            == "transient"
        )
