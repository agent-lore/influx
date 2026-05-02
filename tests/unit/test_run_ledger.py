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
    ledger = RunLedger(tmp_path / "state")
    ledger.record_unresolved_slug_collision(
        profile="staging-robotics",
        source="arxiv",
        source_url="https://arxiv.org/abs/2604.28197",
        title="OmniRobotHome",
        detail="existing_id=doc-A; existing_id=doc-B",
        run_id="run-xyz",
    )
    entries = ledger.unresolved_slug_collisions()
    assert len(entries) == 1
    e = entries[0]
    assert e["profile"] == "staging-robotics"
    assert e["source"] == "arxiv"
    assert e["source_url"] == "https://arxiv.org/abs/2604.28197"
    assert e["title"] == "OmniRobotHome"
    assert "doc-A" in e["detail"]
    assert e["run_id"] == "run-xyz"
    assert "T" in e["timestamp"] and e["timestamp"].endswith(("Z", "+00:00"))


def test_record_unresolved_slug_collision_is_append_only(tmp_path: Path) -> None:
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
    assert [e["title"] for e in entries] == ["paper-0", "paper-1", "paper-2"]


def test_skip_records_skipped_status_with_reason(tmp_path: Path) -> None:
    """``skip`` produces a ``skipped`` ledger entry with the reason captured."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(
        run_id="run-1",
        profile="staging-robotics",
        kind="scheduled",
        run_range=None,
    )
    ledger.skip(run_id="run-1", reason="lithos_unhealthy")

    entry = ledger.recent()[0]
    assert entry["status"] == "skipped"
    assert entry["error"] == "lithos_unhealthy"
    assert entry["degraded"] is False
    assert entry["sources_checked"] is None
    assert entry["ingested"] is None


# ── #36: ingestion-stall detection ──────────────────────────────────


def _start_complete(
    ledger: RunLedger,
    *,
    run_id: str,
    profile: str,
    kind: str = "scheduled",
    sources_checked: int | None,
    ingested: int | None,
    source_acquisition_errors: list[dict[str, str]] | None = None,
) -> list[str]:
    """Helper: start + complete a run, return the degraded_reasons list."""
    ledger.start(run_id=run_id, profile=profile, kind=kind, run_range=None)
    return ledger.complete(
        run_id=run_id,
        sources_checked=sources_checked,
        ingested=ingested,
        source_acquisition_errors=source_acquisition_errors,
    )


def test_complete_returns_empty_reasons_for_clean_run(tmp_path: Path) -> None:
    """A clean run has no degraded_reasons and degraded=False."""
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=5,
        ingested=3,
    )
    assert reasons == []
    entry = ledger.recent()[0]
    assert entry["degraded"] is False
    assert entry["degraded_reasons"] == []


def test_complete_returns_source_acquisition_reason(tmp_path: Path) -> None:
    """source_acquisition_errors → degraded_reasons=['source_acquisition']."""
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=5,
        ingested=2,
        source_acquisition_errors=[
            {"source": "arxiv", "kind": "timeout", "detail": "x"}
        ],
    )
    assert reasons == ["source_acquisition"]
    entry = ledger.recent()[0]
    assert entry["degraded"] is True
    assert entry["degraded_reasons"] == ["source_acquisition"]


def test_single_zero_ingestion_run_is_not_yet_a_stall(tmp_path: Path) -> None:
    """One zero-ingestion run alone doesn't trigger the stall flag.

    The signal must require TWO consecutive matching runs so a single
    quiet sweep doesn't generate noise.
    """
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=5,
        ingested=0,
    )
    assert reasons == []
    entry = ledger.recent()[0]
    assert entry["degraded"] is False


def test_two_consecutive_zero_ingestion_runs_flag_stall(tmp_path: Path) -> None:
    """Second consecutive zero-ingest scheduled run flips degraded=True."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-1", profile="p", sources_checked=5, ingested=0)
    reasons = _start_complete(
        ledger, run_id="r-2", profile="p", sources_checked=4, ingested=0
    )
    assert reasons == ["ingestion_stall"]
    entry = ledger.recent()[0]
    assert entry["degraded"] is True
    assert entry["degraded_reasons"] == ["ingestion_stall"]


def test_zero_sources_checked_does_not_count_as_stall(tmp_path: Path) -> None:
    """sources_checked=0 means a quiet window, not a stall — don't flag."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-1", profile="p", sources_checked=0, ingested=0)
    reasons = _start_complete(
        ledger, run_id="r-2", profile="p", sources_checked=0, ingested=0
    )
    assert reasons == []


def test_successful_ingest_resets_the_stall_streak(tmp_path: Path) -> None:
    """A run that ingested anything resets the streak."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-1", profile="p", sources_checked=5, ingested=0)
    _start_complete(ledger, run_id="r-2", profile="p", sources_checked=5, ingested=2)
    reasons = _start_complete(
        ledger, run_id="r-3", profile="p", sources_checked=5, ingested=0
    )
    # r-3 is the first zero-run after a successful one; not yet a stall.
    assert reasons == []


def test_stall_is_per_profile(tmp_path: Path) -> None:
    """Different profiles don't share a stall streak."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-1", profile="ai", sources_checked=5, ingested=0)
    # Different profile zero-runs in between don't count toward 'ai'.
    _start_complete(
        ledger,
        run_id="r-2",
        profile="robotics",
        sources_checked=5,
        ingested=0,
    )
    reasons = _start_complete(
        ledger, run_id="r-3", profile="ai", sources_checked=5, ingested=0
    )
    assert reasons == ["ingestion_stall"]


def test_backfill_kind_does_not_trigger_stall(tmp_path: Path) -> None:
    """Backfills legitimately ingest 0 (cache hits) — never flag stall."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        kind="backfill",
        sources_checked=10,
        ingested=0,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-2",
        profile="p",
        kind="backfill",
        sources_checked=10,
        ingested=0,
    )
    assert reasons == []


def test_combined_source_acquisition_and_stall(tmp_path: Path) -> None:
    """Both reasons can apply at once — both must appear in the list."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-1", profile="p", sources_checked=5, ingested=0)
    reasons = _start_complete(
        ledger,
        run_id="r-2",
        profile="p",
        sources_checked=4,
        ingested=0,
        source_acquisition_errors=[{"source": "arxiv", "kind": "x", "detail": "y"}],
    )
    assert reasons == ["source_acquisition", "ingestion_stall"]
