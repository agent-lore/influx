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
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from influx.cascade import Acquired, Tier2Result
from influx.config import AppConfig, RepairConfig
from influx.errors import ExtractionError, LCMAError, LithosError
from influx.repair import (
    ContentTooLargeSkipped,
    SweepHooks,
    SweepWriteError,
    _build_sweep_cascade,
    sweep,
)

# ── Helpers ──────────────────────────────────────────────────────────


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
    client.list_notes_body = AsyncMock(return_value={"items": list_items or []})
    if read_responses:
        client.read_note = AsyncMock(side_effect=read_responses)
    else:
        client.read_note = AsyncMock(return_value={"id": "", "content": "", "tags": []})
    client.call_tool = AsyncMock(return_value=_make_write_result(write_status))
    return client


def _sweep_cascade(config: Any) -> Any:
    """Real sweep Cascade so Tier 3 routes through ``enrich`` (3a.2).

    Tier 3's model call (``influx.cascade.tier3_extract``) is monkeypatched
    per test, so this drives the real counter/terminal lifecycle without an
    LLM — the same seam the create path uses.
    """
    return _build_sweep_cascade(config, "ai-robotics")


# ── lithos_list called with correct parameters ──────────────────────


class TestSweepListCall:
    """``sweep`` invokes ``lithos_list`` with exact FR-REP-1 params."""

    async def test_list_called_with_correct_tags_limit_ordering(
        self,
    ) -> None:
        config = _make_config(max_items=50)
        client = _make_client(list_items=[])

        await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

        client.list_notes_body.assert_awaited_once_with(
            tags=["influx:repair-needed", "profile:ai-robotics"],
            limit=50,
            order_by="updated_at",
            order="asc",
        )

    async def test_list_uses_default_limit_100(self) -> None:
        config = _make_config(max_items=100)
        client = _make_client(list_items=[])

        await sweep("web-tech", client=client, config=config, hooks=SweepHooks())

        call_kwargs = client.list_notes_body.call_args.kwargs
        assert call_kwargs["limit"] == 100

    async def test_profile_name_interpolated_into_tag(self) -> None:
        config = _make_config()
        client = _make_client(list_items=[])

        await sweep("ml-research", client=client, config=config, hooks=SweepHooks())

        call_kwargs = client.list_notes_body.call_args.kwargs
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

        result = await sweep(
            "ai-robotics", client=client, config=config, hooks=SweepHooks()
        )

        assert result == []

    async def test_read_note_not_called(self) -> None:
        config = _make_config()
        client = _make_client(list_items=[])

        await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

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

        await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

        assert client.read_note.await_count == 3
        # Verify IDs passed in order.
        calls = client.read_note.call_args_list
        assert calls[0].kwargs["note_id"] == "note-001"
        assert calls[1].kwargs["note_id"] == "note-002"
        assert calls[2].kwargs["note_id"] == "note-003"

    async def test_candidates_sorted_by_updated_at(self) -> None:
        items = [
            {"id": "note-new", "title": "New", "updated_at": "2026-01-03T00:00:00Z"},
            {"id": "note-old", "title": "Old", "updated_at": "2026-01-01T00:00:00Z"},
            {"id": "note-mid", "title": "Mid", "updated_at": "2026-01-02T00:00:00Z"},
        ]
        read_notes = [
            {"id": "note-old", "content": "Old", "tags": []},
            {"id": "note-mid", "content": "Mid", "tags": []},
            {"id": "note-new", "content": "New", "tags": []},
        ]
        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)

        await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

        calls = client.read_note.call_args_list
        assert [call.kwargs["note_id"] for call in calls] == [
            "note-old",
            "note-mid",
            "note-new",
        ]

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

        result = await sweep(
            "ai-robotics", client=client, config=config, hooks=SweepHooks()
        )

        assert len(result) == 2
        assert result[0]["id"] == "note-A"
        assert result[1]["id"] == "note-B"

    async def test_single_candidate(self) -> None:
        items = [{"id": "note-solo", "title": "Solo Paper"}]
        read_notes = [{"id": "note-solo", "content": "Solo", "tags": []}]
        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)

        result = await sweep(
            "ai-robotics", client=client, config=config, hooks=SweepHooks()
        )

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

        result = await sweep(
            "ai-robotics", client=client, config=config, hooks=SweepHooks()
        )

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

        await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

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

        await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

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

        await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

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

    async def test_tier3_lcma_error_is_per_note_failure_not_abort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier-3 model validation errors should not abort the whole sweep."""
        items = [{"id": "n1", "title": "Paper"}]
        note = {
            "id": "n1",
            "title": "Paper",
            "content": (
                "---\n"
                "source_url: https://example.com/paper\n"
                "tags: []\n"
                "confidence: 0.9\n"
                "---\n"
                "# Paper\n\n"
                "## Archive\n"
                "path: arxiv/2026/04/paper.pdf\n\n"
                "## Summary\n"
                "Summary\n\n"
                "## Full Text\n"
                "Full text\n\n"
                "## Profile Relevance\n"
                "### ai-robotics\n"
                "Score: 9/10\n"
                "Relevant\n\n"
                "## User Notes\n"
            ),
            "tags": ["influx:repair-needed", "text:html", "full-text"],
            "source_url": "https://example.com/paper",
            "confidence": 0.9,
        }

        def failing_tier3(*, title: str, full_text: str, config: object) -> None:
            del title, full_text, config
            raise LCMAError("validation failed", stage="validate")

        monkeypatch.setattr("influx.cascade.tier3_extract", failing_tier3)
        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        result = await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(),
            cascade=_sweep_cascade(config),
        )

        assert len(result) == 1
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 1
        write_args = write_calls[0].args[1]
        assert "influx:repair-needed" in write_args["tags"]
        assert "influx:deep-extracted" not in write_args["tags"]


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
        client.list_notes_body = AsyncMock(return_value={"items": items})
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

        await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

        # Two lithos_write calls: initial + retry.
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 2
        # Retry uses refreshed version and preserves the SWEEP's pending
        # content (no user-notes section in either, so merge is the
        # sweep's content unchanged — never the refreshed body).
        retry_args = write_calls[1].args[1]
        assert retry_args["expected_version"] == 2
        assert retry_args["content"] == "C"

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
        client.list_notes_body = AsyncMock(return_value={"items": items})
        client.read_note = AsyncMock(side_effect=[note, refreshed])
        # Both writes return version_conflict.
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("version_conflict"),
                _make_write_result("version_conflict"),
            ]
        )

        with pytest.raises(SweepWriteError, match="version_conflict"):
            await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

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
        client.list_notes_body = AsyncMock(return_value={"items": items})
        client.read_note = AsyncMock(side_effect=[note1, refreshed1])
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("version_conflict"),
                _make_write_result("version_conflict"),
            ]
        )

        with pytest.raises(SweepWriteError):
            await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

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
        client.list_notes_body = AsyncMock(return_value={"items": items})
        client.read_note = AsyncMock(return_value=note)
        client.call_tool = AsyncMock(side_effect=LithosError("connection lost"))

        with pytest.raises(SweepWriteError, match="transport failure"):
            await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

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
        client.list_notes_body = AsyncMock(return_value={"items": items})
        client.read_note = AsyncMock(return_value=note1)
        client.call_tool = AsyncMock(side_effect=LithosError("connection lost"))

        with pytest.raises(SweepWriteError):
            await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

        # Only n1 was read — n2 never reached.
        assert client.read_note.await_count == 1


# ── Chronic content_too_large exemption (US-012, §5.4 failure mode 2) ─


class TestSweepContentTooLargeSkipped:
    """Chronic ``content_too_large`` on repair path: skip, don't abort."""

    async def test_content_too_large_does_not_abort_sweep(self) -> None:
        """Sweep continues to next candidate after content_too_large.

        The chronic-oversize repair-path skip per master PRD §9.7 only
        triggers AFTER the Tier-2 → Tier-1 trim retry sequence (finding
        #2): three ``content_too_large`` responses → ``ContentTooLargeSkipped``.
        """
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
        client.list_notes_body = AsyncMock(return_value={"items": items})
        client.read_note = AsyncMock(side_effect=[note1, note2])
        # n1 chronic-oversize: 3 content_too_large (orig + Tier-2-dropped
        # + Tier-1-only) → ContentTooLargeSkipped.  Then n2 → updated.
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("content_too_large"),
                _make_write_result("content_too_large"),
                _make_write_result("content_too_large"),
                _make_write_result("updated"),
            ]
        )

        result = await sweep(
            "ai-robotics", client=client, config=config, hooks=SweepHooks()
        )

        # Both notes were visited (read).
        assert len(result) == 2
        assert result[0]["id"] == "n1"
        assert result[1]["id"] == "n2"
        # 4 write calls total: 3 trim attempts on n1 + 1 success on n2.
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 4

    async def test_oversize_note_chronic_skip_after_trim_retries(
        self,
    ) -> None:
        """Chronic ``content_too_large`` only after 3 trim attempts.

        Per master PRD §9.7 / finding #2, the sweep first retries with
        Tier 2 dropped, then with Tier 1-only + ``influx:repair-needed``.
        Only when *all three* attempts return ``content_too_large`` is
        the note treated as chronic-oversize.
        """
        items = [{"id": "n1", "title": "Oversize"}]
        note = {
            "id": "n1",
            "content": "Large",
            "tags": ["influx:repair-needed"],
        }
        config = _make_config()
        client = AsyncMock()
        client.list_notes_body = AsyncMock(return_value={"items": items})
        client.read_note = AsyncMock(return_value=note)
        client.call_tool = AsyncMock(
            return_value=_make_write_result("content_too_large"),
        )

        await sweep("ai-robotics", client=client, config=config, hooks=SweepHooks())

        # Three write calls — original + Tier-2-dropped + Tier-1-only.
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 3

        # The third attempt MUST carry ``influx:repair-needed`` and
        # MUST drop ``## Full Text`` / Tier-3 sections (master PRD
        # §9.7 repair-path Tier-1-only retry).
        third_args = write_calls[2].args[1]
        assert "influx:repair-needed" in third_args["tags"]

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
        client.list_notes_body = AsyncMock(return_value={"items": items})
        client.read_note = AsyncMock(side_effect=notes)
        # n1 → oversize×3 (chronic), n2 + n3 → success.
        client.call_tool = AsyncMock(
            side_effect=[
                _make_write_result("content_too_large"),
                _make_write_result("content_too_large"),
                _make_write_result("content_too_large"),
                _make_write_result("updated"),
                _make_write_result("updated"),
            ]
        )

        result = await sweep(
            "ai-robotics", client=client, config=config, hooks=SweepHooks()
        )

        assert len(result) == 3
        # All three were read.
        assert client.read_note.await_count == 3
        # Five write calls total — 3 trim attempts on n1 + n2 + n3.
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 5

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
        client.list_notes_body = AsyncMock(return_value={"items": items})
        client.read_note = AsyncMock(side_effect=notes)
        # Each note gets 3 content_too_large attempts → chronic skip.
        client.call_tool = AsyncMock(
            return_value=_make_write_result("content_too_large"),
        )

        # Does NOT raise — both skipped, sweep completes.
        result = await sweep(
            "ai-robotics", client=client, config=config, hooks=SweepHooks()
        )

        assert len(result) == 2
        # 6 writes total: 3 trim attempts × 2 chronic notes.
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert len(write_calls) == 6


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


