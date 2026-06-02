#!/usr/bin/env python3
"""Submit a URL to the Influx inbox (docs/plans/inbox.md §15).

Creates a single Lithos task tagged ``influx:inbox`` carrying the v1
submission metadata (``kind="url"``, ``url``, ``submitted_by``, optional
``title`` / ``summary`` / ``source_tag``).  Influx's inbox tick claims it,
scores it against every enabled profile, and ingests where it clears.

Write-only against Lithos (one ``lithos_task_create`` MCP call); it makes
no calls to the Influx service itself.  v1 is URL-only — a public PDF URL
works (the cascade branches on URL shape); local-PDF support is v2 (§16).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_TASK_TAG = "influx:inbox"
_SOURCE_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_ALLOWED_SCHEMES = ("http", "https")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _resolve_lithos_url(args: argparse.Namespace, env: dict[str, str]) -> str | None:
    """Resolve the Lithos SSE URL: --lithos-url → LITHOS_URL → influx config."""
    if args.lithos_url:
        return args.lithos_url
    if env.get("LITHOS_URL"):
        return env["LITHOS_URL"]
    try:
        from influx.config import load_config

        return load_config().lithos.url
    except Exception:  # noqa: BLE001 — best-effort config discovery
        return None


def _build_metadata(args: argparse.Namespace) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "kind": "url",
        "url": args.url,
        "submitted_by": args.submitted_by,
    }
    if args.title:
        metadata["title"] = args.title
    summary = args.summary
    if args.summary_file:
        summary = Path(args.summary_file).read_text(encoding="utf-8")
    if summary:
        metadata["summary"] = summary
    if args.source_tag:
        metadata["source_tag"] = args.source_tag
    return metadata


def _build_task_body(args: argparse.Namespace) -> dict[str, Any]:
    label = args.title or args.url
    return {
        "title": f"Influx inbox: {label}",
        "agent": args.submitted_by,
        "tags": [_TASK_TAG],
        "metadata": _build_metadata(args),
    }


async def _submit(lithos_url: str, body: dict[str, Any]) -> str:
    from influx.lithos_client import LithosClient

    client = LithosClient(url=lithos_url)
    try:
        result = await client.task_create_body(
            title=body["title"],
            agent=body["agent"],
            tags=body["tags"],
            metadata=body["metadata"],
        )
    finally:
        await client.close()
    return str(result.get("task_id") or result.get("id") or "<unknown>")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="the URL to submit (http/https)")
    parser.add_argument("--title", help="hint for the candidate's title slot")
    summary_group = parser.add_mutually_exclusive_group()
    summary_group.add_argument(
        "--summary", help="pre-fetched summary for the filter prompt"
    )
    summary_group.add_argument(
        "--summary-file",
        help="read the summary from a file (alternative to --summary)",
    )
    parser.add_argument(
        "--source-tag",
        help="resulting note's source:* tag (default: inbox); conservative slug",
    )
    parser.add_argument(
        "--submitted-by",
        default=f"manual:{getpass.getuser()}",
        help="submitter identifier (default: manual:<username>)",
    )
    parser.add_argument(
        "--env",
        default="staging",
        help="environment name matching docker/.env.<name> (default: staging)",
    )
    parser.add_argument("--lithos-url", help="override the Lithos SSE URL")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the task body that would be sent without creating it",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Validate BEFORE any MCP call (§15.5).
    parsed = urlparse(args.url)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
        print(
            f"error: URL must be http(s) with a host, got {args.url!r}",
            file=sys.stderr,
        )
        return 2
    if args.source_tag and not _SOURCE_TAG_RE.match(args.source_tag):
        print(
            f"error: --source-tag must match {_SOURCE_TAG_RE.pattern}",
            file=sys.stderr,
        )
        return 2

    body = _build_task_body(args)

    if args.dry_run:
        print(json.dumps(body, indent=2))
        return 0

    env_path = _repo_root() / "docker" / f".env.{args.env}"
    env = _load_env(env_path) if env_path.exists() else {}
    lithos_url = _resolve_lithos_url(args, env)
    if not lithos_url:
        print(
            "error: could not resolve the Lithos URL — pass --lithos-url or set "
            "LITHOS_URL",
            file=sys.stderr,
        )
        return 2

    task_id = asyncio.run(_submit(lithos_url, body))
    print(f"Created task {task_id} (kind=url)")
    print(f"Track outcome: lithos task show {task_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
