#!/usr/bin/env python3
"""Diagnose Influx runs from the local run ledger and container logs.

Operator entrypoint for the staging runbook.  Each subcommand wraps a
``jq``/``docker logs``/file-read incantation that is otherwise tedious
to retype.  All subcommands accept ``--env <name>`` (default
``staging``) and read tunables from ``docker/.env.<name>``:

    INFLUX_STATE_PATH        location of runs.jsonl + active-runs.json
    INFLUX_CONTAINER_NAME    docker container to query for logs

Subcommands
-----------
    recent          Recent terminal run ledger entries
    failures        Recent failed or degraded runs
    run RUN_ID      Show ledger entry + matching log lines for one run
    warnings        WARNING/ERROR docker log lines (filterable by run_id)
    terminal-flips  Per-stage terminal-flip log events with note IDs
    cancel          Print the curl line for cancelling an in-flight run
                    (this script never sends destructive HTTP itself)

The script is read-only: no HTTP POSTs, no docker exec, no ledger
writes.  Every command it emits is an inspection.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


# ── env-file loader (mirrors scripts/influx-report.py) ──────────────


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_env(name: str) -> dict[str, str]:
    path = _repo_root() / "docker" / f".env.{name}"
    if not path.exists():
        sys.exit(f"environment file not found: {path}")
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _state_dir(env: dict[str, str]) -> Path:
    state = env.get("INFLUX_STATE_PATH")
    if not state:
        sys.exit(
            "INFLUX_STATE_PATH not set in env file; "
            "cannot locate runs.jsonl"
        )
    return Path(state)


def _container_name(env: dict[str, str]) -> str:
    return env.get("INFLUX_CONTAINER_NAME") or "influx"


# ── ledger readers ──────────────────────────────────────────────────


def _read_runs_jsonl(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / "runs.jsonl"
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def _read_active_runs(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / "active-runs.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    return [v for v in data.values() if isinstance(v, dict)]


def _filter_runs(
    runs: list[dict[str, Any]],
    *,
    profile: str | None = None,
    statuses: set[str] | None = None,
    degraded_only: bool = False,
) -> list[dict[str, Any]]:
    out = list(runs)
    if profile:
        out = [r for r in out if r.get("profile") == profile]
    if statuses:
        out = [r for r in out if r.get("status") in statuses]
    if degraded_only:
        out = [r for r in out if r.get("degraded")]
    return out


def _print_run_row(run: dict[str, Any]) -> None:
    status = run.get("status", "?")
    if run.get("degraded"):
        status = f"{status} (degraded)"
    print(
        f"  {run.get('completed_at') or run.get('started_at')} "
        f"{run.get('profile')} {run.get('kind')} "
        f"{status} run_id={run.get('run_id')} "
        f"checked={run.get('sources_checked')} "
        f"ingested={run.get('ingested')} "
        f"duration={run.get('duration_seconds')}"
    )
    if run.get("error"):
        print(f"      error={run.get('error')}")
    for src_err in run.get("source_acquisition_errors") or []:
        if isinstance(src_err, dict):
            print(
                f"      source_error: source={src_err.get('source')} "
                f"kind={src_err.get('kind')} detail={src_err.get('detail')}"
            )


# ── docker logs ─────────────────────────────────────────────────────


def _have_docker() -> bool:
    return shutil.which("docker") is not None


def _docker_logs_iter(
    container: str,
    *,
    since: str | None = None,
    tail: int | None = None,
) -> Iterator[str]:
    """Stream JSON log lines from ``docker logs`` (one dict per yielded line).

    Lines that aren't valid JSON (rare; usually startup banners) are
    skipped.  We pull stdout+stderr because the JSON formatter writes to
    stderr.
    """
    if not _have_docker():
        sys.exit("'docker' not on PATH; cannot read container logs")
    cmd = ["docker", "logs"]
    if since:
        cmd += ["--since", since]
    if tail is not None:
        cmd += ["--tail", str(tail)]
    cmd.append(container)
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(
            f"docker logs failed (exit={proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    # docker mixes streams; emit both.
    for line in (proc.stdout + proc.stderr).splitlines():
        yield line


def _parse_json_log(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _filter_log_records(
    lines: Iterable[str],
    *,
    levels: set[str] | None = None,
    run_id: str | None = None,
    sweep_stage: str | None = None,
    message_substr: str | None = None,
) -> Iterator[dict[str, Any]]:
    for line in lines:
        rec = _parse_json_log(line)
        if rec is None:
            continue
        if levels is not None and rec.get("level") not in levels:
            continue
        if run_id is not None and rec.get("run_id") != run_id:
            continue
        if sweep_stage is not None and rec.get("sweep_stage") != sweep_stage:
            continue
        if message_substr is not None:
            msg = str(rec.get("message", ""))
            if message_substr not in msg:
                continue
        yield rec


def _fmt_log_record(rec: dict[str, Any]) -> str:
    parts = [
        rec.get("timestamp") or rec.get("time") or "?",
        rec.get("level", "?"),
        rec.get("logger", ""),
    ]
    msg = rec.get("message", "")
    extras = []
    for key in (
        "run_id",
        "profile",
        "note_id",
        "sweep_stage",
        "lithos_status",
        "status",
        "exc_type",
        "stage",
        "kind",
        "detail",
    ):
        val = rec.get(key)
        if val not in (None, "", [], {}):
            extras.append(f"{key}={val}")
    out = "  ".join(p for p in parts if p) + "  " + msg
    if extras:
        out += "  [" + ", ".join(extras) + "]"
    return out


# ── subcommands ─────────────────────────────────────────────────────


def cmd_recent(args: argparse.Namespace) -> int:
    env = _load_env(args.env)
    state = _state_dir(env)
    runs = _read_runs_jsonl(state)
    runs = _filter_runs(runs, profile=args.profile)
    runs = runs[-args.limit :] if args.limit else runs

    active = _read_active_runs(state)
    if active:
        print("Active runs:")
        for run in active:
            _print_run_row(run)
        print()

    print(f"Recent {len(runs)} run(s) from {state / 'runs.jsonl'}:")
    if not runs:
        print("  (none)")
        return 0
    for run in runs:
        _print_run_row(run)
    return 0


def cmd_failures(args: argparse.Namespace) -> int:
    env = _load_env(args.env)
    state = _state_dir(env)
    runs = _read_runs_jsonl(state)
    failed = _filter_runs(
        runs,
        profile=args.profile,
        statuses={"failed", "abandoned"},
    )
    degraded = _filter_runs(runs, profile=args.profile, degraded_only=True)

    print(f"Failed/abandoned runs ({len(failed)}):")
    for run in failed[-args.limit :]:
        _print_run_row(run)
    if not failed:
        print("  (none)")

    print()
    print(f"Degraded runs ({len(degraded)}):")
    for run in degraded[-args.limit :]:
        _print_run_row(run)
    if not degraded:
        print("  (none)")

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    env = _load_env(args.env)
    state = _state_dir(env)
    runs = _read_runs_jsonl(state) + _read_active_runs(state)
    match = next((r for r in runs if r.get("run_id") == args.run_id), None)
    if match is None:
        print(f"run_id {args.run_id!r} not found in ledger")
        return 1

    print("Ledger entry:")
    print(json.dumps(match, indent=2, default=str))

    container = _container_name(env)
    print()
    print(f"Log lines for run from container {container!r} (last {args.tail}):")
    records = list(
        _filter_log_records(
            _docker_logs_iter(container, since=args.since, tail=args.tail),
            run_id=args.run_id,
        )
    )
    if not records:
        print(
            "  (no records — try --since longer, "
            "--tail higher, or check the container is running)"
        )
        return 0
    for rec in records:
        print(_fmt_log_record(rec))
    return 0


def cmd_warnings(args: argparse.Namespace) -> int:
    env = _load_env(args.env)
    container = _container_name(env)
    levels = {args.level} if args.level else {"WARNING", "ERROR", "CRITICAL"}
    records = list(
        _filter_log_records(
            _docker_logs_iter(container, since=args.since, tail=args.tail),
            levels=levels,
            run_id=args.run_id,
            message_substr=args.contains,
        )
    )
    print(
        f"{len(records)} {'/'.join(sorted(levels))} record(s) "
        f"from container {container!r}:"
    )
    for rec in records:
        print(_fmt_log_record(rec))
    return 0


def cmd_terminal_flips(args: argparse.Namespace) -> int:
    env = _load_env(args.env)
    container = _container_name(env)
    flips_by_stage: dict[str, list[dict[str, Any]]] = {
        "tier2_terminal_flip": [],
        "tier3_terminal_flip": [],
        "archive_terminal_flip": [],
    }
    for rec in _filter_log_records(
        _docker_logs_iter(container, since=args.since, tail=args.tail),
        levels={"WARNING"},
    ):
        stage = rec.get("sweep_stage")
        if isinstance(stage, str) and stage in flips_by_stage:
            flips_by_stage[stage].append(rec)

    for stage, recs in flips_by_stage.items():
        print(f"{stage} ({len(recs)}):")
        if not recs:
            print("  (none)")
            continue
        for rec in recs:
            print(
                f"  {rec.get('timestamp')}  note_id={rec.get('note_id')} "
                f"profile={rec.get('profile')} "
                f"kind={rec.get('kind') or rec.get('stage')} "
                f"detail={rec.get('detail')!r}"
            )
        print()
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    env = _load_env(args.env)
    host = env.get("INFLUX_ADMIN_BIND_HOST", "127.0.0.1")
    if host in {"", "0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = env.get("INFLUX_ADMIN_HOST_PORT") or env.get("INFLUX_ADMIN_PORT", "8080")
    base = env.get("INFLUX_ADMIN_URL", f"http://{host}:{port}").rstrip("/")
    print(
        "POST /runs/cancel is not currently exposed by the admin HTTP API "
        "(see http_api.py)."
    )
    print(
        "To stop an in-flight run, restart the container — the active "
        "ledger entry will be marked 'abandoned' on the next start "
        "(see run_ledger.abandon_active)."
    )
    print()
    print(f"Inspect the active ledger first: cat {_state_dir(env) / 'active-runs.json'}")
    print(
        f"Restart command: cd $(git rev-parse --show-toplevel) && "
        f"./docker/run.sh {args.env} restart"
    )
    print(f"Admin base URL (for /status, /runs/recent): {base}")
    return 0


# ── CLI plumbing ────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env",
        default="staging",
        help="environment name matching docker/.env.<name> (default: staging)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_recent = sub.add_parser("recent", help="recent run ledger entries")
    p_recent.add_argument("--profile", help="filter to a single profile")
    p_recent.add_argument(
        "--limit",
        type=int,
        default=20,
        help="cap output rows (default: 20)",
    )
    p_recent.set_defaults(func=cmd_recent)

    p_failures = sub.add_parser("failures", help="recent failed/abandoned/degraded runs")
    p_failures.add_argument("--profile")
    p_failures.add_argument("--limit", type=int, default=20)
    p_failures.set_defaults(func=cmd_failures)

    p_run = sub.add_parser("run", help="ledger entry + log lines for one run_id")
    p_run.add_argument("run_id")
    p_run.add_argument(
        "--since",
        default="24h",
        help="docker logs --since window (default: 24h)",
    )
    p_run.add_argument(
        "--tail",
        type=int,
        default=20000,
        help="docker logs --tail (default: 20000)",
    )
    p_run.set_defaults(func=cmd_run)

    p_warn = sub.add_parser(
        "warnings",
        help="WARNING/ERROR docker log records (filter by run_id or substring)",
    )
    p_warn.add_argument("--run-id", dest="run_id")
    p_warn.add_argument("--contains", help="message substring filter")
    p_warn.add_argument(
        "--level",
        choices=["WARNING", "ERROR", "CRITICAL"],
        help="restrict to a single level (default: all three)",
    )
    p_warn.add_argument("--since", default="24h")
    p_warn.add_argument("--tail", type=int, default=20000)
    p_warn.set_defaults(func=cmd_warnings)

    p_flips = sub.add_parser("terminal-flips", help="per-stage terminal-flip events")
    p_flips.add_argument("--since", default="7d")
    p_flips.add_argument("--tail", type=int, default=50000)
    p_flips.set_defaults(func=cmd_terminal_flips)

    p_cancel = sub.add_parser(
        "cancel",
        help="how to abort an in-flight run (no destructive side effects)",
    )
    p_cancel.set_defaults(func=cmd_cancel)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
