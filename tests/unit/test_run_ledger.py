"""Unit tests for the local run ledger."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


# ── Issue #129: surface recovered source-fetch retries ──────────────


def test_complete_without_retry_counts_persists_empty_dict(
    tmp_path: Path,
) -> None:
    """A clean run with no retry-count input lands ``source_retry_counts={}``
    so downstream consumers always see the field, never KeyError.
    """
    ledger = RunLedger(tmp_path / "state")
    ledger.start(
        run_id="run-1",
        profile="ai-robotics",
        kind="scheduled",
        run_range=None,
    )
    ledger.complete(run_id="run-1", sources_checked=3, ingested=2)
    entry = ledger.recent()[0]
    assert entry["source_retry_counts"] == {}


def test_complete_records_source_retry_counts(tmp_path: Path) -> None:
    """``source_retry_counts`` survives the ledger round-trip and is
    deep-copied so post-complete mutations of the caller's dict do not
    leak into persisted entries (issue #129).
    """
    ledger = RunLedger(tmp_path / "state")
    ledger.start(
        run_id="run-1",
        profile="ai-robotics",
        kind="scheduled",
        run_range=None,
    )
    counts = {"arxiv": {"rate_limit": 2, "timeout": 1}}
    ledger.complete(
        run_id="run-1",
        sources_checked=8,
        ingested=3,
        source_retry_counts=counts,
    )
    entry = ledger.recent()[0]
    assert entry["source_retry_counts"] == {
        "arxiv": {"rate_limit": 2, "timeout": 1},
    }
    # Run that recovered transient retries is **not** degraded — only
    # *swallowed* errors land in the degraded set, retries that
    # succeeded are pure diagnostics.
    assert entry["degraded"] is False
    assert entry["degraded_reasons"] == []

    # Mutating the original dict after ``complete`` must not change
    # what the ledger persisted (deep-copy invariant).
    counts["arxiv"]["rate_limit"] = 99
    counts["rss"] = {"timeout": 7}
    re_read = ledger.recent()[0]
    assert re_read["source_retry_counts"] == {
        "arxiv": {"rate_limit": 2, "timeout": 1},
    }


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
    fetched_total: int | None = None,
    filter_errors_total: int | None = None,
    invalid_url_rejections_total: int | None = None,
    archive_failures_total: int | None = None,
    source_acquisition_errors: list[dict[str, str]] | None = None,
) -> list[str]:
    """Helper: start + complete a run, return the degraded_reasons list.

    ``fetched_total`` defaults to mirror ``sources_checked`` when not
    supplied, preserving the legacy semantics of existing tests written
    before the #85 split (where ``sources_checked == 0`` implied
    ``fetched_total == 0``).  Tests that exercise the new
    ``filter_stall`` / ``filter_error`` discrimination supply both
    values explicitly.

    ``filter_errors_total`` defaults to ``None`` (no scorer failures).
    ``invalid_url_rejections_total`` defaults to ``None`` (no
    pre-acquire URL rejections — issue #131).
    """
    if fetched_total is None:
        fetched_total = sources_checked if isinstance(sources_checked, int) else 0
    ledger.start(run_id=run_id, profile=profile, kind=kind, run_range=None)
    return ledger.complete(
        run_id=run_id,
        sources_checked=sources_checked,
        ingested=ingested,
        fetched_total=fetched_total,
        filter_errors_total=filter_errors_total,
        invalid_url_rejections_total=invalid_url_rejections_total,
        archive_failures_total=archive_failures_total,
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


def test_complete_returns_archive_acquisition_reason(tmp_path: Path) -> None:
    """Accepted archive failures mark the run degraded for note-quality loss."""
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=5,
        ingested=2,
        archive_failures_total=2,
    )
    assert reasons == ["archive_acquisition"]
    entry = ledger.recent()[0]
    assert entry["degraded"] is True
    assert entry["degraded_reasons"] == ["archive_acquisition"]
    assert entry["archive_failures_total"] == 2


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


# ── #50: fetch-stall detection (zero sources_checked) ───────────────


def test_two_consecutive_zero_fetch_runs_after_history_flag_fetch_stall(
    tmp_path: Path,
) -> None:
    """Two consecutive zero-fetch runs after non-zero history → fetch_stall.

    The historical-ratchet means the profile has previously seen
    ``sources_checked > 0``, so the silence is degenerate, not just a
    brand-new profile that hasn't started fetching yet.
    """
    ledger = RunLedger(tmp_path / "state")
    # Seed history: a normal run that did inspect items.
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    # Two consecutive zero-fetch sweeps.
    _start_complete(ledger, run_id="r-1", profile="p", sources_checked=0, ingested=0)
    reasons = _start_complete(
        ledger, run_id="r-2", profile="p", sources_checked=0, ingested=0
    )
    assert reasons == ["fetch_stall"]
    entry = ledger.recent()[0]
    assert entry["degraded"] is True
    assert entry["degraded_reasons"] == ["fetch_stall"]


def test_two_consecutive_zero_fetch_with_no_history_does_not_flag(
    tmp_path: Path,
) -> None:
    """Brand-new profile with no prior non-zero sources_checked → no flag.

    The ratchet exists precisely to silence this case: if the profile
    has never had a non-zero fetch in the recent ledger window, we
    can't distinguish "broken lookback" from "new profile coming online".
    """
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-1", profile="p", sources_checked=0, ingested=0)
    reasons = _start_complete(
        ledger, run_id="r-2", profile="p", sources_checked=0, ingested=0
    )
    assert reasons == []
    entry = ledger.recent()[0]
    assert entry["degraded"] is False
    assert entry["degraded_reasons"] == []


def test_fetch_stall_and_ingestion_stall_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    """A single run can't be both — different sources_checked conditions.

    ``ingestion_stall`` requires ``sources_checked > 0`` while
    ``fetch_stall`` requires ``sources_checked == 0``; mutually
    exclusive by construction.
    """
    ledger = RunLedger(tmp_path / "state")
    # Seed history so fetch_stall ratchet would be satisfied.
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    # Two consecutive zero-fetch runs → fetch_stall, not ingestion_stall.
    _start_complete(ledger, run_id="r-1", profile="p", sources_checked=0, ingested=0)
    reasons_fetch = _start_complete(
        ledger, run_id="r-2", profile="p", sources_checked=0, ingested=0
    )
    assert "fetch_stall" in reasons_fetch
    assert "ingestion_stall" not in reasons_fetch

    # And vice versa: two consecutive zero-ingest (with sources_checked > 0)
    # runs → ingestion_stall, not fetch_stall.
    _start_complete(ledger, run_id="r-3", profile="q", sources_checked=5, ingested=0)
    reasons_ingest = _start_complete(
        ledger, run_id="r-4", profile="q", sources_checked=4, ingested=0
    )
    assert reasons_ingest == ["ingestion_stall"]
    assert "fetch_stall" not in reasons_ingest


def test_single_zero_fetch_run_is_not_yet_a_fetch_stall(tmp_path: Path) -> None:
    """One zero-fetch run alone (even with history) doesn't flag — needs two."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    reasons = _start_complete(
        ledger, run_id="r-1", profile="p", sources_checked=0, ingested=0
    )
    assert reasons == []


