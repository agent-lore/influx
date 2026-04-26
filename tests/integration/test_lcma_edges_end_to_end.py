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


# ── Helpers: item provider for edge tests ─────────────────────────


def _single_item_provider(
    items: list[dict[str, Any]],
) -> Any:
    """Return an item provider that yields the given items once."""

    async def _provider(
        profile: str,
        kind: RunKind,
        run_range: Any,
        filter_prompt: str,
    ) -> list[dict[str, Any]]:
        del profile, kind, run_range, filter_prompt
        return items

    return _provider


# ── US-005: after_write retrieve + related_to edge wiring ─────────


class TestAfterWriteEdgeWiring:
    """Post-write LCMA hook calls retrieve + upserts related_to edges (AC-M2-5/6)."""

    def test_high_score_result_produces_edge(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """score >= 0.75 → one lithos_edge_upsert(type=related_to)."""
        import json as _json

        config = _make_config(fake_lithos_url)

        # Queue a retrieve response with one high-scoring result.
        fake_lithos.retrieve_responses.append(
            _json.dumps(
                {
                    "results": [
                        {
                            "title": "Related Paper A",
                            "score": 0.85,
                            "receipt_id": "rcpt-001",
                            "note_id": "note-related-001",
                        }
                    ]
                }
            )
        )

        items = [
            {
                "title": "New Robotics Paper",
                "source_url": "https://arxiv.org/abs/2601.00001",
                "content": "# Summary\nRobotics paper.",
                "tags": ["profile:ai-robotics", "source:arxiv"],
                "confidence": 0.9,
                "score": 9,
            }
        ]

        result = asyncio.run(
            run_profile(
                "ai-robotics",
                RunKind.MANUAL,
                config=config,
                item_provider=_single_item_provider(items),
            )
        )

        # Verify lithos_retrieve was called with correct args (AC-M2-5).
        retrieve_calls = _calls_by_tool(fake_lithos.calls, "lithos_retrieve")
        assert len(retrieve_calls) == 1
        assert retrieve_calls[0]["agent_id"] == "influx"
        assert retrieve_calls[0]["task_id"] == "task-001"
        assert retrieve_calls[0]["tags"] == ["profile:ai-robotics"]
        assert retrieve_calls[0]["limit"] == 5
        assert "New Robotics Paper" in retrieve_calls[0]["query"]

        # Verify lithos_edge_upsert was called with correct evidence (AC-M2-6).
        edge_calls = _calls_by_tool(fake_lithos.calls, "lithos_edge_upsert")
        assert len(edge_calls) == 1
        assert edge_calls[0]["type"] == "related_to"
        evidence = edge_calls[0]["evidence"]
        assert evidence["kind"] == "lithos_retrieve"
        assert evidence["score"] == 0.85
        assert evidence["receipt_id"] == "rcpt-001"

        # Verify the result carries related_in_lithos for webhook digest.
        assert result is not None
        assert len(result.items) == 1
        assert len(result.items[0].related_in_lithos) == 1
        assert result.items[0].related_in_lithos[0]["title"] == "Related Paper A"
        assert result.items[0].related_in_lithos[0]["score"] == 0.85

    def test_low_score_result_produces_no_edge(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Retrieve result with score < 0.75 → NO lithos_edge_upsert."""
        import json as _json

        config = _make_config(fake_lithos_url)

        # Queue a retrieve response with one low-scoring result.
        fake_lithos.retrieve_responses.append(
            _json.dumps(
                {
                    "results": [
                        {
                            "title": "Weakly Related Paper",
                            "score": 0.5,
                            "receipt_id": "rcpt-002",
                            "note_id": "note-weak-001",
                        }
                    ]
                }
            )
        )

        items = [
            {
                "title": "Another Paper",
                "source_url": "https://arxiv.org/abs/2601.00002",
                "content": "# Summary\nAnother paper.",
                "tags": ["profile:ai-robotics", "source:arxiv"],
                "confidence": 0.8,
                "score": 8,
            }
        ]

        result = asyncio.run(
            run_profile(
                "ai-robotics",
                RunKind.MANUAL,
                config=config,
                item_provider=_single_item_provider(items),
            )
        )

        # Retrieve was called.
        retrieve_calls = _calls_by_tool(fake_lithos.calls, "lithos_retrieve")
        assert len(retrieve_calls) == 1

        # No edge upserted for low-scoring result.
        edge_calls = _calls_by_tool(fake_lithos.calls, "lithos_edge_upsert")
        assert len(edge_calls) == 0

        # related_in_lithos is empty for the highlight.
        assert result is not None
        assert len(result.items) == 1
        assert result.items[0].related_in_lithos == []


# ── US-006: Tier 3 builds_on resolver ────────────────────────────


class TestBuildsOnResolver:
    """Tier 3 builds_on items resolved via lithos_cache_lookup (AC-M2-7/8)."""

    def test_cache_hit_produces_builds_on_edge(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Cache hit on arXiv URL → one lithos_edge_upsert(type=builds_on)."""
        import json as _json

        config = _make_config(fake_lithos_url)

        # Queue a retrieve response (from after_write).
        fake_lithos.retrieve_responses.append(
            _json.dumps({"results": []})
        )

        # Queue a cache_lookup hit for the builds_on arXiv URL.
        # First cache_lookup is the per-item dedup check (miss).
        fake_lithos.cache_lookup_responses.append(
            _json.dumps({"hit": False, "stale_exists": False})
        )
        # Second cache_lookup is the builds_on resolver (hit).
        fake_lithos.cache_lookup_responses.append(
            _json.dumps({
                "hit": True,
                "source_url": "https://arxiv.org/abs/2412.12345",
                "note_id": "note-foonet",
                "title": "FooNet",
            })
        )

        items = [
            {
                "title": "Paper That Builds On FooNet",
                "source_url": "https://arxiv.org/abs/2601.00010",
                "content": "# Summary\nExtends FooNet.",
                "tags": ["profile:ai-robotics", "source:arxiv"],
                "confidence": 0.9,
                "score": 9,
                "builds_on": ["FooNet (arXiv:2412.12345)"],
            }
        ]

        asyncio.run(
            run_profile(
                "ai-robotics",
                RunKind.MANUAL,
                config=config,
                item_provider=_single_item_provider(items),
            )
        )

        # Verify cache_lookup was called with correct args (FR-MCP-3, R-7).
        cache_calls = _calls_by_tool(fake_lithos.calls, "lithos_cache_lookup")
        # One for dedup + one for builds_on.
        assert len(cache_calls) == 2
        builds_on_lookup = cache_calls[1]
        assert builds_on_lookup["query"] == "FooNet"
        assert builds_on_lookup["source_url"] == "https://arxiv.org/abs/2412.12345"

        # Verify lithos_edge_upsert(type=builds_on) was called (AC-M2-7).
        edge_calls = _calls_by_tool(fake_lithos.calls, "lithos_edge_upsert")
        builds_on_edges = [c for c in edge_calls if c["type"] == "builds_on"]
        assert len(builds_on_edges) == 1
        assert builds_on_edges[0]["evidence"] == {
            "kind": "tier3_builds_on_extraction"
        }

    def test_cache_miss_produces_no_builds_on_edge(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Cache miss on arXiv URL → zero builds_on edges (AC-M2-8)."""
        import json as _json

        config = _make_config(fake_lithos_url)

        # Queue a retrieve response (from after_write).
        fake_lithos.retrieve_responses.append(
            _json.dumps({"results": []})
        )

        # Dedup cache_lookup (miss).
        fake_lithos.cache_lookup_responses.append(
            _json.dumps({"hit": False, "stale_exists": False})
        )
        # builds_on cache_lookup (also miss — default response).

        items = [
            {
                "title": "Paper With Unknown Reference",
                "source_url": "https://arxiv.org/abs/2601.00011",
                "content": "# Summary\nReferences unknown work.",
                "tags": ["profile:ai-robotics", "source:arxiv"],
                "confidence": 0.8,
                "score": 8,
                "builds_on": ["FooNet (arXiv:2412.99999)"],
            }
        ]

        asyncio.run(
            run_profile(
                "ai-robotics",
                RunKind.MANUAL,
                config=config,
                item_provider=_single_item_provider(items),
            )
        )

        # builds_on cache_lookup was called.
        cache_calls = _calls_by_tool(fake_lithos.calls, "lithos_cache_lookup")
        assert len(cache_calls) == 2

        # No builds_on edges upserted.
        edge_calls = _calls_by_tool(fake_lithos.calls, "lithos_edge_upsert")
        builds_on_edges = [c for c in edge_calls if c["type"] == "builds_on"]
        assert len(builds_on_edges) == 0
