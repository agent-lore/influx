"""Unit tests for the promotion gate (issue #165).

The gate consumes ledger-shaped entries and produces a pass/fail
verdict for a staging-promotion script.  Tests pin:

* Filtering: only ``status="completed"`` AND ``kind="scheduled"``
  runs count.
* The three failure modes — ``insufficient_runs``,
  ``unexpected_failure_present``, ``expected_lossy_above_threshold``.
* The success mode (``"ok"``) and that the ratio threshold is
  inclusive (a ratio exactly equal to the configured max passes).
* The top-driver / unexpected-failure summary surfaces.
* Backward-compat: entries written before #164 land in the ledger
  without ``degradation_severity``; the gate falls back to
  recomputing from ``degraded_reasons``.
"""

from __future__ import annotations

from typing import Any

from influx.promotion_gate import (
    PromotionGateConfig,
    evaluate_promotion_gate,
    format_gate_summary,
)


def _entry(
    *,
    run_id: str,
    severity: str | None = "success",
    reasons: list[str] | None = None,
    profile: str = "alpha",
    kind: str = "scheduled",
    status: str = "completed",
) -> dict[str, Any]:
    """Build a ledger-shaped entry suitable for the gate."""
    entry: dict[str, Any] = {
        "run_id": run_id,
        "profile": profile,
        "kind": kind,
        "status": status,
        "degraded_reasons": list(reasons or []),
    }
    # ``severity=None`` means "simulate a legacy entry written before
    # issue #164 landed" — the gate must fall back to recomputing.
    if severity is not None:
        entry["degradation_severity"] = severity
    return entry


# ── Filtering ─────────────────────────────────────────────────────────


class TestEntryFiltering:
    """The gate only inspects scheduled completed runs."""

    def test_skipped_and_failed_entries_are_ignored(self) -> None:
        entries = [
            _entry(run_id="ok-1"),
            _entry(run_id="ok-2"),
            _entry(run_id="ok-3"),
            _entry(run_id="ok-4"),
            _entry(run_id="ok-5"),
            _entry(run_id="abandoned", status="abandoned"),
            _entry(run_id="failed", status="failed"),
            _entry(run_id="skipped", status="skipped"),
        ]
        result = evaluate_promotion_gate(
            entries, PromotionGateConfig(min_runs_required=1)
        )
        assert result.runs_evaluated == 5
        assert result.passed is True
        assert result.reason == "ok"

    def test_manual_and_backfill_runs_are_ignored(self) -> None:
        entries = [
            _entry(run_id="manual", kind="manual"),
            _entry(run_id="backfill", kind="backfill"),
        ] + [_entry(run_id=f"s-{i}") for i in range(5)]
        result = evaluate_promotion_gate(
            entries, PromotionGateConfig(min_runs_required=1)
        )
        # Only the 5 scheduled runs are inspected.
        assert result.runs_evaluated == 5

    def test_window_clamps_inspected_runs(self) -> None:
        entries = [_entry(run_id=f"r-{i}") for i in range(30)]
        result = evaluate_promotion_gate(
            entries, PromotionGateConfig(window_runs=10, min_runs_required=1)
        )
        assert result.runs_evaluated == 10


# ── Pass / fail outcomes ──────────────────────────────────────────────


class TestPromotionGateOutcomes:
    """The three failure modes plus the success mode."""

    def test_insufficient_runs_fails_gate(self) -> None:
        entries = [_entry(run_id="r-1"), _entry(run_id="r-2")]
        result = evaluate_promotion_gate(
            entries, PromotionGateConfig(min_runs_required=5)
        )
        assert result.passed is False
        assert result.reason == "insufficient_runs"
        assert result.runs_evaluated == 2

    def test_all_success_passes(self) -> None:
        entries = [_entry(run_id=f"r-{i}") for i in range(5)]
        result = evaluate_promotion_gate(
            entries, PromotionGateConfig(min_runs_required=5)
        )
        assert result.passed is True
        assert result.reason == "ok"

    def test_single_unexpected_failure_fails_gate(self) -> None:
        # Even one unexpected failure in the window is an immediate
        # fail — the acceptance criterion says: "gate fails on
        # unexpected-failure even when the raw degraded ratio is low".
        entries = [
            _entry(
                run_id="bad",
                severity="unexpected_failure",
                reasons=["invalid_note_state"],
            ),
        ] + [_entry(run_id=f"ok-{i}") for i in range(10)]
        result = evaluate_promotion_gate(entries, PromotionGateConfig())
        assert result.passed is False
        assert result.reason == "unexpected_failure_present"
        assert len(result.unexpected_failure_runs) == 1
        assert result.unexpected_failure_runs[0]["run_id"] == "bad"

    def test_unexpected_failure_beats_high_lossy_ratio_in_reason(self) -> None:
        # If both conditions would trip, ``unexpected_failure_present``
        # wins because it's the more actionable signal.
        entries = [
            _entry(
                run_id="bad",
                severity="unexpected_failure",
                reasons=["invalid_note_state"],
            ),
        ] + [
            _entry(
                run_id=f"lossy-{i}",
                severity="expected_lossy",
                reasons=["archive_acquisition"],
            )
            for i in range(9)
        ]
        result = evaluate_promotion_gate(entries, PromotionGateConfig())
        assert result.reason == "unexpected_failure_present"

    def test_expected_lossy_under_threshold_passes(self) -> None:
        # 4 lossy + 6 success = 0.4 ratio; threshold is 0.5 → passes.
        entries = [
            _entry(
                run_id=f"lossy-{i}",
                severity="expected_lossy",
                reasons=["archive_acquisition"],
            )
            for i in range(4)
        ] + [_entry(run_id=f"ok-{i}") for i in range(6)]
        result = evaluate_promotion_gate(
            entries, PromotionGateConfig(max_expected_lossy_ratio=0.5)
        )
        assert result.passed is True
        assert result.reason == "ok"
        assert result.expected_lossy_ratio == 0.4

    def test_ratio_exactly_at_threshold_passes(self) -> None:
        # 5 lossy + 5 success = exactly 0.5 → inclusive at the
        # boundary so an operator setting ``max=0.5`` does not see
        # gate flap from a 50/50 split.
        entries = [
            _entry(
                run_id=f"lossy-{i}",
                severity="expected_lossy",
                reasons=["archive_acquisition"],
            )
            for i in range(5)
        ] + [_entry(run_id=f"ok-{i}") for i in range(5)]
        result = evaluate_promotion_gate(
            entries, PromotionGateConfig(max_expected_lossy_ratio=0.5)
        )
        assert result.passed is True

    def test_expected_lossy_above_threshold_fails_gate(self) -> None:
        # 7 lossy + 3 success = 0.7 ratio; threshold is 0.5 → fail.
        entries = [
            _entry(
                run_id=f"lossy-{i}",
                severity="expected_lossy",
                reasons=["archive_acquisition"],
            )
            for i in range(7)
        ] + [_entry(run_id=f"ok-{i}") for i in range(3)]
        result = evaluate_promotion_gate(
            entries, PromotionGateConfig(max_expected_lossy_ratio=0.5)
        )
        assert result.passed is False
        assert result.reason == "expected_lossy_above_threshold"
        assert result.expected_lossy_ratio == 0.7


