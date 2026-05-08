"""Unit tests for the post-write LCMA wiring entry point (issue #54).

Covers the two acceptance areas that remain after #69 lifted the
unknown-tool latch out of the per-call path:

- ``related_to`` edge_score threshold honoured by ``lithos_retrieve``
  scoring (FR-LCMA-3, AC-M2-5/6).
- Tier 3 ``builds_on`` resolution path via ``lithos_cache_lookup``
  (FR-LCMA-4, AC-M2-7/8).

Mid-run ``LCMAError("unknown_tool")`` propagates as a plain exception
now; latch flipping happens at probe time (see ``test_probes.py``).

Lower-level retrieve / cache-lookup primitives are exercised in
``influx.lcma`` tests; these tests focus on the wiring seam.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from influx.errors import LCMAError
from influx.lcma_wiring import CascadeOutput, LcmaWiringDeps, wire


def _make_client(
    *,
    retrieve_payload: dict[str, Any] | None = None,
    cache_lookup_payload: dict[str, Any] | None = None,
) -> AsyncMock:
    """Build an AsyncMock LithosClient with the listed call returns."""
    client = AsyncMock()
    client.retrieve_body = AsyncMock(return_value=retrieve_payload or {"results": []})
    client.cache_lookup_body = AsyncMock(
        return_value=cache_lookup_payload or {"hit": False}
    )
    client.edge_upsert = AsyncMock()
    return client


def _make_deps(
    client: AsyncMock,
    *,
    lcma_edge_score: float = 0.75,
) -> LcmaWiringDeps:
    return LcmaWiringDeps(
        client=client,
        profile="research",
        run_task_id="task-1",
        lcma_edge_score=lcma_edge_score,
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
            source_url="https://arxiv.org/abs/2601.00001",
            cascade=CascadeOutput(title="A Paper", contributions=["c1"]),
            deps=deps,
        )

        assert related == [{"title": "Prior", "score": 0.9}]
        client.edge_upsert.assert_awaited_once()
        call = client.edge_upsert.await_args
        assert call.kwargs["type"] == "related_to"
        assert call.kwargs["from_id"] == "note-new"
        assert call.kwargs["to_id"] == "note-prior"
        assert call.kwargs["weight"] == 0.9
        assert call.kwargs["namespace"] == "influx"
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
            source_url="https://arxiv.org/abs/2601.00001",
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
            source_url="https://arxiv.org/abs/2601.00001",
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
            source_url="https://arxiv.org/abs/2601.00001",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["FooNet (arXiv:2412.12345)"],
            ),
            deps=deps,
        )

        client.cache_lookup_body.assert_awaited_once()
        # Find the builds_on edge among the upsert calls.
        upserts = [
            call.kwargs
            for call in client.edge_upsert.await_args_list
            if call.kwargs.get("type") == "builds_on"
        ]
        assert len(upserts) == 1
        assert upserts[0]["from_id"] == "note-new"
        assert upserts[0]["to_id"] == "note-prior"
        assert upserts[0]["weight"] == 1.0
        assert upserts[0]["namespace"] == "influx"

    @pytest.mark.asyncio
    async def test_no_arxiv_id_skips_lookup(self) -> None:
        client = _make_client()
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            source_url="https://arxiv.org/abs/2601.00001",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["A handwave reference with no id"],
            ),
            deps=deps,
        )

        client.cache_lookup_body.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_skips_edge(self) -> None:
        client = _make_client(cache_lookup_payload={"hit": False})
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            source_url="https://arxiv.org/abs/2601.00001",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["FooNet (arXiv:2412.12345)"],
            ),
            deps=deps,
        )

        client.cache_lookup_body.assert_awaited_once()
        builds_on_upserts = [
            call.kwargs
            for call in client.edge_upsert.await_args_list
            if call.kwargs.get("type") == "builds_on"
        ]
        assert builds_on_upserts == []

    @pytest.mark.asyncio
    async def test_cache_miss_logs_item_identity_context(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No-hit log carries item identity for diagnosis (#108)."""
        caplog.set_level("INFO")
        client = _make_client(cache_lookup_payload={"hit": False})
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            source_url="https://arxiv.org/abs/2601.00001",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["FooNet (arXiv:2412.12345)"],
            ),
            deps=deps,
        )

        miss_records = [
            record
            for record in caplog.records
            if "LCMA builds_on lookup miss" in record.message
        ]
        assert miss_records, "expected a no-hit log line"
        message = miss_records[0].getMessage()
        assert "FooNet" in message
        assert "2412.12345" in message

    @pytest.mark.asyncio
    async def test_source_url_mismatch_logs_item_identity_context(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """source_url mismatch log carries item identity for diagnosis (#108)."""
        caplog.set_level("INFO")
        client = _make_client(
            cache_lookup_payload={
                "hit": True,
                "source_url": "https://arxiv.org/abs/9999.99999",
                "note_id": "note-other",
            }
        )
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            source_url="https://arxiv.org/abs/2601.00001",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["FooNet (arXiv:2412.12345)"],
            ),
            deps=deps,
        )

        mismatch_records = [
            record
            for record in caplog.records
            if "LCMA builds_on lookup source_url mismatch" in record.message
        ]
        assert mismatch_records, "expected a source_url mismatch log line"
        message = mismatch_records[0].getMessage()
        assert "FooNet" in message
        assert "2412.12345" in message

    @pytest.mark.asyncio
    async def test_source_url_mismatch_skips_edge(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC-M2-8: cache hit with a different source_url is treated as miss."""
        caplog.set_level("INFO")
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
            source_url="https://arxiv.org/abs/2601.00001",
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
        assert "LCMA builds_on lookup source_url mismatch" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_builds_on_is_noop(self) -> None:
        client = _make_client()
        deps = _make_deps(client)

        await wire(
            written_note_id="note-new",
            source_url="https://arxiv.org/abs/2601.00001",
            cascade=CascadeOutput(title="A Paper", builds_on=None),
            deps=deps,
        )

        client.cache_lookup.assert_not_awaited()


# ── LCMA error propagation (post-#69) ──────────────────────────────


class TestLcmaErrorPropagation:
    """LCMA wiring is best-effort at the item boundary.

    The probe loop still drives the ``lcma_unknown_tool_failure`` latch
    via ``tools/list`` (issue #69), but post-write LCMA execution no
    longer aborts the rest of the run after the note has already been
    written.
    """

    @pytest.mark.asyncio
    async def test_unknown_tool_on_retrieve_is_contained(self) -> None:
        client = _make_client()
        client.retrieve_body = AsyncMock(
            side_effect=LCMAError("unknown_tool", stage="lithos_retrieve")
        )
        deps = _make_deps(client)

        related = await wire(
            written_note_id="note-new",
            source_url="https://arxiv.org/abs/2601.00001",
            cascade=CascadeOutput(title="A Paper"),
            deps=deps,
        )

        assert related == []

    @pytest.mark.asyncio
    async def test_other_lcma_error_is_contained_and_builds_on_still_runs(self) -> None:
        client = _make_client()
        client.retrieve_body = AsyncMock(
            side_effect=LCMAError("transport refused", stage="http")
        )
        deps = _make_deps(client)

        related = await wire(
            written_note_id="note-new",
            source_url="https://arxiv.org/abs/2601.00001",
            cascade=CascadeOutput(
                title="A Paper",
                builds_on=["FooNet (arXiv:2412.12345)"],
            ),
            deps=deps,
        )

        assert related == []
        client.cache_lookup_body.assert_awaited_once()