# ── Structured logging on stage failures (staging incident 2026-04-30) ──


class TestStageFailureLogging:
    """Per-stage hook failures must surface ``exc_info`` and structured
    ``extra`` fields so root cause is recoverable from logs alone.
    Pre-incident behaviour was a bare ``logger.info('… failed for <id>')``
    that dropped the exception type, message, model, and stage.
    """

    @staticmethod
    def _note(note_id: str, *, with_full_text: bool = True) -> dict[str, Any]:
        body = (
            "---\n"
            f"source_url: https://example.com/{note_id}\n"
            "tags: []\n"
            "confidence: 0.9\n"
            "---\n"
            "# Paper\n\n"
            "## Archive\n"
            "path: arxiv/2026/04/paper.pdf\n\n"
            "## Summary\n"
            "Summary\n\n"
        )
        if with_full_text:
            body += "## Full Text\nFull text\n\n"
        body += (
            "## Profile Relevance\n"
            "### ai-robotics\n"
            "Score: 9/10\n"
            "Relevant\n\n"
            "## User Notes\n"
        )
        return {
            "id": note_id,
            "title": "Paper",
            "content": body,
            "tags": ["influx:repair-needed", "text:html", "full-text"],
            "source_url": f"https://example.com/{note_id}",
            "confidence": 0.9,
        }

    async def test_tier3_failure_logs_warning_via_cascade(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The sweep now runs Tier 3 through the shared Cascade (3a.2), so a
        Tier-3 failure is logged by ``enrich``.  The sweep re-runs Tier 3
        without Tier 1, so the fallback is *materially degraded* — a WARNING
        with the #151 structured fields — and the counted stage survives in
        the persisted ``## Repair`` counters."""
        import logging

        items = [{"id": "n1", "title": "Paper"}]
        note = self._note("n1")

        def failing(*, title: str, full_text: str, config: object) -> None:
            del title, full_text, config
            raise LCMAError(
                "Tier 3 extraction response failed validation",
                model="extract",
                stage="validate",
                detail="missing 'contributions' field",
            )

        monkeypatch.setattr("influx.cascade.tier3_extract", failing)
        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        with caplog.at_level(logging.WARNING, logger="influx.cascade"):
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(),
                cascade=_sweep_cascade(config),
            )

        matching = [
            r
            for r in caplog.records
            if r.levelname == "WARNING"
            and getattr(r, "tier3_failure_kind", None) == "lcma_error"
        ]
        assert matching, [
            (r.levelname, r.getMessage(), getattr(r, "tier3_failure_kind", None))
            for r in caplog.records
        ]
        rec = matching[0]
        assert getattr(rec, "item_id", None) == "n1"
        assert getattr(rec, "profile", None) == "ai-robotics"
        assert getattr(rec, "tier", None) == "3"
        # No Tier 1 in the sweep → materially degraded (not harmless).
        assert getattr(rec, "effective_extraction_tier", None) == "tier2"

        # The structured failure stage is durably persisted in ## Repair.
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert 'tier3_last_stage: "validate"' in write_calls[-1].args[1]["content"]

    async def test_tier2_counted_failure_persists_counter_via_cascade(self) -> None:
        """The sweep now runs Tier 2 through the shared Cascade (3a.3).

        Unlike Tier 1 / Tier 3, a Tier-2 failure degrades *silently* at the
        Cascade level (no per-pass WARNING) — but a counted-class failure
        (here an unparseable archive → ``parse``) still advances the durable
        ``## Repair`` counter and keeps ``influx:repair-needed`` for the next
        pass, because ``enrich`` owns the counter lifecycle.
        """
        # Score 8 selects tier2 (>= full_text 8) but not tier3 (< deep_extract
        # 9), isolating the Tier-2 path in a single-cascade sweep.
        content = (
            "---\n"
            "source_url: https://example.com/n2\n"
            "tags: []\n"
            "confidence: 0.9\n"
            "---\n"
            "# Paper\n\n"
            "## Archive\n"
            "path: arxiv/2026/04/paper.pdf\n\n"
            "## Summary\n"
            "Summary\n\n"
            "## Profile Relevance\n"
            "### ai-robotics\n"
            "Score: 8/10\n"
            "Relevant\n\n"
            "## User Notes\n"
        )
        note = {
            "id": "n2",
            "title": "Paper",
            "content": content,
            "tags": ["influx:repair-needed", "text:html"],
            "source_url": "https://example.com/n2",
            "confidence": 0.9,
        }
        items = [{"id": "n2", "title": "Paper"}]

        def failing_extractor(acquired: Acquired) -> Tier2Result:
            del acquired
            raise ExtractionError(
                "archive unparseable",
                stage="parse",
                detail="counted failure",
            )

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(),
            cascade=_build_sweep_cascade(
                config, "ai-robotics", tier2_extractor=failing_extractor
            ),
        )

        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        rewritten = write_calls[-1].args[1]
        # Counted failure advanced the durable counter + persisted the stage.
        assert "tier2_attempts: 1" in rewritten["content"]
        assert 'tier2_last_stage: "parse"' in rewritten["content"]
        # Stage failed → repair-needed stays, full-text not added, not terminal.
        assert "influx:repair-needed" in rewritten["tags"]
        assert "full-text" not in rewritten["tags"]
        assert "influx:tier2-terminal" not in rewritten["tags"]


