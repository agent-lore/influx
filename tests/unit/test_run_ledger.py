"""Unit tests for the local run ledger."""

from __future__ import annotations

from pathlib import Path

from influx.run_ledger import RunLedger


def test_run_ledger_records_completed_run(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "state")

    ledger.start(
        run_id="run-1",
        profile="ai-robotics",
        kind="manual",
        run_range={"days": 1},
    )
    ledger.complete(run_id="run-1", sources_checked=8, ingested=3)

    assert ledger.active_runs() == []
    recent = ledger.recent()
    assert len(recent) == 1
    assert recent[0]["run_id"] == "run-1"
    assert recent[0]["status"] == "completed"
    assert recent[0]["sources_checked"] == 8
    assert recent[0]["ingested"] == 3
    assert recent[0]["duration_seconds"] is not None


def test_run_ledger_records_failed_run(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "state")

    ledger.start(
        run_id="run-1",
        profile="ai-robotics",
        kind="scheduled",
        run_range=None,
    )
    ledger.fail(run_id="run-1", error="RuntimeError: boom")

    recent = ledger.recent()
    assert recent[0]["status"] == "failed"
    assert recent[0]["error"] == "RuntimeError: boom"
    assert ledger.active_runs() == []


def test_run_ledger_recent_is_newest_first_and_filterable(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "state")

    for run_id, profile in [
        ("run-1", "ai-robotics"),
        ("run-2", "web-tech"),
    ]:
        ledger.start(
            run_id=run_id,
            profile=profile,
            kind="manual",
            run_range=None,
        )
        ledger.complete(run_id=run_id, sources_checked=1, ingested=1)

    assert [entry["run_id"] for entry in ledger.recent()] == ["run-2", "run-1"]
    assert [entry["run_id"] for entry in ledger.recent(profile="web-tech")] == ["run-2"]
    assert ledger.last_by_profile()["web-tech"]["run_id"] == "run-2"


def test_run_ledger_abandons_stale_active_runs(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "state")
    ledger.start(
        run_id="run-1",
        profile="ai-robotics",
        kind="scheduled",
        run_range=None,
    )

    ledger.abandon_active(reason="process restarted")

    assert ledger.active_runs() == []
    recent = ledger.recent()
    assert recent[0]["run_id"] == "run-1"
    assert recent[0]["status"] == "abandoned"
    assert recent[0]["error"] == "process restarted"
