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
from influx.errors import LithosError
from influx.run import RunOutcome, RunPlan
from influx.run_ledger import RunLedger
from influx.run_service import RunService, run_via_service
from influx.telemetry import (
    current_fetched_total,
    current_filter_errors,
    current_source_acquisition_errors,
    record_tier3_fallback,
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


async def test_tier3_fallback_counts_propagate_to_ledger_entry(
    tmp_path: Any,
) -> None:
    """A run body that records Tier 3 fallbacks lands them on the ledger
    entry as ``tier3_fallbacks`` (#151).

    Uses :func:`record_tier3_fallback` from inside the patched body so
    we exercise the real contextvar wiring inside ``ledger_lifecycle``
    rather than mocking the bucket directly.
    """
    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    async def body_with_tier3_noise(*args: Any, **kwargs: Any) -> RunOutcome:
        # Three harmless fallbacks (Tier 1+2 present, Tier 3 failed) and
        # one degraded (Tier 1 missing) — the shape the Cascade emits
        # via ``Cascade._log_tier3_failure``.
        record_tier3_fallback(kind="harmless")
        record_tier3_fallback(kind="harmless")
        record_tier3_fallback(kind="harmless")
        record_tier3_fallback(kind="degraded")
        return RunOutcome(sources_checked=4, ingested=4)

    with patch("influx.run.Run.execute", new=body_with_tier3_noise):
        await service.execute(_scheduled_plan())

    entry = ledger.recent()[0]
    assert entry["tier3_fallbacks"] == {"harmless": 3, "degraded": 1}
    # Tier 3 fallback counts on their own don't flip the run into
    # ``degraded`` — the dedicated stall / acquisition / archive
    # reasons own that field.  Operators read the counters as a
    # diagnostic split, like ``source_retry_counts``.
    assert entry["degraded"] is False
    assert entry["degraded_reasons"] == []


async def test_tier3_fallback_counts_default_to_empty_dict(
    tmp_path: Any,
) -> None:
    """A clean run with no Tier 3 fallback recording lands
    ``tier3_fallbacks={}`` so downstream consumers always see the field
    (#151).
    """
    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    with patch(
        "influx.run.Run.execute",
        new_callable=AsyncMock,
        return_value=RunOutcome(sources_checked=2, ingested=2),
    ):
        await service.execute(_scheduled_plan())

    entry = ledger.recent()[0]
    assert entry["tier3_fallbacks"] == {}


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


async def test_failed_run_ledger_error_carries_lithos_detail(
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#234: LithosError ``detail`` reaches the ledger error and log line.

    Previously only ``str(exc)`` ("cache_lookup failed") was recorded,
    so diagnosing the underlying Lithos-side crash required reading the
    Lithos container logs.
    """
    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    async def boom(*args: Any, **kwargs: Any) -> RunOutcome:
        raise LithosError(
            "cache_lookup failed",
            operation="cache_lookup",
            detail="TypeError: '<' not supported between instances of 'NoneType' and 'float'",
        )

    with (
        patch("influx.run.Run.execute", side_effect=boom),
        caplog.at_level(logging.ERROR, logger="influx.run_service"),
        pytest.raises(LithosError, match="cache_lookup failed"),
    ):
        await service.execute(_scheduled_plan())

    entry = ledger.recent()[0]
    assert entry["status"] == "failed"
    assert "operation=cache_lookup" in entry["error"]
    assert "'<' not supported" in entry["error"]

    failed_records = [r for r in caplog.records if "run failed" in r.getMessage()]
    assert failed_records, "expected a 'run failed' ERROR record"
    assert "'<' not supported" in failed_records[0].getMessage()


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
    # Issue #164: clean runs carry the ``severity=success`` bucket so
    # operator log greps can filter for non-trivial outcomes uniformly.
    assert "severity=success" in record.message


async def test_run_completed_log_carries_expected_lossy_severity(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #164: archive_acquisition alone → ``severity=expected_lossy``.

    Tolerated upstream lossiness (the openai.com / lesswrong.com
    staging shape) gets the ``expected_lossy`` bucket so dashboards
    can separate release-noise from real breakage without parsing
    the reason list.
    """
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
                    title="Archive failed",
                    score=8,
                    tags=["profile:alpha", "influx:archive-missing"],
                    reason="archive fetch failed",
                    url="https://www.lesswrong.com/posts/x",
                    related_in_lithos=[],
                )
            ],
        ),
    )
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
    assert "degraded=True" in record.message
    assert "severity=expected_lossy" in record.message

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert entry["degradation_severity"] == "expected_lossy"