def test_fetch_stall_streak_resets_on_non_zero_fetch(tmp_path: Path) -> None:
    """A run that fetched anything resets the fetch-stall streak."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    _start_complete(ledger, run_id="r-1", profile="p", sources_checked=0, ingested=0)
    _start_complete(ledger, run_id="r-2", profile="p", sources_checked=3, ingested=1)
    reasons = _start_complete(
        ledger, run_id="r-3", profile="p", sources_checked=0, ingested=0
    )
    # r-3 is the first zero-fetch after a non-zero one; not yet a stall.
    assert reasons == []


def test_fetch_stall_does_not_apply_to_backfills(tmp_path: Path) -> None:
    """Backfills are excluded from fetch_stall (mirrors ingestion_stall)."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        kind="backfill",
        sources_checked=0,
        ingested=0,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-2",
        profile="p",
        kind="backfill",
        sources_checked=0,
        ingested=0,
    )
    assert reasons == []


# ── #85: filter-stall detection (fetched > 0, sources_checked = 0) ──


def test_two_consecutive_filter_stall_runs_after_history_flag_filter_stall(
    tmp_path: Path,
) -> None:
    """Two consecutive (fetched>0, sources_checked=0) runs after non-zero
    history → ``filter_stall``.

    This is the staging-robotics 2026-05-04 12:00:30 shape: sources
    fetched normally (10 arXiv + 51 RSS = 61 fetched_total) but the
    LLM filter rejected every candidate, so ``sources_checked``
    landed at 0.
    """
    ledger = RunLedger(tmp_path / "state")
    # Seed history: a normal run that did inspect items.
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    # Two consecutive filter-stall sweeps: fetched anything, inspected
    # nothing.
    _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-2",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=61,
    )
    assert reasons == ["filter_stall"]
    entry = ledger.recent()[0]
    assert entry["degraded"] is True
    assert entry["degraded_reasons"] == ["filter_stall"]
    assert entry["fetched_total"] == 61


