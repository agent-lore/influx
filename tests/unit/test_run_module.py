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
from pathlib import Path
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
from influx.run_ledger import RunLedger
from influx.source import BoundScoredCandidate, Candidate, ScoredCandidate


def _bound_for(
    item: dict[str, Any], *, source_label: str = "arxiv"
) -> BoundScoredCandidate:
    """Wrap a ProfileItem dict as a BoundScoredCandidate whose acquire
    closure returns the dict (#125 — the Run stage now drives acquire
    after pre-acquire dedup, so test providers yield bounds rather than
    pre-baked items).
    """

    async def _acquire() -> dict[str, Any]:
        return item

    return BoundScoredCandidate(
        scored=ScoredCandidate(
            candidate=Candidate(
                item_id=item.get("source_url", "test-id"),
                title=item.get("title", ""),
                abstract=item.get("abstract_or_summary", "") or "",
                source_url=item.get("source_url", ""),
            ),
            score=int(item.get("score", 0)),
            confidence=float(item.get("confidence", 0.0)),
            reason=item.get("reason", ""),
            filter_tags=tuple(item.get("filter_tags", [])),
        ),
        acquire=_acquire,
        source_label=source_label,
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

    async def task_create_body(self, **kwargs: Any) -> dict[str, Any]:
        await self.task_create(**kwargs)
        return {"task_id": "task-1"}

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

    async def reconnect(self) -> None:
        self.calls.append(("reconnect", {}))


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
) -> list[BoundScoredCandidate]:
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
        patch(
            "influx.feedback.build_negative_examples_block",
            side_effect=_empty_neg_block,
        ),
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


async def test_run_execute_retries_task_complete_after_reconnect() -> None:
    """Successful runs retry final task completion once after reconnect."""
    config = _make_config()
    plan = _scheduled_plan()
    deps = RunDeps(config=config, item_provider=_empty_provider, probe_loop=None)
    client = _NoopClient()

    completion_attempts = 0

    async def flaky_task_complete(**kwargs: Any) -> Any:
        nonlocal completion_attempts
        completion_attempts += 1
        client.calls.append(("task_complete", kwargs))
        if completion_attempts == 1:
            raise RuntimeError("sse dropped")
        from mcp import types as mcp_types

        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps({"status": "completed"})
                )
            ]
        )

    client.task_complete = flaky_task_complete

    with (
        patch("influx.run.LithosClient", return_value=client),
        patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]),
        patch(
            "influx.feedback.build_negative_examples_block",
            side_effect=_empty_neg_block,
        ),
        patch("influx.service.post_run_webhook_hook"),
    ):
        outcome = await Run(plan, deps).execute()

    assert outcome.ingested == 0
    assert [call[0] for call in client.calls] == [
        "task_create",
        "task_complete",
        "reconnect",
        "task_complete",
    ]


async def test_run_execute_suppresses_terminal_task_complete_failure_after_retry() -> (
    None
):
    """Successful ingest remains successful when both completion attempts fail."""
    config = _make_config()
    plan = _scheduled_plan()
    deps = RunDeps(config=config, item_provider=_empty_provider, probe_loop=None)
    client = _NoopClient()

    async def failing_task_complete(**kwargs: Any) -> Any:
        client.calls.append(("task_complete", kwargs))
        raise RuntimeError("sse dropped")

    client.task_complete = failing_task_complete

    with (
        patch("influx.run.LithosClient", return_value=client),
        patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]),
        patch(
            "influx.feedback.build_negative_examples_block",
            side_effect=_empty_neg_block,
        ),
        patch("influx.service.post_run_webhook_hook"),
    ):
        outcome = await Run(plan, deps).execute()

    assert outcome.ingested == 0
    assert [call[0] for call in client.calls] == [
        "task_create",
        "task_complete",
        "reconnect",
        "task_complete",
    ]


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
        patch(
            "influx.feedback.build_negative_examples_block",
            side_effect=_empty_neg_block,
        ),
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
        patch(
            "influx.feedback.build_negative_examples_block",
            side_effect=_empty_neg_block,
        ),
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
    ) -> list[BoundScoredCandidate]:
        return [_bound_for(it) for it in items]

    from mcp import types as mcp_types

    deps = RunDeps(config=config, item_provider=provider, probe_loop=None)

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.list_archive_terminal_arxiv_ids = AsyncMock(return_value=frozenset())
    mock_client.task_create_body = AsyncMock(return_value={"task_id": "task-1"})
    mock_client.task_complete = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps({"status": "completed"})
                )
            ]
        )
    )
    mock_client.cache_lookup_for_item_body = AsyncMock(return_value={"hit": False})
    mock_client.cache_lookup_by_url_body = AsyncMock(return_value={"hit": False})
    write_result = MagicMock()
    write_result.status = "created"
    write_result.note_id = "note-new"
    mock_client.write_note = AsyncMock(return_value=write_result)

    with (
        patch("influx.run.LithosClient", return_value=mock_client),
        patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]),
        patch(
            "influx.feedback.build_negative_examples_block",
            side_effect=_empty_neg_block,
        ),
        patch("influx.run.lcma_wire", new_callable=AsyncMock, return_value=[]) as wire,
        patch("influx.service.post_run_webhook_hook"),
    ):
        outcome = await Run(plan, deps).execute()

    assert outcome.sources_checked == 1
    assert outcome.ingested == 1
    mock_client.write_note.assert_awaited_once()
    wire.assert_awaited_once()


