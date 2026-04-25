"""Tests for the repair sweep entry point (US-004, US-011, US-012).

Verifies that ``sweep(profile)`` calls ``lithos_list`` with the
correct tag set, limit, ordering, iterates returned notes via
``lithos_read``, returns cleanly when no candidates are found,
rewrites every visited note via ``lithos_write`` (retry-order
advancement invariant, §5.4), and handles chronic
``content_too_large`` on the repair path (§5.4 failure mode 2).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from influx.config import AppConfig, RepairConfig
from influx.errors import LithosError
from influx.repair import ContentTooLargeSkipped, SweepWriteError, sweep

# ── Helpers ──────────────────────────────────────────────────────────


def _make_list_result(items: list[dict[str, Any]]) -> MagicMock:
    """Build a fake ``CallToolResult`` for ``list_notes``."""
    text_content = MagicMock()
    text_content.text = json.dumps({"items": items})
    result = MagicMock()
    result.content = [text_content]
    return result


def _make_write_result(status: str = "updated") -> MagicMock:
    """Build a fake ``CallToolResult`` for ``lithos_write``."""
    text_content = MagicMock()
    text_content.text = json.dumps({"status": status})
    result = MagicMock()
    result.content = [text_content]
    return result


def _make_config(max_items: int = 100) -> MagicMock:
    """Build a minimal config mock with ``repair.max_items_per_run``."""
    config = MagicMock(spec=AppConfig)
    config.repair = MagicMock(spec=RepairConfig)
    config.repair.max_items_per_run = max_items
    config.profiles = []
    return config


def _make_client(
    list_items: list[dict[str, Any]] | None = None,
    read_responses: list[dict[str, Any]] | None = None,
    write_status: str = "updated",
) -> AsyncMock:
    """Build a mock LithosClient with ``list_notes`` / ``read_note`` / ``call_tool``."""
    client = AsyncMock()
    client.list_notes = AsyncMock(return_value=_make_list_result(list_items or []))
    if read_responses:
        client.read_note = AsyncMock(side_effect=read_responses)
    else:
        client.read_note = AsyncMock(return_value={"id": "", "content": "", "tags": []})
    client.call_tool = AsyncMock(return_value=_make_write_result(write_status))
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


# ── Rewrite-on-every-visit (US-011, §5.4) ───────────────────────────


class TestSweepRewriteInvariant:
    """Every visited note is rewritten via ``lithos_write`` (AC-X-8)."""

    async def test_every_note_triggers_lithos_write(self) -> None:
        """All visited notes are written back even with no progress."""
        items = [
            {"id": "n1", "title": "A"},
            {"id": "n2", "title": "B"},
        ]
        read_notes = [
            {"id": "n1", "content": "C1", "tags": ["influx:repair-needed"]},
            {"id": "n2", "content": "C2", "tags": ["influx:repair-needed"]},
        ]
        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)

        await sweep("ai-robotics", client=client, config=config)

        # call_tool is used for lithos_write
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 2

    async def test_no_progress_still_rewrites(self) -> None:
        """A note with no stage changes is still rewritten."""
        items = [{"id": "n1", "title": "X"}]
        read_notes = [
            {"id": "n1", "content": "X", "tags": ["influx:repair-needed"]},
        ]
        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)

        await sweep("ai-robotics", client=client, config=config)

        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 1
        # The tags are re-emitted even without changes.
        write_args = write_calls[0].args[1]
        assert "influx:repair-needed" in write_args["tags"]

    async def test_rewrite_includes_note_fields(self) -> None:
        """The rewrite carries the note's id, title, content, etc."""
        items = [{"id": "n1", "title": "Paper"}]
        read_notes = [
            {
                "id": "n1",
                "title": "Paper Title",
                "content": "Body text",
                "tags": ["influx:repair-needed"],
                "source_url": "https://example.com",
                "confidence": 0.7,
                "version": 5,
            },
        ]
        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)

        await sweep("ai-robotics", client=client, config=config)

        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        args = write_calls[0].args[1]
        assert args["id"] == "n1"
        assert args["title"] == "Paper Title"
        assert args["content"] == "Body text"
        assert args["source_url"] == "https://example.com"
        assert args["confidence"] == 0.7
        assert args["expected_version"] == 5


# ── Version conflict handling (AC-06-F) ──────────────────────────────