def test_staging_robotics_2026_05_04_shape_emits_filter_stall(tmp_path: Path) -> None:
    """The exact 2026-05-04 12:00:30 staging-robotics shape.

    arXiv returned 10 items + RSS returned 51 items, so
    ``fetched_total == 61``.  The LLM filter rejected all of them, so
    ``sources_checked == 0``.  After a prior matching scheduled run
    + non-zero history, the second run trips ``filter_stall`` (not
    ``fetch_stall``, which would mis-route the operator to fetch-side
    causes).
    """
    ledger = RunLedger(tmp_path / "state")
    _start_complete(
        ledger,
        run_id="r-history",
        profile="staging-robotics",
        sources_checked=8,
        ingested=3,
    )
    _start_complete(
        ledger,
        run_id="r-prior",
        profile="staging-robotics",
        sources_checked=0,
        ingested=0,
        fetched_total=42,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-2026-05-04T12-00-30",
        profile="staging-robotics",
        sources_checked=0,
        ingested=0,
        fetched_total=61,  # 10 arxiv + 51 rss
    )
    assert reasons == ["filter_stall"]
    assert "fetch_stall" not in reasons


def test_filter_stall_does_not_fire_when_fetched_total_is_zero(
    tmp_path: Path,
) -> None:
    """``fetched_total == 0`` is the ``fetch_stall`` shape, not filter_stall.

    Mutual exclusion: when no candidate items reached the filter,
    blame the fetch path, not the filter.
    """
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=0,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-2",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=0,
    )
    assert reasons == ["fetch_stall"]
    assert "filter_stall" not in reasons


def test_filter_stall_and_fetch_stall_are_mutually_exclusive_on_one_run(
    tmp_path: Path,
) -> None:
    """A single run cannot emit both ``filter_stall`` and ``fetch_stall``.

    The two reasons key off of disjoint ``fetched_total`` values:
    ``fetch_stall`` requires ``fetched_total == 0`` while
    ``filter_stall`` requires ``fetched_total > 0``.
    """
    ledger = RunLedger(tmp_path / "state")
    # Seed history so both ratchets would otherwise be satisfied.
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    # Prior zero-sources run with fetched > 0 (filter-stall shape).
    _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-2",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    assert reasons == ["filter_stall"]
    assert "fetch_stall" not in reasons
    assert "ingestion_stall" not in reasons


def test_three_way_stall_discrimination_matrix(tmp_path: Path) -> None:
    """Verify the three-way (fetched, sources_checked, ingested) discrimination.

    Establishes the truth table from the issue:

    | fetched_total | sources_checked | ingested | expected reason   |
    |---------------|-----------------|----------|-------------------|
    | 0             | 0               | 0        | fetch_stall       |
    | > 0           | 0               | 0        | filter_stall      |
    | > 0           | > 0             | 0        | ingestion_stall   |

    Each profile gets its own history seed so the per-profile streak
    calculation stays clean.
    """
    ledger = RunLedger(tmp_path / "state")

    # Profile A → fetch_stall path.
    _start_complete(ledger, run_id="a-0", profile="a", sources_checked=5, ingested=2)
    _start_complete(
        ledger,
        run_id="a-1",
        profile="a",
        sources_checked=0,
        ingested=0,
        fetched_total=0,
    )
    reasons_a = _start_complete(
        ledger,
        run_id="a-2",
        profile="a",
        sources_checked=0,
        ingested=0,
        fetched_total=0,
    )
    assert reasons_a == ["fetch_stall"]

    # Profile B → filter_stall path.
    _start_complete(ledger, run_id="b-0", profile="b", sources_checked=5, ingested=2)
    _start_complete(
        ledger,
        run_id="b-1",
        profile="b",
        sources_checked=0,
        ingested=0,
        fetched_total=8,
    )
    reasons_b = _start_complete(
        ledger,
        run_id="b-2",
        profile="b",
        sources_checked=0,
        ingested=0,
        fetched_total=12,
    )
    assert reasons_b == ["filter_stall"]

    # Profile C → ingestion_stall path.
    _start_complete(
        ledger,
        run_id="c-1",
        profile="c",
        sources_checked=5,
        ingested=0,
        fetched_total=5,
    )
    reasons_c = _start_complete(
        ledger,
        run_id="c-2",
        profile="c",
        sources_checked=4,
        ingested=0,
        fetched_total=4,
    )
    assert reasons_c == ["ingestion_stall"]


