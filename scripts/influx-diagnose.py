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
    squatters       Find Lithos docs squatting slugs that block Influx
                    writes; optionally delete them with --apply --yes
    slug-collision-backlog
                    List slug collisions that survived the in-client
                    recovery chain (squatter-shape dispatch + suffix
                    retry); read from
                    ${INFLUX_STATE_PATH}/unresolved-slug-collisions.jsonl
    cancel          Print the curl line for cancelling an in-flight run
                    (this script never sends destructive HTTP itself)

Posture
-------
Subcommands default to read-only: no HTTP POSTs, no docker exec, no
ledger writes.  The ``squatters`` subcommand can delete Lithos
documents but only when ``--apply`` is combined with an explicit
per-id ``--yes <doc-id>`` confirmation (or ``--yes-to-all``); the
default invocation is a pure log scan.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
        sys.exit("INFLUX_STATE_PATH not set in env file; cannot locate runs.jsonl")
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


_SINCE_DAYS_RE = re.compile(r"^\s*(\d+)\s*d\s*$", re.IGNORECASE)


def _normalise_since(since: str | None) -> str | None:
    """Translate ``Nd`` (days) into ``(N*24)h`` for ``docker logs --since``.

    Docker's duration parser only understands Go's ``time.ParseDuration``
    units (``s``/``m``/``h``), so a friendly ``--since 7d`` produces
    ``invalid value for "since"``.  We translate at the boundary so the
    operator-facing CLI can keep using day units.  Mixed-unit strings
    (``2d12h``) are out of scope; pass them as ``60h`` directly.
    """
    if not since:
        return since
    m = _SINCE_DAYS_RE.match(since)
    if not m:
        return since
    return f"{int(m.group(1)) * 24}h"


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
    since = _normalise_since(since)
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
        sys.exit(f"docker logs failed (exit={proc.returncode}): {proc.stderr.strip()}")
    # docker mixes streams; emit both.
    yield from (proc.stdout + proc.stderr).splitlines()


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


# ── slug-collision squatter discovery ───────────────────────────────


# Slug-collision WARNINGs from ``influx.scheduler`` carry the lithos
# diagnostic in their ``detail`` extra (PR #30).  Two shapes are
# possible: today only the suffix-retry's response surfaces (covered
# by the ``existing_id=<uuid>; Slug '<slug>' …`` form); a future
# follow-up (#32) will enumerate both attempts in a single detail
# string.  The regexes match on substrings so either shape works.
_SQUATTER_ID_RE = re.compile(
    # Match bare ``existing_id=<uuid>`` (PR #30 shape) and any
    # ``*_existing_id=<uuid>`` prefix variant that #32 may introduce
    # (e.g. ``first_existing_id=`` / ``retry_existing_id=``).
    r"(?:^|[^a-zA-Z0-9])(?:[a-zA-Z]+_)?existing_id=([0-9a-fA-F-]{8,})"
)
_SQUATTER_SLUG_RE = re.compile(r"Slug '([^']+)' already in use")