async def test_run_completed_log_carries_unexpected_failure_severity(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #164: invalid_note_state escalates to ``unexpected_failure``.

    Reproduces the staging shape
    ``reasons=archive_acquisition,invalid_note_state`` — even though
    archive_acquisition is expected-lossy on its own, the
    invalid_note_state co-occurrence escalates the whole run.
    """
    from influx.notifications import HighlightItem, ProfileRunResult, RunStats
    from influx.telemetry import current_write_outcomes

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    async def body_with_invalid_input(self: Any) -> RunOutcome:
        # Stamp an ``invalid_input`` write outcome so the run-service
        # surfaces ``invalid_note_state`` to the ledger.
        counter = current_write_outcomes.get()
        assert counter is not None
        counter[("invalid_input", "arxiv")] = (
            counter.get(("invalid_input", "arxiv"), 0) + 1
        )
        return RunOutcome(
            sources_checked=2,
            ingested=1,
            profile_run_result=ProfileRunResult(
                run_date="2026-05-08",
                profile="alpha",
                stats=RunStats(sources_checked=2, ingested=1),
                items=[
                    HighlightItem(
                        id="note-1",
                        title="Archive failed",
                        score=8,
                        tags=["profile:alpha", "influx:archive-missing"],
                        reason="archive fetch failed",
                        url="https://openai.com/index/x",
                        related_in_lithos=[],
                    )
                ],
            ),
        )

    with (
        caplog.at_level(logging.INFO, logger="influx.run_service"),
        patch("influx.run.Run.execute", new=body_with_invalid_input),
    ):
        await service.execute(_scheduled_plan())

    record = _extract_run_completed_record(caplog)
    assert "degraded=True" in record.message
    assert "severity=unexpected_failure" in record.message
    assert "invalid_note_state" in record.message

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert entry["degradation_severity"] == "unexpected_failure"
    assert "invalid_note_state" in entry["degraded_reasons"]
    # archive_acquisition is still present in the list — the
    # severity bucket escalated, but the reason list preserves both.
    assert "archive_acquisition" in entry["degraded_reasons"]


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


async def test_archive_unsupported_item_does_not_trigger_archive_acquisition(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #161: items from ``unsupported`` policy domains must NOT
    contribute to ``archive_failures_total`` or fire the
    ``archive_acquisition`` degraded reason.

    The run-service derives ``archive_failures_total`` by counting
    items tagged ``influx:archive-missing``.  A note ingested from a
    domain on the ``unsupported`` policy list carries
    ``influx:archive-unsupported`` plus ``influx:archive-terminal``
    instead — the operator has already declared the domain has no
    archive surface, so the run must not degrade purely because that
    expected outcome was observed.  Pins the contract end-to-end so a
    later tag-composition refactor cannot silently re-introduce the
    spurious degradation that #161 fixes for ``openai.com``-style
    domains.
    """
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
                    title="OpenAI Blog Post",
                    score=8,
                    # Crucially: NO ``influx:archive-missing`` tag.
                    # ``unsupported`` is an expected, terminal outcome
                    # for declared-unsupported domains.
                    tags=[
                        "profile:alpha",
                        "influx:archive-unsupported",
                        "influx:archive-terminal",
                    ],
                    reason="domain policy unsupported",
                    url="https://openai.com/index/some-post",
                    related_in_lithos=[],
                )
            ],
        ),
    )
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
    assert "archive_acquisition" not in entry["degraded_reasons"]
    assert entry["archive_failures_total"] == 0

    # No archive_acquisition WARNING should have been emitted.  The
    # informational INFO line below mentions ``archive_acquisition`` in
    # its explanatory tail; restrict the check to WARNING records so
    # the explainer text doesn't false-positive.
    assert not [
        r
        for r in caplog.records
        if "archive_acquisition" in r.message and r.levelname == "WARNING"
    ], "unsupported items must not emit the archive_acquisition warning"

    # But the explicit INFO summary should fire so an operator sees
    # the policy decision in the log stream (acceptance criterion #4).
    info_logs = [
        r
        for r in caplog.records
        if "archive_unsupported" in r.message and r.levelname == "INFO"
    ]
    assert info_logs, "expected an INFO archive_unsupported summary line"
    assert "unsupported_items=1" in info_logs[0].message


