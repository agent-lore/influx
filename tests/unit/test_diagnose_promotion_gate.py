"""Unit tests for the ``influx-diagnose promotion-gate`` subcommand (#165).

The CLI thin-wraps :mod:`influx.promotion_gate` over the local
``runs.jsonl`` ledger.  These tests pin the wiring:

* The subcommand reads the ledger, reverses to newest-first, and
  feeds the entries to the gate.
* Exit code is ``0`` on PASS and ``1`` on FAIL so a CI step can
  consume the verdict directly.
* The summary text is written to stdout (operator log surface).

Gate behaviour is covered by ``test_promotion_gate.py``; here we
only check the script glue.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch


def _load_script() -> Any:
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "influx_diagnose_promotion_gate",
        repo_root / "scripts" / "influx-diagnose.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_DIAGNOSE = _load_script()


def _write_ledger(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    """Write ledger entries as JSONL (one per line, oldest first).

    ``_read_runs_jsonl`` returns them in file order, and
    ``cmd_promotion_gate`` reverses to newest-first before passing
    them to the gate.
    """
    state = tmp_path / "state"
    state.mkdir()
    runs = state / "runs.jsonl"
    runs.write_text("\n".join(json.dumps(e) for e in entries))
    return state


def _scheduled_entry(
    run_id: str,
    *,
    severity: str = "success",
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "profile": "alpha",
        "kind": "scheduled",
        "status": "completed",
        "degraded_reasons": list(reasons or []),
        "degradation_severity": severity,
    }


def _args(**overrides: Any) -> argparse.Namespace:
    base = {
        "env": "staging",
        "window": 20,
        "max_lossy_ratio": 0.5,
        "min_runs": 5,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCmdPromotionGate:
    """The subcommand wires the ledger into the gate and exits 0/1."""

    def test_pass_returns_zero_and_prints_pass(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        state = _write_ledger(tmp_path, [_scheduled_entry(f"r-{i}") for i in range(6)])
        env = {"INFLUX_STATE_PATH": str(state)}

        with (
            patch.object(_DIAGNOSE, "_load_env", return_value=env),
            patch.object(_DIAGNOSE, "_ensure_project_runtime_or_reexec"),
        ):
            rc = _DIAGNOSE.cmd_promotion_gate(_args(min_runs=5))

        assert rc == 0
        out = capsys.readouterr().out
        assert "Promotion gate: PASS" in out
        assert "reason          : ok" in out

    def test_unexpected_failure_returns_one(self, tmp_path: Path, capsys: Any) -> None:
        entries = [
            _scheduled_entry(
                "bad",
                severity="unexpected_failure",
                reasons=["invalid_note_state"],
            ),
        ] + [_scheduled_entry(f"ok-{i}") for i in range(5)]
        state = _write_ledger(tmp_path, entries)
        env = {"INFLUX_STATE_PATH": str(state)}

        with (
            patch.object(_DIAGNOSE, "_load_env", return_value=env),
            patch.object(_DIAGNOSE, "_ensure_project_runtime_or_reexec"),
        ):
            rc = _DIAGNOSE.cmd_promotion_gate(_args())

        assert rc == 1
        out = capsys.readouterr().out
        assert "Promotion gate: FAIL" in out
        assert "unexpected_failure_present" in out
        # The offending run_id appears in the summary so the operator
        # has a direct drill-in target.
        assert "run_id=bad" in out

    def test_lossy_above_ratio_returns_one(self, tmp_path: Path, capsys: Any) -> None:
        # 7 lossy + 3 success = 0.7 ratio; threshold default is 0.5.
        entries = [
            _scheduled_entry(
                f"lossy-{i}",
                severity="expected_lossy",
                reasons=["archive_acquisition"],
            )
            for i in range(7)
        ] + [_scheduled_entry(f"ok-{i}") for i in range(3)]
        state = _write_ledger(tmp_path, entries)
        env = {"INFLUX_STATE_PATH": str(state)}

        with (
            patch.object(_DIAGNOSE, "_load_env", return_value=env),
            patch.object(_DIAGNOSE, "_ensure_project_runtime_or_reexec"),
        ):
            rc = _DIAGNOSE.cmd_promotion_gate(_args())

        assert rc == 1
        out = capsys.readouterr().out
        assert "expected_lossy_above_threshold" in out
        assert "ratio=0.70" in out

    def test_missing_ledger_file_falls_through_to_insufficient_runs(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        # No runs.jsonl in the state dir: empty entries → insufficient.
        state = tmp_path / "state"
        state.mkdir()
        env = {"INFLUX_STATE_PATH": str(state)}

        with (
            patch.object(_DIAGNOSE, "_load_env", return_value=env),
            patch.object(_DIAGNOSE, "_ensure_project_runtime_or_reexec"),
        ):
            rc = _DIAGNOSE.cmd_promotion_gate(_args())

        assert rc == 1
        out = capsys.readouterr().out
        assert "insufficient_runs" in out
