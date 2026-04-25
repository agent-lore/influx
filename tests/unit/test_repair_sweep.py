"""Tests for the repair sweep entry point (US-004).

Verifies that ``sweep(profile)`` calls ``lithos_list`` with the
correct tag set, limit, ordering, iterates returned notes via
``lithos_read``, and returns cleanly when no candidates are found.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from influx.config import AppConfig, RepairConfig
from influx.repair import sweep

# ── Helpers ──────────────────────────────────────────────────────────


def _make_list_result(items: list[dict[str, Any]]) -> MagicMock:
    """Build a fake ``CallToolResult`` for ``list_notes``."""
    text_content = MagicMock()
    text_content.text = json.dumps({"items": items})
    result = MagicMock()
    result.content = [text_content]
    return result


def _make_config(max_items: int = 100) -> MagicMock:
    """Build a minimal config mock with ``repair.max_items_per_run``."""
    config = MagicMock(spec=AppConfig)
    config.repair = MagicMock(spec=RepairConfig)
    config.repair.max_items_per_run = max_items
    return config


def _make_client(
    list_items: list[dict[str, Any]] | None = None,
    read_responses: list[dict[str, Any]] | None = None,
) -> AsyncMock:
    """Build a mock LithosClient with ``list_notes`` / ``read_note``."""
    client = AsyncMock()
    client.list_notes = AsyncMock(return_value=_make_list_result(list_items or []))
    if read_responses:
        client.read_note = AsyncMock(side_effect=read_responses)
    else:
        client.read_note = AsyncMock(return_value={"id": "", "content": "", "tags": []})
    return client


# ── lithos_list called with correct parameters ──────────────────────


class TestSweepListCall:
    """``sweep`` invokes ``lithos_list`` with exact FR-REP-1 params."""

    async def test_list_called_with_correct_tags_limit_ordering(
        self,
    ) -> None:
        config = _make_config(max_items=50)
        client = _make_client(list_items=[])

        await sweep("ai-robotics", client=client, config=config)

        client.list_notes.assert_awaited_once_with(
            tags=["influx:repair-needed", "profile:ai-robotics"],
            limit=50,
            order_by="updated_at",
            order="asc",
        )

    async def test_list_uses_default_limit_100(self) -> None:
        config = _make_config(max_items=100)
        client = _make_client(list_items=[])

        await sweep("web-tech", client=client, config=config)

        call_kwargs = client.list_notes.call_args.kwargs
        assert call_kwargs["limit"] == 100

    async def test_profile_name_interpolated_into_tag(self) -> None:
        config = _make_config()
        client = _make_client(list_items=[])

        await sweep("ml-research", client=client, config=config)

        call_kwargs = client.list_notes.call_args.kwargs
        assert call_kwargs["tags"] == [
            "influx:repair-needed",
            "profile:ml-research",
        ]


# ── Zero candidates → clean return ─────────────────────────────────


class TestSweepZeroCandidates:
    """Empty ``lithos_list`` → return cleanly, no ``lithos_read``."""

    async def test_returns_empty_list(self) -> None:
        config = _make_config()
        client = _make_client(list_items=[])

        result = await sweep("ai-robotics", client=client, config=config)

        assert result == []

    async def test_read_note_not_called(self) -> None:
        config = _make_config()
        client = _make_client(list_items=[])

        await sweep("ai-robotics", client=client, config=config)

        client.read_note.assert_not_awaited()


# ── Non-zero candidates → iterate and re-read ──────────────────────


class TestSweepIteration:
    """Candidates are iterated in order and re-read via ``lithos_read``."""

    async def test_each_candidate_reread(self) -> None:
        items = [
            {"id": "note-001", "title": "Paper A"},
            {"id": "note-002", "title": "Paper B"},
            {"id": "note-003", "title": "Paper C"},
        ]
        read_notes = [
            {"id": "note-001", "content": "A", "tags": ["t1"]},
            {"id": "note-002", "content": "B", "tags": ["t2"]},
            {"id": "note-003", "content": "C", "tags": ["t3"]},
        ]
        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)

        await sweep("ai-robotics", client=client, config=config)

        assert client.read_note.await_count == 3
        # Verify IDs passed in order.
        calls = client.read_note.call_args_list
        assert calls[0].kwargs["note_id"] == "note-001"
        assert calls[1].kwargs["note_id"] == "note-002"
        assert calls[2].kwargs["note_id"] == "note-003"

    async def test_returns_reread_notes_in_order(self) -> None:
        items = [
            {"id": "note-A", "title": "First"},
            {"id": "note-B", "title": "Second"},
        ]
        read_notes = [
            {
                "id": "note-A",
                "content": "Content A",
                "tags": ["influx:repair-needed"],
            },
            {
                "id": "note-B",
                "content": "Content B",
                "tags": ["influx:repair-needed"],
            },
        ]
        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)

        result = await sweep("ai-robotics", client=client, config=config)

        assert len(result) == 2
        assert result[0]["id"] == "note-A"
        assert result[1]["id"] == "note-B"

    async def test_single_candidate(self) -> None:
        items = [{"id": "note-solo", "title": "Solo Paper"}]
        read_notes = [{"id": "note-solo", "content": "Solo", "tags": []}]
        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)

        result = await sweep("ai-robotics", client=client, config=config)

        assert len(result) == 1
        client.read_note.assert_awaited_once_with(note_id="note-solo")

    async def test_skips_items_without_id(self) -> None:
        """Items missing ``id`` are skipped (defensive)."""
        items = [
            {"id": "note-good", "title": "Good"},
            {"title": "No ID"},  # missing id
            {"id": "", "title": "Empty ID"},  # empty id
        ]
        read_notes = [{"id": "note-good", "content": "Good", "tags": []}]
        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)

        result = await sweep("ai-robotics", client=client, config=config)

        assert len(result) == 1
        assert result[0]["id"] == "note-good"
        client.read_note.assert_awaited_once()
