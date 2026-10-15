"""End-to-end smoke of the ``evals`` CLI with a fake poster."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.cases import CandidateRecord, FilterCase, Tier1Case, Tier3Case, write_jsonl

from evals import __main__ as cli
from tests.evals.conftest import chat_response


def _fake_poster(url: str, body: dict[str, Any], **_: Any):
    schema = body.get("response_format", {}).get("json_schema", {}).get("name", "")
    content = body["messages"][0]["content"]
    if "## CANDIDATES" in content:
        return chat_response(
            {"results": [{"id": "x", "score": 8, "tags": ["t"], "reason": "r"}]}
        )
    if schema == "Tier3Extraction" or "claims, datasets" in content:
        return chat_response(
            {
                "claims": ["c"],
                "datasets": [],
                "builds_on": ["arXiv:1706.03762"],
                "open_questions": [],
                "potential_connections": [],
            }
        )
    return chat_response(
        {"contributions": ["a"], "method": "m", "result": "r", "relevance": "x"}
    )


def test_run_then_compare(
    influx_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr("evals.runner.guarded_post_json_fetch", _fake_poster)
    root = tmp_path / "evals"
    ds = root / "datasets" / "d"
    write_jsonl(
        ds / "filter.jsonl",
        [FilterCase("f1", "ai-robotics", (CandidateRecord("x", "T", "A"),))],
    )
    write_jsonl(ds / "tier1.jsonl", [Tier1Case("t1", "ai-robotics", "T", "A")])
    write_jsonl(ds / "tier3.jsonl", [Tier3Case("t3", "T", "Full text body")])
    common = ["--config", str(influx_config_env), "--root", str(root)]

    for label in ("base", "cand"):
        rc = cli.main(
            [
                "run",
                *common,
                "--dataset",
                "d",
                "--label",
                label,
                "--model",
                f"m-{label}",
                "--provider",
                "test-provider",
            ]
        )
        assert rc == 0
    spec = json.loads((root / "runs" / "d" / "cand" / "spec.json").read_text())
    assert spec["model"] == "m-cand"

    rc = cli.main(
        [
            "compare",
            *common,
            "--dataset",
            "d",
            "--baseline",
            "base",
            "--candidate",
            "cand",
        ]
    )

    assert rc == 0
    report = (root / "runs" / "d" / "compare-cand-vs-base.md").read_text()
    assert "## Filter: score agreement" in report
    assert "## tier3: output shape" in report
    assert "arXiv id: baseline 100%, candidate 100%" in report
