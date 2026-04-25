"""Unit tests for hook-mutation rollback (finding #1).

Hooks (``archive_download``, ``re_extract_archive``, ``tier2_enrich``,
``tier3_extract``) receive the live mutable note dict.  When a hook
raises ``ExtractionError`` / ``LithosError`` the sweep MUST treat that
as "stage failed this pass" (per US-003 / US-013) and MUST NOT persist
any partial in-place mutations the hook applied before raising.

Each test below provides a fake hook that mutates ``note["tags"]`` (and
sometimes ``note["content"]``) and then raises; the sweep is expected
to roll the note state back so the eventual ``lithos_write`` payload
does NOT carry the hook's mutations.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from influx.config import AppConfig, RepairConfig
from influx.errors import ExtractionError, LithosError
from influx.repair import (
    ExtractionOutcome,
    ReExtractionResult,
    SweepHooks,
    apply_abstract_only_reextraction,
    sweep,
)

# ── Helpers (mirror test_repair_sweep.py) ───────────────────────────


def _make_list_result(items: list[dict[str, Any]]) -> MagicMock:
    text_content = MagicMock()
    text_content.text = json.dumps({"items": items})
    result = MagicMock()
    result.content = [text_content]
    return result


def _make_write_result(status: str = "updated") -> MagicMock:
    text_content = MagicMock()
    text_content.text = json.dumps({"status": status})
    result = MagicMock()
    result.content = [text_content]
    return result


def _make_config(max_items: int = 100) -> MagicMock:
    config = MagicMock(spec=AppConfig)
    config.repair = MagicMock(spec=RepairConfig)
    config.repair.max_items_per_run = max_items
    config.profiles = []
    return config


def _make_client(
    list_items: list[dict[str, Any]],
    read_responses: list[dict[str, Any]],
    write_status: str = "updated",
) -> AsyncMock:
    client = AsyncMock()
    client.list_notes = AsyncMock(return_value=_make_list_result(list_items))
    client.read_note = AsyncMock(side_effect=read_responses)
    client.call_tool = AsyncMock(return_value=_make_write_result(write_status))
    return client


def _last_write_args(client: AsyncMock) -> dict[str, Any]:
    write_calls = [
        c for c in client.call_tool.call_args_list if c.args[0] == "lithos_write"
    ]
    assert write_calls, "expected at least one lithos_write call"
    args: dict[str, Any] = write_calls[-1].args[1]
    return args


# ── archive_download rollback ───────────────────────────────────────


class TestArchiveDownloadRollback:
    """A failing archive hook must not leak partial mutations."""

    async def test_archive_hook_mutation_then_raise_is_rolled_back(
        self,
    ) -> None:
        items = [{"id": "n1", "title": "Paper"}]
        # archive-missing is present so the archive stage selects.
        read_notes = [
            {
                "id": "n1",
                "title": "Paper",
                "content": "## Archive\n",
                "tags": [
                    "influx:repair-needed",
                    "influx:archive-missing",
                    "text:html",
                ],
            }
        ]

        def bad_archive(note: dict[str, object]) -> str:
            # Mutate before raising — a buggy hook must not leak this.
            tags = list(note.get("tags", []))  # type: ignore[arg-type]
            tags.append("full-text")
            note["tags"] = tags
            note["content"] = "MUTATED-BY-HOOK"
            raise ExtractionError("download failed", stage="archive")

        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)
        hooks = SweepHooks(archive_download=bad_archive)

        await sweep("p", client=client, config=config, hooks=hooks)

        args = _last_write_args(client)
        # Mutated tag from the failing hook MUST NOT be in the rewrite.
        assert "full-text" not in args["tags"]
        # Mutated content MUST NOT be persisted — the original archive
        # marker is what the rewrite carries.
        assert args["content"] == "## Archive\n"
        # influx:archive-missing remains because the stage did NOT
        # succeed and there is no archive path to clear it.
        assert "influx:archive-missing" in args["tags"]


# ── re_extract_archive rollback ─────────────────────────────────────


class TestReExtractArchiveRollback:
    """A failing abstract-only hook must not leak partial mutations."""

    async def test_re_extract_hook_mutation_then_raise_is_rolled_back(
        self,
    ) -> None:
        items = [{"id": "n1", "title": "Paper"}]
        read_notes = [
            {
                "id": "n1",
                "title": "Paper",
                "content": "## Archive\npath: a.pdf\n",
                "tags": [
                    "influx:repair-needed",
                    "text:abstract-only",
                ],
            }
        ]

        def bad_re_extract(
            note: dict[str, object], archive_path: str
        ) -> ReExtractionResult:
            tags = list(note.get("tags", []))  # type: ignore[arg-type]
            tags.append("influx:text-terminal")
            note["tags"] = tags
            note["content"] = "MUTATED"
            raise ExtractionError("re-extract failed", stage="text-extract")

        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)
        hooks = SweepHooks(re_extract_archive=bad_re_extract)

        await sweep("p", client=client, config=config, hooks=hooks)

        args = _last_write_args(client)
        # influx:text-terminal MUST NOT be present — the hook raised,
        # so the Terminal-outcome semantics do not apply.
        assert "influx:text-terminal" not in args["tags"]
        # text:abstract-only is preserved.
        assert "text:abstract-only" in args["tags"]
        # Content rolled back to the original archive section.
        assert args["content"] == "## Archive\npath: a.pdf\n"

    def test_helper_restores_note_state_on_extraction_error(self) -> None:
        """``apply_abstract_only_reextraction`` rolls back on raise."""
        note: dict[str, Any] = {
            "id": "n1",
            "tags": ["text:abstract-only", "influx:repair-needed"],
            "content": "ORIGINAL",
        }
        original_tags = list(note["tags"])
        original_content = note["content"]

        def bad_hook(note: dict[str, object], archive_path: str) -> ReExtractionResult:
            note["tags"] = list(note.get("tags", [])) + [  # type: ignore[arg-type]
                "influx:text-terminal",
            ]
            note["content"] = "MUTATED"
            raise ExtractionError("transient", stage="text-extract")

        result = apply_abstract_only_reextraction(
            tags=original_tags,
            note=note,
            archive_path="a.pdf",
            hook=bad_hook,
        )

        assert result == original_tags
        assert note["tags"] == original_tags
        assert note["content"] == original_content

    def test_helper_restores_note_state_on_lithos_error(self) -> None:
        """``apply_abstract_only_reextraction`` rolls back on LithosError."""
        note: dict[str, Any] = {
            "id": "n1",
            "tags": ["text:abstract-only"],
            "content": "ORIGINAL",
        }

        def bad_hook(note: dict[str, object], archive_path: str) -> ReExtractionResult:
            note["tags"] = ["mutated"]
            raise LithosError("write failed", operation="write_note")

        result = apply_abstract_only_reextraction(
            tags=note["tags"],
            note=note,
            archive_path="a.pdf",
            hook=bad_hook,
        )

        assert result == ["text:abstract-only"]
        assert note["tags"] == ["text:abstract-only"]
        assert note["content"] == "ORIGINAL"

    def test_helper_terminal_outcome_still_applies(self) -> None:
        """A successful hook return still drives the documented mutation."""
        note: dict[str, Any] = {
            "id": "n1",
            "tags": ["text:abstract-only"],
            "content": "C",
        }

        def terminal_hook(
            note: dict[str, object], archive_path: str
        ) -> ReExtractionResult:
            return ReExtractionResult(outcome=ExtractionOutcome.TERMINAL)

        result = apply_abstract_only_reextraction(
            tags=note["tags"],
            note=note,
            archive_path="a.pdf",
            hook=terminal_hook,
        )

        assert "influx:text-terminal" in result
        assert "text:abstract-only" in result


# ── tier2_enrich rollback ───────────────────────────────────────────


class TestTier2EnrichRollback:
    """A failing tier2 hook must not leak ``full-text`` into the rewrite."""

    async def test_tier2_hook_appends_full_text_then_raises(self) -> None:
        items = [{"id": "n1", "title": "Paper"}]
        # Score 9 ≥ default full_text threshold (8) so tier2 is selected.
        read_notes = [
            {
                "id": "n1",
                "title": "Paper",
                "content": (
                    "## Archive\npath: a.pdf\n"
                    "## Profile Relevance\n"
                    "- profile: p, score: 9\n"
                ),
                "tags": [
                    "influx:repair-needed",
                    "text:html",
                    "profile:p",
                ],
            }
        ]

        def bad_tier2(note: dict[str, object]) -> None:
            tags = list(note.get("tags", []))  # type: ignore[arg-type]
            tags.append("full-text")
            note["tags"] = tags
            raise ExtractionError("tier2 failed", stage="full-text")

        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)
        hooks = SweepHooks(tier2_enrich=bad_tier2)

        await sweep("p", client=client, config=config, hooks=hooks)

        args = _last_write_args(client)
        # full-text mutation from the failing hook MUST NOT survive.
        assert "full-text" not in args["tags"]
        # Stage failed so influx:repair-needed must remain.
        assert "influx:repair-needed" in args["tags"]


# ── tier3_extract rollback ──────────────────────────────────────────


class TestTier3ExtractRollback:
    """A failing tier3 hook must not leak ``influx:deep-extracted``."""

    async def test_tier3_hook_appends_deep_extracted_then_raises(
        self,
    ) -> None:
        items = [{"id": "n1", "title": "Paper"}]
        # Score 9 ≥ default deep_extract threshold (9) so tier3 selects.
        read_notes = [
            {
                "id": "n1",
                "title": "Paper",
                "content": (
                    "## Archive\npath: a.pdf\n"
                    "## Profile Relevance\n"
                    "- profile: p, score: 9\n"
                ),
                "tags": [
                    "influx:repair-needed",
                    "text:html",
                    "full-text",
                    "profile:p",
                ],
            }
        ]

        def bad_tier3(note: dict[str, object]) -> None:
            tags = list(note.get("tags", []))  # type: ignore[arg-type]
            tags.append("influx:deep-extracted")
            note["tags"] = tags
            raise ExtractionError("tier3 failed", stage="deep-extract")

        config = _make_config()
        client = _make_client(list_items=items, read_responses=read_notes)
        hooks = SweepHooks(tier3_extract=bad_tier3)

        await sweep("p", client=client, config=config, hooks=hooks)

        args = _last_write_args(client)
        # influx:deep-extracted from the failing hook MUST NOT survive.
        assert "influx:deep-extracted" not in args["tags"]
        # Stage failed, so influx:repair-needed must remain.
        assert "influx:repair-needed" in args["tags"]
