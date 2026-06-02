"""Unit tests for ``scripts/influx-inbox-submit.py`` (Inbox v1 slice 4, §15).

Covers the URL/source-tag validation that must happen BEFORE any MCP call,
the ``--dry-run`` task-body shaping, and metadata assembly from flags.
"""

from __future__ import annotations

import importlib.util
import json
import os
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


def test_summary_and_summary_file_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc:
        _SUBMIT.main([_URL, "--dry-run", "--summary", "x", "--summary-file", "y"])
    assert exc.value.code == 2


def test_apply_env_to_process_uses_setdefault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process-exported vars win over the env file; absent keys are filled in."""
    monkeypatch.setenv("INFLUX_INBOX_WINS", "from-process")
    monkeypatch.delenv("INFLUX_INBOX_FILLED", raising=False)
    _SUBMIT._apply_env_to_process(
        {"INFLUX_INBOX_WINS": "from-file", "INFLUX_INBOX_FILLED": "from-file"}
    )
    assert os.environ["INFLUX_INBOX_WINS"] == "from-process"
    assert os.environ["INFLUX_INBOX_FILLED"] == "from-file"


def test_env_file_reaches_load_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``docker/.env.<env>`` values are visible to the load_config fallback.

    Regression for the #198 helper-script contract: with no --lithos-url and no
    LITHOS_URL in the file, resolution falls through to load_config(), which
    reads INFLUX_CONFIG from os.environ — so the env file must be applied there.
    """
    docker = tmp_path / "docker"
    docker.mkdir()
    (docker / ".env.test").write_text(
        "INFLUX_CONFIG=/custom/influx.toml\n", encoding="utf-8"
    )
    monkeypatch.setattr(_SUBMIT, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("INFLUX_CONFIG", raising=False)
    monkeypatch.delenv("LITHOS_URL", raising=False)

    captured: dict[str, str | None] = {}

    import influx.config as influx_config

    def _fake_load_config() -> Any:
        captured["INFLUX_CONFIG"] = os.environ.get("INFLUX_CONFIG")
        raise RuntimeError("stop before real load")

    monkeypatch.setattr(influx_config, "load_config", _fake_load_config)

    rc = _SUBMIT.main([_URL, "--env", "test"])

    assert captured["INFLUX_CONFIG"] == "/custom/influx.toml"
    assert rc == 2  # load_config raised → URL unresolved → exit 2