async def test_non_html_source_item_does_not_trigger_archive_acquisition(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #160: ``non_html_source`` items must NOT inflate the run's
    ``archive_failures_total`` or fire the ``archive_acquisition``
    degraded reason.

    The run-service derives ``archive_failures_total`` by counting
    items tagged ``influx:archive-missing``.  An item that was
    short-circuited at the URL-shape level (RSS/feed pointer, HN
    discussion link) carries ``influx:archive-non-html-source`` plus
    ``influx:archive-terminal`` instead — it is a deliberate skip, not
    a missing archive.  This test pins that contract end-to-end so a
    future tag-composition refactor cannot silently re-introduce the
    spurious degradation that #160 fixed.
    """
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
                    title="Feed Pointer Note",
                    score=8,
                    # Crucially: NO ``influx:archive-missing`` tag.  The
                    # non-html-source skip is informational + terminal.
                    tags=[
                        "profile:alpha",
                        "influx:archive-non-html-source",
                        "influx:archive-terminal",
                    ],
                    reason="non-html URL shape",
                    url="https://csdb.dk/rss/upcomingevents.php",
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
    assert "archive_acquisition" not in entry["degraded_reasons"]
    assert entry["archive_failures_total"] == 0

    # And no archive_acquisition warning should have been emitted.
    assert not [r for r in caplog.records if "archive_acquisition" in r.message], (
        "non_html_source items must not emit the archive_acquisition warning"
    )


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


# ── #152: degradation summary flows from RunService into the ledger ─


async def test_degradation_summary_aggregates_archive_failures_by_domain(
    tmp_path: Path,
) -> None:
    """End-to-end: the RunService passes per-item archive failures into
    the ledger so the persisted ``degradation_summary.archive.by_domain``
    spotlights the dominant host (#152)."""
    from influx.notifications import HighlightItem, ProfileRunResult, RunStats

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    body_outcome = RunOutcome(
        sources_checked=4,
        ingested=3,
        profile_run_result=ProfileRunResult(
            run_date="2026-05-12",
            profile="alpha",
            stats=RunStats(sources_checked=4, ingested=3),
            items=[
                HighlightItem(
                    id="note-1",
                    title="A",
                    score=8,
                    tags=["profile:alpha", "influx:archive-missing"],
                    reason="x",
                    url="https://ieee.org/a",
                ),
                HighlightItem(
                    id="note-2",
                    title="B",
                    score=8,
                    tags=[
                        "profile:alpha",
                        "influx:archive-missing",
                        "influx:archive-blocked",
                    ],
                    reason="x",
                    url="https://ieee.org/b",
                ),
                HighlightItem(
                    id="note-3",
                    title="C",
                    score=8,
                    tags=["profile:alpha", "influx:archive-missing"],
                    reason="x",
                    url="https://acm.org/c",
                ),
            ],
        ),
    )
    with patch(
        "influx.run.Run.execute",
        new_callable=AsyncMock,
        return_value=body_outcome,
    ):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    summary = entry["degradation_summary"]
    assert summary is not None
    by_domain = {row["domain"]: row["count"] for row in summary["archive"]["by_domain"]}
    assert by_domain == {"ieee.org": 2, "acm.org": 1}
    by_kind = {row["kind"]: row["count"] for row in summary["archive"]["by_kind"]}
    # 1 item carries the ``influx:archive-blocked`` policy tag, 2 do not
    # → bucketed as "unspecified".
    assert by_kind == {"unspecified": 2, "blocked": 1}


async def test_degradation_summary_cache_hits_do_not_inflate_breakdowns(
    tmp_path: Path,
) -> None:
    """Backfill regression: a run whose only "issue" is duplicate/dedupe
    cache hits must NOT show degradation breakdowns dominated by cache
    activity.  The ledger's ``degraded`` flag stays false and the
    degradation breakdowns stay empty (#152 acceptance criteria —
    "duplicate/dedupe outcomes don't dominate the degradation summary").
    """
    from influx.telemetry import current_cache_hits

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    async def body_with_only_cache_hits(self: Any) -> RunOutcome:
        # Simulate 25 cache hits in the Ingest stage (typical
        # backfill shape where every candidate already exists).
        cache_hits_counter = current_cache_hits.get()
        assert cache_hits_counter is not None
        cache_hits_counter[0] = 25
        return RunOutcome(sources_checked=0, ingested=0)

    with patch("influx.run.Run.execute", new=body_with_only_cache_hits):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    summary = entry["degradation_summary"]
    # Run is NOT degraded — cache hits are expected behaviour.
    assert entry["degraded"] is False
    # The dedupe volume is visible to operators...
    assert summary["totals"]["cache_hits"] == 25
    # ...but it does NOT pollute the degradation breakdowns.
    assert summary["totals"]["archive_failures"] == 0
    assert summary["totals"]["source_acquisition_errors"] == 0
    assert summary["archive"]["by_domain"] == []
    assert summary["archive"]["by_kind"] == []
    assert summary["source_acquisition"]["by_kind"] == []


async def test_degradation_summary_log_tail_includes_top_drivers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``run completed`` log line carries a compact ``top_drivers=``
    tail when the run is degraded so single-log triage is possible
    without fetching the ledger (#152)."""
    from influx.notifications import HighlightItem, ProfileRunResult, RunStats

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    body_outcome = RunOutcome(
        sources_checked=2,
        ingested=1,
        profile_run_result=ProfileRunResult(
            run_date="2026-05-12",
            profile="alpha",
            stats=RunStats(sources_checked=2, ingested=1),
            items=[
                HighlightItem(
                    id="note-1",
                    title="t",
                    score=8,
                    tags=["profile:alpha", "influx:archive-missing"],
                    reason="x",
                    url="https://ieee.org/a",
                ),
            ],
        ),
    )

    async def body(self: Any) -> RunOutcome:
        errors = current_source_acquisition_errors.get()
        assert errors is not None
        errors.append(
            {
                "source": "arxiv",
                "kind": "rate_limit_upstream_capacity",
                "detail": "429",
            }
        )
        return body_outcome

    with (
        caplog.at_level(logging.INFO, logger="influx.run_service"),
        patch("influx.run.Run.execute", new=body),
    ):
        await service.execute(_scheduled_plan())

    record = _extract_run_completed_record(caplog)
    assert "top_drivers=" in record.message
    assert "ieee.org=1" in record.message
    assert "arxiv/rate_limit_upstream_capacity=1" in record.message


async def test_degradation_summary_log_tail_absent_on_clean_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ``top_drivers=`` tail on clean runs — keeps the log line lean
    when there's nothing to triage (#152)."""
    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    body_outcome = RunOutcome(sources_checked=2, ingested=1)
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
    assert "top_drivers=" not in record.message


# ── #152 review: write-outcome end-to-end plumbing ──────────────────


async def test_write_outcomes_recorded_during_run_flow_into_ledger(
    tmp_path: Path,
) -> None:
    """End-to-end: the Ingest stage's :func:`record_write_outcome` calls
    accumulate into the per-run ContextVar and the ledger entry's
    ``degradation_summary`` surfaces both the duplicate volume and the
    invalid-note-state failures (#152 review)."""
    from influx.telemetry import current_write_outcomes, record_write_outcome

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    async def body(self: Any) -> RunOutcome:
        # The CM set the counter to an empty dict — record a mix of
        # success / duplicate / invalid_input outcomes.
        assert current_write_outcomes.get() is not None
        record_write_outcome(outcome="created", source="arxiv")
        record_write_outcome(outcome="duplicate", source="arxiv")
        record_write_outcome(outcome="duplicate", source="rss")
        record_write_outcome(outcome="invalid_input", source="rss")
        return RunOutcome(sources_checked=4, ingested=1)

    with patch("influx.run.Run.execute", new=body):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    summary = entry["degradation_summary"]
    by_outcome = {
        row["outcome"]: row["count"] for row in summary["writes"]["by_outcome"]
    }
    assert by_outcome == {"created": 1, "duplicate": 2, "invalid_input": 1}
    invalid_by_kind = {
        row["kind"]: row["count"] for row in summary["invalid_note_state"]["by_kind"]
    }
    assert invalid_by_kind == {"invalid_input": 1}
    # Single invalid_input is degrading; duplicates are not.
    assert "invalid_note_state" in entry["degraded_reasons"]
    assert entry["degraded"] is True


async def test_duplicate_only_run_via_service_not_degraded(
    tmp_path: Path,
) -> None:
    """End-to-end regression (#152 review / PR #153 contract): a
    scheduled re-run whose every write is ``duplicate`` is NOT
    degraded and the duplicate volume does not bleed into archive /
    source-acquisition / invalid-note-state counters."""
    from influx.telemetry import record_write_outcome

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    async def body(self: Any) -> RunOutcome:
        # Simulate the backfill / scheduled re-run shape: every
        # candidate already exists in Lithos.
        for _ in range(7):
            record_write_outcome(outcome="duplicate", source="arxiv")
        return RunOutcome(sources_checked=7, ingested=0)

    with patch("influx.run.Run.execute", new=body):
        await service.execute(_scheduled_plan())

    entry = next(e for e in ledger.recent() if e["status"] == "completed")
    assert entry["degraded"] is False
    assert entry["degraded_reasons"] == []

    summary = entry["degradation_summary"]
    # Visible to operators: 7 duplicates surfaced under writes.
    by_outcome = {
        row["outcome"]: row["count"] for row in summary["writes"]["by_outcome"]
    }
    assert by_outcome == {"duplicate": 7}
    # NOT visible (= zero) in any degradation driver.
    assert summary["totals"]["invalid_note_state"] == 0
    assert summary["totals"]["archive_failures"] == 0
    assert summary["totals"]["source_acquisition_errors"] == 0
    assert summary["invalid_note_state"]["by_kind"] == []
    assert summary["invalid_note_state"]["by_source"] == []


async def test_invalid_note_state_emits_warning_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the run is flagged ``invalid_note_state``, the log tail
    carries the by-kind digest so single-log triage is possible
    without fetching the ledger (#152 review)."""
    from influx.telemetry import record_write_outcome

    config = _make_config()
    ledger = RunLedger(tmp_path)
    service = RunService(config=config, ledger=ledger)

    async def body(self: Any) -> RunOutcome:
        record_write_outcome(outcome="version_conflict", source="arxiv")
        record_write_outcome(outcome="version_conflict", source="arxiv")
        record_write_outcome(outcome="slug_collision", source="rss")
        return RunOutcome(sources_checked=3, ingested=0)

    with (
        caplog.at_level(logging.WARNING, logger="influx.run_service"),
        patch("influx.run.Run.execute", new=body),
    ):
        await service.execute(_scheduled_plan())

    matches = [r for r in caplog.records if "invalid_note_state" in r.getMessage()]
    assert matches, "expected an invalid_note_state warning to be emitted"
    message = matches[-1].getMessage()
    assert "version_conflict=2" in message
    assert "slug_collision=1" in message