class TestSweepVersionConflict:
    """Version-conflict handling: re-read + re-merge + retry once."""

    async def test_version_conflict_triggers_reread_and_retry(
        self,
    ) -> None:
        """First conflict → re-read + retry; second conflict → abort."""
        items = [{"id": "n1", "title": "Paper"}]
        note = {
            "id": "n1",
            "content": "C",
            "tags": ["influx:repair-needed"],
            "version": 1,
        }
        refreshed = {
            "id": "n1",
            "content": "C-refreshed",
            "tags": ["influx:repair-needed", "external:tag"],
            "version": 2,
        }
        config = _make_config()
        client = AsyncMock()
        client.list_notes = AsyncMock(return_value=_make_list_result(items))
        # read_note: first call is the initial re-read, second is the
        # FR-MCP-7 re-read after version_conflict.
        client.read_note = AsyncMock(side_effect=[note, refreshed])
        # call_tool: first write → version_conflict, retry → success.
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("version_conflict"),
                _make_write_result("updated"),
            ]
        )

        await sweep("ai-robotics", client=client, config=config)

        # Two lithos_write calls: initial + retry.
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 2
        # Retry uses refreshed version.
        retry_args = write_calls[1].args[1]
        assert retry_args["expected_version"] == 2
        assert retry_args["content"] == "C-refreshed"

    async def test_unresolved_conflict_aborts_sweep(self) -> None:
        """Second version_conflict → SweepWriteError → abort."""
        items = [
            {"id": "n1", "title": "A"},
            {"id": "n2", "title": "B"},
        ]
        note = {
            "id": "n1",
            "content": "C",
            "tags": ["influx:repair-needed"],
            "version": 1,
        }
        refreshed = {
            "id": "n1",
            "content": "C2",
            "tags": ["influx:repair-needed"],
            "version": 2,
        }
        config = _make_config()
        client = AsyncMock()
        client.list_notes = AsyncMock(return_value=_make_list_result(items))
        client.read_note = AsyncMock(side_effect=[note, refreshed])
        # Both writes return version_conflict.
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("version_conflict"),
                _make_write_result("version_conflict"),
            ]
        )

        with pytest.raises(SweepWriteError, match="version_conflict"):
            await sweep("ai-robotics", client=client, config=config)

        # Only one note was attempted (abort after n1 failed).
        assert client.read_note.await_count == 2  # initial + re-read

    async def test_no_later_candidate_after_abort(self) -> None:
        """After abort on note 1, note 2 is never rewritten."""
        items = [
            {"id": "n1", "title": "A"},
            {"id": "n2", "title": "B"},
        ]
        note1 = {
            "id": "n1",
            "content": "C1",
            "tags": ["influx:repair-needed"],
            "version": 1,
        }
        refreshed1 = {
            "id": "n1",
            "content": "C1r",
            "tags": ["influx:repair-needed"],
            "version": 2,
        }
        config = _make_config()
        client = AsyncMock()
        client.list_notes = AsyncMock(return_value=_make_list_result(items))
        client.read_note = AsyncMock(side_effect=[note1, refreshed1])
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("version_conflict"),
                _make_write_result("version_conflict"),
            ]
        )

        with pytest.raises(SweepWriteError):
            await sweep("ai-robotics", client=client, config=config)

        # n2 was never read — the sweep aborted on n1.
        read_ids = [c.kwargs["note_id"] for c in client.read_note.call_args_list]
        assert "n2" not in read_ids


# ── Transport failure (§5.4 failure mode 1) ──────────────────────────


class TestSweepTransportFailure:
    """Generic write transport failure aborts the run."""

    async def test_write_transport_failure_aborts(self) -> None:
        items = [{"id": "n1", "title": "A"}]
        note = {
            "id": "n1",
            "content": "C",
            "tags": ["influx:repair-needed"],
        }
        config = _make_config()
        client = AsyncMock()
        client.list_notes = AsyncMock(return_value=_make_list_result(items))
        client.read_note = AsyncMock(return_value=note)
        client.call_tool = AsyncMock(side_effect=LithosError("connection lost"))

        with pytest.raises(SweepWriteError, match="transport failure"):
            await sweep("ai-robotics", client=client, config=config)

    async def test_transport_failure_no_later_candidate(self) -> None:
        items = [
            {"id": "n1", "title": "A"},
            {"id": "n2", "title": "B"},
        ]
        note1 = {
            "id": "n1",
            "content": "C1",
            "tags": ["influx:repair-needed"],
        }
        config = _make_config()
        client = AsyncMock()
        client.list_notes = AsyncMock(return_value=_make_list_result(items))
        client.read_note = AsyncMock(return_value=note1)
        client.call_tool = AsyncMock(side_effect=LithosError("connection lost"))

        with pytest.raises(SweepWriteError):
            await sweep("ai-robotics", client=client, config=config)

        # Only n1 was read — n2 never reached.
        assert client.read_note.await_count == 1


# ── Chronic content_too_large exemption (US-012, §5.4 failure mode 2) ─


