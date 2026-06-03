"""Unit tests for ``./scripts/influx-diagnose.py strip-embedded-frontmatter``.

The subcommand strips the redundant embedded YAML frontmatter block from
the ~1,241 legacy Influx notes (created 2026-05-01 → 2026-05-24) whose
doc-level metadata is already valid.  Unlike ``rewrite-legacy-notes``
(#176), which recovers EMPTY metadata from the embedded block, this job
leaves doc-level metadata untouched and rewrites only ``content`` so a
cleaned note matches the current (post-05-24) shape exactly.

See agent-lore/influx#225.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _load_script() -> Any:
    """Load ``scripts/influx-diagnose.py`` as a module for direct testing."""
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "influx_diagnose",
        repo_root / "scripts" / "influx-diagnose.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DIAGNOSE = _load_script()


# ── Fixtures ────────────────────────────────────────────────────────


# read_note content shape for a #225 legacy note: Lithos has already
# stripped the one ``# Title`` it prepended on save, so the body STARTS
# with the redundant embedded frontmatter fence, then the renderer's
# title heading, then the section body.
LEGACY_READ_CONTENT = """---
note_type: summary
namespace: influx
source_url: https://huggingface.co/blog/1b-sentence-embeddings
tags:
  - profile:knowledge-systems
  - source:blog
  - feed-slug:huggingface-blog
  - ingested-by:influx
  - schema:1
confidence: 1.0
---
# Train a Sentence Embedding Model with 1B Training Pairs

## Archive
path: blog/2021/10/huggingface-blog-2021-10-25-8c9a1e482e.html

## Summary
### Contributions
- Developed a sentence embedding model trained on 1 billion pairs

## User Notes
hand-written note, must survive byte-for-byte
"""

# What the cleaned content must look like — identical to a current,
# post-05-24 note's read shape (title heading + sections, no fence).
CLEANED_READ_CONTENT = """# Train a Sentence Embedding Model with 1B Training Pairs

## Archive
path: blog/2021/10/huggingface-blog-2021-10-25-8c9a1e482e.html

## Summary
### Contributions
- Developed a sentence embedding model trained on 1 billion pairs