# ── Tier 2 recovery via the shared Cascade (3a.3) ────────────────────


class TestSweepTier2RecoveryViaCascade:
    """Tier 2 recovery runs through the injected Cascade (3a.3): the sweep
    reconstructs an ``Acquired`` from the persisted note, hands its archive
    path to the Cascade's extractor, and persists the outcome (full text on
    success; advanced ``## Repair`` counters + ``influx:tier2-terminal`` at
    the cap).  These prove the sweep-level handoff/persistence that the
    permissive fake-Cascade integration test cannot.
    """

    @staticmethod
    def _note_for_tier2(
        note_id: str, *, score: int = 8, repair_section: str = ""
    ) -> dict[str, Any]:
        # No ## Full Text so tier2 is selected; score 8 keeps tier3 (needs
        # >= 9) out, isolating the Tier-2 path in a single-cascade sweep.
        body = (
            "---\n"
            f"source_url: https://example.com/{note_id}\n"
            "tags: []\n"
            "confidence: 0.9\n"
            "---\n"
            "# Paper\n\n"
            "## Archive\n"
            "path: arxiv/2026/04/paper.pdf\n\n"
            "## Summary\nSummary\n\n"
            f"{repair_section}"
            "## Profile Relevance\n"
            f"### ai-robotics\nScore: {score}/10\nRelevant\n\n"
            "## User Notes\n"
        )
        return {
            "id": note_id,
            "title": "Paper",
            "content": body,
            "tags": ["influx:repair-needed", "text:html"],
            "source_url": f"https://example.com/{note_id}",
            "confidence": 0.9,
        }

    @staticmethod
    def _last_write_args(client: AsyncMock) -> dict[str, Any]:
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert write_calls, "expected lithos_write call"
        return dict(write_calls[-1].args[1])

    async def test_tier2_success_recovers_full_text_and_clears_repair_needed(
        self,
    ) -> None:
        """Real Cascade + archive-reading extractor: the reconstructed
        ``Acquired`` carries the note's persisted archive path, the recovered
        text lands as ``## Full Text`` + ``full-text``, and — Tier 3 not
        required at score 8 — ``influx:repair-needed`` clears."""
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_tier2("n1", score=8)

        seen: list[str | None] = []

        def extractor(acquired: Acquired) -> Tier2Result:
            # Prove the persisted ## Archive path reached the extractor —
            # a bug reconstructing archive_path (e.g. None) fails here.
            seen.append(acquired.archive_path)
            return Tier2Result(
                text="Recovered full text body.",
                flavour="html",
                text_tag="text:html",
            )

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(),
            cascade=_build_sweep_cascade(
                config, "ai-robotics", tier2_extractor=extractor
            ),
        )

        # The reconstructed Acquired carried the persisted archive path.
        assert seen == ["arxiv/2026/04/paper.pdf"]

        rewritten = self._last_write_args(client)
        assert "## Full Text" in rewritten["content"]
        assert "Recovered full text body." in rewritten["content"]
        assert "full-text" in rewritten["tags"]
        # Archive + text + full-text present, Tier 3 waived at score 8.
        assert "influx:repair-needed" not in rewritten["tags"]

    async def test_tier2_counted_failure_at_cap_flips_terminal_and_logs(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """At the counted-failure cap the sweep persists ``tier2_attempts: 3``,
        adds ``influx:tier2-terminal``, and logs
        ``sweep_stage=tier2_terminal_flip`` — the persistence/logging step
        that lives in repair.py, not cascade.py."""
        import logging

        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_tier2(
            "n1",
            score=8,
            repair_section=(
                "## Repair\n"
                "- tier2_attempts: 2\n"
                '- tier2_last_stage: "parse"\n'
                '- tier2_last_error: "earlier failure"\n'
                "- tier3_attempts: 0\n"
                '- tier3_last_stage: ""\n'
                '- tier3_last_error: ""\n\n'
            ),
        )

        def failing(acquired: Acquired) -> Tier2Result:
            del acquired
            raise ExtractionError("archive unparseable", stage="parse")

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        with caplog.at_level(logging.WARNING, logger="influx.repair"):
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(),
                cascade=_build_sweep_cascade(
                    config, "ai-robotics", tier2_extractor=failing
                ),
            )

        rewritten = self._last_write_args(client)
        assert "influx:tier2-terminal" in rewritten["tags"]
        assert "tier2_attempts: 3" in rewritten["content"]
        # At the cap, Tier 2 can never succeed, so the condition it gates
        # is waived and the note leaves the sweep.  Keeping
        # influx:repair-needed here is what made capped notes immortal
        # sweep candidates: re-selected every run, and rewritten every
        # run for retry-order advancement (AC-X-8), which pinned
        # updated_at to "now" forever and dominated retrieval ranking.
        assert "influx:repair-needed" not in rewritten["tags"]

        flip_logs = [
            r
            for r in caplog.records
            if getattr(r, "sweep_stage", None) == "tier2_terminal_flip"
        ]
        assert flip_logs, "expected tier2_terminal_flip log"
        rec = flip_logs[0]
        assert getattr(rec, "tier2_attempts", None) == 3
        assert getattr(rec, "stage", None) == "parse"


