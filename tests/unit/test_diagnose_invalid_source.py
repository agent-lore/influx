"""Unit tests for the ``influx-diagnose invalid-source`` subcommand (#162).

Covers the CLI plumbing that wraps :mod:`influx.audit_invalid_source`:

* The audit / apply orchestration in ``cmd_invalid_source``.
* The async ``_fetch_invalid_source_notes`` helper resolves the
  candidate set via ``lithos_list`` (or skips it when ``--id`` is
  supplied).
* The async ``_apply_invalid_source_action`` helper rewrites a note
  with the new tag list, short-circuits when the target tag set
  matches the existing set, and surfaces unexpected write statuses
  as "refused".

The pure classification + tag-rewrite logic is exercised by
``test_audit_invalid_source.py`` — this file only pins the wiring.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


def _load_script() -> Any:
    """Load ``scripts/influx-diagnose.py`` as a module for direct testing."""
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "influx_diagnose_invalid_source",
        repo_root / "scripts" / "influx-diagnose.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DIAGNOSE = _load_script()


# ── Shared fixtures ───────────────────────────────────────────────────


def _invalid_note(
    *,
    note_id: str,
    source_url: str = "",
    path: str = "",
    title: str = "Title",
) -> dict[str, Any]:
    return {
        "id": note_id,
        "title": title,
        "path": path,
        "source_url": source_url,
        "content": "## Summary\nfoo\n",
        "tags": [
            "profile:retro-computing",
            "text:abstract-only",
            "influx:text-terminal",
            "influx:source-invalid",
        ],
        "confidence": 0.5,
        "note_type": "summary",
        "namespace": "influx",
    }


def _make_client(notes: list[dict[str, Any]]) -> MagicMock:
    """Build a mock ``LithosClient`` that returns *notes* for list+read."""
    client = MagicMock()
    client.list_notes_body = AsyncMock(
        return_value={"items": [{"id": n["id"]} for n in notes]}
    )
    by_id = {n["id"]: n for n in notes}

    async def _read(*, note_id: str) -> dict[str, Any]:
        return by_id[note_id]

    client.read_note = AsyncMock(side_effect=_read)

    write_result = MagicMock()
    write_result.status = "updated"
    client.write_note = AsyncMock(return_value=write_result)
    client.close = AsyncMock()
    return client


# ── _fetch_invalid_source_notes ───────────────────────────────────────


class TestFetchInvalidSourceNotes:
    """The fetch helper uses lithos_list by default and lithos_read per id."""

    def test_list_scan_returns_full_notes(self) -> None:
        notes = [_invalid_note(note_id="a"), _invalid_note(note_id="b")]
        client = _make_client(notes)

        with patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client):
            result = asyncio.run(
                _DIAGNOSE._fetch_invalid_source_notes(
                    lithos_url="http://example/sse",
                )
            )

        # lithos_list was queried with the invalid-source tag.
        client.list_notes_body.assert_awaited_once()
        kwargs = client.list_notes_body.await_args.kwargs
        assert kwargs["tags"] == ["influx:source-invalid"]

        # Each listed id was read for the full body.
        assert client.read_note.await_count == 2
        assert [n["id"] for n in result] == ["a", "b"]

    def test_id_list_bypasses_list_scan(self) -> None:
        notes = [_invalid_note(note_id="a")]
        client = _make_client(notes)

        with patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client):
            result = asyncio.run(
                _DIAGNOSE._fetch_invalid_source_notes(
                    lithos_url="http://example/sse",
                    note_ids=["a"],
                )
            )

        # list_notes is NOT called when ids are passed explicitly.
        client.list_notes_body.assert_not_awaited()
        # Read was still called for the explicit id.
        client.read_note.assert_awaited_once_with(note_id="a")
        assert [n["id"] for n in result] == ["a"]

    def test_read_failure_is_skipped_with_warning(self, capsys: Any) -> None:
        notes = [_invalid_note(note_id="ok")]
        client = _make_client(notes)

        async def _read(*, note_id: str) -> dict[str, Any]:
            if note_id == "missing":
                raise RuntimeError("doc not found")
            return notes[0]

        client.read_note = AsyncMock(side_effect=_read)

        with patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client):
            result = asyncio.run(
                _DIAGNOSE._fetch_invalid_source_notes(
                    lithos_url="http://example/sse",
                    note_ids=["missing", "ok"],
                )
            )

        # Only the readable note made it through.
        assert [n["id"] for n in result] == ["ok"]
        captured = capsys.readouterr()
        assert "warning: read failed for missing" in captured.err


# ── _apply_invalid_source_action ──────────────────────────────────────


class TestApplyInvalidSourceAction:
    """The apply helper rewrites notes and surfaces unexpected outcomes."""

    def test_writes_with_new_tag_set(self) -> None:
        note = _invalid_note(note_id="a", source_url="https://arxiv.org/abs/1.2")
        client = _make_client([note])

        new_tags = list(note["tags"]) + ["source:arxiv"]

        with patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client):
            outcome, reason = asyncio.run(
                _DIAGNOSE._apply_invalid_source_action(
                    lithos_url="http://example/sse",
                    note=note,
                    new_tags=new_tags,
                    agent="influx-diagnose",
                )
            )

        assert outcome == "applied"
        assert reason == "updated"
        client.write_note.assert_awaited_once()
        call_kwargs = client.write_note.await_args.kwargs
        assert call_kwargs["tags"] == new_tags
        assert call_kwargs["agent"] == "influx-diagnose"
        # Confidence / note_type / namespace round-trip from the read note.
        assert call_kwargs["confidence"] == 0.5
        assert call_kwargs["note_type"] == "summary"

    def test_already_clean_skips_write(self) -> None:
        note = _invalid_note(note_id="a")
        client = _make_client([note])

        # New tag set is identical to the existing one (order irrelevant).
        new_tags = list(reversed(note["tags"]))

        with patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client):
            outcome, reason = asyncio.run(
                _DIAGNOSE._apply_invalid_source_action(
                    lithos_url="http://example/sse",
                    note=note,
                    new_tags=new_tags,
                    agent="influx-diagnose",
                )
            )

        assert outcome == "already_clean"
        client.write_note.assert_not_awaited()

    def test_unexpected_status_is_refused(self) -> None:
        note = _invalid_note(note_id="a")
        client = _make_client([note])

        bad_result = MagicMock()
        bad_result.status = "invalid_input"
        client.write_note = AsyncMock(return_value=bad_result)

        with patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client):
            outcome, reason = asyncio.run(
                _DIAGNOSE._apply_invalid_source_action(
                    lithos_url="http://example/sse",
                    note=note,
                    new_tags=list(note["tags"]) + ["foo"],
                    agent="influx-diagnose",
                )
            )

        assert outcome == "refused"
        assert "invalid_input" in reason


# ── cmd_invalid_source — end-to-end orchestration ─────────────────────


def _build_args(**overrides: Any) -> argparse.Namespace:
    base = {
        "env": "staging",
        "id": None,
        "limit": None,
        "apply": False,
        "yes": None,
        "yes_to_all": False,
        "agent": "influx-diagnose",
        "lithos_url": "http://example/sse",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCmdInvalidSourceReadOnly:
    """Default mode prints the audit report and never calls write_note."""

    def test_reports_and_does_not_write(self, capsys: Any) -> None:
        notes = [
            _invalid_note(note_id="recoverable", source_url="https://arxiv.org/abs/1"),
            _invalid_note(note_id="unrecoverable"),
        ]
        client = _make_client(notes)

        with (
            patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client),
            patch.object(_DIAGNOSE, "_load_env", return_value={}),
            patch.object(_DIAGNOSE, "_read_lithos_url", return_value="http://x/sse"),
            patch.object(_DIAGNOSE, "_ensure_project_runtime_or_reexec"),
        ):
            rc = _DIAGNOSE.cmd_invalid_source(_build_args())

        assert rc == 0
        client.write_note.assert_not_awaited()

        out = capsys.readouterr().out
        assert "[RECONSTRUCT] recoverable" in out
        assert "[TOMBSTONE] unrecoverable" in out
        assert "1 recoverable, 1 unrecoverable" in out
        # The footer prompts the operator to add --apply.
        assert "--apply" in out


class TestCmdInvalidSourceApply:
    """``--apply`` paths rewrite notes per the audit's recommendation."""

    def test_apply_yes_to_all_writes_each_note_with_recommended_tags(
        self, capsys: Any
    ) -> None:
        notes = [
            _invalid_note(note_id="r", source_url="https://arxiv.org/abs/1"),
            _invalid_note(note_id="u"),
        ]
        client = _make_client(notes)

        with (
            patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client),
            patch.object(_DIAGNOSE, "_load_env", return_value={}),
            patch.object(_DIAGNOSE, "_read_lithos_url", return_value="http://x/sse"),
            patch.object(_DIAGNOSE, "_ensure_project_runtime_or_reexec"),
        ):
            rc = _DIAGNOSE.cmd_invalid_source(_build_args(apply=True, yes_to_all=True))

        assert rc == 0
        # Two write_note calls: one reconstruct, one tombstone.
        assert client.write_note.await_count == 2

        # Look at the tag sets passed to write_note.
        writes_by_id: dict[str, list[str]] = {}
        for call in client.write_note.await_args_list:
            kwargs = call.kwargs
            writes_by_id[kwargs["source_url"] or kwargs["path"] or "u"] = list(
                kwargs["tags"]
            )

        # The reconstruct candidate (source_url set) gets the
        # backfilled source tag and the repair-needed tag.
        recon_tags = writes_by_id["https://arxiv.org/abs/1"]
        assert "source:arxiv" in recon_tags
        assert "influx:repair-needed" in recon_tags
        assert "influx:source-invalid" not in recon_tags
        assert "influx:text-terminal" not in recon_tags

        # The tombstone candidate gets the tombstone tag without
        # losing its in-band terminal state.
        tomb_tags = writes_by_id["u"]
        assert "influx:tombstone" in tomb_tags
        assert "influx:source-invalid" in tomb_tags
        assert "influx:text-terminal" in tomb_tags

        out = capsys.readouterr().out
        assert "RECONSTRUCT" in out
        assert "TOMBSTONE" in out
        assert "applied=2" in out

    def test_apply_without_confirmation_aborts(self) -> None:
        notes = [_invalid_note(note_id="r", source_url="https://arxiv.org/abs/1")]
        client = _make_client(notes)

        with (
            patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client),
            patch.object(_DIAGNOSE, "_load_env", return_value={}),
            patch.object(_DIAGNOSE, "_read_lithos_url", return_value="http://x/sse"),
            patch.object(_DIAGNOSE, "_ensure_project_runtime_or_reexec"),
        ):
            try:
                _DIAGNOSE.cmd_invalid_source(_build_args(apply=True))
            except SystemExit as exc:
                # ``sys.exit`` is invoked with an error message.
                assert "--apply requires" in str(exc.code)
            else:
                raise AssertionError("expected SystemExit when --apply has no confirm")

    def test_apply_yes_per_id_only_writes_named_note(self, capsys: Any) -> None:
        notes = [
            _invalid_note(note_id="r", source_url="https://arxiv.org/abs/1"),
            _invalid_note(note_id="other", source_url="https://arxiv.org/abs/2"),
        ]
        client = _make_client(notes)

        with (
            patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client),
            patch.object(_DIAGNOSE, "_load_env", return_value={}),
            patch.object(_DIAGNOSE, "_read_lithos_url", return_value="http://x/sse"),
            patch.object(_DIAGNOSE, "_ensure_project_runtime_or_reexec"),
        ):
            rc = _DIAGNOSE.cmd_invalid_source(_build_args(apply=True, yes=["r"]))

        assert rc == 0
        # Only one write — the one named via --yes r.
        assert client.write_note.await_count == 1
        call_kwargs = client.write_note.await_args_list[0].kwargs
        assert "source:arxiv" in call_kwargs["tags"]

    def test_apply_id_only_writes_that_note_no_list_scan(self) -> None:
        notes = [_invalid_note(note_id="r", source_url="https://arxiv.org/abs/1")]
        client = _make_client(notes)

        with (
            patch.object(_DIAGNOSE, "_make_lithos_client", return_value=client),
            patch.object(_DIAGNOSE, "_load_env", return_value={}),
            patch.object(_DIAGNOSE, "_read_lithos_url", return_value="http://x/sse"),
            patch.object(_DIAGNOSE, "_ensure_project_runtime_or_reexec"),
        ):
            rc = _DIAGNOSE.cmd_invalid_source(_build_args(apply=True, id=["r"]))

        assert rc == 0
        # No list scan when --id is used.
        client.list_notes_body.assert_not_awaited()
        client.write_note.assert_awaited_once()
