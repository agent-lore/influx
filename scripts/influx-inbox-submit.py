#!/usr/bin/env python3
"""Submit a URL or local PDF to the Influx inbox (docs/plans/inbox.md §15/§16).

Creates a single Lithos task tagged ``influx:inbox``.  The positional
argument is auto-detected: an ``http(s)://`` URL submits ``kind="url"``
(``url`` metadata); any other value is treated as a local PDF path and
submits ``kind="pdf"`` (``local_path`` metadata).  Both carry
``submitted_by`` and optional ``title`` / ``summary`` / ``source_tag``.
Influx's inbox tick claims the task, scores it against every enabled
profile, and ingests where it clears.

For a local PDF the script resolves ``[inbox] pdf_root`` from config and,
if the file is not already under it, copies it in under a deterministic
``<sha256-prefix>-<basename>`` name (the original is never moved/deleted);
the ``local_path`` sent is always the in-``pdf_root`` path.

Write-only against Lithos (one ``lithos_task_create`` MCP call); it makes
no calls to the Influx service itself.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

_TASK_TAG = "influx:inbox"
_SOURCE_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# An http(s) URL → kind="url".  Any other URI scheme (file:, ftp:, mailto:,
# data:, …) on a value that is not an existing file is rejected as not-http;
# everything else is treated as a local path.
_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


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


def _apply_env_to_process(env: dict[str, str]) -> None:
    """Make ``docker/.env.<env>`` values visible to ``influx.config.load_config``.

    ``load_config`` discovers ``INFLUX_CONFIG``/``LITHOS_URL``/etc. from
    ``os.environ`` (config.py:749, :780), not from our local dict — so without
    this the ``--env`` file never influences the config-loader fallback path.
    ``setdefault`` keeps an explicitly-exported process var winning over the
    file (the file is the fallback, never an override).
    """
    for key, value in env.items():
        os.environ.setdefault(key, value)


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


def _add_optional_metadata(
    args: argparse.Namespace, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Fold the shared optional fields (title/summary/source_tag) into *metadata*."""
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


def _looks_like_pdf(path: Path) -> bool:
    """A ``.pdf`` suffix or a ``%PDF`` magic header (cheap pre-MCP check)."""
    if path.suffix.lower() == ".pdf":
        return True
    try:
        with path.open("rb") as fh:
            return fh.read(5).startswith(b"%PDF")
    except OSError:
        return False


def _resolve_pdf_root(args: argparse.Namespace, env: dict[str, str]) -> Path | None:
    """Resolve pdf_root: --pdf-root → INFLUX_PDF_ROOT → influx config."""
    if args.pdf_root:
        return Path(args.pdf_root)
    if env.get("INFLUX_PDF_ROOT"):
        return Path(env["INFLUX_PDF_ROOT"])
    try:
        from influx.config import load_config

        root = load_config().inbox.pdf_root
        return Path(root) if root else None
    except Exception:  # noqa: BLE001 — best-effort config discovery
        return None


def _stage_pdf_into_root(path: Path, pdf_root: Path, *, copy: bool) -> Path:
    """Return the in-``pdf_root`` path for *path*, copying it in when outside.

    A file already under *pdf_root* is used in place; otherwise it is copied
    to a deterministic ``<sha256[:16]>-<basename>`` name (collision-safe and
    keeps the human-readable name).  The user's original is never moved or
    deleted.  When *copy* is false (``--dry-run``) the destination path is
    computed but no file is written.
    """
    resolved = path.resolve()
    root = pdf_root.resolve()
    if resolved.is_relative_to(root):
        return resolved
    h = hashlib.sha256()
    with resolved.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    dest = root / f"{h.hexdigest()[:16]}-{resolved.name}"
    if copy and not dest.exists():
        # Atomic publish: copy to a temp sibling then rename, so an
        # interrupted copy never leaves a partial file that a later run
        # would skip (dest.exists()) and submit as a valid PDF.
        tmp = dest.with_name(f".{dest.name}.tmp")
        shutil.copy2(resolved, tmp)
        os.replace(tmp, dest)
    return dest


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
    parser.add_argument(
        "url",
        metavar="URL_OR_PDF",
        help="an http(s) URL, or a path to a local PDF (kind is auto-detected)",
    )
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
        "--pdf-root",
        help="override [inbox] pdf_root (local PDFs are staged under it)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the task body that would be sent without creating it",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    target = args.url

    # All validation happens BEFORE any MCP call (§15.5).
    if args.source_tag and not _SOURCE_TAG_RE.match(args.source_tag):
        print(
            f"error: --source-tag must match {_SOURCE_TAG_RE.pattern}",
            file=sys.stderr,
        )
        return 2

    # Env feeds both pdf_root and Lithos-URL resolution.
    env_path = _repo_root() / "docker" / f".env.{args.env}"
    env = _load_env(env_path) if env_path.exists() else {}
    _apply_env_to_process(env)

    if _HTTP_RE.match(target):
        from urllib.parse import urlparse

        if not urlparse(target).netloc:
            print(f"error: URL must have a host, got {target!r}", file=sys.stderr)
            return 2
        kind = "url"
        metadata = _add_optional_metadata(
            args,
            {"kind": "url", "url": target, "submitted_by": args.submitted_by},
        )
        label = args.title or target
    else:
        kind = "pdf"
        path = Path(target).expanduser()
        if not path.is_file():
            # A non-http URI scheme (file:, ftp:, mailto:, …) is a malformed
            # URL, not a path; everything else is a missing/non-file path.
            if path.exists():
                print(f"error: not a regular file: {target!r}", file=sys.stderr)
            elif _URI_SCHEME_RE.match(target):
                print(f"error: URL must be http(s), got {target!r}", file=sys.stderr)
            else:
                print(f"error: PDF file not found: {target!r}", file=sys.stderr)
            return 2
        if not _looks_like_pdf(path):
            print(
                f"error: not a PDF (need a .pdf suffix or %PDF header): {target!r}",
                file=sys.stderr,
            )
            return 2
        pdf_root = _resolve_pdf_root(args, env)
        if pdf_root is None:
            print(
                "error: could not resolve [inbox] pdf_root — pass --pdf-root or "
                "set INFLUX_PDF_ROOT",
                file=sys.stderr,
            )
            return 2
        if not pdf_root.is_dir():
            if args.dry_run:
                # Preview only — warn but still show the would-be local_path.
                print(
                    f"warning: pdf_root does not exist yet: {pdf_root}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"error: pdf_root is not a directory: {pdf_root}",
                    file=sys.stderr,
                )
                return 2
        local_path = _stage_pdf_into_root(path, pdf_root, copy=not args.dry_run)
        metadata = _add_optional_metadata(
            args,
            {
                "kind": "pdf",
                "local_path": str(local_path),
                "submitted_by": args.submitted_by,
            },
        )
        label = args.title or path.name

    body = {
        "title": f"Influx inbox: {label}",
        "agent": args.submitted_by,
        "tags": [_TASK_TAG],
        "metadata": metadata,
    }

    if args.dry_run:
        print(json.dumps(body, indent=2))
        return 0

    lithos_url = _resolve_lithos_url(args, env)
    if not lithos_url:
        print(
            "error: could not resolve the Lithos URL — pass --lithos-url or set "
            "LITHOS_URL",
            file=sys.stderr,
        )
        return 2

    task_id = asyncio.run(_submit(lithos_url, body))
    print(f"Created task {task_id} (kind={kind})")
    print(f"Track outcome: lithos task show {task_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
