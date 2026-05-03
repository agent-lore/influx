"""Unit tests for the Run module (issue #58).

Covers the AC requirements:

- Happy-path execution through all five stages
- ``RunAborted`` propagation from the Repair stage (the stage that
  actually emits aborts in #58's slice — Feedback / Acquire / Ingest /
  Finalise propagate plain exceptions)
- StageDiagnostics folding across stages
- Health-action application (``repair_write_failure`` flip + clear)

Stages are exercised through ``Run.execute()`` end-to-end with
mocked LithosClient + repair sweep + feedback helper, mirroring the
patches existing scheduler tests use.  This is the contract the
``run_profile`` SCHEDULED dispatch upholds.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
from influx.repair import SweepWriteError
from influx.run import (
    HealthAction,
    Run,
    RunAborted,
    RunDeps,
    RunPlan,
    StageDiagnostics,
    _merge_diagnostics,
    _run_repair_stage,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_config() -> AppConfig:
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


class _NoopClient:
    """Minimal duck-typed LithosClient stub for end-to-end Run tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def close(self) -> None: ...

    async def list_archive_terminal_arxiv_ids(self, *, profile: str) -> frozenset[str]:
        return frozenset()

    async def task_create(self, **kwargs: Any) -> Any:
        from mcp import types as mcp_types

        self.calls.append(("task_create", kwargs))
        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps({"task_id": "task-1"})
                )
            ]
        )

    async def task_complete(self, **kwargs: Any) -> Any:
        from mcp import types as mcp_types

        self.calls.append(("task_complete", kwargs))
        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps({"status": "completed"})
                )
            ]
        )


# ── StageDiagnostics merge ─────────────────────────────────────────


def test_merge_diagnostics_concats_health_actions() -> None:
    a = StageDiagnostics(
        health_actions=(HealthAction(op="clear", latch="repair_write_failure"),)
    )
    b = StageDiagnostics(
        health_actions=(
            HealthAction(op="flip", latch="repair_write_failure", detail="x"),
        )
    )
    merged = _merge_diagnostics(a, b)
    assert len(merged.health_actions) == 2
    assert merged.health_actions[0].op == "clear"
    assert merged.health_actions[1].op == "flip"


def test_merge_diagnostics_dedupes_degraded_reasons() -> None:
    a = StageDiagnostics(degraded_reasons=("ingestion_stall",))
    b = StageDiagnostics(degraded_reasons=("ingestion_stall", "source_acquisition"))
    merged = _merge_diagnostics(a, b)
    assert merged.degraded_reasons == ("ingestion_stall", "source_acquisition")


# ── Repair stage ───────────────────────────────────────────────────


async def test_repair_stage_skipped_when_plan_skip_repair() -> None:
    """``plan.skip_repair=True`` (backfills) bypasses the sweep entirely."""
    plan = RunPlan(profile="alpha", kind=RunKind.BACKFILL, skip_repair=True)
    config = _make_config()
    client = AsyncMock()

    result, diagnostics = await _run_repair_stage(plan, client=client, config=config)

    assert result.candidates_visited == 0
    assert diagnostics.health_actions == ()
    client.assert_not_awaited()


async def test_repair_stage_emits_clear_action_on_success() -> None:
    """Successful sweep returns a ``clear`` HealthAction for the repair latch."""
    plan = _scheduled_plan()
    config = _make_config()
    client = AsyncMock()

    with patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]):
        result, diagnostics = await _run_repair_stage(
            plan, client=client, config=config
        )

    assert result.candidates_visited == 0
    assert len(diagnostics.health_actions) == 1
    action = diagnostics.health_actions[0]
    assert action.op == "clear"
    assert action.latch == "repair_write_failure"


async def test_repair_stage_raises_run_aborted_on_sweep_write_error() -> None:
    """``SweepWriteError`` becomes ``RunAborted`` carrying a flip action."""
    plan = _scheduled_plan()
    config = _make_config()
    client = AsyncMock()

    async def failing_sweep(*args: Any, **kwargs: Any) -> list[Any]:
        raise SweepWriteError("abort")

    with (
        patch("influx.run.repair_sweep", side_effect=failing_sweep),
        pytest.raises(RunAborted) as excinfo,
    ):
        await _run_repair_stage(plan, client=client, config=config)

    assert excinfo.value.reason == "repair_write_failure"
    actions = excinfo.value.diagnostics.health_actions
    assert len(actions) == 1
    assert actions[0].op == "flip"
    assert actions[0].latch == "repair_write_failure"
    # Original SweepWriteError is preserved as the cause.
    assert isinstance(excinfo.value.__cause__, SweepWriteError)


# ── Run.execute() end-to-end ───────────────────────────────────────


async def _empty_provider(
    profile: str, kind: RunKind, run_range: Any, filter_prompt: str
) -> list[dict[str, Any]]:
    return []


async def _empty_neg_block(*args: Any, **kwargs: Any) -> str:
    return ""


async def test_run_execute_happy_path_returns_outcome() -> None:
    """Happy path: empty provider → outcome with zero ingested, no error."""
    config = _make_config()
    plan = _scheduled_plan()
    deps = RunDeps(config=config, item_provider=_empty_provider, probe_loop=None)
    client = _NoopClient()

    with (
        patch("influx.run.LithosClient", return_value=client),
        patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]),
        patch("influx.run.build_negative_examples_block", side_effect=_empty_neg_block),
        patch("influx.service.post_run_webhook_hook"),
    ):
        outcome = await Run(plan, deps).execute()

    assert outcome.sources_checked == 0
    assert outcome.ingested == 0
    assert outcome.profile_run_result is not None
    assert outcome.profile_run_result.profile == "alpha"
    tools = [c[0] for c in client.calls]
    assert tools == ["task_create", "task_complete"]
    assert client.calls[1][1]["outcome"] == "success"