# ── Layer 2 self-repair: counter + terminal flip ─────────────────────


class TestSweepTier2ReArmContract:
    """The persisted counter — not the terminal tag — gates execution.

    ``Cascade.enrich`` checks ``counters.tier2_attempts >=
    REPAIR_COUNTED_CAP`` *before* calling the extractor
    (``cascade.py``), so an operator who removes
    ``influx:tier2-terminal`` and re-adds ``influx:repair-needed`` but
    leaves the counter at the cap gets no recovery attempt: the stage is
    skipped, the terminal tag is re-emitted, the clearing waiver drops
    ``influx:repair-needed`` again, and the note leaves the sweep
    unchanged in substance.

    These two tests pin that contract in both directions so the
    documented re-arm procedure in ``docs/SPECIFICATION.md`` §11.1 and
    ``docs/operations/runbook.md`` §6 cannot silently drift from it.
    """

    @staticmethod
    def _note(repair_section: str) -> dict[str, Any]:
        return TestSweepTier2RecoveryViaCascade._note_for_tier2(
            "n1", score=8, repair_section=repair_section
        )

    @staticmethod
    def _repair_section(attempts: int) -> str:
        return (
            "## Repair\n"
            f"- tier2_attempts: {attempts}\n"
            '- tier2_last_stage: "parse"\n'
            '- tier2_last_error: "earlier failure"\n'
            "- tier3_attempts: 0\n"
            '- tier3_last_stage: ""\n'
            '- tier3_last_error: ""\n\n'
        )

    async def test_removing_terminal_tag_alone_does_not_re_run_tier2(self) -> None:
        """Tag removed, counter still at cap -> extractor never invoked."""
        from influx.repair_counters import REPAIR_COUNTED_CAP

        note = self._note(self._repair_section(REPAIR_COUNTED_CAP))
        # Operator removed influx:tier2-terminal and left repair-needed.
        assert "influx:tier2-terminal" not in note["tags"]
        assert "influx:repair-needed" in note["tags"]

        calls = 0

        def extractor(acquired: Acquired) -> Tier2Result:
            nonlocal calls
            calls += 1
            del acquired
            return Tier2Result(text="recovered", flavour="pdf", text_tag="text:pdf")

        config = _make_config()
        client = _make_client(
            list_items=[{"id": "n1", "title": "Paper"}], read_responses=[note]
        )

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(),
            cascade=_build_sweep_cascade(
                config, "ai-robotics", tier2_extractor=extractor
            ),
        )

        # The capped counter wins: no recovery attempt at all.
        assert calls == 0
        rewritten = TestSweepTier2RecoveryViaCascade._last_write_args(client)
        # And the terminal tag is immediately re-applied, so the note
        # drops straight back out of the sweep set.
        assert "influx:tier2-terminal" in rewritten["tags"]
        assert "influx:repair-needed" not in rewritten["tags"]

    async def test_resetting_counter_below_cap_re_runs_tier2(self) -> None:
        """Counter reset -> extractor runs and the note actually recovers."""
        note = self._note(self._repair_section(0))

        calls = 0

        def extractor(acquired: Acquired) -> Tier2Result:
            nonlocal calls
            calls += 1
            del acquired
            return Tier2Result(
                text="recovered body", flavour="pdf", text_tag="text:pdf"
            )

        config = _make_config()
        client = _make_client(
            list_items=[{"id": "n1", "title": "Paper"}], read_responses=[note]
        )

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(),
            cascade=_build_sweep_cascade(
                config, "ai-robotics", tier2_extractor=extractor
            ),
        )

        assert calls == 1
        rewritten = TestSweepTier2RecoveryViaCascade._last_write_args(client)
        assert "influx:tier2-terminal" not in rewritten["tags"]
        assert "full-text" in rewritten["tags"]
        assert "recovered body" in rewritten["content"]