# ── Severity fallback ─────────────────────────────────────────────────


class TestLegacyEntryFallback:
    """Entries written before #164 lack ``degradation_severity``."""

    def test_legacy_entry_uses_classify_from_reasons(self) -> None:
        # Legacy entry: no degradation_severity field, but reasons
        # list contains an unexpected-failure reason — gate must
        # still classify it as unexpected_failure and hard-fail.
        entries = [
            _entry(
                run_id="legacy",
                severity=None,  # simulate missing field
                reasons=["invalid_note_state"],
            ),
        ] + [_entry(run_id=f"ok-{i}") for i in range(5)]
        result = evaluate_promotion_gate(entries, PromotionGateConfig())
        assert result.reason == "unexpected_failure_present"

    def test_legacy_clean_entry_classified_as_success(self) -> None:
        entries = [
            _entry(run_id="legacy", severity=None, reasons=[]),
        ] + [_entry(run_id=f"ok-{i}") for i in range(5)]
        result = evaluate_promotion_gate(entries, PromotionGateConfig())
        assert result.passed is True
        assert result.severity_counts["success"] == 6


# ── Summary rendering ─────────────────────────────────────────────────


class TestFormatGateSummary:
    """The CLI summary surfaces the verdict + supporting numbers."""

    def test_pass_summary_lists_verdict_and_counts(self) -> None:
        entries = [_entry(run_id=f"r-{i}") for i in range(5)]
        result = evaluate_promotion_gate(
            entries, PromotionGateConfig(min_runs_required=1)
        )
        text = format_gate_summary(result, PromotionGateConfig(min_runs_required=1))
        assert "PASS" in text
        assert "reason          : ok" in text
        assert "runs_evaluated  : 5" in text
        assert "success=5" in text

    def test_fail_summary_includes_offending_run_ids(self) -> None:
        entries = [
            _entry(
                run_id="bad-run-1",
                severity="unexpected_failure",
                reasons=["invalid_note_state", "archive_acquisition"],
                profile="ai-agents",
            ),
        ] + [_entry(run_id=f"ok-{i}") for i in range(5)]
        result = evaluate_promotion_gate(entries, PromotionGateConfig())
        text = format_gate_summary(result, PromotionGateConfig())
        assert "FAIL" in text
        assert "unexpected_failure_present" in text
        # The offending run is listed by id + profile + reasons.
        assert "bad-run-1" in text
        assert "ai-agents" in text
        assert "invalid_note_state" in text

    def test_top_reasons_block_appears(self) -> None:
        entries = [
            _entry(
                run_id=f"r-{i}",
                severity="expected_lossy",
                reasons=["archive_acquisition", "source_acquisition"],
            )
            for i in range(5)
        ]
        result = evaluate_promotion_gate(entries, PromotionGateConfig())
        text = format_gate_summary(result, PromotionGateConfig())
        assert "top reasons" in text
        assert "archive_acquisition=5" in text
        assert "source_acquisition=5" in text

    def test_empty_window_summary_is_safe(self) -> None:
        result = evaluate_promotion_gate([], PromotionGateConfig(min_runs_required=5))
        text = format_gate_summary(result, PromotionGateConfig())
        assert "FAIL" in text
        assert "insufficient_runs" in text
        assert "runs_evaluated  : 0" in text