def test_filter_stall_requires_two_consecutive_runs(tmp_path: Path) -> None:
    """A single filter-stall-shaped run alone doesn't flag — needs two."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    assert reasons == []


def test_filter_stall_streak_resets_on_non_zero_sources_checked(
    tmp_path: Path,
) -> None:
    """A run that inspected anything resets the filter-stall streak."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    _start_complete(ledger, run_id="r-2", profile="p", sources_checked=3, ingested=1)
    reasons = _start_complete(
        ledger,
        run_id="r-3",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    # r-3 is the first filter-stall-shaped run after a normal one;
    # streak == 1, not yet a stall.
    assert reasons == []


def test_filter_stall_does_not_apply_to_backfills(tmp_path: Path) -> None:
    """Backfills are excluded from filter_stall (mirrors fetch_stall)."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(ledger, run_id="r-0", profile="p", sources_checked=5, ingested=2)
    _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        kind="backfill",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-2",
        profile="p",
        kind="backfill",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    assert reasons == []


def test_filter_stall_brand_new_profile_no_history_does_not_flag(
    tmp_path: Path,
) -> None:
    """Brand-new profile with no prior non-zero sources_checked → no flag.

    Mirrors the historical-ratchet for fetch_stall.  Without prior
    evidence that the profile *can* reach the inspection loop, two
    consecutive zero-checked runs may just be a profile coming online
    with mismatched filter prompt — not a regression worth alerting
    on.
    """
    ledger = RunLedger(tmp_path / "state")
    _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-2",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    assert reasons == []
    entry = ledger.recent()[0]
    assert entry["degraded"] is False
    assert entry["degraded_reasons"] == []


def test_complete_persists_fetched_total(tmp_path: Path) -> None:
    """The ledger entry round-trips ``fetched_total`` for the diagnose path."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=4,
        ingested=2,
        fetched_total=20,
    )
    entry = ledger.recent()[0]
    assert entry["fetched_total"] == 20


def test_complete_default_fetched_total_when_omitted(tmp_path: Path) -> None:
    """Calling ``complete`` without ``fetched_total`` defaults to ``None``.

    Backward-compatibility for any external caller that hasn't been
    updated yet — the legacy ``complete`` signature must keep working.
    """
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    reasons = ledger.complete(run_id="r-1", sources_checked=4, ingested=2)
    assert reasons == []
    entry = ledger.recent()[0]
    # ``fetched_total`` is recorded on every entry; absent when caller
    # didn't supply one.
    assert entry.get("fetched_total") is None


# ── #85 review: filter_error detection (scorer execution failure) ───


def test_filter_error_fires_on_single_run_when_filter_errors_total_positive(
    tmp_path: Path,
) -> None:
    """A single FilterScorerError raises ``filter_error`` immediately.

    Single-run signal — no consecutive-runs gate.  An ``ingested > 0``
    case still fires because the scorer failed at least once even
    if other batches/feeds succeeded (partial-failure visibility).
    """
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=5,
        ingested=3,
        fetched_total=10,
        filter_errors_total=1,
    )
    assert reasons == ["filter_error"]
    entry = ledger.recent()[0]
    assert entry["degraded"] is True
    assert entry["degraded_reasons"] == ["filter_error"]
    assert entry["filter_errors_total"] == 1


def test_filter_error_takes_precedence_over_filter_stall(tmp_path: Path) -> None:
    """When both would apply, ``filter_error`` wins (mutual exclusion).

    Reviewer concern from PR for #85: a scorer transport/parse/provider
    failure leaves ``fetched_total > 0`` and ``sources_checked == 0``,
    which used to misclassify as ``filter_stall`` and point operators
    at the profile description / prompt.  After this fix the more-
    specific ``filter_error`` is emitted instead.
    """
    ledger = RunLedger(tmp_path / "state")
    # Build a history shape that would otherwise satisfy filter_stall:
    # prior non-zero history + a zero-checked-with-fetch run.
    _start_complete(
        ledger,
        run_id="r-history",
        profile="p",
        sources_checked=10,
        ingested=5,
        fetched_total=10,
    )
    _start_complete(
        ledger,
        run_id="r-prior",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )
    # Now the run with a scorer error.  filter_stall conditions are met
    # (two consecutive sources_checked=0 with fetched_total>0 + history)
    # but filter_errors_total > 0 must redirect to filter_error.
    reasons = _start_complete(
        ledger,
        run_id="r-now",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
        filter_errors_total=2,
    )
    assert "filter_error" in reasons
    assert "filter_stall" not in reasons