class TestSweepCapCounterAndTerminalFlip:
    """Repeat-fail sweeps cap counted failures at REPAIR_COUNTED_CAP and add
    ``influx:tier{N}-terminal`` so future sweeps skip the broken stage.
    """

    @staticmethod
    def _note_for_tier3(note_id: str) -> dict[str, Any]:
        # Has full-text + text:html so tier2 is NOT selected; tier3 IS.
        body = (
            "---\n"
            f"source_url: https://example.com/{note_id}\n"
            "tags: []\n"
            "confidence: 0.9\n"
            "---\n"
            "# Paper\n\n"
            "## Archive\n"
            "path: arxiv/2026/04/paper.pdf\n\n"
            "## Summary\nSummary\n\n"
            "## Full Text\nFull text\n\n"
            "## Profile Relevance\n"
            "### ai-robotics\nScore: 9/10\nRelevant\n\n"
            "## User Notes\n"
        )
        return {
            "id": note_id,
            "title": "Paper",
            "content": body,
            "tags": ["influx:repair-needed", "text:html", "full-text"],
            "source_url": f"https://example.com/{note_id}",
            "confidence": 0.9,
        }

    @staticmethod
    def _last_write_args(client: AsyncMock) -> dict[str, Any]:
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert write_calls, "expected lithos_write call"
        args = write_calls[-1].args[1]
        return dict(args)

    async def test_validate_failure_bumps_counter_in_repair_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single counted failure increments tier3_attempts in ## Repair."""
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_tier3("n1")

        def failing(*, title: str, full_text: str, config: object) -> None:
            del title, full_text, config
            raise LCMAError("validation failed", model="extract", stage="validate")

        monkeypatch.setattr("influx.cascade.tier3_extract", failing)
        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(),
            cascade=_sweep_cascade(config),
        )

        rewritten = self._last_write_args(client)
        content = rewritten["content"]
        assert "## Repair" in content
        assert "tier3_attempts: 1" in content
        assert 'tier3_last_stage: "validate"' in content
        # Terminal not yet flipped.
        assert "influx:tier3-terminal" not in rewritten["tags"]

    async def test_http_failure_does_not_bump_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Transient (HTTP) failures must NOT advance the cap counter."""
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_tier3("n1")

        def failing(*, title: str, full_text: str, config: object) -> None:
            del title, full_text, config
            raise LCMAError("connect timeout", model="extract", stage="http")

        monkeypatch.setattr("influx.cascade.tier3_extract", failing)
        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(),
            cascade=_sweep_cascade(config),
        )

        rewritten = self._last_write_args(client)
        content = rewritten["content"]
        # Either no Repair section, or counter is 0.
        if "## Repair" in content:
            assert "tier3_attempts: 0" in content
        assert "influx:tier3-terminal" not in rewritten["tags"]

    async def test_third_counted_failure_flips_tier3_terminal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """At cap=3, ``influx:tier3-terminal`` is added and a WARNING is logged."""
        import logging

        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_tier3("n1")
        # Pre-existing ## Repair section showing 2 prior counted failures.
        note["content"] = note["content"].replace(
            "## User Notes\n",
            (
                "## Repair\n"
                "- tier2_attempts: 0\n"
                '- tier2_last_stage: ""\n'
                '- tier2_last_error: ""\n'
                "- tier3_attempts: 2\n"
                '- tier3_last_stage: "validate"\n'
                '- tier3_last_error: "earlier failure"\n\n'
                "## User Notes\n"
            ),
        )

        def failing(*, title: str, full_text: str, config: object) -> None:
            del title, full_text, config
            raise LCMAError("schema mismatch", model="extract", stage="validate")

        monkeypatch.setattr("influx.cascade.tier3_extract", failing)
        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        with caplog.at_level(logging.WARNING, logger="influx.repair"):
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(),
                cascade=_sweep_cascade(config),
            )

        rewritten = self._last_write_args(client)
        assert "influx:tier3-terminal" in rewritten["tags"]
        assert "tier3_attempts: 3" in rewritten["content"]
        # This fixture is otherwise complete (archive + text:html +
        # full-text), so the Tier 3 waiver is the last outstanding
        # condition and the note must leave the sweep set.  Asserted at
        # the sweep level, not just on compute_clearing, because the
        # regression is the tag surviving into the persisted write.
        assert "influx:repair-needed" not in rewritten["tags"]

        flip_logs = [
            r
            for r in caplog.records
            if getattr(r, "sweep_stage", None) == "tier3_terminal_flip"
        ]
        assert flip_logs, "expected tier3_terminal_flip log"
        rec = flip_logs[0]
        assert getattr(rec, "tier3_attempts", None) == 3
        assert getattr(rec, "stage", None) == "validate"

    async def test_tier3_terminal_present_skips_tier3(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once ``influx:tier3-terminal`` is set, Tier 3 is not attempted."""
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_tier3("n1")
        note["tags"] = list(note["tags"]) + ["influx:tier3-terminal"]

        call_count = 0

        def spy(*, title: str, full_text: str, config: object) -> None:
            nonlocal call_count
            del title, full_text, config
            call_count += 1

        monkeypatch.setattr("influx.cascade.tier3_extract", spy)
        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(),
            cascade=_sweep_cascade(config),
        )

        assert call_count == 0


