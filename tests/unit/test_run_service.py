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
"""

from __future__ import annotations

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
