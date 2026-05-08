"""Unit tests for RunService (issue #61).

Covers the request lifecycle that wraps :class:`influx.run.Run`:

- Skip gates: ``lithos_circuit_open`` and ``lcma_tools_unavailable``
  flip ``ledger.skip`` and tick ``runs_skipped``; the body never runs.
- Happy path: ledger entry opened on enter, completed on exit, body
  runs and returns its outcome.
- Failure path: the body's exception propagates after ``ledger.fail``.
- ``run_via_service`` builds the right :class:`RunPlan` shape per
  :class:`RunKind` (BACKFILL flips ``skip_repair`` /
  ``skip_cache_hits`` / ``notify``).
- ``run completed`` log line reflects degraded state (issue #79):
  the ``degraded=`` field tracks the structured ``degraded_reasons``
  list so ``ingestion_stall`` / ``fetch_stall`` show up as
  ``degraded=True`` for operators reading the log stream.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from influx.config import (
    AppConfig,
    LithosConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.coordinator import RunKind
from influx.run import RunOutcome, RunPlan
from influx.run_ledger import RunLedger
from influx.run_service import RunService, run_via_service
from influx.telemetry import (
    current_fetched_total,
    current_filter_errors,
    current_source_acquisition_errors,
)


def _make_config(state_dir: str = "/tmp/influx-test") -> AppConfig:
    return AppConfig(
        schedule=ScheduleConfig(
            cron="0 6 * * *", timezone="UTC", misfire_grace_seconds=3600
        ),
        lithos=LithosConfig(url="http://example.invalid/sse"),
        profiles=[ProfileConfig(name="alpha")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="t"),
            tier1_enrich=PromptEntryConfig(text="t"),
            tier3_extract=PromptEntryConfig(text="t"),
        ),
    )


def _scheduled_plan() -> RunPlan:
    return RunPlan(profile="alpha", kind=RunKind.SCHEDULED)


# ── Skip gates ──────────────────────────────────────────────────────


async def test_circuit_breaker_skips_run(tmp_path: Any) -> None:
    """``lithos_circuit_open`` skips the run; body never invoked."""

    class _Probe:
        lithos_unhealthy_consecutive = 5

        def lithos_circuit_open(self, *, threshold: int = 3) -> bool:
            return True

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, probe_loop=_Probe(), ledger=ledger)

    with patch(
        "influx.run.Run.execute",
        new_callable=AsyncMock,
        return_value=RunOutcome(),
    ) as body:
        outcome = await service.execute(_scheduled_plan())

    body.assert_not_called()
    assert outcome.skipped is True
    assert outcome.skip_reason == "lithos_unhealthy"
    entry = ledger.recent()[0]
    assert entry["status"] == "skipped"
    assert entry["error"] == "lithos_unhealthy"


async def test_lcma_tools_unavailable_skips_run(tmp_path: Any) -> None:
    """``lcma_tools_unavailable`` skips the run with the right reason."""

    class _Probe:
        def lithos_circuit_open(self, *, threshold: int = 3) -> bool:
            return False

        def lcma_tools_unavailable(self) -> bool:
            return True

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, probe_loop=_Probe(), ledger=ledger)

    with patch(
        "influx.run.Run.execute",
        new_callable=AsyncMock,
        return_value=RunOutcome(),
    ) as body:
        outcome = await service.execute(_scheduled_plan())

    body.assert_not_called()
    assert outcome.skip_reason == "lcma_tools_unavailable"
    entry = ledger.recent()[0]
    assert entry["error"] == "lcma_tools_unavailable"


# ── Happy path ─────────────────────────────────────────────────────


async def test_happy_path_runs_body_and_completes_ledger(tmp_path: Any) -> None:
    """Body runs; ledger.complete recorded with outcome stats."""
    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    body_outcome = RunOutcome(sources_checked=3, ingested=2)
    with patch(
        "influx.run.Run.execute",
        new_callable=AsyncMock,
        return_value=body_outcome,
    ) as body:
        outcome = await service.execute(_scheduled_plan())

    body.assert_awaited_once()
    assert outcome is body_outcome
    entry = ledger.recent()[0]
    assert entry["status"] == "completed"
    assert entry["sources_checked"] == 3
    assert entry["ingested"] == 2


async def test_body_exception_marks_ledger_failed_and_propagates(
    tmp_path: Any,
) -> None:
    """Body exception → ledger.fail recorded; exception propagates."""
    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    async def boom(*args: Any, **kwargs: Any) -> RunOutcome:
        raise RuntimeError("body crashed")

    with (
        patch("influx.run.Run.execute", side_effect=boom),
        pytest.raises(RuntimeError, match="body crashed"),
    ):
        await service.execute(_scheduled_plan())

    entry = ledger.recent()[0]
    assert entry["status"] == "failed"
    assert "RuntimeError" in entry["error"]


# ── run_via_service: kind → RunPlan flag mapping ───────────────────


async def test_run_via_service_backfill_uses_backfill_flags(tmp_path: Any) -> None:
    """BACKFILL → skip_repair=True, skip_cache_hits=True, notify=False."""
    config = _make_config()
    ledger = RunLedger(tmp_path)
    captured: dict[str, RunPlan] = {}

    async def capture_execute(self: Any) -> RunOutcome:
        captured["plan"] = self.plan
        return RunOutcome()

    with patch("influx.run.Run.execute", new=capture_execute):
        await run_via_service(
            "alpha",
            RunKind.BACKFILL,
            run_range={"days": 7},
            config=config,
            run_ledger=ledger,
        )

    plan = captured["plan"]
    assert plan.kind == RunKind.BACKFILL
    assert plan.skip_repair is True
    assert plan.skip_cache_hits is True
    assert plan.notify is False
    assert plan.date_window == {"days": 7}


async def test_run_via_service_scheduled_uses_full_run_flags(tmp_path: Any) -> None:
    """SCHEDULED → skip_repair=False, skip_cache_hits=False, notify=True."""
    config = _make_config()
    ledger = RunLedger(tmp_path)
    captured: dict[str, RunPlan] = {}

    async def capture_execute(self: Any) -> RunOutcome:
        captured["plan"] = self.plan
        return RunOutcome()

    with patch("influx.run.Run.execute", new=capture_execute):
        await run_via_service(
            "alpha",
            RunKind.SCHEDULED,
            config=config,
            run_ledger=ledger,
        )

    plan = captured["plan"]
    assert plan.skip_repair is False
    assert plan.skip_cache_hits is False
    assert plan.notify is True


async def test_run_via_service_returns_profile_run_result(tmp_path: Any) -> None:
    """``run_via_service`` unwraps RunOutcome.profile_run_result for legacy callers."""
    from influx.notifications import ProfileRunResult, RunStats

    config = _make_config()
    ledger = RunLedger(tmp_path)
    legacy_result = ProfileRunResult(
        run_date="2026-05-03",
        profile="alpha",
        stats=RunStats(sources_checked=1, ingested=1),
        items=[],
    )
    body_outcome = RunOutcome(profile_run_result=legacy_result)

    with patch(
        "influx.run.Run.execute",
        new_callable=AsyncMock,
        return_value=body_outcome,
    ):
        result = await run_via_service(
            "alpha",
            RunKind.SCHEDULED,
            config=config,
            run_ledger=ledger,
        )

    assert result is legacy_result


async def test_run_via_service_returns_none_when_body_returns_none(
    tmp_path: Any,
) -> None:
    """Legacy contract: ``Run.execute()`` may return None (test patches)."""
    config = _make_config()
    ledger = RunLedger(tmp_path)

    with patch(
        "influx.run.Run.execute",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await run_via_service(
            "alpha",
            RunKind.SCHEDULED,
            config=config,
            run_ledger=ledger,
        )

    assert result is None


# ── #79: ``run completed`` log line reflects degraded state ────────


def _extract_run_completed_record(
    caplog: pytest.LogCaptureFixture,
) -> logging.LogRecord:
    """Find the single ``run completed`` log record emitted by the lifecycle CM."""
    matches = [r for r in caplog.records if r.message.startswith("run completed")]
    assert len(matches) == 1, (
        f"expected exactly one 'run completed' log line, got {len(matches)}: "
        f"{[r.message for r in matches]}"
    )
    return matches[0]


def _seed_zero_ingestion_stall_history(ledger: RunLedger, profile: str) -> None:
    """Seed one prior scheduled zero-ingestion run.

    The next zero-ingestion run on the same profile then trips
    ``ingestion_stall``.
    """
    ledger.start(
        run_id="prior-ingestion",
        profile=profile,
        kind="scheduled",
        run_range=None,
    )
    ledger.complete(
        run_id="prior-ingestion",
        sources_checked=5,
        ingested=0,
    )


def _seed_fetch_stall_history(ledger: RunLedger, profile: str) -> None:
    """Seed history so the next zero-fetch run trips fetch_stall.

    Requires (a) a prior non-zero fetch (the historical-ratchet) and
    (b) one prior consecutive zero-fetch scheduled run.
    """
    ledger.start(
        run_id="prior-history",
        profile=profile,
        kind="scheduled",
        run_range=None,
    )
    ledger.complete(
        run_id="prior-history",
        sources_checked=5,
        ingested=2,
        fetched_total=5,
    )
    ledger.start(
        run_id="prior-zero-fetch",
        profile=profile,
        kind="scheduled",
        run_range=None,
    )
    ledger.complete(
        run_id="prior-zero-fetch",
        sources_checked=0,
        ingested=0,
        fetched_total=0,
    )


def _seed_filter_stall_history(ledger: RunLedger, profile: str) -> None:
    """Seed history so the next (fetched > 0, sources_checked = 0) run
    trips filter_stall (#85).

    Requires (a) a prior non-zero ``sources_checked`` run (the
    historical-ratchet) and (b) one prior consecutive
    ``sources_checked == 0 AND fetched_total > 0`` scheduled run.
    """
    ledger.start(
        run_id="prior-history",
        profile=profile,
        kind="scheduled",
        run_range=None,
    )
    ledger.complete(
        run_id="prior-history",
        sources_checked=5,
        ingested=2,
        fetched_total=5,
    )
    ledger.start(
        run_id="prior-filter-rejected",
        profile=profile,
        kind="scheduled",
        run_range=None,
    )
    ledger.complete(
        run_id="prior-filter-rejected",
        sources_checked=0,
        ingested=0,
        fetched_total=10,
    )


async def test_run_completed_log_degraded_true_for_source_errors(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """source_acquisition errors → ``degraded=True`` in the log line."""
    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    async def body_with_source_errors(self: Any) -> RunOutcome:
        errors = current_source_acquisition_errors.get() or []
        errors.append(
            {"source": "arxiv", "kind": "http", "detail": "HTTP 500 from upstream"}
        )
        current_source_acquisition_errors.set(errors)
        return RunOutcome(sources_checked=3, ingested=2)

    with (
        caplog.at_level(logging.INFO, logger="influx.run_service"),
        patch("influx.run.Run.execute", new=body_with_source_errors),
    ):
        await service.execute(_scheduled_plan())

    record = _extract_run_completed_record(caplog)
    assert "degraded=True" in record.message


async def test_run_completed_log_degraded_true_for_ingestion_stall(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ingestion_stall reason → ``degraded=True`` in the log line.

    Regression for #79: the old code logged ``bool(source_errors)``,
    which stayed False for the stall-only path even though
    ``degraded_reasons`` flagged ``ingestion_stall``.
    """
    config = _make_config()
    ledger = RunLedger(tmp_path)
    _seed_zero_ingestion_stall_history(ledger, profile="alpha")
    service = RunService(config=config, ledger=ledger)

    # No source errors; ingested=0 with sources_checked>0 trips
    # ingestion_stall on the second consecutive zero-ingestion run.
    body_outcome = RunOutcome(sources_checked=4, ingested=0)
    with (
        caplog.at_level(logging.INFO, logger="influx.run_service"),
        patch(
            "influx.run.Run.execute",
            new_callable=AsyncMock,
            return_value=body_outcome,
        ),
    ):
        await service.execute(_scheduled_plan())

    # Sanity: the ledger really did record the stall reason.
    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert "ingestion_stall" in entry["degraded_reasons"]

    record = _extract_run_completed_record(caplog)
    assert "degraded=True" in record.message


async def test_run_completed_log_degraded_true_for_fetch_stall(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """fetch_stall reason → ``degraded=True`` in the log line.

    Regression for #79: source_errors is empty and sources_checked
    is 0, so the old ``bool(source_errors)`` argument logged
    ``degraded=False`` even though ``fetch_stall`` was flagged.
    """
    config = _make_config()
    ledger = RunLedger(tmp_path)
    _seed_fetch_stall_history(ledger, profile="alpha")
    service = RunService(config=config, ledger=ledger)

    body_outcome = RunOutcome(sources_checked=0, ingested=0)
    with (
        caplog.at_level(logging.INFO, logger="influx.run_service"),
        patch(
            "influx.run.Run.execute",
            new_callable=AsyncMock,
            return_value=body_outcome,
        ),
    ):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert "fetch_stall" in entry["degraded_reasons"]

    record = _extract_run_completed_record(caplog)
    assert "degraded=True" in record.message


async def test_filter_stall_emits_warning_and_metric_tick(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """filter_stall (#85) fires its WARNING and ticks the stall metric.

    Mirrors the staging-robotics 2026-05-04 12:00:30 shape: sources
    fetched 61 items (10 arXiv + 51 RSS) but the LLM filter rejected
    every candidate so ``sources_checked`` landed at 0.  A prior
    matching run + non-zero history is seeded so the second run trips
    ``filter_stall`` (not ``fetch_stall``).
    """
    config = _make_config()
    ledger = RunLedger(tmp_path)
    _seed_filter_stall_history(ledger, profile="alpha")
    service = RunService(config=config, ledger=ledger)

    async def body_with_fetched_items(self: Any) -> RunOutcome:
        # Populate the per-run pre-filter counter the way the real
        # source layer does: from inside the lifecycle CM, after the
        # contextvar has been set.
        counter = current_fetched_total.get()
        assert counter is not None
        counter[0] += 61  # 10 arxiv + 51 rss → matches the real shape
        return RunOutcome(sources_checked=0, ingested=0, fetched_total=61)

    with (
        caplog.at_level(logging.WARNING, logger="influx.run_service"),
        patch("influx.run.Run.execute", new=body_with_fetched_items),
    ):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert entry["degraded_reasons"] == ["filter_stall"]
    assert entry["fetched_total"] == 61
    # Verify the operator-facing WARNING fired with filter-side guidance.
    warnings = [r for r in caplog.records if "filter_stall" in r.message]
    assert warnings, "expected a filter_stall WARNING"
    assert "filter rejected all candidates" in warnings[0].message


async def test_filter_stall_does_not_collide_with_fetch_stall(
    tmp_path: Path,
) -> None:
    """A run with ``fetched_total > 0`` and ``sources_checked == 0`` emits
    ``filter_stall`` only — never ``fetch_stall``.

    Mutual exclusion guard.  Seeded history satisfies both ratchets,
    but the (fetched, checked) shape selects exactly one reason.
    """
    config = _make_config()
    ledger = RunLedger(tmp_path)
    _seed_filter_stall_history(ledger, profile="alpha")
    service = RunService(config=config, ledger=ledger)

    async def body_with_fetched_items(self: Any) -> RunOutcome:
        counter = current_fetched_total.get()
        assert counter is not None
        counter[0] += 7
        return RunOutcome(sources_checked=0, ingested=0, fetched_total=7)

    with patch("influx.run.Run.execute", new=body_with_fetched_items):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert "filter_stall" in entry["degraded_reasons"]
    assert "fetch_stall" not in entry["degraded_reasons"]
    assert "ingestion_stall" not in entry["degraded_reasons"]


async def test_run_completed_log_degraded_false_for_clean_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Clean run (no errors, no stall reasons) → ``degraded=False`` in the log."""
    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    body_outcome = RunOutcome(sources_checked=3, ingested=2)
    with (
        caplog.at_level(logging.INFO, logger="influx.run_service"),
        patch(
            "influx.run.Run.execute",
            new_callable=AsyncMock,
            return_value=body_outcome,
        ),
    ):
        await service.execute(_scheduled_plan())

    record = _extract_run_completed_record(caplog)
    assert "degraded=False" in record.message


async def test_archive_acquisition_degrades_run_and_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Accepted archive failures surface as a degraded run-level signal."""
    from influx.notifications import HighlightItem, ProfileRunResult, RunStats

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    body_outcome = RunOutcome(
        sources_checked=2,
        ingested=1,
        profile_run_result=ProfileRunResult(
            run_date="2026-05-08",
            profile="alpha",
            stats=RunStats(sources_checked=2, ingested=1),
            items=[
                HighlightItem(
                    id="note-1",
                    title="Archived Poorly",
                    score=8,
                    tags=["profile:alpha", "influx:archive-missing"],
                    reason="archive fetch accepted without attachment",
                    url="https://example.com/archive-429",
                    related_in_lithos=[],
                )
            ],
        ),
    )
    with (
        caplog.at_level(logging.WARNING, logger="influx.run_service"),
        patch(
            "influx.run.Run.execute",
            new_callable=AsyncMock,
            return_value=body_outcome,
        ),
    ):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert entry["degraded_reasons"] == ["archive_acquisition"]
    assert entry["archive_failures_total"] == 1

    warning = next(r for r in caplog.records if "archive_acquisition" in r.message)
    assert "archive_failures=1" in warning.message
    assert "influx:archive-missing" in warning.message


async def test_filter_error_emits_warning_with_scorer_failure_guidance(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """filter_error (#85 review) fires its WARNING with scorer-failure text.

    A FilterScorerError caught by a source adapter increments the
    per-run filter_errors counter.  Even when the run shape would also
    satisfy filter_stall (fetched_total > 0, sources_checked == 0),
    the operator-facing WARNING must point at scorer execution causes
    (provider config / model availability / response schema), NOT at
    profile description / prompt / threshold.
    """
    config = _make_config()
    ledger = RunLedger(tmp_path)
    _seed_filter_stall_history(ledger, profile="alpha")
    service = RunService(config=config, ledger=ledger)

    async def body_with_scorer_failure(self: Any) -> RunOutcome:
        # Source layer fetched items, then the scorer raised.
        fetched_counter = current_fetched_total.get()
        assert fetched_counter is not None
        fetched_counter[0] += 10
        # Source layer caught FilterScorerError → record it.
        filter_errors_counter = current_filter_errors.get()
        assert filter_errors_counter is not None
        filter_errors_counter[0] += 1
        # No items survive the failed scorer batch.
        return RunOutcome(sources_checked=0, ingested=0, fetched_total=10)

    with (
        caplog.at_level(logging.WARNING, logger="influx.run_service"),
        patch("influx.run.Run.execute", new=body_with_scorer_failure),
    ):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert "filter_error" in entry["degraded_reasons"]
    # Mutual exclusion: filter_stall must NOT also fire.
    assert "filter_stall" not in entry["degraded_reasons"]
    assert entry["filter_errors_total"] == 1

    warnings = [r for r in caplog.records if "filter_error" in r.message]
    assert warnings, "expected a filter_error WARNING"
    msg = warnings[0].message
    # Scorer-failure guidance — operator should look at provider, not profile.
    assert "FilterScorerError" in msg
    assert "provider config" in msg or "provider" in msg.lower()
    # Must NOT misdirect to filter_stall guidance.
    assert "min_score_in_results" not in msg
    assert "profile description" not in msg


async def test_filter_error_does_not_collide_with_filter_stall(
    tmp_path: Path,
) -> None:
    """When filter_errors_total > 0 in a filter_stall-shaped run, ONLY
    ``filter_error`` is emitted (mutual exclusion at the ledger boundary).
    """
    config = _make_config()
    ledger = RunLedger(tmp_path)
    _seed_filter_stall_history(ledger, profile="alpha")
    service = RunService(config=config, ledger=ledger)

    async def body(self: Any) -> RunOutcome:
        fetched_counter = current_fetched_total.get()
        assert fetched_counter is not None
        fetched_counter[0] += 7
        filter_errors_counter = current_filter_errors.get()
        assert filter_errors_counter is not None
        filter_errors_counter[0] += 2
        return RunOutcome(sources_checked=0, ingested=0, fetched_total=7)

    with patch("influx.run.Run.execute", new=body):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert "filter_error" in entry["degraded_reasons"]
    assert "filter_stall" not in entry["degraded_reasons"]
    assert "fetch_stall" not in entry["degraded_reasons"]