def test_filter_stall_still_fires_when_filter_errors_total_is_zero(
    tmp_path: Path,
) -> None:
    """Clean filter rejection (no scorer errors) still produces filter_stall.

    Guard against over-correcting: filter_stall is a real signal when
    the scorer ran successfully but rejected everything.  filter_error
    must not eclipse it.
    """
    ledger = RunLedger(tmp_path / "state")
    _start_complete(
        ledger,
        run_id="r-history",
        profile="p",
        sources_checked=10,
        ingested=5,
        fetched_total=10,
    )
    _start_complete(
        ledger,
        run_id="r-prior",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
        filter_errors_total=0,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-now",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
        filter_errors_total=0,
    )
    assert reasons == ["filter_stall"]


def test_filter_error_fires_on_backfill_run(tmp_path: Path) -> None:
    """Scorer failure is actionable on any run kind, not just scheduled.

    Stalls are gated on ``kind=scheduled`` because they describe trends.
    A single scorer-execution failure is point-in-time and actionable
    regardless of run kind, so ``filter_error`` does not gate on kind.
    """
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        kind="backfill",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
        filter_errors_total=1,
    )
    assert reasons == ["filter_error"]


def test_filter_error_persists_filter_errors_total_in_ledger_entry(
    tmp_path: Path,
) -> None:
    """The ``filter_errors_total`` field is recorded on every completed run."""
    ledger = RunLedger(tmp_path / "state")
    _start_complete(
        ledger,
        run_id="r-clean",
        profile="p",
        sources_checked=5,
        ingested=3,
        fetched_total=5,
        filter_errors_total=0,
    )
    _start_complete(
        ledger,
        run_id="r-error",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
        filter_errors_total=3,
    )
    history = ledger.recent()
    by_id = {e["run_id"]: e for e in history}
    assert by_id["r-clean"]["filter_errors_total"] == 0
    assert by_id["r-error"]["filter_errors_total"] == 3


def test_filter_error_can_co_occur_with_source_acquisition(tmp_path: Path) -> None:
    """A run can carry both ``source_acquisition`` and ``filter_error``.

    They describe distinct, orthogonal failure modes — a swallowed
    source-fetch error and a separate scorer execution failure can
    happen on the same run (e.g. one source's fetch failed, another's
    fetch succeeded but its filter call raised).
    """
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
        filter_errors_total=1,
        source_acquisition_errors=[
            {"source": "rss", "kind": "timeout", "detail": "feed X"},
        ],
    )
    assert "source_acquisition" in reasons
    assert "filter_error" in reasons
    assert "filter_stall" not in reasons


def test_complete_default_filter_errors_total_when_omitted(tmp_path: Path) -> None:
    """Omitting ``filter_errors_total`` keeps the legacy signature working.

    Mirrors the equivalent test for ``fetched_total``.
    """
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    reasons = ledger.complete(run_id="r-1", sources_checked=4, ingested=2)
    assert reasons == []
    entry = ledger.recent()[0]
    assert entry.get("filter_errors_total") is None


# ── #131 review concern 2: invalid_url_stall ────────────────────────


def test_all_items_url_rejected_flags_invalid_url_stall(tmp_path: Path) -> None:
    """Feed returned items but every URL was bad → invalid_url_stall.

    Single-run signal — no consecutive-runs ratchet, unlike fetch_stall
    or filter_stall.
    """
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=5,
        invalid_url_rejections_total=5,
    )
    assert "invalid_url_stall" in reasons
    entry = ledger.recent()[0]
    assert entry["degraded"] is True
    assert "invalid_url_stall" in entry["degraded_reasons"]
    assert entry["invalid_url_rejections_total"] == 5


def test_invalid_url_stall_preempts_filter_stall(tmp_path: Path) -> None:
    """When all items were URL-rejected, the filter never ran — don't
    misclassify as ``filter_stall``."""
    ledger = RunLedger(tmp_path / "state")
    # Seed history so filter_stall ratchet would otherwise be satisfied.
    _start_complete(
        ledger, run_id="r-history", profile="p", sources_checked=4, ingested=2
    )
    _start_complete(
        ledger,
        run_id="r-bad",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=3,
        invalid_url_rejections_total=3,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-current",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=3,
        invalid_url_rejections_total=3,
    )
    assert "invalid_url_stall" in reasons
    assert "filter_stall" not in reasons