async def test_run_execute_threads_tags_source_url_confidence_to_lithos_write() -> None:
    """Regression test for the 2026-05-05→07 writer bug (#179).

    Between 2026-05-05 and 2026-05-07, ~245 Influx-authored docs were
    persisted to staging Lithos with empty doc-level ``tags`` /
    ``source_url`` / ``confidence`` despite the source builders setting
    those fields on the ``ProfileItem`` dict and the renderer emitting
    them inside an embedded YAML frontmatter block.  The fix landed by
    2026-05-08 but the exact commit was never pinned — see #179 for
    investigation notes.  This test locks in the call shape so a future
    refactor cannot silently re-introduce the bug: any drift between
    the ``item`` dict's structured fields and the kwargs delivered to
    ``LithosClient.write_note`` would fail this assertion.

    The check is at the ``write_note`` boundary, not deeper at
    ``call_tool``, because ``write_note`` was always correct (we
    verified — see #176's audit-stage investigation); the dropped path
    is the cascade between the source builder's return dict and the
    ``write_note(...)`` call site in ``_run_ingest_stage`` (run.py:602).
    """
    config = _make_config()
    plan = _scheduled_plan()

    item: dict[str, Any] = {
        "title": "Regression Test Paper",
        "source_url": "https://arxiv.org/abs/2401.99999",
        "content": "# Summary\n\nbody",
        "tags": [
            "profile:alpha",
            "source:arxiv",
            "arxiv-id:2401.99999",
            "ingested-by:influx",
            "schema:1",
        ],
        "confidence": 0.85,
        "score": 9,
        "path": "papers/arxiv/2024/01",
        "abstract_or_summary": "abs",
    }

    async def provider(
        profile: str, kind: RunKind, run_range: Any, filter_prompt: str
    ) -> list[BoundScoredCandidate]:
        return [_bound_for(item)]

    from mcp import types as mcp_types

    deps = RunDeps(config=config, item_provider=provider, probe_loop=None)

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.list_archive_terminal_arxiv_ids = AsyncMock(return_value=frozenset())
    mock_client.task_create_body = AsyncMock(return_value={"task_id": "task-1"})
    mock_client.task_complete = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps({"status": "completed"})
                )
            ]
        )
    )
    mock_client.cache_lookup_for_item_body = AsyncMock(return_value={"hit": False})
    mock_client.cache_lookup_by_url_body = AsyncMock(return_value={"hit": False})
    write_result = MagicMock()
    write_result.status = "created"
    write_result.note_id = "note-new"
    mock_client.write_note = AsyncMock(return_value=write_result)

    with (
        patch("influx.run.LithosClient", return_value=mock_client),
        patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]),
        patch(
            "influx.feedback.build_negative_examples_block",
            side_effect=_empty_neg_block,
        ),
        patch("influx.run.lcma_wire", new_callable=AsyncMock, return_value=[]),
        patch("influx.service.post_run_webhook_hook"),
    ):
        await Run(plan, deps).execute()

    # Pin the call shape.  Each assertion targets one structured field
    # the historical bug dropped between the source-builder ``item``
    # dict and the MCP-bound ``write_note`` kwargs.
    mock_client.write_note.assert_awaited_once()
    kwargs = mock_client.write_note.await_args.kwargs

    # title flows from the item.
    assert kwargs["title"] == "Regression Test Paper"

    # path flows from the item (used by Lithos to compute the storage
    # location; an empty path here was one observable shape of the bug).
    assert kwargs["path"] == "papers/arxiv/2024/01"

    # source_url is the canonical dedup key — empty source_url at
    # doc level was the primary observable symptom of the bug.
    assert kwargs["source_url"] == "https://arxiv.org/abs/2401.99999"

    # tags is the full pre-merge tag list.  Catches the historical
    # shape where ``item.get('tags', [])`` returned ``[]`` because
    # tags weren't attached to the item correctly.
    assert kwargs["tags"] == [
        "profile:alpha",
        "source:arxiv",
        "arxiv-id:2401.99999",
        "ingested-by:influx",
        "schema:1",
    ]

    # confidence flows through as a float (the bug shape had this
    # default to 0.0 in the persisted doc).
    assert kwargs["confidence"] == 0.85

    # content is the rendered note body that was passed in.
    assert kwargs["content"] == "# Summary\n\nbody"