## User Notes
hand-written note, must survive byte-for-byte
"""


def _legacy_doc(doc_id: str = "legacy-225-id") -> dict[str, Any]:
    """read_note envelope for a legacy note: valid metadata, bloated content."""
    return {
        "id": doc_id,
        "title": "Train a Sentence Embedding Model with 1B Training Pairs",
        "content": LEGACY_READ_CONTENT,
        "path": None,
        "source_url": "https://huggingface.co/blog/1b-sentence-embeddings",
        "tags": [
            "profile:knowledge-systems",
            "source:blog",
            "feed-slug:huggingface-blog",
            "ingested-by:influx",
            "schema:1",
        ],
        "confidence": 1.0,
        "note_type": "summary",
        "namespace": "influx",
        "version": 2,
        "author": "influx",
        "metadata": {
            "author": "influx",
            "title": "Train a Sentence Embedding Model with 1B Training Pairs",
            "source_url": "https://huggingface.co/blog/1b-sentence-embeddings",
            "tags": [
                "profile:knowledge-systems",
                "source:blog",
                "feed-slug:huggingface-blog",
                "ingested-by:influx",
                "schema:1",
            ],
            "confidence": 1.0,
            "note_type": "summary",
            "namespace": "influx",
            "version": 2,
        },
    }


def _clean_doc(doc_id: str = "clean-id") -> dict[str, Any]:
    """A current-shape note: content starts with the title heading, no fence."""
    return {
        "id": doc_id,
        "title": "Already Clean",
        "content": "# Already Clean\n\n## Archive\nClean body.\n\n## User Notes\n",
        "path": None,
        "source_url": "https://example.com/clean",
        "tags": ["profile:ai-foundations", "ingested-by:influx"],
        "confidence": 1.0,
        "note_type": "summary",
        "namespace": "influx",
        "version": 1,
        "author": "influx",
        "metadata": {"author": "influx", "version": 1},
    }


def _make_args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "env": "staging",
        "apply": False,
        "yes": None,
        "yes_to_all": False,
        "id": None,
        "limit": None,
        "lithos_url": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _patch_runtime(client: MagicMock) -> Any:
    """Context-manager bundle of patches every cmd test needs."""
    return patch.multiple(
        _DIAGNOSE,
        _make_lithos_client=lambda url: client,
        _load_env=lambda env: {},
        _read_lithos_url=lambda args, env: "http://stub.lithos/sse",
        _ensure_project_runtime_or_reexec=lambda: None,
    )


def _write_ok_result() -> MagicMock:
    """A lithos_write response object decoding to ``status=updated``."""
    result = MagicMock()
    result.content = [MagicMock(text='{"status": "updated"}')]
    return result


# ── strip_embedded_frontmatter_block ────────────────────────────────


class TestStripEmbeddedFrontmatterBlock:
    """Pure content transform."""

    def test_strips_block_to_current_shape(self) -> None:
        assert (
            _DIAGNOSE.strip_embedded_frontmatter_block(LEGACY_READ_CONTENT)
            == CLEANED_READ_CONTENT
        )

    def test_returns_none_when_no_fence(self) -> None:
        # Current-shape note: starts with the title heading, not a fence.
        assert (
            _DIAGNOSE.strip_embedded_frontmatter_block("# Title\n\n## Archive\nbody\n")
            is None
        )

    def test_idempotent_on_cleaned_content(self) -> None:
        # Re-running over an already-cleaned note is a no-op (returns None).
        assert _DIAGNOSE.strip_embedded_frontmatter_block(CLEANED_READ_CONTENT) is None

    def test_preserves_user_notes_byte_for_byte(self) -> None:
        out = _DIAGNOSE.strip_embedded_frontmatter_block(LEGACY_READ_CONTENT)
        assert out is not None
        assert out.endswith(
            "## User Notes\nhand-written note, must survive byte-for-byte\n"
        )

    def test_line_anchored_not_fooled_by_dashes_in_value(self) -> None:
        # A frontmatter VALUE containing ``---`` must not truncate the split;
        # the closing fence is the line-anchored ``---``, not the substring.
        content = (
            "---\n"
            "source_url: https://example.com/a---b\n"
            "tags:\n"
            "  - source:blog\n"
            "---\n"
            "# Title\n\n## Archive\nbody\n"
        )
        out = _DIAGNOSE.strip_embedded_frontmatter_block(content)
        assert out == "# Title\n\n## Archive\nbody\n"


# ── _select_embedded_frontmatter_doc_ids_from_corpus ────────────────


def _write_note(
    articles: Path,
    rel: str,
    *,
    doc_id: str,
    author: str,
    body: str,
) -> None:
    """Write an on-disk note with an outer doc-level frontmatter block."""
    path = articles / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    outer = f"---\nid: {doc_id}\nauthor: {author}\ntitle: A Title\n---\n"
    path.write_text(outer + body, encoding="utf-8")


# On disk, Lithos prepends its own title, so the legacy body carries the
# title heading twice around the embedded fence.
_LEGACY_DISK_BODY = (
    "\n\n# A Title\n\n"
    "---\n"
    "note_type: summary\n"
    "source_url: https://example.com/x\n"
    "tags:\n  - source:blog\n"
    "---\n"
    "# A Title\n\n## Archive\nbody\n"
)
_CLEAN_DISK_BODY = "\n\n# A Title\n\n# A Title\n\n## Archive\nbody\n"


class TestSelectEmbeddedFrontmatterDocIds:
    def test_selects_influx_legacy_skips_clean_and_non_influx(
        self, tmp_path: Path
    ) -> None:
        articles = tmp_path / "articles"
        _write_note(
            articles,
            "a.md",
            doc_id="legacy-1",
            author="influx",
            body=_LEGACY_DISK_BODY,
        )
        _write_note(
            articles,
            "b.md",
            doc_id="clean-1",
            author="influx",
            body=_CLEAN_DISK_BODY,
        )
        _write_note(
            articles,
            "c.md",
            doc_id="other-1",
            author="someone-else",
            body=_LEGACY_DISK_BODY,
        )
        ids = _DIAGNOSE._select_embedded_frontmatter_doc_ids_from_corpus(articles)
        assert ids == ["legacy-1"]

    def test_missing_articles_dir_returns_empty(self, tmp_path: Path) -> None:
        ids = _DIAGNOSE._select_embedded_frontmatter_doc_ids_from_corpus(
            tmp_path / "does-not-exist"
        )
        assert ids == []

    def test_deduplicates_repeated_ids(self, tmp_path: Path) -> None:
        articles = tmp_path / "articles"
        _write_note(
            articles,
            "a.md",
            doc_id="dup",
            author="influx",
            body=_LEGACY_DISK_BODY,
        )
        _write_note(
            articles,
            "nested/a.md",
            doc_id="dup",
            author="influx",
            body=_LEGACY_DISK_BODY,
        )
        ids = _DIAGNOSE._select_embedded_frontmatter_doc_ids_from_corpus(articles)
        assert ids == ["dup"]


# ── _process_one_embedded_frontmatter_doc ───────────────────────────


class TestProcessOneEmbeddedFrontmatterDoc:
    @pytest.mark.asyncio
    async def test_dry_run_plans_strip(self) -> None:
        client = MagicMock()
        client.read_note = AsyncMock(return_value=_legacy_doc())
        client.call_tool = AsyncMock()

        outcome, _reason, plan = await _DIAGNOSE._process_one_embedded_frontmatter_doc(
            client=client, doc_id="legacy-225-id", apply=False
        )
        assert outcome == _DIAGNOSE._EMBEDDED_FM_OUTCOME_PLANNED
        assert plan is not None
        assert plan["new_content_len"] < plan["old_content_len"]
        client.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_writes_cleaned_content_and_preserves_metadata(
        self,
    ) -> None:
        client = MagicMock()
        client.read_note = AsyncMock(return_value=_legacy_doc())
        client.call_tool = AsyncMock(return_value=_write_ok_result())

        outcome, _reason, _plan = await _DIAGNOSE._process_one_embedded_frontmatter_doc(
            client=client, doc_id="legacy-225-id", apply=True
        )
        assert outcome == _DIAGNOSE._EMBEDDED_FM_OUTCOME_STRIPPED

        client.call_tool.assert_awaited_once()
        tool_name, write_args = client.call_tool.await_args.args
        assert tool_name == "lithos_write"
        # Content rewritten to the current shape — no embedded fence.
        assert write_args["content"] == CLEANED_READ_CONTENT
        assert "\n---\n" not in write_args["content"]
        # Doc-level metadata preserved byte-for-byte.
        assert write_args["source_url"] == (
            "https://huggingface.co/blog/1b-sentence-embeddings"
        )
        assert write_args["tags"] == [
            "profile:knowledge-systems",
            "source:blog",
            "feed-slug:huggingface-blog",
            "ingested-by:influx",
            "schema:1",
        ]
        assert write_args["confidence"] == 1.0
        assert write_args["expected_version"] == 2

    @pytest.mark.asyncio
    async def test_skips_already_clean_note(self) -> None:
        client = MagicMock()
        client.read_note = AsyncMock(return_value=_clean_doc())
        client.call_tool = AsyncMock()

        outcome, _reason, plan = await _DIAGNOSE._process_one_embedded_frontmatter_doc(
            client=client, doc_id="clean-id", apply=True
        )
        assert outcome == _DIAGNOSE._EMBEDDED_FM_OUTCOME_SKIPPED_ALREADY_CLEAN
        assert plan is None
        client.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_non_influx_authored(self) -> None:
        doc = _legacy_doc()
        doc["author"] = "someone-else"
        doc["metadata"]["author"] = "someone-else"
        client = MagicMock()
        client.read_note = AsyncMock(return_value=doc)
        client.call_tool = AsyncMock()

        outcome, _reason, _plan = await _DIAGNOSE._process_one_embedded_frontmatter_doc(
            client=client, doc_id="legacy-225-id", apply=True
        )
        assert outcome == _DIAGNOSE._EMBEDDED_FM_OUTCOME_REFUSED_NON_INFLUX
        client.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_failure_is_reported(self) -> None:
        client = MagicMock()
        client.read_note = AsyncMock(side_effect=RuntimeError("boom"))
        outcome, reason, _plan = await _DIAGNOSE._process_one_embedded_frontmatter_doc(
            client=client, doc_id="legacy-225-id", apply=True
        )
        assert outcome == _DIAGNOSE._EMBEDDED_FM_OUTCOME_FAILED
        assert "read failed" in reason

    @pytest.mark.asyncio
    async def test_unexpected_write_status_is_failure(self) -> None:
        result = MagicMock()
        result.content = [MagicMock(text='{"status": "rejected"}')]
        client = MagicMock()
        client.read_note = AsyncMock(return_value=_legacy_doc())
        client.call_tool = AsyncMock(return_value=result)

        outcome, reason, _plan = await _DIAGNOSE._process_one_embedded_frontmatter_doc(
            client=client, doc_id="legacy-225-id", apply=True
        )
        assert outcome == _DIAGNOSE._EMBEDDED_FM_OUTCOME_FAILED
        assert "rejected" in reason


# ── cmd_strip_embedded_frontmatter ──────────────────────────────────


class TestCmdStripEmbeddedFrontmatter:
    def test_apply_without_confirmation_aborts(self) -> None:
        with (
            patch.object(_DIAGNOSE, "_load_env", lambda env: {}),
            pytest.raises(SystemExit),
        ):
            _DIAGNOSE.cmd_strip_embedded_frontmatter(_make_args(apply=True))

    def test_dry_run_audit_lists_and_counts(self, capsys: Any) -> None:
        client = MagicMock()
        client.read_note = AsyncMock(return_value=_legacy_doc())
        client.call_tool = AsyncMock()
        client.close = AsyncMock()

        with _patch_runtime(client):
            rc = _DIAGNOSE.cmd_strip_embedded_frontmatter(
                _make_args(id=["legacy-225-id"])
            )
        out = capsys.readouterr().out
        assert rc == 0
        assert "would_strip" in out
        assert "Summary:" in out
        client.call_tool.assert_not_called()

    def test_apply_with_id_strips_without_yes(self) -> None:
        client = MagicMock()
        client.read_note = AsyncMock(return_value=_legacy_doc())
        client.call_tool = AsyncMock(return_value=_write_ok_result())
        client.close = AsyncMock()

        with _patch_runtime(client):
            rc = _DIAGNOSE.cmd_strip_embedded_frontmatter(
                _make_args(apply=True, id=["legacy-225-id"])
            )
        assert rc == 0
        client.call_tool.assert_awaited_once()

    def test_apply_yes_to_all_over_corpus_scan(self) -> None:
        client = MagicMock()
        client.read_note = AsyncMock(return_value=_legacy_doc())
        client.call_tool = AsyncMock(return_value=_write_ok_result())
        client.close = AsyncMock()

        patches = patch.multiple(
            _DIAGNOSE,
            _make_lithos_client=lambda url: client,
            _load_env=lambda env: {},
            _read_lithos_url=lambda args, env: "http://stub.lithos/sse",
            _ensure_project_runtime_or_reexec=lambda: None,
            _resolve_corpus_articles_path=lambda env: Path("/unused"),
            _select_embedded_frontmatter_doc_ids_from_corpus=(
                lambda p: ["legacy-225-id"]
            ),
        )
        with patches:
            rc = _DIAGNOSE.cmd_strip_embedded_frontmatter(
                _make_args(apply=True, yes_to_all=True)
            )
        assert rc == 0
        client.call_tool.assert_awaited_once()

    def test_limit_caps_candidate_set(self) -> None:
        client = MagicMock()
        client.read_note = AsyncMock(return_value=_legacy_doc())
        client.call_tool = AsyncMock()
        client.close = AsyncMock()

        patches = patch.multiple(
            _DIAGNOSE,
            _make_lithos_client=lambda url: client,
            _load_env=lambda env: {},
            _read_lithos_url=lambda args, env: "http://stub.lithos/sse",
            _ensure_project_runtime_or_reexec=lambda: None,
            _resolve_corpus_articles_path=lambda env: Path("/unused"),
            _select_embedded_frontmatter_doc_ids_from_corpus=(
                lambda p: ["a", "b", "c", "d"]
            ),
        )
        with patches:
            _DIAGNOSE.cmd_strip_embedded_frontmatter(_make_args(limit=2))
        # Only the first two candidates are read.
        assert client.read_note.await_count == 2