def test_partial_url_rejection_does_not_flag_invalid_url_stall(
    tmp_path: Path,
) -> None:
    """Some URLs invalid + others reach the filter → not invalid_url_stall.

    Filter-side diagnosis (filter_stall) is correct here: items DID
    reach the scorer.
    """
    ledger = RunLedger(tmp_path / "state")
    # Seed history so filter_stall ratchet is satisfied.
    _start_complete(
        ledger, run_id="r-history", profile="p", sources_checked=4, ingested=2
    )
    _start_complete(
        ledger,
        run_id="r-prev",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
        invalid_url_rejections_total=2,
    )
    reasons = _start_complete(
        ledger,
        run_id="r-current",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
        invalid_url_rejections_total=2,
    )
    # 8 items reached the filter and were rejected — that's filter_stall.
    assert "invalid_url_stall" not in reasons
    assert "filter_stall" in reasons


def test_invalid_url_stall_does_not_fire_when_fetched_total_zero(
    tmp_path: Path,
) -> None:
    """``fetch_stall`` (not invalid_url_stall) when nothing was fetched."""
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="p",
        sources_checked=0,
        ingested=0,
        fetched_total=0,
        invalid_url_rejections_total=0,
    )
    assert "invalid_url_stall" not in reasons


def test_invalid_url_stall_fires_on_first_run(tmp_path: Path) -> None:
    """No consecutive-runs gate — single-run signal."""
    ledger = RunLedger(tmp_path / "state")
    reasons = _start_complete(
        ledger,
        run_id="r-1",
        profile="brand-new-profile",
        sources_checked=0,
        ingested=0,
        fetched_total=2,
        invalid_url_rejections_total=2,
    )
    assert "invalid_url_stall" in reasons


# ── #152: degradation summary ────────────────────────────────────────


def test_degradation_summary_present_on_clean_run(tmp_path: Path) -> None:
    """Clean runs still carry a ``degradation_summary`` so consumers
    never need to defend against a missing field (#152)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    ledger.complete(run_id="r-1", sources_checked=5, ingested=3)

    entry = ledger.recent()[0]
    summary = entry["degradation_summary"]
    assert summary is not None
    assert summary["totals"]["archive_failures"] == 0
    assert summary["totals"]["source_acquisition_errors"] == 0
    assert summary["totals"]["cache_hits"] == 0
    assert summary["archive"]["by_kind"] == []
    assert summary["archive"]["by_domain"] == []
    assert summary["source_acquisition"]["by_kind"] == []
    assert summary["source_acquisition"]["by_source"] == []
    assert summary["retries_recovered"]["by_kind"] == []


def test_degradation_summary_top_archive_by_domain(tmp_path: Path) -> None:
    """Archive failures group by URL domain so operators see WHICH
    domain dominates, not just the total (#152)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    archive = [
        {"url": "https://ieee.org/a", "tags": ["influx:archive-missing"]},
        {"url": "https://ieee.org/b", "tags": ["influx:archive-missing"]},
        {"url": "https://ieee.org/c", "tags": ["influx:archive-missing"]},
        {"url": "https://acm.org/x", "tags": ["influx:archive-missing"]},
        {"url": "https://acm.org/y", "tags": ["influx:archive-missing"]},
        {"url": "https://other.example/z", "tags": ["influx:archive-missing"]},
    ]
    ledger.complete(
        run_id="r-1",
        sources_checked=10,
        ingested=4,
        archive_failures_total=len(archive),
        archive_failures=archive,
    )

    summary = ledger.recent()[0]["degradation_summary"]
    domains = summary["archive"]["by_domain"]
    assert domains[0] == {"domain": "ieee.org", "count": 3}
    assert domains[1] == {"domain": "acm.org", "count": 2}
    assert domains[2] == {"domain": "other.example", "count": 1}
    assert summary["totals"]["archive_failures"] == 6