class TestSweepArchiveTerminalCap:
    """Repeated counted-class archive failures (e.g. oversize) flip
    ``influx:archive-terminal`` so the sweep stops re-attempting a
    download that will never succeed.
    """

    @staticmethod
    def _note_for_archive(note_id: str) -> dict[str, Any]:
        # influx:archive-missing tagged so archive_retry is selected.
        body = (
            "---\n"
            f"source_url: https://arxiv.org/abs/{note_id}\n"
            "tags: []\n"
            "confidence: 0.9\n"
            "---\n"
            "# Paper\n\n"
            "## Archive\n\n"
            "## Summary\nSummary\n\n"
            "## Profile Relevance\n"
            "### ai-robotics\nScore: 9/10\nRelevant\n\n"
            "## User Notes\n"
        )
        return {
            "id": note_id,
            "title": "Paper",
            "content": body,
            "tags": ["influx:repair-needed", "influx:archive-missing"],
            "source_url": f"https://arxiv.org/abs/{note_id}",
            "confidence": 0.9,
        }

    @staticmethod
    def _last_write_args(client: AsyncMock) -> dict[str, Any]:
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert write_calls, "expected lithos_write call"
        return dict(write_calls[-1].args[1])

    async def test_oversize_failure_bumps_archive_counter(self) -> None:
        """One counted oversize failure increments archive_attempts."""
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_archive("n1")

        def failing(note: dict[str, object]) -> str:
            del note
            raise ExtractionError(
                "Response body exceeds 100000000 bytes",
                url="https://arxiv.org/pdf/x.pdf",
                stage="oversize",
            )

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(archive_download=failing),
        )

        rewritten = self._last_write_args(client)
        content = rewritten["content"]
        assert "## Repair" in content
        assert "archive_attempts: 1" in content
        assert 'archive_last_kind: "oversize"' in content
        assert "influx:archive-terminal" not in rewritten["tags"]

    async def test_transient_archive_failure_does_not_bump_counter(self) -> None:
        """LithosError-class archive failures (transport flakes) must NOT
        advance the cap counter — they may heal on retry.
        """
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_archive("n1")

        def failing(note: dict[str, object]) -> str:
            del note
            raise LithosError("connection refused", operation="archive_download")

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(archive_download=failing),
        )

        rewritten = self._last_write_args(client)
        content = rewritten["content"]
        if "## Repair" in content:
            assert "archive_attempts: 0" in content
        assert "influx:archive-terminal" not in rewritten["tags"]

    async def test_third_oversize_failure_flips_archive_terminal(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """At cap=3, ``influx:archive-terminal`` is added and a WARNING is logged."""
        import logging

        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_archive("n1")
        note["content"] = note["content"].replace(
            "## User Notes\n",
            (
                "## Repair\n"
                "- archive_attempts: 2\n"
                '- archive_last_kind: "oversize"\n'
                '- archive_last_error: "earlier failure"\n\n'
                "## User Notes\n"
            ),
        )

        def failing(note: dict[str, object]) -> str:
            del note
            raise ExtractionError(
                "Response body exceeds 100000000 bytes",
                url="https://arxiv.org/pdf/x.pdf",
                stage="oversize",
            )

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        with caplog.at_level(logging.WARNING, logger="influx.repair"):
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(archive_download=failing),
            )

        rewritten = self._last_write_args(client)
        assert "influx:archive-terminal" in rewritten["tags"]
        assert "archive_attempts: 3" in rewritten["content"]

        flip_logs = [
            r
            for r in caplog.records
            if getattr(r, "sweep_stage", None) == "archive_terminal_flip"
        ]
        assert flip_logs, "expected archive_terminal_flip log"
        rec = flip_logs[0]
        assert getattr(rec, "archive_attempts", None) == 3
        assert getattr(rec, "kind", None) == "oversize"

    async def test_archive_terminal_releases_otherwise_complete_note(self) -> None:
        """Archive cap on a note with every *other* condition satisfied.

        ``_note_for_archive`` carries no ``text:*`` tag, so its cap-flip
        test cannot show that ``influx:archive-terminal`` actually
        releases a note — condition (b) fails independently.  This
        fixture satisfies text and both tiers, leaving the archive
        waiver as the only thing standing between the note and the exit,
        and pins that ``influx:archive-missing`` survives the release.
        """
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_archive("n1")
        note["tags"] = [
            "influx:repair-needed",
            "influx:archive-missing",
            "text:html",
            "full-text",
            "influx:deep-extracted",
        ]
        note["content"] = note["content"].replace(
            "## User Notes\n",
            (
                "## Repair\n"
                "- archive_attempts: 2\n"
                '- archive_last_kind: "oversize"\n'
                '- archive_last_error: "earlier failure"\n\n'
                "## User Notes\n"
            ),
        )

        def failing(note: dict[str, object]) -> str:
            del note
            raise ExtractionError(
                "Response body exceeds 100000000 bytes",
                url="https://arxiv.org/pdf/x.pdf",
                stage="oversize",
            )

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(archive_download=failing),
        )

        rewritten = self._last_write_args(client)
        assert "influx:archive-terminal" in rewritten["tags"]
        # Released from the sweep set...
        assert "influx:repair-needed" not in rewritten["tags"]
        # ...but the factual "no archive stored" tag is retained.
        assert "influx:archive-missing" in rewritten["tags"]

    async def test_archive_terminal_present_skips_archive_retry(self) -> None:
        """Once ``influx:archive-terminal`` is set, archive_download is not called."""
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_for_archive("n1")
        note["tags"] = list(note["tags"]) + ["influx:archive-terminal"]

        call_count = 0

        def spy(note: dict[str, object]) -> str:
            nonlocal call_count
            del note
            call_count += 1
            return "x.pdf"

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(archive_download=spy),
        )

        assert call_count == 0