async def test_run_execute_continues_after_lcma_wiring_failure() -> None:
    """A post-write LCMA failure does not prevent later items from ingesting."""
    config = _make_config()
    plan = _scheduled_plan()

    items = [
        {
            "title": "First Paper",
            "source_url": "https://arxiv.org/abs/2401.00001",
            "content": "# Summary\n\nbody",
            "tags": ["profile:alpha", "source:arxiv"],
            "confidence": 0.9,
            "score": 9,
            "path": "papers/arxiv/2024/01",
            "abstract_or_summary": "abs",
        },
        {
            "title": "Second Paper",
            "source_url": "https://arxiv.org/abs/2401.00002",
            "content": "# Summary\n\nbody",
            "tags": ["profile:alpha", "source:arxiv"],
            "confidence": 0.9,
            "score": 9,
            "path": "papers/arxiv/2024/01",
            "abstract_or_summary": "abs",
        },
    ]

    async def provider(
        profile: str, kind: RunKind, run_range: Any, filter_prompt: str
    ) -> list[BoundScoredCandidate]:
        return [_bound_for(it) for it in items]

    from mcp import types as mcp_types

    deps = RunDeps(config=config, item_provider=provider, probe_loop=None)

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.list_archive_terminal_arxiv_ids = AsyncMock(return_value=frozenset())
    mock_client.task_create_body = AsyncMock(return_value={"task_id": "task-1"})
    mock_client.task_complete = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps({"status": "completed"})
                )
            ]
        )
    )
    mock_client.cache_lookup_for_item_body = AsyncMock(return_value={"hit": False})
    mock_client.cache_lookup_by_url_body = AsyncMock(return_value={"hit": False})
    write_result_1 = MagicMock()
    write_result_1.status = "created"
    write_result_1.note_id = "note-1"
    write_result_2 = MagicMock()
    write_result_2.status = "created"
    write_result_2.note_id = "note-2"
    mock_client.write_note = AsyncMock(side_effect=[write_result_1, write_result_2])

    with (
        patch("influx.run.LithosClient", return_value=mock_client),
        patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]),
        patch(
            "influx.feedback.build_negative_examples_block",
            side_effect=_empty_neg_block,
        ),
        patch(
            "influx.run.lcma_wire",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("retrieve failed"), []],
        ) as wire,
        patch("influx.service.post_run_webhook_hook"),
    ):
        outcome = await Run(plan, deps).execute()

    assert outcome.sources_checked == 2
    assert outcome.ingested == 2
    assert wire.await_count == 2


async def test_run_execute_persists_unresolved_slug_collision_without_injected_ledger(
    tmp_path: Path,
) -> None:
    """Slug-collision backlog still persists when ``RunDeps.ledger`` is ``None``."""
    from mcp import types as mcp_types

    config = _make_config().model_copy(
        update={
            "storage": _make_config().storage.model_copy(
                update={"state_dir": tmp_path / "state"}
            )
        }
    )
    plan = _scheduled_plan()

    items = [
        {
            "title": "Colliding Paper",
            "source_url": "https://example.com/paper",
            "content": "# Summary\n\nbody",
            "tags": ["profile:alpha", "source:rss"],
            "confidence": 0.9,
            "score": 9,
            "path": "papers/rss/2026/05",
            "abstract_or_summary": "abs",
        }
    ]

    async def provider(
        profile: str, kind: RunKind, run_range: Any, filter_prompt: str
    ) -> list[BoundScoredCandidate]:
        return [_bound_for(it) for it in items]

    deps = RunDeps(config=config, item_provider=provider, probe_loop=None, ledger=None)

    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    mock_client.list_archive_terminal_arxiv_ids = AsyncMock(return_value=frozenset())
    mock_client.task_create_body = AsyncMock(return_value={"task_id": "task-1"})
    mock_client.task_complete = AsyncMock(
        return_value=mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(
                    type="text", text=json.dumps({"status": "completed"})
                )
            ]
        )
    )
    mock_client.cache_lookup_for_item_body = AsyncMock(return_value={"hit": False})
    mock_client.cache_lookup_by_url_body = AsyncMock(return_value={"hit": False})
    write_result = MagicMock()
    write_result.status = "slug_collision"
    write_result.note_id = ""
    write_result.detail = "first_existing_id=doc-a; retry_existing_id=doc-b"
    mock_client.write_note = AsyncMock(return_value=write_result)

    with (
        patch("influx.run.LithosClient", return_value=mock_client),
        patch("influx.run.repair_sweep", new_callable=AsyncMock, return_value=[]),
        patch(
            "influx.feedback.build_negative_examples_block",
            side_effect=_empty_neg_block,
        ),
        patch("influx.service.post_run_webhook_hook"),
    ):
        outcome = await Run(plan, deps).execute()

    assert outcome.ingested == 0
    backlog = RunLedger(Path(config.storage.state_dir)).unresolved_slug_collisions()
    assert len(backlog) == 1
    assert backlog[0]["title"] == "Colliding Paper"
    assert backlog[0]["source"] == "rss"


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
