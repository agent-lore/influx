"""Unit tests for ``./scripts/influx-diagnose.py rewrite-legacy-notes``.

The subcommand rewrites the ~245 pre-fix legacy notes (created
2026-05-05 → 2026-05-07) whose outer Lithos doc-level frontmatter has
empty ``tags`` / ``source_url`` / ``confidence`` but whose ``content``
carries an embedded YAML frontmatter block with the correct values.
Once rewritten, ``_classify_squatter`` (lithos_client.py:244) can match
incoming writes by ``source_url`` and the slug-collision backlog drains
on the next sweep.

See agent-lore/influx#176.
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


LEGACY_CONTENT = """---
note_type: summary
namespace: influx
source_url: https://openai.com/index/gpt-5-3-codex-system-card
tags:
  - profile:ai-agents
  - source:blog
  - feed-slug:openai-news
  - ingested-by:influx
  - schema:1
  - influx:archive-missing
  - influx:repair-needed
confidence: 1.0
---
# GPT-5.3-Codex System Card

## Archive

## Summary
### Contributions
- Integration of advanced coding capabilities

### Method
Combines frontier coding performance.

## Profile Relevance
### ai-agents
Score: 8/10
Highly relevant to LLM-based autonomous agents.

## User Notes
"""


def _legacy_doc(doc_id: str = "legacy-id-1") -> dict[str, Any]:
    """Pre-fix doc shape: outer metadata empty, inner frontmatter populated."""
    return {
        "id": doc_id,
        "title": "GPT-5.3-Codex System Card [openai.com]",
        "content": LEGACY_CONTENT,
        "path": "articles/blog/2026/02/gpt-53-codex-system-card-openaicom.md",
        "metadata": {
            "tags": [],
            "source_url": None,
            "confidence": 0.0,
            "author": "influx",
            "note_type": "summary",
            "namespace": "influx",
            "version": 3,
        },
    }


def _fixed_doc(doc_id: str = "fixed-id-1") -> dict[str, Any]:
    """Post-fix doc shape: outer metadata populated, no embedded frontmatter."""
    return {
        "id": doc_id,
        "title": "Already Fixed Doc",
        "content": "## Archive\n\n## Summary\nClean body.\n\n## User Notes\n",
        "path": "articles/blog/2026/05/already-fixed.md",
        "metadata": {
            "tags": ["profile:ai-agents", "ingested-by:influx"],
            "source_url": "https://example.com/already-fixed",
            "confidence": 1.0,
            "author": "influx",
            "version": 1,
        },
    }


def _repair_swept_broken_doc(doc_id: str = "repair-swept-id") -> dict[str, Any]:
    """Hybrid shape: tags populated by repair sweep, source_url still empty.

    The repair sweep adds tags like ``text:abstract-only`` /
    ``influx:source-invalid`` to broken docs without ever recovering the
    source_url.  ``_classify_squatter`` matches incoming writes by
    source_url, so this doc is still effectively broken from the
    recovery chain's perspective and SHOULD be rewritten by this job.
    """
    return {
        "id": doc_id,
        "title": "Repair-swept legacy doc",
        "content": LEGACY_CONTENT,
        "path": "articles/blog/2026/02/repair-swept.md",
        "metadata": {
            "tags": [
                "text:abstract-only",
                "influx:text-terminal",
                "influx:source-invalid",
            ],
            "source_url": None,
            "confidence": 0.0,
            "author": "influx",
            "version": 5,
        },
    }


def _make_args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "env": "staging",
        "apply": False,
        "yes": None,
        "yes_to_all": False,
        "id": None,
        "corpus_scan": False,
        "lithos_url": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _patch_runtime(client: MagicMock) -> Any:
    """Context-manager bundle of the patches every cmd_rewrite test needs."""
    return patch.multiple(
        _DIAGNOSE,
        _make_lithos_client=lambda url: client,
        _load_env=lambda env: {},
        _read_lithos_url=lambda args, env: "http://stub.lithos/sse",
        _ensure_project_runtime_or_reexec=lambda: None,
    )


# ── parse_legacy_note_frontmatter ───────────────────────────────────


class TestParseLegacyNoteFrontmatter:
    """Pure parsing of the embedded ``---``-fenced YAML block."""

    def test_parses_valid_legacy_content(self) -> None:
        parsed = _DIAGNOSE.parse_legacy_note_frontmatter(LEGACY_CONTENT)
        assert parsed is not None
        assert "profile:ai-agents" in parsed.tags
        assert "ingested-by:influx" in parsed.tags
        assert parsed.source_url == "https://openai.com/index/gpt-5-3-codex-system-card"
        assert parsed.confidence == 1.0

    def test_returns_none_when_no_frontmatter(self) -> None:
        body_only = "# Title\n\n## Section\nText.\n"
        assert _DIAGNOSE.parse_legacy_note_frontmatter(body_only) is None

    def test_returns_none_when_yaml_malformed(self) -> None:
        bad = "---\n: not: valid: yaml :\n---\n# Title\n"
        # Must not raise — return None so the caller can skip the doc cleanly.
        assert _DIAGNOSE.parse_legacy_note_frontmatter(bad) is None

    def test_returns_none_when_tags_missing(self) -> None:
        no_tags = (
            "---\n"
            "note_type: summary\n"
            "source_url: https://example.com\n"
            "confidence: 1.0\n"
            "---\n"
            "# Title\n"
        )
        assert _DIAGNOSE.parse_legacy_note_frontmatter(no_tags) is None

    def test_returns_none_when_source_url_missing(self) -> None:
        no_url = "---\ntags: [a, b]\nconfidence: 1.0\n---\n# Title\n"
        assert _DIAGNOSE.parse_legacy_note_frontmatter(no_url) is None

    def test_returns_none_when_tags_is_empty_list(self) -> None:
        # An already-stripped doc would never re-enter this path, but if the
        # inner frontmatter explicitly carries ``tags: []`` we must NOT rewrite
        # with empty tags — that's the very state we are trying to escape.
        empty_tags = (
            "---\n"
            "tags: []\n"
            "source_url: https://example.com\n"
            "confidence: 1.0\n"
            "---\n"
            "# Title\n"
        )
        assert _DIAGNOSE.parse_legacy_note_frontmatter(empty_tags) is None


# ── strip_legacy_frontmatter ───────────────────────────────────────


class TestStripLegacyFrontmatter:
    """Frontmatter + title stripping to produce a body-only content string."""

    def test_strips_frontmatter_and_title(self) -> None:
        body = _DIAGNOSE.strip_legacy_frontmatter(LEGACY_CONTENT)
        # Must not retain the ``---`` fence.
        assert not body.startswith("---")
        # Must not retain the embedded ``# Title`` line — Lithos re-prepends
        # ``# {doc.title}`` from doc metadata on next save, and leaving the
        # inner title here would produce two ``# X`` headings.
        assert not body.startswith("# ")
        # Section content must be preserved byte-for-byte.
        assert body.startswith("## Archive")
        assert "## Summary" in body
        assert "## Profile Relevance" in body
        assert "## User Notes" in body
        # Inner scoring/score values from sections must remain intact.
        assert "Score: 8/10" in body


# ── cmd_rewrite_legacy_notes — end-to-end ────────────────────────────


class TestRewriteLegacyNotesCommand:
    """End-to-end ``cmd_rewrite_legacy_notes`` with a mocked LithosClient."""

    def test_dry_run_lists_broken_doc_no_write(self, capsys: Any) -> None:
        doc = _legacy_doc()
        client = MagicMock()
        client.read_note = AsyncMock(return_value=doc)
        client.call_tool = AsyncMock()
        client.close = AsyncMock()

        args = _make_args(id=[doc["id"]])
        with _patch_runtime(client):
            rc = _DIAGNOSE.cmd_rewrite_legacy_notes(args)

        captured = capsys.readouterr()
        assert rc == 0
        assert doc["id"] in captured.out
        out = captured.out
        assert "would_rewrite" in out or "would rewrite" in out.lower()
        client.call_tool.assert_not_called()

    def test_apply_yes_to_all_rewrites_doc(self) -> None:
        doc = _legacy_doc()
        client = MagicMock()
        client.read_note = AsyncMock(return_value=doc)
        # ``call_tool`` is the canonical UPDATE-by-id seam — write_note() can't
        # carry the ``id`` arg.  Mock the MCP wire shape (``CallToolResult``-ish)
        # so the response decoder sees a clean ``{"status": "updated"}`` body.
        client.call_tool = AsyncMock(
            return_value=MagicMock(
                content=[MagicMock(text='{"status": "updated", "id": "legacy-id-1"}')]
            )
        )
        client.close = AsyncMock()

        args = _make_args(apply=True, yes_to_all=True, id=[doc["id"]])
        with _patch_runtime(client):
            rc = _DIAGNOSE.cmd_rewrite_legacy_notes(args)

        assert rc == 0
        assert client.call_tool.await_count == 1
        call = client.call_tool.await_args
        # call_tool(name, args_dict) — positional.
        assert call.args[0] == "lithos_write"
        write_args = call.args[1]
        # Identity-preserving args (UPDATE path).
        assert write_args["id"] == doc["id"]
        assert write_args["title"] == doc["title"]
        assert write_args["path"] == doc["path"]
        assert write_args["agent"] == "influx"
        # Parsed structured fields land as API parameters.
        assert "profile:ai-agents" in write_args["tags"]
        assert "ingested-by:influx" in write_args["tags"]
        assert (
            write_args["source_url"]
            == "https://openai.com/index/gpt-5-3-codex-system-card"
        )
        assert write_args["confidence"] == 1.0
        # Stripped body: no embedded frontmatter, no inner title heading.
        assert not write_args["content"].startswith("---")
        assert not write_args["content"].startswith("# ")
        assert "## Archive" in write_args["content"]
        # Version safety against concurrent edits.
        assert write_args["expected_version"] == doc["metadata"]["version"]

    def test_skips_already_fixed_doc(self, capsys: Any) -> None:
        doc = _fixed_doc()
        client = MagicMock()
        client.read_note = AsyncMock(return_value=doc)
        client.call_tool = AsyncMock()
        client.close = AsyncMock()

        args = _make_args(apply=True, yes_to_all=True, id=[doc["id"]])
        with _patch_runtime(client):
            rc = _DIAGNOSE.cmd_rewrite_legacy_notes(args)

        captured = capsys.readouterr()
        assert rc == 0
        client.call_tool.assert_not_called()
        out = captured.out
        assert "already_fixed" in out or "already fixed" in out.lower()

    def test_rewrites_repair_swept_doc_with_empty_source_url(self) -> None:
        # Regression for the staging incident where docs touched by the
        # repair sweep (tags=['text:abstract-only', 'influx:source-invalid', ...])
        # but still missing source_url were treated as 'already fixed' by
        # an over-aggressive idempotency guard.  The recovery chain matches
        # on source_url, so source_url is the only authoritative signal.
        doc = _repair_swept_broken_doc()
        client = MagicMock()
        client.read_note = AsyncMock(return_value=doc)
        client.call_tool = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text='{"status": "updated"}')])
        )
        client.close = AsyncMock()

        args = _make_args(apply=True, yes_to_all=True, id=[doc["id"]])
        with _patch_runtime(client):
            rc = _DIAGNOSE.cmd_rewrite_legacy_notes(args)

        assert rc == 0
        assert client.call_tool.await_count == 1
        write_args = client.call_tool.await_args.args[1]
        assert (
            write_args["source_url"]
            == "https://openai.com/index/gpt-5-3-codex-system-card"
        )

    def test_skips_unparseable_content(self, capsys: Any) -> None:
        doc = _legacy_doc()
        doc["content"] = "# Bare title\n\n## Some section\nnope.\n"
        client = MagicMock()
        client.read_note = AsyncMock(return_value=doc)
        client.call_tool = AsyncMock()
        client.close = AsyncMock()

        args = _make_args(apply=True, yes_to_all=True, id=[doc["id"]])
        with _patch_runtime(client):
            rc = _DIAGNOSE.cmd_rewrite_legacy_notes(args)

        captured = capsys.readouterr()
        assert rc == 0
        client.call_tool.assert_not_called()
        assert "unparseable" in captured.out.lower()

    def test_refuses_non_influx_authored(self, capsys: Any) -> None:
        doc = _legacy_doc()
        doc["metadata"]["author"] = "not-influx"
        client = MagicMock()
        client.read_note = AsyncMock(return_value=doc)
        client.call_tool = AsyncMock()
        client.close = AsyncMock()

        args = _make_args(apply=True, yes_to_all=True, id=[doc["id"]])
        with _patch_runtime(client):
            rc = _DIAGNOSE.cmd_rewrite_legacy_notes(args)

        captured = capsys.readouterr()
        assert rc == 0
        client.call_tool.assert_not_called()
        out = captured.out.lower()
        assert "not influx-authored" in out or "refused" in out

    def test_apply_handles_duplicate_partner_response(self, capsys: Any) -> None:
        # When the colliding partner doc has already landed this source_url
        # (typical during a batch apply where pair members are processed in
        # close succession), Lithos returns status=duplicate.  This is the
        # expected outcome, NOT a failure — the partner is the canonical
        # source-of-truth for the URL and the slug-recovery chain will
        # resolve future collisions against it.
        doc = _legacy_doc()
        client = MagicMock()
        client.read_note = AsyncMock(return_value=doc)
        client.call_tool = AsyncMock(
            return_value=MagicMock(
                content=[
                    MagicMock(
                        text=(
                            '{"status": "duplicate", '
                            '"duplicate_of": {"id": "partner-id-1", '
                            '"title": "Partner", '
                            '"source_url": '
                            '"https://openai.com/index/gpt-5-3-codex-system-card"}, '
                            '"message": "URL already exists"}'
                        )
                    )
                ]
            )
        )
        client.close = AsyncMock()

        args = _make_args(apply=True, yes_to_all=True, id=[doc["id"]])
        with _patch_runtime(client):
            rc = _DIAGNOSE.cmd_rewrite_legacy_notes(args)

        captured = capsys.readouterr()
        # Exit code MUST be 0 — duplicate-partner is a success outcome.
        assert rc == 0
        # The outcome bucket must be its own label, not 'failed'.
        assert "partner_owns_url" in captured.out
        assert "failed" not in captured.out.split("Summary:")[1].lower()

    def test_apply_requires_yes_or_yes_to_all(self) -> None:
        # ``--apply`` alone without ``--yes`` / ``--yes-to-all`` / ``--id``
        # must be rejected — mirrors cmd_squatters's safety contract.
        args = _make_args(apply=True)
        client = MagicMock()
        with _patch_runtime(client), pytest.raises(SystemExit):
            _DIAGNOSE.cmd_rewrite_legacy_notes(args)


# ── --corpus-scan selector (#181) ───────────────────────────────────


def _write_md(path: Path, *, frontmatter: dict[str, Any], body: str = "body") -> None:
    """Write a Lithos-shaped markdown file at *path*.

    Mirrors the on-disk shape Lithos produces: ``---``-fenced YAML
    frontmatter followed by markdown body content.  Used by the
    corpus-scan tests to seed a temp directory with realistic fixtures.
    """
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.safe_dump(frontmatter, sort_keys=True).strip()
    path.write_text(f"---\n{fm_text}\n---\n\n{body}\n", encoding="utf-8")


class TestSelectLegacyDocIdsFromCorpus:
    """``_select_legacy_doc_ids_from_corpus`` walks the on-disk articles tree."""

    def test_finds_influx_authored_docs_with_empty_source_url(
        self, tmp_path: Path
    ) -> None:
        # Broken: influx-authored, no source_url.
        _write_md(
            tmp_path / "blog/2026/02/broken-1.md",
            frontmatter={
                "id": "broken-1",
                "author": "influx",
                "source_url": None,
                "tags": [],
            },
        )
        _write_md(
            tmp_path / "blog/2026/02/broken-2.md",
            frontmatter={
                "id": "broken-2",
                "author": "influx",
                "source_url": "",
                "tags": [],
            },
        )
        # Healthy: influx-authored, populated source_url.
        _write_md(
            tmp_path / "blog/2026/05/healthy.md",
            frontmatter={
                "id": "healthy-1",
                "author": "influx",
                "source_url": "https://example.com/healthy",
                "tags": ["ingested-by:influx"],
            },
        )
        # Other-agent: not in scope.
        _write_md(
            tmp_path / "agent-zero/personal.md",
            frontmatter={
                "id": "agent-zero-1",
                "author": "agent-zero",
                "source_url": None,
                "tags": [],
            },
        )

        ids = _DIAGNOSE._select_legacy_doc_ids_from_corpus(tmp_path)
        assert sorted(ids) == ["broken-1", "broken-2"]

    def test_returns_empty_when_articles_path_missing(self, tmp_path: Path) -> None:
        # Non-existent directory should be a graceful empty list, not a crash.
        ids = _DIAGNOSE._select_legacy_doc_ids_from_corpus(tmp_path / "nope")
        assert ids == []

    def test_returns_empty_when_corpus_has_no_broken_docs(self, tmp_path: Path) -> None:
        _write_md(
            tmp_path / "blog/healthy.md",
            frontmatter={
                "id": "h1",
                "author": "influx",
                "source_url": "https://example.com/h1",
                "tags": ["ingested-by:influx"],
            },
        )
        ids = _DIAGNOSE._select_legacy_doc_ids_from_corpus(tmp_path)
        assert ids == []

    def test_skips_files_without_frontmatter(self, tmp_path: Path) -> None:
        # A stray non-Lithos markdown file in the tree shouldn't trip the
        # parser — it has no ``---`` fence at the top.
        (tmp_path / "stray.md").write_text("# Just markdown\n\nno frontmatter\n")
        ids = _DIAGNOSE._select_legacy_doc_ids_from_corpus(tmp_path)
        assert ids == []

    def test_skips_malformed_yaml(self, tmp_path: Path) -> None:
        # Frontmatter fences present but YAML body is malformed.  Don't crash.
        (tmp_path / "malformed.md").write_text(
            "---\n: not: valid: yaml :\n---\n\nbody\n"
        )
        ids = _DIAGNOSE._select_legacy_doc_ids_from_corpus(tmp_path)
        assert ids == []

    def test_dedupes_when_same_id_appears_twice(self, tmp_path: Path) -> None:
        # Defensive: two on-disk files claiming the same id (e.g. a
        # stale duplicate from a manual move) collapse to one entry.
        for sub in ("a", "b"):
            _write_md(
                tmp_path / f"blog/{sub}/dup.md",
                frontmatter={
                    "id": "dup-id",
                    "author": "influx",
                    "source_url": None,
                    "tags": [],
                },
            )
        ids = _DIAGNOSE._select_legacy_doc_ids_from_corpus(tmp_path)
        assert ids == ["dup-id"]

    def test_fence_in_value_does_not_truncate_frontmatter(self, tmp_path: Path) -> None:
        # Regression: a substring-style closing-fence search (``find("---", 4)``)
        # stops at the first ``---`` ANYWHERE in the file, including inside a
        # YAML value like ``title: 'Foo --- Bar'``.  yaml.safe_load on the
        # truncated payload then fails and the doc is silently skipped from
        # the candidate set — a false negative that loses real broken docs.
        # The fix is a line-anchored fence search; this test pins it.
        path = tmp_path / "blog/2026/02/fence-in-value.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write the file by hand — yaml.safe_dump would quote the ``---`` away
        # so we wouldn't reproduce the substring-find bug shape.
        path.write_text(
            (
                "---\n"
                "author: influx\n"
                "id: fence-in-value-doc\n"
                "source_url:\n"
                "tags: []\n"
                "title: 'OmniRobot --- a multi-camera platform'\n"
                "---\n"
                "\n"
                "body\n"
            ),
            encoding="utf-8",
        )
        ids = _DIAGNOSE._select_legacy_doc_ids_from_corpus(tmp_path)
        assert ids == ["fence-in-value-doc"]


class TestCorpusScanFlagWiring:
    """``--corpus-scan`` end-to-end behaviour in ``cmd_rewrite_legacy_notes``."""

    def test_corpus_scan_drives_the_candidate_list(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        # Seed a corpus with one broken doc + one healthy doc.
        _write_md(
            tmp_path / "blog/broken.md",
            frontmatter={
                "id": "scan-broken-1",
                "author": "influx",
                "source_url": None,
                "tags": [],
            },
        )
        _write_md(
            tmp_path / "blog/healthy.md",
            frontmatter={
                "id": "scan-healthy-1",
                "author": "influx",
                "source_url": "https://example.com/healthy",
                "tags": ["ingested-by:influx"],
            },
        )

        # Mock the per-doc read so the only doc actually fetched is the
        # one the selector identified.
        doc = _legacy_doc("scan-broken-1")
        client = MagicMock()
        client.read_note = AsyncMock(return_value=doc)
        client.call_tool = AsyncMock()
        client.close = AsyncMock()

        args = _make_args(corpus_scan=True)
        with patch.multiple(
            _DIAGNOSE,
            _make_lithos_client=lambda url: client,
            _load_env=lambda env: {"LITHOS_KNOWLEDGE_PATH": str(tmp_path.parent)},
            _resolve_corpus_articles_path=lambda env: tmp_path,
            _read_lithos_url=lambda args, env: "http://stub.lithos/sse",
            _ensure_project_runtime_or_reexec=lambda: None,
        ):
            rc = _DIAGNOSE.cmd_rewrite_legacy_notes(args)

        captured = capsys.readouterr()
        assert rc == 0
        # Selector found the broken doc; the healthy doc never even reaches
        # the read path because the selector filtered it out upstream.
        assert "scan-broken-1" in captured.out
        assert "scan-healthy-1" not in captured.out
        # One ``read_note`` call (for scan-broken-1); no writes (dry-run).
        client.read_note.assert_awaited_once()
        client.call_tool.assert_not_called()

    def test_corpus_scan_and_id_are_mutually_exclusive(self) -> None:
        # The runtime check fires regardless of argparse settings —
        # documents the contract even if the parser-level group is bypassed.
        args = _make_args(corpus_scan=True, id=["any-id"])
        client = MagicMock()
        with _patch_runtime(client), pytest.raises(SystemExit):
            _DIAGNOSE.cmd_rewrite_legacy_notes(args)

    def test_corpus_scan_with_no_broken_docs_reports_clean_state(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        # Empty corpus → friendly "no candidates" message, not a crash.
        client = MagicMock()
        client.read_note = AsyncMock()
        client.call_tool = AsyncMock()
        client.close = AsyncMock()

        args = _make_args(corpus_scan=True)
        with patch.multiple(
            _DIAGNOSE,
            _make_lithos_client=lambda url: client,
            _load_env=lambda env: {"LITHOS_KNOWLEDGE_PATH": str(tmp_path.parent)},
            _resolve_corpus_articles_path=lambda env: tmp_path,
            _read_lithos_url=lambda args, env: "http://stub.lithos/sse",
            _ensure_project_runtime_or_reexec=lambda: None,
        ):
            rc = _DIAGNOSE.cmd_rewrite_legacy_notes(args)

        captured = capsys.readouterr()
        assert rc == 0
        assert "corpus scan" in captured.out.lower()
        client.read_note.assert_not_called()


class TestResolveCorpusArticlesPath:
    """``_resolve_corpus_articles_path`` env-resolution logic."""

    def test_explicit_knowledge_path_wins(self, tmp_path: Path) -> None:
        env = {"LITHOS_KNOWLEDGE_PATH": str(tmp_path / "kb")}
        resolved = _DIAGNOSE._resolve_corpus_articles_path(env)
        assert resolved == tmp_path / "kb" / "articles"

    def test_falls_back_to_archive_path_staging_convention(
        self, tmp_path: Path
    ) -> None:
        # Mirrors staging: ``data/staging/archive`` →
        # ``data/staging/lithos/knowledge/articles``.
        archive = tmp_path / "data" / "staging" / "archive"
        env = {"INFLUX_ARCHIVE_PATH": str(archive)}
        resolved = _DIAGNOSE._resolve_corpus_articles_path(env)
        expected = tmp_path / "data" / "staging" / "lithos" / "knowledge" / "articles"
        assert resolved == expected

    def test_missing_both_env_vars_exits_with_message(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _DIAGNOSE._resolve_corpus_articles_path({})
        assert "LITHOS_KNOWLEDGE_PATH" in str(exc_info.value)