class TestSweepTextExtractionRetry:
    """text_extraction stage executes when the note has no ``text:*`` tag.

    Verifies issue #24 wiring: select_stages selects the stage, the
    executor calls the hook, and a successful return appends the new
    ``text:*`` tag to the rewritten note.
    """

    @staticmethod
    def _note_textless(note_id: str) -> dict[str, Any]:
        body = (
            "---\n"
            f"source_url: https://arxiv.org/abs/{note_id}\n"
            "tags: []\n"
            "confidence: 0.9\n"
            "---\n"
            "# Paper\n\n"
            "## Archive\n"
            "path: arxiv/2026/04/x.pdf\n\n"
            "## Summary\nSummary\n\n"
            "## Profile Relevance\n"
            "### ai-robotics\nScore: 5/10\nRelevant\n\n"
            "## User Notes\n"
        )
        return {
            "id": note_id,
            "title": "Paper",
            "content": body,
            "tags": ["influx:repair-needed"],
            "source_url": f"https://arxiv.org/abs/{note_id}",
            "confidence": 0.9,
        }

    @staticmethod
    def _last_write_args(client: AsyncMock) -> dict[str, Any]:
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert write_calls, "expected lithos_write call"
        return dict(write_calls[-1].args[1])

    async def test_success_appends_text_tag(self) -> None:
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_textless("n1")

        def hook(note: dict[str, object]) -> str:
            del note
            return "text:html"

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(text_extraction=hook),
        )

        rewritten = self._last_write_args(client)
        assert "text:html" in rewritten["tags"]

    async def test_skipped_when_text_tag_already_present(self) -> None:
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_textless("n1")
        note["tags"] = list(note["tags"]) + ["text:abstract-only"]

        call_count = 0

        def hook(note: dict[str, object]) -> str:
            nonlocal call_count
            del note
            call_count += 1
            return "text:html"

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(text_extraction=hook),
        )

        assert call_count == 0

    async def test_failure_keeps_note_textless_and_repair_needed(self) -> None:
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_textless("n1")

        def hook(note: dict[str, object]) -> str:
            del note
            raise ExtractionError(
                "cascade fell through",
                stage="cascade",
                detail="all paths failed",
            )

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(text_extraction=hook),
        )

        rewritten = self._last_write_args(client)
        assert not any(t.startswith("text:") for t in rewritten["tags"])
        assert "influx:repair-needed" in rewritten["tags"]

    async def test_unsupported_source_becomes_terminal_and_leaves_sweep(self) -> None:
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_textless("n1")

        def hook(note: dict[str, object]) -> str:
            del note
            raise ExtractionError(
                "text_extraction retry: source '' not supported",
                stage="unsupported_source",
            )

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(text_extraction=hook),
        )

        rewritten = self._last_write_args(client)
        assert "text:abstract-only" in rewritten["tags"]
        assert "influx:text-terminal" in rewritten["tags"]
        assert "influx:repair-needed" not in rewritten["tags"]

    async def test_unsupported_source_keeps_repair_needed_when_archive_missing(
        self,
    ) -> None:
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_textless("n1")
        note["tags"] = list(note["tags"]) + ["influx:archive-missing"]
        note["content"] = str(note["content"]).replace(
            "path: arxiv/2026/04/x.pdf\n\n",
            "",
        )

        def hook(note: dict[str, object]) -> str:
            del note
            raise ExtractionError(
                "text_extraction retry: source '' not supported",
                stage="unsupported_source",
            )

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(text_extraction=hook),
        )

        rewritten = self._last_write_args(client)
        assert "text:abstract-only" in rewritten["tags"]
        assert "influx:text-terminal" in rewritten["tags"]
        assert "influx:repair-needed" in rewritten["tags"]

    async def test_invalid_source_metadata_flips_terminal_and_tags_source_invalid(
        self,
    ) -> None:
        """Regression for the #150 staging incident.

        A note with empty/garbled source metadata that the hook
        cannot infer raises ``ExtractionError(stage=
        invalid_source_metadata)``.  The sweep flips it terminal AND
        tags it ``influx:source-invalid`` so the bad-state notes are
        independently discoverable for operator cleanup, and removes
        ``influx:repair-needed`` so the note exits the sweep entirely
        instead of re-logging ``source '' not supported`` every pass.
        """
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_textless("n1")

        def hook(note: dict[str, object]) -> str:
            del note
            raise ExtractionError(
                "text_extraction retry: note has no recoverable source metadata",
                stage="invalid_source_metadata",
            )

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(text_extraction=hook),
        )

        rewritten = self._last_write_args(client)
        assert "text:abstract-only" in rewritten["tags"]
        assert "influx:text-terminal" in rewritten["tags"]
        assert "influx:source-invalid" in rewritten["tags"]
        assert "influx:repair-needed" not in rewritten["tags"]

    async def test_invalid_source_metadata_does_not_retry_next_pass(
        self,
    ) -> None:
        """Once terminal, the note carries ``influx:text-terminal`` and the
        text-extraction stage is no longer selected (AC: 'repeated runs
        do not re-log the same warning forever').
        """
        from influx.repair import select_stages

        # Tags as they exist *after* the first sweep flipped the note
        # terminal via _terminate_invalid_source_metadata.
        post_terminal_tags = [
            "profile:ai-robotics",
            "text:abstract-only",
            "influx:text-terminal",
            "influx:source-invalid",
        ]
        sel = select_stages(
            tags=post_terminal_tags,
            archive_path="papers/arxiv/2026/04/x.pdf",
            max_profile_score=5,
            full_text_threshold=8,
            deep_extract_threshold=9,
        )
        assert sel.text_extraction_retry is False
        assert sel.abstract_only_reextraction is False
        assert sel.tier2_retry is False
        assert sel.tier3_retry is False

    async def test_backfilled_source_tag_is_persisted_on_success(self) -> None:
        """When inference backfills ``source:*`` mid-stage, the rewrite
        persists the new tag so subsequent sweeps don't re-infer."""
        items = [{"id": "n1", "title": "Paper"}]
        note = self._note_textless("n1")

        # Note that the textless fixture already lacks a source:* tag.
        # Simulate a hook that backfills the tag (as the production
        # hook does via infer_note_source) and then succeeds.
        def hook(note: dict[str, object]) -> str:
            tags = cast(list[str], note.get("tags") or [])
            if not any(t.startswith("source:") for t in tags):
                tags = [*tags, "source:arxiv"]
                note["tags"] = tags
            return "text:html"

        config = _make_config()
        client = _make_client(list_items=items, read_responses=[note])

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(text_extraction=hook),
        )

        rewritten = self._last_write_args(client)
        assert "source:arxiv" in rewritten["tags"]
        assert "text:html" in rewritten["tags"]


def _payload_result(payload: dict[str, Any]) -> MagicMock:
    """Build a fake ``CallToolResult`` whose body is *payload*."""
    text_content = MagicMock()
    text_content.text = json.dumps(payload)
    result = MagicMock()
    result.content = [text_content]
    result.isError = False
    return result


class TestSweepEnvelopeNormalisation:
    """#187 end-to-end: a repair-needed RSS note whose structured fields
    arrive nested under ``metadata`` (the real ``lithos_read`` shape) must
    be rewritten with source_url / path / source-tag intact, not stripped
    into an ``influx:source-invalid`` zombie.

    Unlike the other sweep tests this drives a *real* ``LithosClient`` with
    only the transport (``call_tool``) mocked, so the read goes through the
    real ``read_note`` envelope-normalisation (the fix site).
    """

    async def test_metadata_nested_source_survives_sweep_rewrite(self) -> None:
        from influx.lithos_client import LithosClient
        from influx.repair_hooks import infer_note_source

        note_id = "11111111-2222-3333-4444-555555555555"  # Lithos UUID id
        source_url = "https://www.alignmentforum.org/posts/x/retrying-vs-resampling"
        nested_read = {
            "id": note_id,
            "title": "Retrying vs Resampling in AI Control",
            "content": "# Retrying vs Resampling\n\n## Archive\n\n## Summary\nBody.\n",
            # Real Lithos shape: structured fields under ``metadata`` only.
            "metadata": {
                "tags": [
                    "profile:ai-foundations",
                    "source:rss",
                    "feed-slug:alignmentforum",
                    "ingested-by:influx",
                    "influx:repair-needed",
                ],
                "source_url": source_url,
                "path": "articles/rss/2026/05",
                "confidence": 0.4,
                "version": 3,
            },
        }

        def _dispatch(tool: str, args: dict[str, Any]) -> MagicMock:
            if tool == "lithos_list":
                return _payload_result({"items": [{"id": note_id}]})
            if tool == "lithos_read":
                return _payload_result(nested_read)
            if tool == "lithos_write":
                return _payload_result({"status": "updated"})
            return _payload_result({"status": "ok"})

        client = LithosClient(url="http://localhost:1234/sse")
        client.call_tool = AsyncMock(side_effect=_dispatch)  # type: ignore[method-assign]

        await sweep(
            "ai-foundations", client=client, config=_make_config(), hooks=SweepHooks()
        )

        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert write_calls, "expected a lithos_write rewrite"
        args = cast("dict[str, Any]", write_calls[0].args[1])

        # The nested-only fields must reach the rewrite — not be blanked.
        assert args["source_url"] == source_url
        assert args["path"] == "articles/rss/2026/05"
        assert "source:rss" in args["tags"]
        assert "profile:ai-foundations" in args["tags"]

        # Causal chain: the rewritten note is still source-recoverable, so a
        # subsequent sweep would NOT terminalise it as influx:source-invalid.
        rewritten = {
            "id": note_id,
            "tags": args["tags"],
            "source_url": args["source_url"],
            "path": args["path"],
            "content": args["content"],
        }
        assert infer_note_source(rewritten) == "rss"


