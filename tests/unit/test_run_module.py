"""Unit tests for the Run module (issue #58).

Stripped to a minimal smoke set while CI segfault is being diagnosed.
"""

from __future__ import annotations

from influx.run import HealthAction, RunAborted, StageDiagnostics


def test_health_action_construction() -> None:
    action = HealthAction(op="flip", latch="repair_write_failure", detail="x")
    assert action.op == "flip"
    assert action.latch == "repair_write_failure"
    assert action.detail == "x"


def test_stage_diagnostics_empty() -> None:
    d = StageDiagnostics()
    assert d.degraded_reasons == ()
    assert d.health_actions == ()


def test_run_aborted_carries_diagnostics() -> None:
    d = StageDiagnostics(
        health_actions=(HealthAction(op="flip", latch="repair_write_failure"),),
        degraded_reasons=("source_acquisition",),
    )
    exc = RunAborted("repair_write_failure", d)
    assert exc.reason == "repair_write_failure"
    assert exc.diagnostics is d