def _extract_squatters_from_logs(
    records: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Scan WARNING records for slug_collision squatters.

    Returns a dict keyed by squatter doc-id.  Each value carries the
    slug, the colliding incoming source_url + title (if present in the
    log record's ``extra`` fields), and first/last/total seen counts.

    Pure function: no I/O.  Unit-tested without docker by feeding it a
    list of dicts shaped like ``InfluxJsonFormatter`` output.
    """
    squatters: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.get("level") != "WARNING":
            continue
        if rec.get("status") != "slug_collision" and (
            "slug_collision" not in str(rec.get("message", ""))
        ):
            continue
        # The structured ``detail`` field carries the diagnostic; fall
        # back to the message text if a slimmer log shape ever appears.
        detail = str(rec.get("detail") or "")
        if not detail:
            detail = str(rec.get("message", ""))
        # A single detail string may reference multiple squatters once
        # #32 lands; iterate every match so both ids are captured.
        ids = _SQUATTER_ID_RE.findall(detail)
        slugs = _SQUATTER_SLUG_RE.findall(detail)
        ts = rec.get("timestamp") or ""
        source_url = str(rec.get("source_url") or "")
        title = str(rec.get("title") or "")
        for idx, doc_id in enumerate(ids):
            entry = squatters.setdefault(
                doc_id,
                {
                    "doc_id": doc_id,
                    "slugs": [],
                    "source_urls": set(),
                    "titles": set(),
                    "first_seen": ts,
                    "last_seen": ts,
                    "count": 0,
                },
            )
            slug = slugs[idx] if idx < len(slugs) else ""
            if slug and slug not in entry["slugs"]:
                entry["slugs"].append(slug)
            if source_url:
                entry["source_urls"].add(source_url)
            if title:
                entry["titles"].add(title)
            if ts:
                if not entry["first_seen"] or ts < entry["first_seen"]:
                    entry["first_seen"] = ts
                if ts > entry["last_seen"]:
                    entry["last_seen"] = ts
            entry["count"] += 1
    return squatters


def _print_squatter(entry: dict[str, Any]) -> None:
    print(f"SQUATTER doc_id={entry['doc_id']}")
    for slug in entry["slugs"]:
        print(f"   slug={slug!r}")
    if entry["first_seen"] or entry["last_seen"]:
        print(
            f"   seen {entry['count']}x  "
            f"first={entry['first_seen']}  last={entry['last_seen']}"
        )
    for url in sorted(entry["source_urls"]):
        print(f"   colliding source_url={url}")
    for title in sorted(entry["titles"]):
        print(f"   colliding title={title!r}")


def _running_inside_docker() -> bool:
    """Detect whether we're inside a docker container.

    The ``[lithos] url`` in ``influx.toml`` resolves
    ``host.docker.internal`` to the docker host from inside the influx
    container, but that hostname is not resolvable on the host.  When
    the script is invoked from the host we have to swap in the
    loopback address that the docker port mapping exposes.
    """
    return Path("/.dockerenv").exists()


def _rewrite_url_for_host(url: str) -> str:
    """Substitute ``host.docker.internal`` → ``127.0.0.1`` when on the host.

    Operator-friendly default: the staging / dev influx.toml stores the
    URL the **container** uses (``host.docker.internal:<port>``), so the
    bare diagnose script invoked from the host would otherwise fail with
    a DNS error.  Pass ``--lithos-url`` to override this rewrite.
    """
    if _running_inside_docker():
        return url
    if "host.docker.internal" not in url:
        return url
    return url.replace("host.docker.internal", "127.0.0.1")


def _read_lithos_url(args: argparse.Namespace, env: dict[str, str]) -> str:
    """Resolve the Lithos MCP/SSE URL for ``--apply`` mode.

    Resolution order:
      1. ``--lithos-url`` CLI flag (explicit override; never rewritten).
      2. ``LITHOS_URL`` env var on the host (rewritten if needed).
      3. ``[lithos] url`` in ``${INFLUX_DATA_PATH}/influx.toml``
         (rewritten if needed — see :func:`_rewrite_url_for_host`).
    """
    if getattr(args, "lithos_url", None):
        return str(args.lithos_url)
    if "LITHOS_URL" in os.environ:
        return _rewrite_url_for_host(os.environ["LITHOS_URL"])
    data_path = env.get("INFLUX_DATA_PATH")
    if data_path:
        toml_path = Path(data_path) / "influx.toml"
        if toml_path.exists():
            try:
                import tomllib
            except ImportError:  # pragma: no cover — Python < 3.11
                sys.exit("tomllib unavailable; pass --lithos-url explicitly")
            with toml_path.open("rb") as fh:
                cfg = tomllib.load(fh)
            url = cfg.get("lithos", {}).get("url")
            if isinstance(url, str) and url:
                rewritten = _rewrite_url_for_host(url)
                if rewritten != url:
                    print(
                        f"note: rewrote container-only host "
                        f"{url!r} -> {rewritten!r} for host invocation; "
                        "pass --lithos-url to override"
                    )
                return rewritten
    sys.exit(
        "Lithos URL not resolved.  Pass --lithos-url, set $LITHOS_URL, "
        "or ensure [lithos] url is set in $INFLUX_DATA_PATH/influx.toml."
    )


def _summarise_doc_for_refusal(doc: dict[str, Any]) -> str:
    """Concise one-line preview of a Lithos doc for the safety-check refusal.

    The operator needs four signals to triage a non-Influx-authored
    squatter without a separate ``lithos_read`` round trip: the
    title, the source URL (if any), the ingester / author tag, and
    the full tag list.  Render those compactly on a single message.
    """
    title = str(doc.get("title") or "(no title)")[:80]
    source_url = str(doc.get("source_url") or "")
    tags = list(doc.get("tags") or [])
    author = str(doc.get("author") or "")
    ingester = next(
        (t.split(":", 1)[1] for t in tags if t.startswith("ingested-by:")),
        "(no ingested-by tag)",
    )
    parts = [
        f"title={title!r}",
        f"author={author!r}" if author else None,
        f"ingested_by={ingester}",
        f"source_url={source_url}" if source_url else None,
        f"tags={tags}",
    ]
    return " ".join(p for p in parts if p)


def _format_exception_chain(exc: BaseException) -> str:
    """Render an exception (and its causes / sub-exceptions) compactly.

    The MCP client uses ``anyio`` task groups under the hood, so a
    transport-level failure surfaces as
    ``ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)``
    by default — the actual diagnostic (DNS error, connection refused)
    lives inside ``exc.exceptions[0]``.  Walk the chain and surface
    every distinct ``type(exc).__name__: <str>`` so the operator can
    diagnose without re-running with ``--verbose``.
    """
    seen: list[str] = []

    def _walk(e: BaseException) -> None:
        rendered = f"{type(e).__name__}: {e}"
        if rendered not in seen:
            seen.append(rendered)
        # ExceptionGroup (PEP 654) carries siblings on .exceptions
        sub = getattr(e, "exceptions", None)
        if isinstance(sub, (list, tuple)):
            for child in sub:
                if isinstance(child, BaseException):
                    _walk(child)
        # Regular cause / context chain
        for nxt in (e.__cause__, e.__context__):
            if isinstance(nxt, BaseException):
                _walk(nxt)

    _walk(exc)
    # Cap the rendered chain so a deep cause stack stays readable.
    return " | ".join(seen[:6])


def _ensure_project_runtime_or_reexec() -> None:
    """Re-exec the script under ``uv run`` if project deps are missing.

    The script is operator-facing and meant to be invoked as
    ``./scripts/influx-diagnose.py …`` rather than
    ``uv run scripts/influx-diagnose.py …``.  Read-only subcommands do
    not import any project module, so the bare invocation works under
    the system Python.  ``squatters --apply`` does need
    ``influx.lithos_client``, which transitively pulls in ``mcp`` from
    the project venv.

    Detect the gap by attempting an import inside this current
    interpreter.  If it fails, ``os.execvp`` ourselves under
    ``uv run`` so the operator's argv reaches the venv-backed
    interpreter unchanged.  No-op when the imports already succeed
    (``uv run …`` invocation, or ``PYTHONPATH`` already set).
    """
    src_path = str(_repo_root() / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    try:
        import influx.lithos_client  # noqa: F401

        return  # already runnable
    except ImportError:
        pass

    if not shutil.which("uv"):
        sys.exit(
            "Project dependencies are not importable and 'uv' is not on "
            "PATH.  Install uv (https://docs.astral.sh/uv/) or run the "
            f"script as: uv run {sys.argv[0]} {' '.join(sys.argv[1:])}"
        )

    if os.environ.get("INFLUX_DIAGNOSE_REEXECED") == "1":
        # Defensive: avoid an infinite re-exec loop if uv run somehow
        # still produces an environment without the project deps.
        sys.exit(
            "uv run did not provide importable project dependencies.  "
            "Check that this directory contains a valid pyproject.toml + "
            "uv.lock and that 'uv sync' has been run."
        )

    os.environ["INFLUX_DIAGNOSE_REEXECED"] = "1"
    os.execvp(
        "uv",
        ["uv", "run", "--project", str(_repo_root()), sys.argv[0], *sys.argv[1:]],
    )


# Outcomes for ``_delete_squatter``.  Distinguishing ``already_gone``
# from ``refused`` matters: a doc that vanished between scan and delete
# (e.g. cleaned up by a parallel run, or by the operator outside this
# script) is effectively the success state for our caller, not a fault
# requiring intervention.
DELETE_OK = "deleted"
DELETE_ALREADY_GONE = "already_gone"
DELETE_REFUSED = "refused"


def _is_doc_not_found(message: str) -> bool:
    """Match Lithos's ``doc_not_found`` shapes from delete + read paths."""
    if not message:
        return False
    needle = message.lower()
    return "document not found" in needle or "doc_not_found" in needle


async def _delete_squatter(
    *,
    lithos_url: str,
    doc_id: str,
    agent: str,
    require_influx_authored: bool,
) -> tuple[str, str]:
    """Read-then-delete one squatter.

    Returns ``(outcome, reason)`` where ``outcome`` is one of
    :data:`DELETE_OK`, :data:`DELETE_ALREADY_GONE`, or
    :data:`DELETE_REFUSED`.  The caller is responsible for invoking
    :func:`_ensure_project_runtime_or_reexec` first — see
    ``cmd_squatters``.
    """
    from influx.lithos_client import LithosClient

    client = LithosClient(url=lithos_url)
    try:
        try:
            doc = await client.read_note(note_id=doc_id)
        except BaseException as exc:  # noqa: BLE001
            chain = _format_exception_chain(exc)
            if _is_doc_not_found(chain):
                # The doc vanished before we could even read it — most
                # likely a parallel cleanup or out-of-band delete.
                # Effective success for our caller.
                return DELETE_ALREADY_GONE, "doc not found at read time"
            return DELETE_REFUSED, f"read failed: {chain}"

        tags = list(doc.get("tags") or [])
        if require_influx_authored and "ingested-by:influx" not in tags:
            # Surface enough of the doc to let the operator decide
            # whether the squatter is an old Influx note that just lost
            # its tag (safe to delete after manual review) or a genuine
            # user/other-agent note (must NOT be deleted).
            preview = _summarise_doc_for_refusal(doc)
            return (
                DELETE_REFUSED,
                "doc is not influx-authored "
                "(missing 'ingested-by:influx' tag).  "
                f"Preview: {preview}  "
                "If safe to delete after review, re-run with "
                "--no-require-influx-authored.",
            )

        try:
            result = await client.call_tool(
                "lithos_delete",
                {"id": doc_id, "agent": agent},
            )
        except BaseException as exc:  # noqa: BLE001
            return DELETE_REFUSED, f"delete failed: {_format_exception_chain(exc)}"

        # Lithos returns either a success envelope or an error envelope
        # with ``status="error"`` (and ``code`` / ``message``).  The
        # ``doc_not_found`` shape happens when the read succeeded but
        # the doc was deleted between the read and the delete — treat
        # that as ``already_gone`` rather than ``refused``.
        try:
            body = json.loads(result.content[0].text)  # type: ignore[union-attr]
        except (AttributeError, IndexError, TypeError, json.JSONDecodeError):
            body = {"status": "deleted"}
        if body.get("status") == "error":
            code = body.get("code", "")
            message = body.get("message", body)
            if code == "doc_not_found" or _is_doc_not_found(str(message)):
                return DELETE_ALREADY_GONE, "doc not found at delete time"
            return DELETE_REFUSED, f"lithos_delete error: {message}"
        return DELETE_OK, "deleted"
    finally:
        await client.close()


def cmd_squatters(args: argparse.Namespace) -> int:
    env = _load_env(args.env)
    container = _container_name(env)

    records = list(
        _filter_log_records(
            _docker_logs_iter(container, since=args.since, tail=args.tail),
            levels={"WARNING"},
            message_substr="slug_collision",
        )
    )
    squatters = _extract_squatters_from_logs(records)

    if not squatters:
        print(
            f"No slug_collision squatters found in container {container!r} "
            f"(window: --since {args.since} --tail {args.tail})."
        )
        return 0

    print(
        f"{len(squatters)} squatter(s) found in container {container!r} "
        f"(window: --since {args.since} --tail {args.tail}):\n"
    )
    # Sorted by last_seen desc so the freshest pain is at the top.
    ordered = sorted(
        squatters.values(),
        key=lambda e: str(e.get("last_seen") or ""),
        reverse=True,
    )
    for entry in ordered:
        _print_squatter(entry)
        print()

    if not args.apply:
        print(
            "Default mode is read-only.  To delete a squatter, re-run with:\n"
            "    --apply --yes <doc-id>     (per-id confirmation, repeatable)\n"
            "    --apply --yes-to-all       (delete every squatter listed above)\n"
            "Pre-delete safety: each doc is fetched and its tags checked to\n"
            "ensure it carries 'ingested-by:influx' before deletion."
        )
        return 0

    # ``--apply`` needs the project deps (LithosClient → mcp).  Re-exec
    # under ``uv run`` if we are not already in the project venv so the
    # operator can keep using the bare ``./scripts/influx-diagnose.py``
    # invocation without thinking about the runtime.
    _ensure_project_runtime_or_reexec()

    confirmed: set[str] = set(args.yes or [])
    if args.yes_to_all:
        confirmed.update(squatters.keys())
    if not confirmed:
        sys.exit(
            "--apply requires at least one --yes <doc-id> (or --yes-to-all).  Aborted."
        )
    unknown = confirmed - set(squatters.keys())
    if unknown:
        print(
            "Warning: --yes ids not present in the log scan results: "
            + ", ".join(sorted(unknown))
        )
        confirmed -= unknown
    if not confirmed:
        sys.exit("No matching --yes ids; nothing to delete.")

    lithos_url = _read_lithos_url(args, env)
    print(
        f"Apply mode: deleting {len(confirmed)} squatter(s) via "
        f"lithos_delete on {lithos_url}\n"
    )

    import asyncio

    deleted = 0
    already_gone = 0
    refused = 0
    for doc_id in sorted(confirmed):
        outcome, reason = asyncio.run(
            _delete_squatter(
                lithos_url=lithos_url,
                doc_id=doc_id,
                agent=args.agent,
                require_influx_authored=not args.no_require_influx_authored,
            )
        )
        if outcome == DELETE_OK:
            print(f"DELETED      doc_id={doc_id}  ({reason})")
            deleted += 1
        elif outcome == DELETE_ALREADY_GONE:
            # Effective success — the squatter is no longer in Lithos.
            # Logged separately so a parallel cleanup is observable.
            print(f"ALREADY GONE doc_id={doc_id}  ({reason})")
            already_gone += 1
        else:
            print(f"REFUSED      doc_id={doc_id}  reason={reason}")
            refused += 1

    print()
    print(f"Summary: deleted={deleted} already_gone={already_gone} refused={refused}")
    if already_gone:
        print(
            "Note: 'already gone' squatters are still surfacing because the "
            "log scan reads historical WARNINGs from the docker buffer; the "
            "doc itself has been removed from Lithos.  The next sweep will "
            "either succeed cleanly or surface a fresh squatter."
        )
    # Exit non-zero only on genuine refusals; ``already_gone`` is success.
    return 0 if refused == 0 else 1


def _read_slug_collision_backlog(state_dir: Path) -> list[dict[str, Any]]:
    """Read ``state_dir/unresolved-slug-collisions.jsonl`` (#31 backlog).

    Pure helper so unit tests can drive it without a live state dir.
    Mirrors ``RunLedger.unresolved_slug_collisions`` so this script and
    the daemon agree on the on-disk format without taking a runtime
    dependency on ``influx.run_ledger``.
    """
    path = state_dir / "unresolved-slug-collisions.jsonl"
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


def cmd_slug_collision_backlog(args: argparse.Namespace) -> int:
    env = _load_env(args.env)
    state = _state_dir(env)
    entries = _read_slug_collision_backlog(state)
    if not entries:
        print(
            f"No unresolved slug collisions found "
            f"({state / 'unresolved-slug-collisions.jsonl'})."
        )
        return 0
    print(
        f"{len(entries)} unresolved slug collision(s) "
        f"({state / 'unresolved-slug-collisions.jsonl'}):\n"
    )
    for entry in entries:
        print(
            f"  {entry.get('timestamp', '?')}  "
            f"profile={entry.get('profile', '?')}  "
            f"source={entry.get('source', '?')}  "
            f"run_id={entry.get('run_id', '?')}"
        )
        print(f"      title={entry.get('title', '?')!r}")
        print(f"      source_url={entry.get('source_url', '?')}")
        detail = entry.get("detail", "")
        if detail:
            print(f"      detail={detail}")
        print()
    print(
        "To clean up the squatters that caused these collisions, run:\n"
        "    ./scripts/influx-diagnose.py squatters --apply --yes <doc-id>\n"
        "(the doc ids are embedded in the 'detail' field above)."
    )
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
    print(
        f"Inspect the active ledger first: cat {_state_dir(env) / 'active-runs.json'}"
    )
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

    p_failures = sub.add_parser(
        "failures", help="recent failed/abandoned/degraded runs"
    )
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

    p_squatters = sub.add_parser(
        "squatters",
        help=(
            "find Lithos docs squatting slugs that block influx writes; "
            "default is read-only (log scan), --apply enables deletion"
        ),
    )
    p_squatters.add_argument(
        "--since",
        default="7d",
        help="docker logs --since window (default: 7d)",
    )
    p_squatters.add_argument(
        "--tail",
        type=int,
        default=50000,
        help="docker logs --tail (default: 50000)",
    )
    p_squatters.add_argument(
        "--apply",
        action="store_true",
        help=(
            "actually delete squatters (default: dry-run / log scan only). "
            "Must be combined with --yes <doc-id> or --yes-to-all."
        ),
    )
    p_squatters.add_argument(
        "--yes",
        action="append",
        metavar="DOC_ID",
        help=(
            "confirm deletion of a specific squatter (repeatable). "
            "Required with --apply unless --yes-to-all is set."
        ),
    )
    p_squatters.add_argument(
        "--yes-to-all",
        action="store_true",
        help=("with --apply, delete every squatter listed in the scan. Use with care."),
    )
    p_squatters.add_argument(
        "--agent",
        default="influx-diagnose",
        help=(
            "agent name passed to lithos_delete for the audit trail "
            "(default: influx-diagnose)"
        ),
    )
    p_squatters.add_argument(
        "--lithos-url",
        help=(
            "override the Lithos MCP/SSE URL.  Default resolution: "
            "$LITHOS_URL, then [lithos] url in $INFLUX_DATA_PATH/influx.toml."
        ),
    )
    p_squatters.add_argument(
        "--no-require-influx-authored",
        action="store_true",
        help=(
            "skip the safety check that refuses to delete docs that lack "
            "the 'ingested-by:influx' tag.  Use only after manual review."
        ),
    )
    p_squatters.set_defaults(func=cmd_squatters)

    p_backlog = sub.add_parser(
        "slug-collision-backlog",
        help=(
            "list unresolved slug collisions (papers the squatter-shape "
            "recovery chain in lithos_client could not land)"
        ),
    )
    p_backlog.set_defaults(func=cmd_slug_collision_backlog)

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