class TestSweepNoteParse:
    """#220: ``read_note`` strips the ``# {title}`` heading from ``content``
    (title is doc-level metadata), but the parser requires it. The sweep parses
    via ``canonical_note.parse_lenient`` with ``_doc_title(note)`` as the
    fallback so archive-path / profile-relevance extraction still works —
    without it the archive path (present in the body) is silently lost and
    stage selection runs blind.

    The granular title-detection nuances (indented H1, H2 exclusion) now live
    in ``canonical_note`` and are covered by test_canonical_note.py; this class
    pins the repair-side composition (``parse_lenient`` + ``_doc_title``).
    """

    def test_reattaches_title_so_archive_path_parses(self) -> None:
        from influx.canonical_note import parse_lenient
        from influx.notes import parse_archive_path
        from influx.repair import _doc_title

        # The real read_note body shape: no leading '# Title'.
        note: dict[str, Any] = {
            "id": "rss-x",
            "title": "Some Paper",
            "content": (
                "## Archive\npath: arxiv/2026/05/2605.20049.pdf\n\n## Summary\nx\n"
            ),
        }
        parsed = parse_lenient(
            str(note.get("content") or ""), fallback_title=_doc_title(note)
        )
        assert parsed.title == "Some Paper"
        assert parse_archive_path(parsed) == "arxiv/2026/05/2605.20049.pdf"

    def test_doc_title_resolution_order(self) -> None:
        from influx.repair import _doc_title

        assert _doc_title({"title": "top"}) == "top"
        assert _doc_title({"metadata": {"title": "nested"}}) == "nested"
        assert _doc_title({"title": "", "metadata": {"title": "nested"}}) == "nested"
        assert _doc_title({"id": "x"}) == ""

    def test_none_content_parses_gracefully(self) -> None:
        from influx.canonical_note import parse_lenient
        from influx.repair import _doc_title

        # str(None) would yield "None"; the caller coerces via `or ""`.
        note: dict[str, Any] = {"id": "rss-x", "title": "T", "content": None}
        parsed = parse_lenient(
            str(note.get("content") or ""), fallback_title=_doc_title(note)
        )
        assert parsed.title == "T"


class TestSweepUnsupportedSourceArchiveConvergence:
    """A source with no reacquirer converges instead of churning forever.

    Reproduces the production shape that survived the terminal-waiver
    fix: ``influx:archive-missing`` + ``text:abstract-only`` +
    ``influx:text-terminal``, whose ``source:*`` tag has no registered
    reacquirer.  ``_reacquirer_for_note`` returns ``None``, the archive
    hook raises ``ExtractionError(stage="unsupported_source")`` before
    any network call, and — while that was classified transient — the
    counter never moved, the note never reached a cap, and every sweep
    rewrote it (observed at v108/v101 after ~36 days).
    """

    @staticmethod
    def _note(archive_attempts: int) -> dict[str, Any]:
        body = (
            "---\n"
            "source_url: https://origintrail.io/blog/shared-context-graphs\n"
            "tags: []\n"
            "confidence: 1.0\n"
            "---\n"
            "# Shared Context Graphs\n\n"
            "## Archive\n\n"
            "## Summary\nSummary\n\n"
            "## Repair\n"
            f"- archive_attempts: {archive_attempts}\n"
            '- archive_last_kind: "unsupported_source"\n'
            '- archive_last_error: "earlier failure"\n\n'
            "## Profile Relevance\n"
            "### ai-robotics\nScore: 9/10\nRelevant\n\n"
            "## User Notes\n"
        )
        return {
            "id": "n1",
            "title": "Shared Context Graphs",
            "content": body,
            "tags": [
                "influx:repair-needed",
                "influx:archive-missing",
                "text:abstract-only",
                "influx:text-terminal",
            ],
            "source_url": "https://origintrail.io/blog/shared-context-graphs",
            "confidence": 1.0,
        }

    @staticmethod
    def _unsupported(note: dict[str, object]) -> str:
        del note
        raise ExtractionError(
            "archive_download retry: source 'ai-agents-briefing' not supported",
            stage="unsupported_source",
        )

    @staticmethod
    def _write_args(client: AsyncMock) -> dict[str, Any]:
        write_calls = [
            c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
        ]
        assert write_calls, "expected lithos_write call"
        return dict(write_calls[-1].args[1])

    async def test_unsupported_source_advances_archive_counter(self) -> None:
        """Below the cap: counter moves, note stays in the sweep."""
        config = _make_config()
        client = _make_client(
            list_items=[{"id": "n1", "title": "Shared Context Graphs"}],
            read_responses=[self._note(0)],
        )

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(archive_download=self._unsupported),
        )

        rewritten = self._write_args(client)
        assert "archive_attempts: 1" in rewritten["content"]
        assert "influx:archive-terminal" not in rewritten["tags"]
        assert "influx:repair-needed" in rewritten["tags"]

    async def test_at_cap_flips_terminal_and_releases_note(self) -> None:
        """At the cap the note goes terminal and leaves the sweep set."""
        from influx.repair_counters import REPAIR_COUNTED_CAP

        config = _make_config()
        client = _make_client(
            list_items=[{"id": "n1", "title": "Shared Context Graphs"}],
            read_responses=[self._note(REPAIR_COUNTED_CAP - 1)],
        )

        await sweep(
            "ai-robotics",
            client=client,
            config=config,
            hooks=SweepHooks(archive_download=self._unsupported),
        )

        rewritten = self._write_args(client)
        assert f"archive_attempts: {REPAIR_COUNTED_CAP}" in rewritten["content"]
        assert "influx:archive-terminal" in rewritten["tags"]
        # The whole point: it stops being an immortal sweep candidate.
        assert "influx:repair-needed" not in rewritten["tags"]
        # Still factually archive-less.
        assert "influx:archive-missing" in rewritten["tags"]
