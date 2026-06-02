"""Unit tests for ``scripts/influx-inbox-submit.py`` (Inbox §15/§16).

Covers the URL/PDF auto-detection + validation that must happen BEFORE any
MCP call, the ``--dry-run`` task-body shaping, metadata assembly from flags,
and the local-PDF staging into ``pdf_root`` (v2 slice B).
"""

from __future__ import annotations

import hashlib
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


def test_non_http_url_scheme_rejected_before_mcp(
    capsys: pytest.CaptureFixture,
) -> None:
    rc = _SUBMIT.main(["file:///etc/passwd", "--dry-run"])
    assert rc == 2
    assert "must be http" in capsys.readouterr().err


def test_nonexistent_path_treated_as_pdf_and_rejected(
    capsys: pytest.CaptureFixture,
) -> None:
    rc = _SUBMIT.main(["not a url", "--dry-run"])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_http_url_without_host_rejected(capsys: pytest.CaptureFixture) -> None:
    """Regression: a scheme-only https:// (no host) must not reach MCP."""
    rc = _SUBMIT.main(["https://", "--dry-run"])
    assert rc == 2
    assert "host" in capsys.readouterr().err


def test_mailto_scheme_rejected_as_non_http(capsys: pytest.CaptureFixture) -> None:
    rc = _SUBMIT.main(["mailto:someone@example.com", "--dry-run"])
    assert rc == 2
    assert "must be http" in capsys.readouterr().err


def test_directory_argument_rejected(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    rc = _SUBMIT.main([str(tmp_path), "--dry-run"])
    assert rc == 2
    assert "not a regular file" in capsys.readouterr().err


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


# ── Local-PDF mode (v2 §16) ──────────────────────────────────────────


def _write_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.4 a minimal local pdf body for tests")
    return path


def test_pdf_inside_root_dry_run_body(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    pdf = _write_pdf(root / "paper.pdf")
    rc = _SUBMIT.main([str(pdf), "--pdf-root", str(root), "--dry-run"])
    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body["tags"] == ["influx:inbox"]
    assert body["title"] == "Influx inbox: paper.pdf"
    assert body["metadata"]["kind"] == "pdf"
    # A file already inside pdf_root is used in place.
    assert body["metadata"]["local_path"] == str(pdf.resolve())


def test_pdf_outside_root_dry_run_computes_staged_path(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    pdf = _write_pdf(tmp_path / "outside.pdf")
    rc = _SUBMIT.main([str(pdf), "--pdf-root", str(root), "--dry-run"])
    assert rc == 0
    local_path = json.loads(capsys.readouterr().out)["metadata"]["local_path"]
    assert local_path.startswith(str(root.resolve()))
    assert local_path.endswith("-outside.pdf")
    # --dry-run must not actually copy.
    assert list(root.iterdir()) == []


def test_pdf_magic_bytes_detected_without_suffix(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    doc = root / "doc"  # no .pdf suffix
    doc.write_bytes(b"%PDF-1.7 body")
    rc = _SUBMIT.main([str(doc), "--pdf-root", str(root), "--dry-run"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["metadata"]["kind"] == "pdf"


def test_pdf_dry_run_includes_optional_flags(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    pdf = _write_pdf(root / "paper.pdf")
    rc = _SUBMIT.main(
        [
            str(pdf),
            "--pdf-root",
            str(root),
            "--dry-run",
            "--title",
            "A Paper",
            "--source-tag",
            "ai-news",
        ]
    )
    assert rc == 0
    md = json.loads(capsys.readouterr().out)["metadata"]
    assert md["title"] == "A Paper"
    assert md["source_tag"] == "ai-news"


def test_nonexistent_pdf_rejected_before_mcp(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    rc = _SUBMIT.main([str(tmp_path / "missing.pdf"), "--dry-run"])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_non_pdf_file_rejected(capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    txt = tmp_path / "notes.txt"
    txt.write_text("just text, not a pdf", encoding="utf-8")
    rc = _SUBMIT.main([str(txt), "--pdf-root", str(tmp_path), "--dry-run"])
    assert rc == 2
    assert "not a PDF" in capsys.readouterr().err


def test_pdf_root_unresolved_rejected(
    capsys: pytest.CaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _write_pdf(tmp_path / "paper.pdf")
    monkeypatch.setattr(_SUBMIT, "_resolve_pdf_root", lambda *a, **k: None)
    rc = _SUBMIT.main([str(pdf), "--dry-run"])
    assert rc == 2
    assert "pdf_root" in capsys.readouterr().err


def test_stage_copies_when_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    src = _write_pdf(tmp_path / "src.pdf")
    dest = _SUBMIT._stage_pdf_into_root(src, root, copy=True)
    assert dest.parent == root.resolve()
    assert dest.read_bytes() == src.read_bytes()
    assert src.exists()  # original never moved/deleted
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    assert dest.name == f"{digest[:16]}-src.pdf"


def test_stage_uses_file_in_place_when_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "pdfs"
    root.mkdir()
    inside = _write_pdf(root / "paper.pdf")
    dest = _SUBMIT._stage_pdf_into_root(inside, root, copy=True)
    assert dest == inside.resolve()
    assert list(root.iterdir()) == [inside]  # no duplicate copy


def test_url_mode_still_reports_kind_url(capsys: pytest.CaptureFixture) -> None:
    rc = _SUBMIT.main([_URL, "--dry-run"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["metadata"]["kind"] == "url"
