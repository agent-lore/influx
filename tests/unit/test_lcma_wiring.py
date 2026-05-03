"""Unit tests for the post-write LCMA wiring entry point (issue #54).

Covers the three issue acceptance areas:

- ``related_to`` edge_score threshold honoured by ``lithos_retrieve``
  scoring (FR-LCMA-3, AC-M2-5/6).
- Tier 3 ``builds_on`` resolution path via ``lithos_cache_lookup``
  (FR-LCMA-4, AC-M2-7/8).
- Unknown LCMA tool latches the ``lcma_unknown_tool_failure`` flag
  (FR-LCMA-6) and re-raises so the run aborts.

Lower-level retrieve / cache-lookup primitives are exercised in
``influx.lcma`` tests; these tests focus on the wiring seam.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from influx.errors import LCMAError
from influx.lcma_wiring import CascadeOutput, LcmaWiringDeps, wire


def _mcp_text_result(payload: dict[str, Any]) -> MagicMock:
    """Build a fake MCP-style result whose ``content[0].text`` is JSON."""
    text_content = MagicMock()
    text_content.text = json.dumps(payload)
    result = MagicMock()
    result.content = [text_content]
    return result


def _make_client(
    *,
    retrieve_payload: dict[str, Any] | None = None,
    cache_lookup_payload: dict[str, Any] | None = None,
) -> AsyncMock:
    """Build an AsyncMock LithosClient with the listed call returns."""
    client = AsyncMock()
    client.retrieve = AsyncMock(
        return_value=_mcp_text_result(retrieve_payload or {"results": []})
    )
    client.cache_lookup = AsyncMock(
        return_value=_mcp_text_result(cache_lookup_payload or {"hit": False})
    )
    client.edge_upsert = AsyncMock()
    return client


def _make_deps(
    client: AsyncMock,
    *,
    lcma_edge_score: float = 0.75,
    on_unknown_tool: Any = None,
) -> LcmaWiringDeps:
    return LcmaWiringDeps(
        client=client,
        profile="research",
        run_task_id="task-1",
        lcma_edge_score=lcma_edge_score,
        on_unknown_tool=on_unknown_tool,
    )


# ── related_to edge-score threshold ────────────────────────────────


class TestRelatedToEdgeScoreThreshold:
    """Results scoring at or above ``lcma_edge_score`` upsert ``related_to``."""

    @pytest.mark.asyncio
    async def test_above_threshold_upserts_edge(self) -> None:
        client = _make_client(
            retrieve_payload={
                "results": [
                    {
                        "title": "Prior",
                        "score": 0.9,
                        "note_id": "note-prior",
                        "receipt_id": "rcpt-1",
                    }
                ]
            }
        )
        deps = _make_deps(client, lcma_edge_score=0.75)

        related = await wire(
            written_note_id="note-new",
            cascade=CascadeOutput(title="A Paper", contributions=["c1"]),
            deps=deps,
        )

        assert related == [{"title": "Prior", "score": 0.9}]
        client.edge_upsert.assert_awaited_once()
        call = client.edge_upsert.await_args
        assert call.kwargs["type"] == "related_to"
        assert call.kwargs["source_note_id"] == "note-new"
        assert call.kwargs["target_note_id"] == "note-prior"
        assert call.kwargs["evidence"]["score"] == 0.9

    @pytest.mark.asyncio
    async def test_below_threshold_skips_edge(self) -> None:
        client = _make_client(
            retrieve_payload={
                "results": [
                    {"title": "Weak match", "score": 0.5, "note_id": "note-weak"},
                ]
            }
        )
        deps = _make_deps(client, lcma_edge_score=0.75)

        related = await wire(
            written_note_id="note-new",
            cascade=CascadeOutput(title="A Paper"),
            deps=deps,
        )

        assert related == []
        client.edge_upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_threshold_boundary_is_inclusive(self) -> None:
        """A result scoring exactly at the threshold is kept (>=, not >)."""
        client = _make_client(
            retrieve_payload={
                "results": [
                    {"title": "Edge", "score": 0.75, "note_id": "note-edge"},
                ]
            }
        )
        deps = _make_deps(client, lcma_edge_score=0.75)

        related = await wire(
            written_note_id="note-new",
            cascade=CascadeOutput(title="A Paper"),
            deps=deps,
        )

        assert related == [{"title": "Edge", "score": 0.75}]
        client.edge_upsert.assert_awaited_once()


# ── Tier 3 builds_on resolution ────────────────────────────────────


class TestBuildsOnResolution:
    """Tier 3 ``builds_on`` items resolve via ``lithos_cache_lookup``."""

    @pytest.mark.asyncio
    async def test_arxiv_match_upserts_builds_on_edge(self) -> None:
        client = _make_client(
            cache_lookup_payload={
                "hit": True,
                "source_url": "https://arxiv.org/abs/2412.12345",
                "note_id": "note-prior",
            }
        )
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["FooNet (arXiv:2412.12345)"],
            ),
            deps=deps,
        )

        client.cache_lookup.assert_awaited_once()
        # Find the builds_on edge among the upsert calls.
        upserts = [
            call.kwargs
            for call in client.edge_upsert.await_args_list
            if call.kwargs.get("type") == "builds_on"
        ]
        assert len(upserts) == 1
        assert upserts[0]["source_note_id"] == "note-new"
        assert upserts[0]["target_note_id"] == "note-prior"

    @pytest.mark.asyncio
    async def test_no_arxiv_id_skips_lookup(self) -> None:
        client = _make_client()
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["A handwave reference with no id"],
            ),
            deps=deps,
        )

        client.cache_lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_skips_edge(self) -> None:
        client = _make_client(cache_lookup_payload={"hit": False})
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["FooNet (arXiv:2412.12345)"],
            ),
            deps=deps,
        )

        client.cache_lookup.assert_awaited_once()
        builds_on_upserts = [
            call.kwargs
            for call in client.edge_upsert.await_args_list
            if call.kwargs.get("type") == "builds_on"
        ]
        assert builds_on_upserts == []

    @pytest.mark.asyncio
    async def test_source_url_mismatch_skips_edge(self) -> None:
        """AC-M2-8: cache hit with a different source_url is treated as miss."""
        client = _make_client(
            cache_lookup_payload={
                "hit": True,
                "source_url": "https://arxiv.org/abs/9999.99999",  # different paper
                "note_id": "note-other",
            }
        )
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["FooNet (arXiv:2412.12345)"],
            ),
            deps=deps,
        )

        builds_on_upserts = [
            call.kwargs
            for call in client.edge_upsert.await_args_list
            if call.kwargs.get("type") == "builds_on"
        ]
        assert builds_on_upserts == []

    @pytest.mark.asyncio
    async def test_empty_builds_on_is_noop(self) -> None:
        client = _make_client()
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            cascade=CascadeOutput(title="A Paper", builds_on=None),
            deps=deps,
        )

        client.cache_lookup.assert_not_awaited()


# ── Unknown LCMA tool latch ────────────────────────────────────────


class TestUnknownToolLatch:
    """Unknown LCMA tools latch the readiness flag and re-raise."""

    @pytest.mark.asyncio
    async def test_unknown_tool_on_retrieve_latches(self) -> None:
        client = _make_client()
        client.retrieve = AsyncMock(
            side_effect=LCMAError("unknown_tool", stage="lithos_retrieve")
        )
        latched: list[dict[str, str]] = []

        def fake_latch(*, profile: str, detail: str) -> None:
            latched.append({"profile": profile, "detail": detail})

        deps = _make_deps(client, on_unknown_tool=fake_latch)

        with pytest.raises(LCMAError):
            await wire(
                written_note_id="note-new",
                cascade=CascadeOutput(title="A Paper"),
                deps=deps,
            )

        assert latched == [
            {"profile": "research", "detail": "tool='lithos_retrieve'"},
        ]

    @pytest.mark.asyncio
    async def test_unknown_tool_on_cache_lookup_latches(self) -> None:
        client = _make_client()
        client.cache_lookup = AsyncMock(
            side_effect=LCMAError("unknown_tool", stage="lithos_cache_lookup")
        )
        latched: list[dict[str, str]] = []

        def fake_latch(*, profile: str, detail: str) -> None:
            latched.append({"profile": profile, "detail": detail})

        deps = _make_deps(client, on_unknown_tool=fake_latch)

        with pytest.raises(LCMAError):
            await wire(
                written_note_id="note-new",
                cascade=CascadeOutput(
                    title="A Paper",
                    builds_on=["FooNet (arXiv:2412.12345)"],
                ),
                deps=deps,
            )

        assert latched == [
            {"profile": "research", "detail": "tool='lithos_cache_lookup'"},
        ]

    @pytest.mark.asyncio
    async def test_other_lcma_error_is_not_latched(self) -> None:
        """Non-unknown_tool LCMA errors propagate without latching."""
        client = _make_client()
        client.retrieve = AsyncMock(
            side_effect=LCMAError("transport refused", stage="http")
        )
        latched: list[dict[str, str]] = []

        def fake_latch(*, profile: str, detail: str) -> None:
            latched.append({"profile": profile, "detail": detail})

        deps = _make_deps(client, on_unknown_tool=fake_latch)

        with pytest.raises(LCMAError):
            await wire(
                written_note_id="note-new",
                cascade=CascadeOutput(title="A Paper"),
                deps=deps,
            )

        assert latched == []  # no latch for non-unknown_tool error

    @pytest.mark.asyncio
    async def test_unknown_tool_without_latch_callback_still_reraises(self) -> None:
        """The latch callback is optional; absence does not swallow the error."""
        client = _make_client()
        client.retrieve = AsyncMock(side_effect=LCMAError("unknown_tool"))

        deps = _make_deps(client, on_unknown_tool=None)

        with pytest.raises(LCMAError):
            await wire(
                written_note_id="note-new",
                cascade=CascadeOutput(title="A Paper"),
                deps=deps,
            )
