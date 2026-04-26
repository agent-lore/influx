"""End-to-end integration tests for LCMA task bracketing and edges (PRD 08).

Exercises ``run_profile`` against a fake Lithos server that supports the
four LCMA tools (task_create, task_complete, retrieve, edge_upsert) and
verifies the task-bracketing contract (FR-LCMA-5, AC-M2-10) and edge
wiring (FR-LCMA-2, FR-LCMA-3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any

import pytest

from influx.config import (
    AppConfig,
    ArxivSourceConfig,
    ExtractionConfig,
    FeedbackConfig,
    LithosConfig,
    NotificationsConfig,
    ProfileConfig,
    ProfileSources,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.coordinator import RunKind
from influx.scheduler import run_profile
from tests.contract.test_lithos_client import FakeLithosServer

# ── Helpers ────────────────────────────────────────────────────────


def _make_config(lithos_url: str) -> AppConfig:
    """Build an AppConfig with one profile for LCMA integration tests."""
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="Robotics papers",
                thresholds=ProfileThresholds(
                    relevance=100,
                    full_text=8,
                    deep_extract=100,
                    notify_immediate=8,
                    lcma_edge_score=0.75,
                ),
                sources=ProfileSources(
                    arxiv=ArxivSourceConfig(
                        enabled=True,
                        categories=["cs.RO"],
                        max_results_per_category=10,
                        lookback_days=30,
                    ),
                ),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(),
        feedback=FeedbackConfig(),
    )


def _calls_by_tool(
    calls: list[tuple[str, dict[str, Any]]], tool: str
) -> list[dict[str, Any]]:
    """Filter recorded calls by tool name, returning just the args dicts."""
    return [args for name, args in calls if name == tool]


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def fake_lithos() -> Generator[FakeLithosServer, None, None]:
    """Module-scoped fake Lithos MCP server with LCMA tools."""
    server = FakeLithosServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def fake_lithos_url(fake_lithos: FakeLithosServer) -> str:
    return f"http://127.0.0.1:{fake_lithos.port}/sse"


@pytest.fixture(autouse=True)
def clear_lithos(fake_lithos: FakeLithosServer) -> None:
    """Clear all recorded state before each test."""
    fake_lithos.calls.clear()
    fake_lithos.write_responses.clear()
    fake_lithos.read_responses.clear()
    fake_lithos.cache_lookup_responses.clear()
    fake_lithos.list_responses.clear()
    fake_lithos.retrieve_responses.clear()
    fake_lithos.edge_upsert_responses.clear()
    fake_lithos.task_create_responses.clear()
    fake_lithos.task_complete_responses.clear()


# ── US-004: Task bracketing ──────────────────────────────────────


class TestTaskBracketing:
    """Profile run produces exactly one task_create + one task_complete (AC-M2-10)."""

    def test_manual_run_brackets_with_task(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Manual run → one task_create + one task_complete with matching task_id."""
        config = _make_config(fake_lithos_url)

        asyncio.run(
            run_profile(
                "ai-robotics",
                RunKind.MANUAL,
                config=config,
            )
        )

        create_calls = _calls_by_tool(fake_lithos.calls, "lithos_task_create")
        complete_calls = _calls_by_tool(fake_lithos.calls, "lithos_task_complete")

        assert len(create_calls) == 1
        assert len(complete_calls) == 1

        # Verify task_create args (FR-LCMA-5).
        assert create_calls[0]["agent"] == "influx"
        assert "influx:run" in create_calls[0]["tags"]
        assert "profile:ai-robotics" in create_calls[0]["tags"]
        assert create_calls[0]["title"].startswith("Influx run ai-robotics ")

        # Verify task_complete args — matching task_id, agent, outcome.
        assert complete_calls[0]["task_id"] == "task-001"
        assert complete_calls[0]["agent"] == "influx"
        assert complete_calls[0]["outcome"] == "success"
        # tags must NOT be passed to task_complete.
        assert "tags" not in complete_calls[0]

    def test_scheduled_run_brackets_with_task(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Scheduled run also brackets with task_create/task_complete."""
        config = _make_config(fake_lithos_url)

        asyncio.run(
            run_profile(
                "ai-robotics",
                RunKind.SCHEDULED,
                config=config,
            )
        )

        create_calls = _calls_by_tool(fake_lithos.calls, "lithos_task_create")
        complete_calls = _calls_by_tool(fake_lithos.calls, "lithos_task_complete")

        assert len(create_calls) == 1
        assert len(complete_calls) == 1
        assert complete_calls[0]["task_id"] == "task-001"
        assert complete_calls[0]["outcome"] == "success"

    def test_backfill_run_does_not_bracket(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Backfill run does NOT call task_create or task_complete (PRD 08 §4)."""
        config = _make_config(fake_lithos_url)

        asyncio.run(
            run_profile(
                "ai-robotics",
                RunKind.BACKFILL,
                config=config,
            )
        )

        create_calls = _calls_by_tool(fake_lithos.calls, "lithos_task_create")
        complete_calls = _calls_by_tool(fake_lithos.calls, "lithos_task_complete")

        assert len(create_calls) == 0
        assert len(complete_calls) == 0

    def test_task_complete_called_on_error(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """task_complete fires with outcome="error" even when the run fails."""
        config = _make_config(fake_lithos_url)

        # Force a failure by providing an item provider that raises.
        async def _failing_provider(
            profile: str,
            kind: RunKind,
            run_range: Any,
            filter_prompt: str,
        ) -> list[dict[str, Any]]:
            raise RuntimeError("simulated provider failure")

        with pytest.raises(RuntimeError, match="simulated provider failure"):
            asyncio.run(
                run_profile(
                    "ai-robotics",
                    RunKind.MANUAL,
                    config=config,
                    item_provider=_failing_provider,
                )
            )

        create_calls = _calls_by_tool(fake_lithos.calls, "lithos_task_create")
        complete_calls = _calls_by_tool(fake_lithos.calls, "lithos_task_complete")

        assert len(create_calls) == 1
        assert len(complete_calls) == 1
        assert complete_calls[0]["task_id"] == "task-001"
        assert complete_calls[0]["outcome"] == "error"
