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


# ── Issue #20: surface swallowed source-acquisition failures ────────


def test_complete_without_source_errors_marks_run_not_degraded(
    tmp_path: Path,
) -> None:
    """A clean run carries ``degraded=false`` and an empty error list so
    dashboards can grep on the field reliably (issue #20)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(
        run_id="run-1",
        profile="ai-robotics",
        kind="scheduled",
        run_range=None,
    )
    ledger.complete(run_id="run-1", sources_checked=10, ingested=4)

    entry = ledger.recent()[0]
    assert entry["degraded"] is False
    assert entry["source_acquisition_errors"] == []


def test_complete_with_source_errors_marks_run_degraded(tmp_path: Path) -> None:
    """A run that swallowed a fetch failure surfaces as ``degraded=true``
    with the structured error list preserved verbatim (issue #20).

    Distinguishes a partial-failure run from a quiet window in which the
    source legitimately had no items — both used to land as
    ``sources_checked=0, error=null``.
    """
    ledger = RunLedger(tmp_path / "state")
    ledger.start(
        run_id="run-1",
        profile="ai-robotics",
        kind="scheduled",
        run_range=None,
    )
    errors = [
        {
            "source": "arxiv",
            "kind": "http",
            "detail": "HTTP 500 from arXiv API",
        }
    ]
    ledger.complete(
        run_id="run-1",
        sources_checked=0,
        ingested=0,
        source_acquisition_errors=errors,
    )

    entry = ledger.recent()[0]
    assert entry["status"] == "completed"
    assert entry["degraded"] is True
    assert entry["source_acquisition_errors"] == errors


def test_failed_run_has_degraded_false(tmp_path: Path) -> None:
    """``fail`` always lands ``degraded=false`` so the field's semantics
    stay narrow: it means *partial source-fetch failure on an otherwise
    completed run*, not "anything went wrong".
    """
    ledger = RunLedger(tmp_path / "state")
    ledger.start(
        run_id="run-1",
        profile="ai-robotics",
        kind="scheduled",
        run_range=None,
    )
    ledger.fail(run_id="run-1", error="RuntimeError: boom")

    entry = ledger.recent()[0]
    assert entry["degraded"] is False
    assert entry["source_acquisition_errors"] == []


def test_unresolved_slug_collisions_starts_empty(tmp_path: Path) -> None:
    """Backlog read returns ``[]`` when the file does not yet exist."""
    ledger = RunLedger(tmp_path / "state")
    assert ledger.unresolved_slug_collisions() == []


def test_record_unresolved_slug_collision_appends_entry(tmp_path: Path) -> None:
    """One ``record_unresolved_slug_collision`` writes one structured line."""
    ledger = RunLedger(tmp_path / "state")
    ledger.record_unresolved_slug_collision(
        profile="staging-robotics",
        source="arxiv",
        source_url="https://arxiv.org/abs/2604.28197",
        title="OmniRobotHome",
        detail="attempt=1 existing_id=doc-A; attempt=2 existing_id=doc-B",
        run_id="run-xyz",
    )
    entries = ledger.unresolved_slug_collisions()
    assert len(entries) == 1
    e = entries[0]
    assert e["profile"] == "staging-robotics"
    assert e["source"] == "arxiv"
    assert e["source_url"] == "https://arxiv.org/abs/2604.28197"
    assert e["title"] == "OmniRobotHome"
    assert "doc-A" in e["detail"] and "doc-B" in e["detail"]
    assert e["run_id"] == "run-xyz"
    # Timestamp present and ISO-formatted.
    assert "T" in e["timestamp"] and e["timestamp"].endswith(("Z", "+00:00"))


def test_record_unresolved_slug_collision_is_append_only(tmp_path: Path) -> None:
    """Multiple records accumulate; existing entries are not rewritten."""
    ledger = RunLedger(tmp_path / "state")
    for i in range(3):
        ledger.record_unresolved_slug_collision(
            profile="p",
            source="arxiv",
            source_url=f"https://arxiv.org/abs/2601.0000{i}",
            title=f"paper-{i}",
            detail=f"squatter-{i}",
            run_id=f"run-{i}",
        )
    entries = ledger.unresolved_slug_collisions()
    assert len(entries) == 3
    assert [e["title"] for e in entries] == ["paper-0", "paper-1", "paper-2"]