def test_degradation_summary_archive_by_kind_uses_policy_tags(
    tmp_path: Path,
) -> None:
    """Archive ``by_kind`` distinguishes blocked / rate_limited /
    missing_by_policy / unspecified from the tag taxonomy (#152)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    archive = [
        {
            "url": "https://a.example/1",
            "tags": ["influx:archive-missing", "influx:archive-blocked"],
        },
        {
            "url": "https://a.example/2",
            "tags": ["influx:archive-missing", "influx:archive-blocked"],
        },
        {
            "url": "https://b.example/1",
            "tags": ["influx:archive-missing", "influx:archive-rate-limited"],
        },
        {
            "url": "https://c.example/1",
            "tags": ["influx:archive-missing", "influx:archive-skipped-by-policy"],
        },
        {
            # Generic upstream failure (no policy tag): bucketed as
            # "unspecified" so the long tail stays visible.
            "url": "https://d.example/1",
            "tags": ["influx:archive-missing"],
        },
    ]
    ledger.complete(
        run_id="r-1",
        sources_checked=10,
        ingested=5,
        archive_failures_total=len(archive),
        archive_failures=archive,
    )

    by_kind = ledger.recent()[0]["degradation_summary"]["archive"]["by_kind"]
    kinds = {row["kind"]: row["count"] for row in by_kind}
    assert kinds == {
        "blocked": 2,
        "rate_limited": 1,
        "missing_by_policy": 1,
        "unspecified": 1,
    }


def test_degradation_summary_source_acquisition_breakdown(tmp_path: Path) -> None:
    """Source-acquisition failures break down by source AND by kind so
    operators can distinguish "arXiv-only rate-limit storm" from
    "every source is timing out" (#152)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    errors = [
        {
            "source": "arxiv",
            "kind": "rate_limit_upstream_capacity",
            "detail": "429",
        },
        {
            "source": "arxiv",
            "kind": "rate_limit_upstream_capacity",
            "detail": "429",
        },
        {"source": "arxiv", "kind": "rate_limit_local", "detail": "429"},
        {"source": "rss", "kind": "timeout", "detail": "slow"},
    ]
    ledger.complete(
        run_id="r-1",
        sources_checked=2,
        ingested=0,
        source_acquisition_errors=errors,
    )

    summary = ledger.recent()[0]["degradation_summary"]
    kinds = {
        row["kind"]: row["count"] for row in summary["source_acquisition"]["by_kind"]
    }
    sources = {
        row["source"]: row["count"]
        for row in summary["source_acquisition"]["by_source"]
    }
    assert kinds == {
        "rate_limit_upstream_capacity": 2,
        "rate_limit_local": 1,
        "timeout": 1,
    }
    assert sources == {"arxiv": 3, "rss": 1}


