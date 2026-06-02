"""Regression: inbox (RunKind.INBOX) runs are excluded from the scheduled-only
stall heuristics (Inbox v1 slice 4, §11.3).

An inbox Run that filters everything out (ingested==0, sources_checked>0) is a
low-score candidate, not an ``ingestion_stall``; and a prior inbox run must not
count toward a later scheduled run's consecutive-stall ratchet.  The behaviour
is enforced by ``run_ledger`` gating on ``kind == "scheduled"`` — these tests
lock it in (no code change in this slice).
"""

from __future__ import annotations

from pathlib import Path

from influx.run_ledger import RunLedger


def _run(
    ledger: RunLedger,
    *,
    run_id: str,
    kind: str,
    profile: str = "p",
    sources_checked: int = 1,
    ingested: int = 0,
) -> list[str]:
    ledger.start(run_id=run_id, profile=profile, kind=kind, run_range=None)
    return ledger.complete(
        run_id=run_id,
        sources_checked=sources_checked,
        ingested=ingested,
        fetched_total=sources_checked,
    )


def test_inbox_zero_ingestion_run_is_not_ingestion_stall(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "state")
    # Two consecutive zero-ingestion inbox runs — would be a stall if scheduled.
    _run(ledger, run_id="i-1", kind="inbox")
    reasons = _run(ledger, run_id="i-2", kind="inbox")
    assert "ingestion_stall" not in reasons


def test_prior_inbox_run_does_not_feed_scheduled_stall_walk(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path / "state")
    # A prior zero-ingestion inbox run must not count toward the scheduled
    # run's consecutive ratchet, so the single scheduled run does not stall.
    _run(ledger, run_id="i-1", kind="inbox")
    reasons = _run(ledger, run_id="s-1", kind="scheduled")
    assert "ingestion_stall" not in reasons


def test_two_scheduled_zero_runs_do_stall_baseline(tmp_path: Path) -> None:
    """Baseline: the heuristic still fires for genuine scheduled stalls."""
    ledger = RunLedger(tmp_path / "state")
    _run(ledger, run_id="s-1", kind="scheduled")
    reasons = _run(ledger, run_id="s-2", kind="scheduled")
    assert "ingestion_stall" in reasons
