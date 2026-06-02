"""Unit tests for ``scripts/influx-inbox-submit.py`` (Inbox v1 slice 4, §15).

Covers the URL/source-tag validation that must happen BEFORE any MCP call,
the ``--dry-run`` task-body shaping, and metadata assembly from flags.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


def _load_script() -> Any:
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "influx_inbox_submit",
        repo_root / "scripts" / "influx-inbox-submit.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SUBMIT = _load_script()
_URL = "https://example.com/article"


def test_dry_run_prints_task_body_without_mcp(capsys: pytest.CaptureFixture) -> None:
    rc = _SUBMIT.main([_URL, "--dry-run", "--submitted-by", "agent:test"])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body["tags"] == ["influx:inbox"]
    assert body["agent"] == "agent:test"
    assert body["title"] == f"Influx inbox: {_URL}"
    assert body["metadata"] == {
        "kind": "url",
        "url": _URL,
        "submitted_by": "agent:test",
    }


def test_dry_run_includes_optional_flags(capsys: pytest.CaptureFixture) -> None:
    rc = _SUBMIT.main(
        [
            _URL,
            "--dry-run",
            "--title",
            "A Paper",
            "--summary",
            "an excerpt",
            "--source-tag",
            "ai-news",
        ]
    )
    assert rc == 0
    md = json.loads(capsys.readouterr().out)["metadata"]
    assert md["title"] == "A Paper"
    assert md["summary"] == "an excerpt"
    assert md["source_tag"] == "ai-news"


def test_summary_file_is_read(capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    f = tmp_path / "s.txt"
    f.write_text("summary from file", encoding="utf-8")
    rc = _SUBMIT.main([_URL, "--dry-run", "--summary-file", str(f)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["metadata"]["summary"] == (
        "summary from file"
    )


def test_default_submitted_by_uses_username(
    capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_SUBMIT.getpass, "getuser", lambda: "dave")
    rc = _SUBMIT.main([_URL, "--dry-run"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["metadata"]["submitted_by"] == (
        "manual:dave"
    )


def test_invalid_url_rejected_before_mcp(capsys: pytest.CaptureFixture) -> None:
    rc = _SUBMIT.main(["file:///etc/passwd", "--dry-run"])
    assert rc == 2
    assert "must be http" in capsys.readouterr().err


def test_non_url_argument_rejected(capsys: pytest.CaptureFixture) -> None:
    rc = _SUBMIT.main(["not a url", "--dry-run"])
    assert rc == 2


def test_invalid_source_tag_rejected(capsys: pytest.CaptureFixture) -> None:
    rc = _SUBMIT.main([_URL, "--dry-run", "--source-tag", "Not A Slug"])
    assert rc == 2
    assert "source-tag" in capsys.readouterr().err