def test_degradation_summary_caps_breakdowns_at_top_n(tmp_path: Path) -> None:
    """Long tails get truncated — operators need top drivers, not every
    one-off failure (#152)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    # 10 distinct domains, descending counts 10..1
    archive: list[dict[str, Any]] = []
    for i, count in enumerate(range(10, 0, -1)):
        for _ in range(count):
            archive.append(
                {
                    "url": f"https://host-{i:02d}.example/p",
                    "tags": ["influx:archive-missing"],
                }
            )
    ledger.complete(
        run_id="r-1",
        sources_checked=20,
        ingested=10,
        archive_failures_total=len(archive),
        archive_failures=archive,
    )

    domains = ledger.recent()[0]["degradation_summary"]["archive"]["by_domain"]
    # Bounded to top 5; first entry is the heaviest hitter.
    assert len(domains) == 5
    assert domains[0]["count"] == 10
    assert domains[-1]["count"] == 6


def test_degradation_summary_does_not_count_cache_hits_as_degradation(
    tmp_path: Path,
) -> None:
    """A run whose only "issue" is dedupe is NOT degraded — cache hits
    must NOT inflate the archive or source-acquisition breakdowns
    (#152 acceptance criteria)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="backfill", run_range=None)
    reasons = ledger.complete(
        run_id="r-1",
        sources_checked=0,
        ingested=0,
        cache_hits_total=100,
    )

    entry = ledger.recent()[0]
    assert reasons == []
    assert entry["degraded"] is False
    summary = entry["degradation_summary"]
    assert summary["totals"]["cache_hits"] == 100
    # Cache hits MUST NOT show up in archive / source-acquisition
    # breakdowns — they are not failures.
    assert summary["archive"]["by_domain"] == []
    assert summary["archive"]["by_kind"] == []
    assert summary["source_acquisition"]["by_kind"] == []


def test_degradation_summary_includes_retries_recovered(tmp_path: Path) -> None:
    """Recovered retries are surfaced so operators see "we burned the
    retry budget" patterns even on otherwise-clean runs (#152 +
    #129)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    ledger.complete(
        run_id="r-1",
        sources_checked=4,
        ingested=2,
        source_retry_counts={
            "arxiv": {"rate_limit_upstream_capacity": 3, "network": 1},
            "rss": {"timeout": 2},
        },
    )

    summary = ledger.recent()[0]["degradation_summary"]
    assert summary["totals"]["retries_recovered"] == 6
    by_kind = {
        row["kind"]: row["count"] for row in summary["retries_recovered"]["by_kind"]
    }
    by_source = {
        row["source"]: row["count"] for row in summary["retries_recovered"]["by_source"]
    }
    assert by_kind == {
        "rate_limit_upstream_capacity": 3,
        "timeout": 2,
        "network": 1,
    }
    assert by_source == {"arxiv": 4, "rss": 2}


def test_degradation_summary_distinguishes_all_failure_classes(
    tmp_path: Path,
) -> None:
    """Run mixing archive / source-acquisition / timeouts / invalid-url
    stay distinguishable in the totals block (#152 acceptance
    criteria)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    ledger.complete(
        run_id="r-1",
        sources_checked=3,
        ingested=1,
        filter_errors_total=2,
        invalid_url_rejections_total=4,
        archive_failures_total=2,
        archive_failures=[
            {"url": "https://x.example/1", "tags": ["influx:archive-missing"]},
            {
                "url": "https://x.example/2",
                "tags": ["influx:archive-missing", "influx:archive-rate-limited"],
            },
        ],
        source_acquisition_errors=[
            {"source": "arxiv", "kind": "timeout", "detail": "slow"},
        ],
        cache_hits_total=7,
    )

    totals = ledger.recent()[0]["degradation_summary"]["totals"]
    assert totals == {
        "archive_failures": 2,
        "source_acquisition_errors": 1,
        "filter_errors": 2,
        "invalid_url_rejections": 4,
        "cache_hits": 7,
        "retries_recovered": 0,
    }


def test_degradation_summary_top_n_is_deterministic_on_ties(
    tmp_path: Path,
) -> None:
    """Ties in count break to the lower-keyed entry so two operators
    inspecting the same run see the same ordering (#152)."""
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r-1", profile="p", kind="scheduled", run_range=None)
    archive = [
        {"url": "https://b.example/1", "tags": ["influx:archive-missing"]},
        {"url": "https://a.example/1", "tags": ["influx:archive-missing"]},
    ]
    ledger.complete(
        run_id="r-1",
        sources_checked=2,
        ingested=0,
        archive_failures_total=len(archive),
        archive_failures=archive,
    )

    domains = [
        row["domain"]
        for row in ledger.recent()[0]["degradation_summary"]["archive"]["by_domain"]
    ]
    # Both at count=1; ``a.example`` sorts first because count is tied
    # and we fall back to ascending key.
    assert domains == ["a.example", "b.example"]


# ── #152: build_degradation_summary unit coverage ───────────────────


def test_build_degradation_summary_handles_all_none(tmp_path: Path) -> None:
    """All-``None`` inputs produce a zero-shaped summary so the
    ``ledger.complete`` happy path doesn't have to defend (#152)."""
    from influx.run_ledger import build_degradation_summary

    summary = build_degradation_summary(
        source_acquisition_errors=None,
        archive_failures=None,
        source_retry_counts=None,
        filter_errors_total=None,
        invalid_url_rejections_total=None,
        cache_hits_total=None,
    )
    assert summary["totals"]["archive_failures"] == 0
    assert summary["totals"]["source_acquisition_errors"] == 0
    assert summary["totals"]["retries_recovered"] == 0
    assert summary["archive"]["by_domain"] == []
    assert summary["source_acquisition"]["by_source"] == []
    assert summary["retries_recovered"]["by_kind"] == []


def test_build_degradation_summary_ignores_unparseable_archive_urls(
    tmp_path: Path,
) -> None:
    """Items with empty / malformed URLs still show up in by_kind
    (they're real failures) but do not pollute by_domain (#152)."""
    from influx.run_ledger import build_degradation_summary

    summary = build_degradation_summary(
        source_acquisition_errors=None,
        archive_failures=[
            {"url": "", "tags": ["influx:archive-missing"]},
            {"url": "not-a-url", "tags": ["influx:archive-missing"]},
            {"url": "https://real.example/x", "tags": ["influx:archive-missing"]},
        ],
        source_retry_counts=None,
        filter_errors_total=None,
        invalid_url_rejections_total=None,
        cache_hits_total=None,
    )
    assert summary["totals"]["archive_failures"] == 3
    by_kind = {row["kind"]: row["count"] for row in summary["archive"]["by_kind"]}
    assert by_kind == {"unspecified": 3}
    # Only the one parseable URL contributes to ``by_domain``.
    by_domain = {row["domain"]: row["count"] for row in summary["archive"]["by_domain"]}
    assert by_domain == {"real.example": 1}