class TestSweepContentTooLargeSkipped:
    """Chronic ``content_too_large`` on repair path: skip, don't abort."""

    async def test_content_too_large_does_not_abort_sweep(self) -> None:
        """Sweep continues to next candidate after content_too_large."""
        items = [
            {"id": "n1", "title": "Oversize"},
            {"id": "n2", "title": "Normal"},
        ]
        note1 = {
            "id": "n1",
            "content": "Large",
            "tags": ["influx:repair-needed"],
        }
        note2 = {
            "id": "n2",
            "content": "Small",
            "tags": ["influx:repair-needed"],
        }
        config = _make_config()
        client = AsyncMock()
        client.list_notes = AsyncMock(return_value=_make_list_result(items))
        client.read_note = AsyncMock(side_effect=[note1, note2])
        # n1 write → content_too_large; n2 write → success.
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("content_too_large"),
                _make_write_result("updated"),
            ]
        )

        result = await sweep("ai-robotics", client=client, config=config)

        # Both notes were visited (read).
        assert len(result) == 2
        assert result[0]["id"] == "n1"
        assert result[1]["id"] == "n2"
        # n2 was still written (sweep didn't abort).
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 2

    async def test_oversize_note_untouched_no_second_write(
        self,
    ) -> None:
        """content_too_large note gets only one write attempt (no retry)."""
        items = [{"id": "n1", "title": "Oversize"}]
        note = {
            "id": "n1",
            "content": "Large",
            "tags": ["influx:repair-needed"],
        }
        config = _make_config()
        client = AsyncMock()
        client.list_notes = AsyncMock(return_value=_make_list_result(items))
        client.read_note = AsyncMock(return_value=note)
        client.call_tool = AsyncMock(
            return_value=_make_write_result("content_too_large"),
        )

        await sweep("ai-robotics", client=client, config=config)

        # Only one write call — no version_conflict retry loop.
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 1

    async def test_other_notes_still_make_progress(self) -> None:
        """Notes after the oversize one are rewritten normally."""
        items = [
            {"id": "n1", "title": "Oversize"},
            {"id": "n2", "title": "Normal"},
            {"id": "n3", "title": "Also Normal"},
        ]
        notes = [
            {
                "id": "n1",
                "content": "Large",
                "tags": ["influx:repair-needed"],
            },
            {
                "id": "n2",
                "content": "Small",
                "tags": ["influx:repair-needed"],
            },
            {
                "id": "n3",
                "content": "Medium",
                "tags": ["influx:repair-needed"],
            },
        ]
        config = _make_config()
        client = AsyncMock()
        client.list_notes = AsyncMock(return_value=_make_list_result(items))
        client.read_note = AsyncMock(side_effect=notes)
        # n1 → oversize, n2 + n3 → success.
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("content_too_large"),
                _make_write_result("updated"),
                _make_write_result("updated"),
            ]
        )

        result = await sweep("ai-robotics", client=client, config=config)

        assert len(result) == 3
        # All three were read.
        assert client.read_note.await_count == 3
        # Three write calls total — n1 (failed), n2, n3.
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 3

    async def test_multiple_oversize_notes_all_skipped(self) -> None:
        """Multiple content_too_large notes are all skipped; no abort."""
        items = [
            {"id": "n1", "title": "Big1"},
            {"id": "n2", "title": "Big2"},
        ]
        notes = [
            {
                "id": "n1",
                "content": "Large1",
                "tags": ["influx:repair-needed"],
            },
            {
                "id": "n2",
                "content": "Large2",
                "tags": ["influx:repair-needed"],
            },
        ]
        config = _make_config()
        client = AsyncMock()
        client.list_notes = AsyncMock(return_value=_make_list_result(items))
        client.read_note = AsyncMock(side_effect=notes)
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("content_too_large"),
                _make_write_result("content_too_large"),
            ]
        )

        # Does NOT raise — both skipped, sweep completes.
        result = await sweep("ai-robotics", client=client, config=config)

        assert len(result) == 2


class TestContentTooLargeSkippedException:
    """Unit tests for the ContentTooLargeSkipped exception itself."""

    def test_exception_stores_note_id(self) -> None:
        exc = ContentTooLargeSkipped("note-xyz")
        assert exc.note_id == "note-xyz"

    def test_exception_message_contains_note_id(self) -> None:
        exc = ContentTooLargeSkipped("note-abc")
        assert "note-abc" in str(exc)

    def test_exception_is_not_sweep_write_error(self) -> None:
        """ContentTooLargeSkipped is NOT a SweepWriteError."""
        exc = ContentTooLargeSkipped("n1")
        assert not isinstance(exc, SweepWriteError)
        assert not isinstance(exc, LithosError)