async def test_run_execute_propagates_sweep_write_error_with_health_action() -> None:
    """SweepWriteError flips ``repair_write_failure`` and re-raises the original."""
    config = _make_config()
    plan = _scheduled_plan()

    class _ProbeLoop:
        def __init__(self) -> None:
            self.marked = False
            self.cleared = False

        def mark_repair_write_failure(
            self, *, profile: str = "", detail: str = ""
        ) -> None:
            self.marked = True

        def clear_repair_write_failure(self) -> None:
            self.cleared = True

    probe_loop = _ProbeLoop()
    deps = RunDeps(config=config, item_provider=_empty_provider, probe_loop=probe_loop)
    client = _NoopClient()

    async def failing_sweep(*args: Any, **kwargs: Any) -> list[Any]:
        raise SweepWriteError("abort")

    with (
        patch("influx.run.LithosClient", return_value=client),
        patch("influx.run.repair_sweep", side_effect=failing_sweep),
        pytest.raises(SweepWriteError),
    ):
        await Run(plan, deps).execute()

    assert probe_loop.marked is True
    assert probe_loop.cleared is False
    complete_call = next(c for c in client.calls if c[0] == "task_complete")
    assert complete_call[1]["outcome"] == "error"


async def test_run_execute_clears_repair_latch_on_success() -> None:
    """Successful sweep clears the repair latch via the HealthAction path."""
    config = _make_config()
    plan = _scheduled_plan()

    class _ProbeLoop:
        def __init__(self) -> None:
            self.cleared = False

        def mark_repair_write_failure(
            self, *, profile: str = "", detail: str = ""
        ) -> None:
            pass

        def clear_repair_write_failure(self) -> None:
            self.cleared = True

    probe_loop = _ProbeLoop()
    deps = RunDeps(config=config, item_provider=_empty_provider, probe_loop=probe_loop)
    client = _NoopClient()

    with (
        patch("influx.run.LithosClient", return_value=client),
        patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]),
        patch("influx.run.build_negative_examples_block", side_effect=_empty_neg_block),
        patch("influx.service.post_run_webhook_hook"),
    ):
        await Run(plan, deps).execute()

    assert probe_loop.cleared is True


async def test_run_execute_skips_repair_for_backfill() -> None:
    """``plan.skip_repair=True`` short-circuits the repair sweep."""
    config = _make_config()
    plan = RunPlan(profile="alpha", kind=RunKind.BACKFILL, skip_repair=True)
    deps = RunDeps(config=config, item_provider=_empty_provider, probe_loop=None)
    client = _NoopClient()

    sweep_mock = AsyncMock()
    with (
        patch("influx.run.LithosClient", return_value=client),
        patch("influx.run.repair_sweep", new=sweep_mock),
        patch("influx.run.build_negative_examples_block", side_effect=_empty_neg_block),
        patch("influx.service.post_run_webhook_hook"),
    ):
        await Run(plan, deps).execute()

    sweep_mock.assert_not_awaited()
    create_call = next(c for c in client.calls if c[0] == "task_create")
    assert "influx:backfill" in create_call[1]["tags"]


async def test_run_execute_walks_provider_and_writes_per_item() -> None:
    """End-to-end: provider yields items → cache_lookup + write_note + lcma_wire."""
    config = _make_config()
    plan = _scheduled_plan()

    items = [
        {
            "title": "Test Paper",
            "source_url": "https://arxiv.org/abs/2401.00001",
            "content": "# Summary\n\nbody",
            "tags": ["profile:alpha", "source:arxiv"],
            "confidence": 0.9,
            "score": 9,
            "path": "papers/arxiv/2024/01",
            "abstract_or_summary": "abs",
        }
    ]

    async def provider(
        profile: str, kind: RunKind, run_range: Any, filter_prompt: str
    ) -> list[dict[str, Any]]:
        return items

    from mcp import types as mcp_types

    deps = RunDeps(config=config, item_provider=provider, probe_loop=None)

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.list_archive_terminal_arxiv_ids = AsyncMock(return_value=frozenset())
    mock_client.task_create = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps({"task_id": "task-1"})
                )
            ]
        )
    )
    mock_client.task_complete = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps({"status": "completed"})
                )
            ]
        )
    )
    mock_client.cache_lookup_for_item = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text='{"hit": false}')]
        )
    )
    write_result = MagicMock()
    write_result.status = "created"
    write_result.note_id = "note-new"
    mock_client.write_note = AsyncMock(return_value=write_result)

    with (
        patch("influx.run.LithosClient", return_value=mock_client),
        patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]),
        patch("influx.run.build_negative_examples_block", side_effect=_empty_neg_block),
        patch("influx.run.lcma_wire", new_callable=AsyncMock, return_value=[]) as wire,
        patch("influx.service.post_run_webhook_hook"),
    ):
        outcome = await Run(plan, deps).execute()

    assert outcome.sources_checked == 1
    assert outcome.ingested == 1
    mock_client.write_note.assert_awaited_once()
    wire.assert_awaited_once()


def test_run_aborted_carries_diagnostics_for_apply() -> None:
    """``RunAborted.diagnostics`` is the channel for abort-path health actions."""
    diagnostics = StageDiagnostics(
        health_actions=(
            HealthAction(op="flip", latch="repair_write_failure", detail="x"),
        ),
        degraded_reasons=("source_acquisition",),
    )
    exc = RunAborted("repair_write_failure", diagnostics)

    assert exc.reason == "repair_write_failure"
    assert exc.diagnostics is diagnostics
    assert exc.diagnostics.health_actions[0].latch == "repair_write_failure"
