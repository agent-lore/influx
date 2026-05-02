#!/usr/bin/env python3
"""Generate a small operator report for an Influx environment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        env[key.strip()] = value
    return env


def _admin_base_url(env: dict[str, str]) -> str:
    if env.get("INFLUX_ADMIN_URL"):
        return env["INFLUX_ADMIN_URL"].rstrip("/")
    host = env.get("INFLUX_ADMIN_BIND_HOST", "127.0.0.1")
    if host in {"", "0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = env.get("INFLUX_ADMIN_HOST_PORT") or env.get("INFLUX_ADMIN_PORT", "8080")
    return f"http://{host}:{port}"


def _get_json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=10) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError(f"{url} did not return a JSON object")
    return data


def _count_files(path: Path, suffix: str | None = None) -> int | None:
    if not path.exists():
        return None
    if suffix is None:
        return sum(1 for child in path.rglob("*") if child.is_file())
    return sum(1 for child in path.rglob(f"*{suffix}") if child.is_file())


def _infer_knowledge_articles_path(env: dict[str, str]) -> Path | None:
    explicit = env.get("LITHOS_KNOWLEDGE_PATH")
    if explicit:
        return Path(explicit) / "articles"

    archive_path = env.get("INFLUX_ARCHIVE_PATH")
    if not archive_path:
        return None
    staging_root = Path(archive_path).parent
    candidate = staging_root / "lithos" / "knowledge" / "articles"
    return candidate


def _print_status(status: dict[str, object]) -> None:
    print(f"Status: {status.get('status')} ready={status.get('ready')}")
    print(f"Version: {status.get('version')}")
    profiles = status.get("profiles")
    if isinstance(profiles, dict):
        print("\nProfiles:")
        for name, raw in profiles.items():
            if not isinstance(raw, dict):
                continue
            print(
                "  "
                f"{name}: running={raw.get('currently_running')} "
                f"last={raw.get('last_run_status')} at={raw.get('last_run_at')} "
                f"next={raw.get('next_run_at')}"
            )


def _print_runs(runs_payload: dict[str, object]) -> None:
    warning = runs_payload.get("warning")
    if warning:
        print(f"\nRecent Runs: unavailable ({warning})")
        return

    active = runs_payload.get("active")
    if isinstance(active, list) and active:
        print("\nActive Runs:")
        for raw in active:
            if isinstance(raw, dict):
                print(
                    "  "
                    f"{raw.get('profile')} {raw.get('kind')} "
                    f"run_id={raw.get('run_id')} started={raw.get('started_at')}"
                )

    runs = runs_payload.get("runs")
    print("\nRecent Runs:")
    if not isinstance(runs, list) or not runs:
        print("  none")
        return
    for raw in runs:
        if not isinstance(raw, dict):
            continue
        status = raw.get("status")
        if raw.get("degraded"):
            status = f"{status} (degraded)"
        print(
            "  "
            f"{raw.get('completed_at')} {raw.get('profile')} {raw.get('kind')} "
            f"{status} checked={raw.get('sources_checked')} "
            f"ingested={raw.get('ingested')} duration={raw.get('duration_seconds')}"
        )
        if raw.get("error"):
            print(f"    error={raw.get('error')}")
        for src_err in raw.get("source_acquisition_errors") or []:
            if isinstance(src_err, dict):
                print(
                    f"    source_error: source={src_err.get('source')} "
                    f"kind={src_err.get('kind')} "
                    f"detail={src_err.get('detail')}"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Standardised on ``--env <name>`` to match scripts/influx-diagnose.py.
    # The legacy positional is preserved as a backwards-compat alias so
    # existing operator muscle memory (``./scripts/influx-report.py dev``)
    # keeps working; ``--env`` wins if both are given.
    parser.add_argument(
        "--env",
        dest="env",
        default="staging",
        help="environment name matching docker/.env.<name> (default: staging)",
    )
    parser.add_argument(
        "environment",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,  # legacy positional alias for --env
    )
    parser.add_argument("--base-url", help="override admin API base URL")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    # ``--env`` wins when explicitly set; otherwise honour the legacy
    # positional.  We can't directly tell whether ``--env`` was passed
    # (argparse hides that), so we fall back to the positional only when
    # ``--env`` still holds the default and a positional was given.
    environment = (
        args.environment
        if args.environment is not None and args.env == "staging"
        else args.env
    )
    env_path = _repo_root() / "docker" / f".env.{environment}"
    if not env_path.exists():
        print(f"Environment file not found: {env_path}", file=sys.stderr)
        return 2

    env = _load_env(env_path)
    base_url = (args.base_url or _admin_base_url(env)).rstrip("/")

    try:
        status = _get_json(f"{base_url}/status")
        try:
            runs = _get_json(f"{base_url}/runs/recent?limit={args.limit}")
        except HTTPError as exc:
            if exc.code != 404:
                raise
            runs = {
                "active": [],
                "runs": [],
                "warning": "GET /runs/recent returned 404; rebuild/restart Influx",
            }
    except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to query {base_url}: {exc}", file=sys.stderr)
        return 1

    print(f"Influx Report: {environment}")
    print(f"Admin API: {base_url}")
    _print_status(status)
    _print_runs(runs)

    archive_path = env.get("INFLUX_ARCHIVE_PATH")
    if archive_path:
        count = _count_files(Path(archive_path))
        print(f"\nArchive files: {count if count is not None else 'unavailable'}")

    knowledge_articles = _infer_knowledge_articles_path(env)
    if knowledge_articles is not None:
        count = _count_files(knowledge_articles, ".md")
        print(
            f"Knowledge article notes: {count if count is not None else 'unavailable'}"
        )

    state_path = env.get("INFLUX_STATE_PATH")
    if state_path:
        print(f"Run ledger: {Path(state_path) / 'runs.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
