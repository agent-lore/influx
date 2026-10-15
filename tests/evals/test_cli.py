"""End-to-end smoke of the ``evals`` CLI with a fake poster."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.cases import CandidateRecord, FilterCase, Tier1Case, Tier3Case, write_jsonl
from evals.labels import Label, append_label, read_labels

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


def _two_runs(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[str]]:
    """Build a one-case-per-slot dataset and run a ``base`` and a ``cand`` model."""
    monkeypatch.setattr("evals.runner.guarded_post_json_fetch", _fake_poster)
    root = tmp_path / "evals"
    ds = root / "datasets" / "d"
    write_jsonl(
        ds / "filter.jsonl",
        [FilterCase("f1", "ai-robotics", (CandidateRecord("x", "T", "A"),))],
    )
    write_jsonl(ds / "tier1.jsonl", [Tier1Case("t1", "ai-robotics", "T", "A")])
    write_jsonl(ds / "tier3.jsonl", [Tier3Case("t3", "T", "Full text body")])
    common = ["--config", str(config_path), "--root", str(root)]

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
    return root, common


def _compare(common: list[str]) -> int:
    return cli.main(
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


def test_run_then_compare(
    influx_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, common = _two_runs(influx_config_env, tmp_path, monkeypatch)
    spec = json.loads((root / "runs" / "d" / "cand" / "spec.json").read_text())
    assert spec["model"] == "m-cand"

    assert _compare(common) == 0

    report = (root / "runs" / "d" / "compare-cand-vs-base.md").read_text()
    assert "## Filter: score agreement" in report
    assert "## tier3: output shape" in report
    assert "arXiv id: baseline 100%, candidate 100%" in report
    assert "against your labels" not in report


def test_label_then_grade_and_compare(
    influx_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, common = _two_runs(influx_config_env, tmp_path, monkeypatch)
    answers = iter(["1 squarely on topic"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    rc = cli.main(["label", *common, "--dataset", "d", "--labeller", "dave"])

    assert rc == 0
    labels = read_labels(root / "datasets" / "d" / "labels.jsonl")
    assert labels[("ai-robotics", "x")].tier == 1
    assert labels[("ai-robotics", "x")].labeller == "dave"

    rc = cli.main(["grade", *common, "--dataset", "d", "--runs", "base,cand"])

    assert rc == 0
    graded = (root / "runs" / "d" / "grade.md").read_text()
    assert "## Filter: graded against your labels" in graded
    # The fake model scores x 8: a correct ingest call, one tier too high.
    assert "| base | ingest | 1 | 100% | 100% |" in graded
    assert "| cand | 1 | 0/1 |" in graded

    assert _compare(common) == 0
    report = (root / "runs" / "d" / "compare-cand-vs-base.md").read_text()
    assert "## Filter: graded against your labels" in report


def test_grade_without_labels_fails_clearly(
    influx_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, common = _two_runs(influx_config_env, tmp_path, monkeypatch)

    with pytest.raises(SystemExit, match="no labels"):
        cli.main(["grade", *common, "--dataset", "d", "--runs", "base"])


def test_grade_rejects_a_run_without_filter_results(
    influx_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, common = _two_runs(influx_config_env, tmp_path, monkeypatch)
    append_label(root / "datasets" / "d" / "labels.jsonl", Label("ai-robotics", "x", 1))
    (root / "runs" / "d" / "base" / "filter.jsonl").unlink()

    with pytest.raises(SystemExit, match="'base' has no filter results"):
        cli.main(["grade", *common, "--dataset", "d", "--runs", "base,cand"])


def test_bad_labels_file_is_a_clean_error(
    influx_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, common = _two_runs(influx_config_env, tmp_path, monkeypatch)
    (root / "datasets" / "d" / "labels.jsonl").write_text(
        '{"profile": "ai-robotics", "item_id": "x", "tier": 9}\n'
    )

    for argv in (
        ["grade", *common, "--dataset", "d", "--runs", "base"],
        ["label", *common, "--dataset", "d"],
    ):
        with pytest.raises(SystemExit, match=r"labels\.jsonl:1: bad label"):
            cli.main(argv)
