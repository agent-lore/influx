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
    invalid-source  Audit / clean up notes tagged
                    ``influx:source-invalid`` (issue #162); default
                    read-only, ``--apply`` reconstructs recoverable
                    notes or tombstones unrecoverable ones
    promotion-gate  Evaluate recent scheduled-run health against a
                    configurable severity policy (issue #165); exits
                    0 on PASS / 1 on FAIL so a CI promotion step can
                    consume it directly
    feed-yield      Notes-ingested-per-RSS-feed from the on-disk corpus
                    (joins influx.toml feeds against feed-slug: note
                    tags); surfaces dead/broken, silent, and
                    top-producing feeds for curation (issue #233)
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
        # #36 / #50: surface the structured degraded_reasons so an
        # operator immediately sees WHY a run was flagged
        # (source_acquisition / ingestion_stall / fetch_stall / a
        # combination) rather than just ``(degraded)``.
        reasons = run.get("degraded_reasons") or []
        if reasons:
            status = f"{status} (degraded: {','.join(str(r) for r in reasons)})"
        else:
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
    # #129: surface recovered-retry counts so an operator can see "we
    # hit arXiv 429 twice but recovered" alongside any swallowed final
    # errors.  Empty / missing dicts are skipped silently.
    retry_counts = run.get("source_retry_counts") or {}
    if isinstance(retry_counts, dict) and retry_counts:
        for source, by_kind in sorted(retry_counts.items()):
            if not isinstance(by_kind, dict) or not by_kind:
                continue
            kinds = " ".join(
                f"{kind}={count}" for kind, count in sorted(by_kind.items())
            )
            print(f"      source_retries: source={source} {kinds}")


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
    skipped = _filter_runs(runs, profile=args.profile, statuses={"skipped"})
    degraded = _filter_runs(runs, profile=args.profile, degraded_only=True)

    print(f"Failed/abandoned runs ({len(failed)}):")
    for run in failed[-args.limit :]:
        _print_run_row(run)
    if not failed:
        print("  (none)")

    print()
    print(f"Skipped runs ({len(skipped)}):  # circuit-breaker short-circuit (#40)")
    for run in skipped[-args.limit :]:
        _print_run_row(run)
    if not skipped:
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


def _make_lithos_client(url: str) -> Any:
    """Construct a ``LithosClient`` for the given URL.

    Indirected through this thin factory so unit tests can monkeypatch
    the construction without having to install ``mcp`` (the transitive
    dep that ``influx.lithos_client`` pulls in).  Production callers
    use the factory the same way as a direct construction; the import
    is local so the factory itself stays cheap to call from the
    read-only paths that ``_ensure_project_runtime_or_reexec`` re-execs
    for.
    """
    from influx.lithos_client import LithosClient

    return LithosClient(url=url)


def _squatters_from_id_list(ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Build a ``squatters``-shaped dict from a flat list of doc ids.

    Mirrors the dict ``_extract_squatters_from_logs`` returns but with
    empty metadata: when the operator passes ``--id`` we have no log
    context, so the slug / source_url / title fields are empty.  This
    is the seam that lets ``cmd_squatters`` dispatch identically to
    the log-scan and the ``--id`` paths once a candidate set exists.

    Repeated ids collapse into a single entry — operators sometimes
    paste the same id twice from a backlog list.
    """
    out: dict[str, dict[str, Any]] = {}
    for doc_id in ids:
        if doc_id in out:
            continue
        out[doc_id] = {
            "doc_id": doc_id,
            "slugs": [],
            "source_urls": set(),
            "titles": set(),
            "first_seen": "",
            "last_seen": "",
            "count": 0,
        }
    return out


async def _preview_squatter_by_id(
    *,
    lithos_url: str,
    doc_id: str,
) -> tuple[str, dict[str, Any] | None, str]:
    """Read-only preview helper for the ``--id`` dry-run path.

    Returns ``(outcome, doc_or_none, reason)`` where ``outcome`` is
    one of :data:`DELETE_OK` (doc fetched, dry-run), :data:`DELETE_ALREADY_GONE`
    (doc already deleted), or :data:`DELETE_REFUSED` (read failed for
    another reason).  The doc dict (if any) is returned so the caller
    can render the operator-facing preview without re-fetching.
    """
    client = _make_lithos_client(lithos_url)
    try:
        try:
            doc = await client.read_note(note_id=doc_id)
        except BaseException as exc:  # noqa: BLE001
            chain = _format_exception_chain(exc)
            if _is_doc_not_found(chain):
                return DELETE_ALREADY_GONE, None, "doc not found at read time"
            return DELETE_REFUSED, None, f"read failed: {chain}"
        return DELETE_OK, doc, "doc read"
    finally:
        await client.close()


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
    client = _make_lithos_client(lithos_url)
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


def _print_id_preview(doc_id: str, doc: dict[str, Any] | None, reason: str) -> None:
    """Render a one-screen preview line for a ``--id`` dry-run candidate.

    Surfaces title, tags, and source_url (the four operator signals
    from the acceptance criteria) plus the planned deletion command.
    """
    print(f"SQUATTER doc_id={doc_id}")
    if doc is None:
        print(f"   read failed: {reason}")
        return
    title = str(doc.get("title") or "(no title)")
    source_url = str(doc.get("source_url") or "")
    tags = list(doc.get("tags") or [])
    has_influx_tag = "ingested-by:influx" in tags
    print(f"   title={title!r}")
    if source_url:
        print(f"   source_url={source_url}")
    print(f"   tags={tags}")
    if not has_influx_tag:
        print(
            "   WARNING: missing 'ingested-by:influx' tag — "
            "delete will be REFUSED unless --no-require-influx-authored is set"
        )


def cmd_squatters(args: argparse.Namespace) -> int:
    env = _load_env(args.env)
    container = _container_name(env)

    # ── Candidate-set seam ──
    # Two ways to reach the read-then-delete pipeline:
    #   1. ``--id <doc-id>`` (issue #34): direct, no docker scan.
    #   2. log scan: original path, parses ``slug_collision`` WARNINGs.
    # Both produce the same dict shape, so the apply path below is shared.
    id_list = list(getattr(args, "id", None) or [])
    via_id_flag = bool(id_list)

    if via_id_flag:
        squatters = _squatters_from_id_list(id_list)
    else:
        records = list(
            _filter_log_records(
                _docker_logs_iter(container, since=args.since, tail=args.tail),
                levels={"WARNING"},
                message_substr="slug_collision",
            )
        )
        squatters = _extract_squatters_from_logs(records)

    if not squatters:
        # Only reachable on the log-scan path (``--id`` always seeds at
        # least one candidate).  Keep the exact wording so the existing
        # operator runbook stays accurate.
        print(
            f"No slug_collision squatters found in container {container!r} "
            f"(window: --since {args.since} --tail {args.tail})."
        )
        return 0

    if not via_id_flag:
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

    # ── Read-only paths ──
    if not args.apply:
        if via_id_flag:
            # Operator passed ``--id`` without ``--apply`` — fetch each
            # doc via ``lithos_read`` and surface the preview signals
            # plus the planned deletion command, per acceptance criteria.
            _ensure_project_runtime_or_reexec()
            lithos_url = _read_lithos_url(args, env)
            print(
                f"Read-only preview for {len(squatters)} doc-id(s) via "
                f"lithos_read on {lithos_url}:\n"
            )

            import asyncio

            for doc_id in sorted(squatters.keys()):
                outcome, doc, reason = asyncio.run(
                    _preview_squatter_by_id(
                        lithos_url=lithos_url,
                        doc_id=doc_id,
                    )
                )
                _print_id_preview(doc_id, doc, reason)
                if outcome == DELETE_ALREADY_GONE:
                    print(f"   note: {reason}")
                print()
            print(
                "To delete the docs above, re-run with:\n"
                "    --apply --id <doc-id>      (repeatable; no extra --yes needed)\n"
                "Pre-delete safety: each doc is fetched and its tags checked to\n"
                "ensure it carries 'ingested-by:influx' before deletion."
            )
            return 0

        print(
            "Default mode is read-only.  To delete a squatter, re-run with:\n"
            "    --apply --yes <doc-id>     (per-id confirmation, repeatable)\n"
            "    --apply --id <doc-id>      (skip log scan, repeatable)\n"
            "    --apply --yes-to-all       (delete every squatter listed above)\n"
            "Pre-delete safety: each doc is fetched and its tags checked to\n"
            "ensure it carries 'ingested-by:influx' before deletion."
        )
        return 0

    # ── Apply path ──
    # ``--apply`` needs the project deps (LithosClient → mcp).  Re-exec
    # under ``uv run`` if we are not already in the project venv so the
    # operator can keep using the bare ``./scripts/influx-diagnose.py``
    # invocation without thinking about the runtime.
    _ensure_project_runtime_or_reexec()

    if via_id_flag:
        # Per issue #34: ``--apply --id <X>`` already names the doc to
        # delete; no additional ``--yes <X>`` is required.  The id list
        # is already the confirmed set.
        confirmed: set[str] = set(squatters.keys())
    else:
        confirmed = set(args.yes or [])
        if args.yes_to_all:
            confirmed.update(squatters.keys())
        if not confirmed:
            sys.exit(
                "--apply requires at least one --yes <doc-id> "
                "(or --yes-to-all, or --id <doc-id>).  Aborted."
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
    if already_gone and not via_id_flag:
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


# ── rewrite-legacy-notes (#176) ─────────────────────────────────────


from typing import NamedTuple as _NamedTuple_for_legacy  # noqa: E402


class ParsedLegacyFrontmatter(_NamedTuple_for_legacy):
    """Structured fields extracted from a pre-fix Influx note's embedded YAML.

    Pre-fix notes (created 2026-05-05 → 2026-05-07) carry an embedded
    ``---``-fenced YAML block at the top of ``content`` with the tag set,
    canonical source URL, and confidence value the renderer computed at
    write time.  Doc-level Lithos metadata for these notes is empty;
    the rewrite job recovers the values from here.

    NamedTuple is used instead of ``@dataclass`` to keep the type usable
    when the diagnose script is loaded via
    ``importlib.util.spec_from_file_location`` (used by unit tests),
    which doesn't register the module in ``sys.modules`` — the dataclass
    decorator's module-resolution lookup fails in that mode.
    """

    tags: list[str]
    source_url: str
    confidence: float


def parse_legacy_note_frontmatter(content: str) -> ParsedLegacyFrontmatter | None:
    """Parse the embedded YAML frontmatter block from a legacy note's content.

    Returns ``None`` when the content does not start with a valid ``---``
    frontmatter fence, when the YAML body is unparseable, or when the
    required fields (``tags`` as a non-empty list, ``source_url`` as a
    non-empty string) are missing.  ``confidence`` defaults to 1.0 when
    absent or unparseable — every pre-fix note the renderer produced
    carried it, but we are defensive.
    """
    import yaml

    from influx.canonical_note import NoteParseError, _split_frontmatter

    try:
        frontmatter_raw, _body = _split_frontmatter(content)
    except NoteParseError:
        return None
    try:
        data = yaml.safe_load(frontmatter_raw)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    tags_raw = data.get("tags")
    if not isinstance(tags_raw, list) or not tags_raw:
        return None
    tags = [str(t) for t in tags_raw]
    source_url = data.get("source_url")
    if not isinstance(source_url, str) or not source_url:
        return None
    confidence_raw = data.get("confidence", 1.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 1.0
    return ParsedLegacyFrontmatter(
        tags=tags,
        source_url=source_url,
        confidence=confidence,
    )


def strip_legacy_frontmatter(content: str) -> str:
    """Return body-only markdown after stripping the embedded frontmatter and title.

    The inner ``# Title`` line is removed too — Lithos's serialiser
    re-prepends ``# {doc.title}`` from doc metadata on every save, so
    leaving the inner title would produce a double-heading shape after
    the rewrite lands.  Section content (``## Archive``, ``## Summary``,
    ``## User Notes``, etc.) is preserved byte-for-byte.
    """
    from influx.canonical_note import _split_frontmatter, _split_title

    _frontmatter, after_fm = _split_frontmatter(content)
    _title, body = _split_title(after_fm)
    return body.lstrip("\n")


# Outcome buckets — also used as the per-doc status label on stdout
# and as the keys for the closing summary line.
_LEGACY_OUTCOME_PLANNED = "would_rewrite"
_LEGACY_OUTCOME_SKIPPED_ALREADY_FIXED = "skipped_already_fixed"
_LEGACY_OUTCOME_SKIPPED_UNPARSEABLE = "skipped_unparseable"
_LEGACY_OUTCOME_REFUSED_NON_INFLUX = "refused_non_influx_authored"
_LEGACY_OUTCOME_REWROTE = "rewrote"
# Lithos rejected the rewrite because the partner doc in a collision pair
# already owns the source_url.  The doc stays a zombie but is harmless —
# the partner is now the canonical source-of-truth for the URL and
# ``_classify_squatter`` will resolve future collisions via that doc.
_LEGACY_OUTCOME_REJECTED_PARTNER_OWNS_URL = "partner_owns_url"
_LEGACY_OUTCOME_FAILED = "failed"


def _resolve_corpus_articles_path(env: dict[str, str]) -> Path:
    """Resolve the on-disk path to the Lithos ``articles/`` subtree (#181).

    Resolution order mirrors ``scripts/influx-report.py``:

    1. ``$LITHOS_KNOWLEDGE_PATH`` — explicit override (lithos's
       knowledge-root path; we append ``articles``).
    2. ``$INFLUX_ARCHIVE_PATH`` — fall back to the staging convention
       ``<staging_root>/lithos/knowledge/articles`` derived from the
       archive path's parent.

    Exits with a clear message when neither is available, so the
    operator knows which env var to set.
    """
    explicit = env.get("LITHOS_KNOWLEDGE_PATH")
    if explicit:
        return Path(explicit) / "articles"

    archive_path = env.get("INFLUX_ARCHIVE_PATH")
    if not archive_path:
        sys.exit(
            "Cannot resolve Lithos articles path: set $LITHOS_KNOWLEDGE_PATH "
            "in the env file (or ensure $INFLUX_ARCHIVE_PATH is set for the "
            "staging-convention fallback)."
        )
    staging_root = Path(archive_path).parent
    return staging_root / "lithos" / "knowledge" / "articles"


def _select_legacy_doc_ids_from_corpus(articles_path: Path) -> list[str]:
    """Scan the on-disk corpus for Influx-authored docs with empty source_url.

    Walks ``articles_path`` recursively, parses each ``*.md`` file's
    outer YAML frontmatter, and returns the ids of docs where
    ``author == 'influx'`` AND ``source_url`` is missing/empty.  This
    is the broader selector for the ``rewrite-legacy-notes`` job — the
    backlog-derived selector only catches docs that have already
    collided, while this catches every recoverable doc on disk
    regardless of collision history (#181).

    The closing-fence parser delegates to
    :func:`influx.canonical_note._split_frontmatter`, which line-anchors the
    fence match.  A naive ``text.find("---", 4)`` would stop at the
    first ``---`` substring anywhere in the file, including inside a
    frontmatter VALUE like ``title: Foo --- Bar`` — and yaml.safe_load
    on the truncated payload would then fail, silently dropping a
    legitimate legacy doc from the candidate set.  The canonical
    splitter looks for ``\\n---`` followed by a CR/LF terminator so
    fences embedded in values are correctly ignored.

    Files that don't start with a ``---`` fence, parse failures, and
    non-influx-authored docs are silently skipped — the per-doc
    rewrite path's own author / parseable-frontmatter guards are the
    authoritative refusals.  This selector only short-circuits the
    obvious mismatches so an operator doesn't see noise in dry-run
    output for the bulk of healthy docs.
    """
    import yaml

    from influx.canonical_note import NoteParseError, _split_frontmatter

    ids: list[str] = []
    seen: set[str] = set()
    if not articles_path.exists():
        return ids
    for md in articles_path.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            frontmatter_raw, _rest = _split_frontmatter(text)
        except NoteParseError:
            continue
        try:
            meta = yaml.safe_load(frontmatter_raw)
        except yaml.YAMLError:
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get("author") != "influx":
            continue
        # Empty/None source_url is the broken-doc shape we care about.
        if meta.get("source_url"):
            continue
        doc_id = meta.get("id")
        if not isinstance(doc_id, str) or not doc_id:
            continue
        if doc_id in seen:
            continue
        seen.add(doc_id)
        ids.append(doc_id)
    return ids


def _select_legacy_doc_ids_from_backlog(state_dir: Path) -> list[str]:
    """Extract candidate doc ids from ``unresolved-slug-collisions.jsonl``.

    Each backlog ``detail`` field carries two doc ids in the form
    ``first_existing_id=<UUID>; ...; retry_existing_id=<UUID>; ...``.
    Both sides may carry the legacy shape (the first existed before
    today's run; the retry squatter is also a pre-fix doc when the
    suffixed slug was taken historically).  We collect both; the
    per-doc idempotency guard short-circuits already-fixed ones at
    read time.
    """
    entries = _read_slug_collision_backlog(state_dir)
    ids: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        detail = str(entry.get("detail", "") or "")
        for marker in ("first_existing_id=", "retry_existing_id="):
            idx = detail.find(marker)
            if idx < 0:
                continue
            tail = detail[idx + len(marker) :]
            end = tail.find(";")
            doc_id = (tail[:end] if end >= 0 else tail).strip()
            if doc_id and doc_id not in seen:
                seen.add(doc_id)
                ids.append(doc_id)
    return ids


async def _process_one_legacy_doc(
    *,
    client: Any,
    doc_id: str,
    apply: bool,
) -> tuple[str, str, dict[str, Any] | None]:
    """Read + classify one doc, and rewrite when ``apply=True``.

    Returns ``(outcome, reason, plan)`` where ``plan`` is a dict of the
    rewrite shape (parsed tags, source_url, confidence, content length
    delta) so the operator-facing line can surface it for both dry-run
    and apply modes.  ``plan`` is ``None`` for skip/refuse outcomes.
    """
    from influx.lithos_client import _doc_source_url

    try:
        doc = await client.read_note(note_id=doc_id)
    except BaseException as exc:  # noqa: BLE001
        return (
            _LEGACY_OUTCOME_FAILED,
            f"read failed: {_format_exception_chain(exc)}",
            None,
        )

    metadata = doc.get("metadata") or {}
    author = metadata.get("author") or doc.get("author") or ""
    if author != "influx":
        return (
            _LEGACY_OUTCOME_REFUSED_NON_INFLUX,
            f"doc.author={author!r} — refusing to touch a non-influx-authored doc",
            None,
        )

    # Idempotency: a doc is "fixed" once its doc-level source_url is populated.
    # Tags alone are NOT a sufficient signal — the repair sweep adds tags
    # like ``text:abstract-only`` / ``influx:source-invalid`` to broken docs
    # without ever recovering the source_url, and ``_classify_squatter``
    # matches incoming writes by source_url (Match #2), so a doc with tags
    # but no source_url is still effectively broken from the recovery
    # chain's perspective.
    if _doc_source_url(doc) is not None:
        return (
            _LEGACY_OUTCOME_SKIPPED_ALREADY_FIXED,
            "doc-level source_url already populated — nothing to do",
            None,
        )

    content = str(doc.get("content") or "")
    parsed = parse_legacy_note_frontmatter(content)
    if parsed is None:
        return (
            _LEGACY_OUTCOME_SKIPPED_UNPARSEABLE,
            "content has no embedded frontmatter or required fields missing",
            None,
        )

    new_content = strip_legacy_frontmatter(content)
    plan: dict[str, Any] = {
        "tags": parsed.tags,
        "source_url": parsed.source_url,
        "confidence": parsed.confidence,
        "old_content_len": len(content),
        "new_content_len": len(new_content),
    }

    if not apply:
        return _LEGACY_OUTCOME_PLANNED, "ready for rewrite", plan

    # UPDATE-by-id needs the ``id`` and ``expected_version`` args to reach
    # the MCP boundary; ``LithosClient.write_note`` doesn't accept those.
    # ``client.call_tool("lithos_write", args)`` is the canonical UPDATE
    # path — same pattern as ``influx.repair::_sweep_call_write``.
    write_args: dict[str, Any] = {
        "id": doc_id,
        "title": str(doc.get("title") or metadata.get("title") or ""),
        "content": new_content,
        "agent": "influx",
        "path": str(doc.get("path") or metadata.get("path") or ""),
        "source_url": parsed.source_url,
        "tags": parsed.tags,
        "confidence": parsed.confidence,
        "note_type": str(metadata.get("note_type") or "summary"),
        "namespace": str(metadata.get("namespace") or "influx"),
    }
    if metadata.get("version") is not None:
        write_args["expected_version"] = metadata["version"]

    try:
        result = await client.call_tool("lithos_write", write_args)
    except BaseException as exc:  # noqa: BLE001
        return (
            _LEGACY_OUTCOME_FAILED,
            f"write failed: {_format_exception_chain(exc)}",
            plan,
        )

    try:
        body = json.loads(result.content[0].text)
    except (AttributeError, IndexError, json.JSONDecodeError, TypeError) as exc:
        return (
            _LEGACY_OUTCOME_FAILED,
            f"could not decode lithos_write response: {exc!r}",
            plan,
        )

    status = body.get("status", "")
    if status in {"updated", "created"}:
        return _LEGACY_OUTCOME_REWROTE, f"write status={status}", plan
    if status == "duplicate":
        # The partner doc in a collision pair owns this source_url now —
        # it was rewritten earlier in this same run (or externally).  The
        # partner is canonical; this doc remains an empty-metadata zombie
        # but harmless: the slug-recovery chain matches via the partner,
        # which carries the source_url at the doc level.
        dup = body.get("duplicate_of") or {}
        return (
            _LEGACY_OUTCOME_REJECTED_PARTNER_OWNS_URL,
            f"partner doc owns source_url (partner_id={dup.get('id', '?')!r})",
            plan,
        )
    return (
        _LEGACY_OUTCOME_FAILED,
        f"unexpected write status: {status!r} (body={body!r})",
        plan,
    )


def cmd_rewrite_legacy_notes(args: argparse.Namespace) -> int:
    """Rewrite the ~245 pre-fix legacy notes that block slug-collision recovery.

    See agent-lore/influx#176.  Selects candidate doc ids from
    ``--id <doc-id>`` (repeatable, no extra confirmation needed) or
    from ``${INFLUX_STATE_PATH}/unresolved-slug-collisions.jsonl``
    (default selector — the backlog file maintained by the run ledger).
    Each candidate is read; legacy-shape docs are rewritten via
    ``lithos_write`` with the structured fields parsed from the
    embedded frontmatter as proper API parameters.  Already-fixed,
    unparseable, and non-influx-authored docs are skipped or refused.
    """
    env = _load_env(args.env)

    id_list = list(getattr(args, "id", None) or [])
    corpus_scan = bool(getattr(args, "corpus_scan", False))

    # ``--id`` and ``--corpus-scan`` are mutually exclusive at the
    # argparse layer, but the runtime sanity check is cheap and
    # documents the contract for anyone reading the body.
    if id_list and corpus_scan:
        sys.exit(
            "--id and --corpus-scan are mutually exclusive — pass one or "
            "the other, not both.  Aborted."
        )

    # Early validation: --apply without any confirmation flag is rejected
    # before we read the backlog file (so the test surface stays clean
    # and the failure mode is obvious to the operator).
    if args.apply and not id_list and not args.yes and not args.yes_to_all:
        sys.exit(
            "--apply requires at least one --yes <doc-id> "
            "(or --yes-to-all, or --id <doc-id>).  Aborted."
        )

    # ── Candidate selection ──
    if id_list:
        seen: set[str] = set()
        doc_ids: list[str] = []
        for doc_id in id_list:
            if doc_id not in seen:
                seen.add(doc_id)
                doc_ids.append(doc_id)
    elif corpus_scan:
        articles = _resolve_corpus_articles_path(env)
        doc_ids = _select_legacy_doc_ids_from_corpus(articles)
    else:
        state = _state_dir(env)
        doc_ids = _select_legacy_doc_ids_from_backlog(state)

    if not doc_ids:
        if corpus_scan:
            print(
                "No candidate doc ids — the corpus scan found no "
                "Influx-authored docs with empty source_url."
            )
        else:
            print(
                "No candidate doc ids — pass --id <doc-id> (repeatable), "
                "--corpus-scan, or ensure "
                "${INFLUX_STATE_PATH}/unresolved-slug-collisions.jsonl is populated."
            )
        return 0

    # ── Apply-mode subset narrowing ──
    if args.apply:
        if id_list:
            # --id names the targets explicitly; no extra --yes required.
            confirmed: set[str] = set(doc_ids)
        else:
            confirmed = set(args.yes or [])
            if args.yes_to_all:
                confirmed.update(doc_ids)
            unknown = confirmed - set(doc_ids)
            if unknown:
                print(
                    "Warning: --yes ids not present in the candidate set: "
                    + ", ".join(sorted(unknown))
                )
                confirmed -= unknown
            if not confirmed:
                sys.exit("No matching --yes ids; nothing to rewrite.")
        doc_ids = [d for d in doc_ids if d in confirmed]

    # ── Re-exec under uv if needed (lithos_client imports mcp) ──
    _ensure_project_runtime_or_reexec()
    lithos_url = _read_lithos_url(args, env)

    mode = "Apply" if args.apply else "Dry-run"
    suffix = "+lithos_write" if args.apply else " (no writes)"
    print(
        f"{mode} mode: processing {len(doc_ids)} doc id(s) via "
        f"lithos_read{suffix} on {lithos_url}\n"
    )

    import asyncio

    counts: dict[str, int] = {}

    async def _drive() -> None:
        client = _make_lithos_client(lithos_url)
        try:
            for doc_id in doc_ids:
                outcome, reason, plan = await _process_one_legacy_doc(
                    client=client,
                    doc_id=doc_id,
                    apply=args.apply,
                )
                counts[outcome] = counts.get(outcome, 0) + 1
                line = f"{outcome:>30}  doc_id={doc_id}  {reason}"
                if plan is not None:
                    line += (
                        f"  tags={len(plan['tags'])}"
                        f"  source_url={plan['source_url']}"
                        f"  confidence={plan['confidence']}"
                        f"  content_bytes={plan['old_content_len']}"
                        f"→{plan['new_content_len']}"
                    )
                print(line)
        finally:
            await client.close()

    asyncio.run(_drive())

    print()
    print("Summary: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not args.apply:
        print(
            "\nTo rewrite these docs, re-run with:\n"
            "    --apply --yes-to-all       (rewrite every 'would_rewrite' doc)\n"
            "    --apply --yes <doc-id>     (rewrite specific docs, repeatable)\n"
            "    --apply --id <doc-id>      (skip backlog scan, repeatable)"
        )
    # Exit non-zero only on genuine failures.
    return 0 if counts.get(_LEGACY_OUTCOME_FAILED, 0) == 0 else 1


# ── strip-embedded-frontmatter (#225) ───────────────────────────────
#
# Distinct from rewrite-legacy-notes (#176).  Those notes have EMPTY
# doc-level metadata and recover the values FROM the embedded block.
# These ~1,241 notes (created 2026-05-01 → 2026-05-24) have VALID
# doc-level metadata — the embedded block is redundant content bloat.
# The writer stopped emitting this shape after ~2026-05-24, so the
# population is fixed.  This job strips the block and leaves doc-level
# metadata untouched: a pure content rewrite via the Lithos write path.

_EMBEDDED_FM_OUTCOME_PLANNED = "would_strip"
_EMBEDDED_FM_OUTCOME_SKIPPED_ALREADY_CLEAN = "skipped_already_clean"
_EMBEDDED_FM_OUTCOME_REFUSED_NON_INFLUX = "refused_non_influx_authored"
_EMBEDDED_FM_OUTCOME_STRIPPED = "stripped"
_EMBEDDED_FM_OUTCOME_FAILED = "failed"


def strip_embedded_frontmatter_block(content: str) -> str | None:
    """Strip a leading embedded ``---`` frontmatter block from read_note content.

    Legacy notes (#225) carry — after Lithos strips the one ``# Title``
    it prepended on save — a content body that *starts* with a redundant
    ``---``-fenced YAML block duplicating the doc-level metadata,
    followed by the renderer's ``# Title`` heading and the ``## ``
    section body::

        ---
        note_type: summary
        source_url: ...
        tags: [...]
        ---
        # Title

        ## Archive
        ...

    Returns the content with that leading fenced block removed (leaving
    ``# Title\\n\\n## Archive ...`` — byte-identical to a current,
    post-05-24 note's read shape), or ``None`` when *content* does not
    start with a frontmatter fence (an already-clean / current-shape
    note, which the caller skips).

    The fence split delegates to :func:`influx.canonical_note._split_frontmatter`,
    which is line-anchored: a naive ``content.find("---")`` would match a
    ``---`` inside a YAML value.  The renderer title heading and every
    ``## `` section (``## Archive``, ``## Summary``, ``## User Notes``,
    …) are preserved byte-for-byte.
    """
    from influx.canonical_note import NoteParseError, _split_frontmatter

    try:
        _frontmatter, body = _split_frontmatter(content)
    except NoteParseError:
        return None
    return body.lstrip("\n")


def _select_embedded_frontmatter_doc_ids_from_corpus(
    articles_path: Path,
) -> list[str]:
    """Scan the on-disk corpus for Influx-authored docs with an embedded fm block.

    Walks ``articles_path`` recursively and returns the ids of
    ``author == 'influx'`` docs whose content body — after the single
    ``# Title`` Lithos prepends on save — begins with a redundant
    ``---``-fenced frontmatter block (the #225 legacy shape).  The
    per-doc processor re-validates authoritatively via ``read_note`` +
    :func:`strip_embedded_frontmatter_block`; this selector only narrows
    the candidate set so dry-run output isn't dominated by healthy docs.

    Both the outer (doc-level) fence and the inner ``# Title`` heading
    are split with the line-anchored helpers from :mod:`influx.notes`,
    so a ``---`` embedded in a frontmatter VALUE is never mistaken for a
    fence.  Files without an outer fence, parse failures, and
    non-influx-authored docs are silently skipped — the per-doc guards
    are the authoritative refusals.
    """
    import yaml

    from influx.canonical_note import NoteParseError, _split_frontmatter, _split_title

    ids: list[str] = []
    seen: set[str] = set()
    if not articles_path.exists():
        return ids
    for md in articles_path.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            frontmatter_raw, after_outer = _split_frontmatter(text)
        except NoteParseError:
            continue
        try:
            meta = yaml.safe_load(frontmatter_raw)
        except yaml.YAMLError:
            continue
        if not isinstance(meta, dict) or meta.get("author") != "influx":
            continue
        # Mirror Lithos's read path: it strips the one title it prepended
        # on save.  An embedded fm block is present iff the body then
        # begins with a fence.
        try:
            _title, body = _split_title(after_outer)
        except NoteParseError:
            continue
        if not body.lstrip("\n").startswith("---"):
            continue
        doc_id = meta.get("id")
        if not isinstance(doc_id, str) or not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        ids.append(doc_id)
    return ids


async def _process_one_embedded_frontmatter_doc(
    *,
    client: Any,
    doc_id: str,
    apply: bool,
) -> tuple[str, str, dict[str, Any] | None]:
    """Read one doc, strip its embedded fm block, and rewrite when ``apply=True``.

    Doc-level metadata (tags, source_url, confidence, title, path) is
    written back unchanged — only ``content`` is rewritten.  Returns
    ``(outcome, reason, plan)`` where ``plan`` carries the byte delta
    for the operator line and is ``None`` for skip/refuse outcomes.
    """
    try:
        doc = await client.read_note(note_id=doc_id)
    except BaseException as exc:  # noqa: BLE001
        return (
            _EMBEDDED_FM_OUTCOME_FAILED,
            f"read failed: {_format_exception_chain(exc)}",
            None,
        )

    metadata = doc.get("metadata") or {}
    author = metadata.get("author") or doc.get("author") or ""
    if author != "influx":
        return (
            _EMBEDDED_FM_OUTCOME_REFUSED_NON_INFLUX,
            f"doc.author={author!r} — refusing to touch a non-influx-authored doc",
            None,
        )

    content = str(doc.get("content") or "")
    new_content = strip_embedded_frontmatter_block(content)
    if new_content is None:
        return (
            _EMBEDDED_FM_OUTCOME_SKIPPED_ALREADY_CLEAN,
            "content has no embedded frontmatter block — already current shape",
            None,
        )

    plan: dict[str, Any] = {
        "old_content_len": len(content),
        "new_content_len": len(new_content),
    }
    if not apply:
        return (
            _EMBEDDED_FM_OUTCOME_PLANNED,
            "ready to strip embedded frontmatter",
            plan,
        )

    # Content-only rewrite: every doc-level field is read back from the
    # note envelope unchanged (mirrors influx.repair's proven write
    # contract — see _build_sweep_write_args).  Only ``content`` differs.
    conf_raw = doc.get("confidence")
    if conf_raw is None:
        conf_raw = metadata.get("confidence", 1.0)
    try:
        confidence = float(conf_raw)
    except (TypeError, ValueError):
        confidence = 1.0

    write_args: dict[str, Any] = {
        "id": doc_id,
        "title": str(doc.get("title") or metadata.get("title") or ""),
        "content": new_content,
        "agent": "influx",
        "path": str(doc.get("path") or metadata.get("path") or ""),
        "source_url": str(doc.get("source_url") or metadata.get("source_url") or ""),
        "tags": list(doc.get("tags") or metadata.get("tags") or []),
        "confidence": confidence,
        "note_type": str(
            doc.get("note_type") or metadata.get("note_type") or "summary"
        ),
        "namespace": str(doc.get("namespace") or metadata.get("namespace") or "influx"),
    }
    version = doc.get("version")
    if version is None:
        version = metadata.get("version")
    if version is not None:
        write_args["expected_version"] = version

    try:
        result = await client.call_tool("lithos_write", write_args)
    except BaseException as exc:  # noqa: BLE001
        return (
            _EMBEDDED_FM_OUTCOME_FAILED,
            f"write failed: {_format_exception_chain(exc)}",
            plan,
        )

    try:
        body = json.loads(result.content[0].text)
    except (AttributeError, IndexError, json.JSONDecodeError, TypeError) as exc:
        return (
            _EMBEDDED_FM_OUTCOME_FAILED,
            f"could not decode lithos_write response: {exc!r}",
            plan,
        )

    status = body.get("status", "")
    if status in {"updated", "created"}:
        return _EMBEDDED_FM_OUTCOME_STRIPPED, f"write status={status}", plan
    return (
        _EMBEDDED_FM_OUTCOME_FAILED,
        f"unexpected write status: {status!r} (body={body!r})",
        plan,
    )


def cmd_strip_embedded_frontmatter(args: argparse.Namespace) -> int:
    """Strip redundant embedded frontmatter from legacy note bodies (#225).

    Candidates come from ``--id <doc-id>`` (repeatable) or, by default,
    a scan of the on-disk Lithos ``articles/`` subtree for
    Influx-authored docs carrying an embedded frontmatter block.  Each
    candidate is read via ``lithos_read``; legacy-shape docs have the
    block stripped and the (unchanged) doc-level metadata written back
    via ``lithos_write``.  Already-clean and non-influx-authored docs
    are skipped or refused.  Dry-run by default; ``--apply`` requires an
    explicit confirmation flag.
    """
    env = _load_env(args.env)

    id_list = list(getattr(args, "id", None) or [])

    # Early validation: --apply without confirmation is rejected before
    # any corpus scan, so the failure mode is obvious to the operator.
    if args.apply and not id_list and not args.yes and not args.yes_to_all:
        sys.exit(
            "--apply requires at least one --yes <doc-id> "
            "(or --yes-to-all, or --id <doc-id>).  Aborted."
        )

    # ── Candidate selection ──
    if id_list:
        seen: set[str] = set()
        doc_ids: list[str] = []
        for doc_id in id_list:
            if doc_id not in seen:
                seen.add(doc_id)
                doc_ids.append(doc_id)
    else:
        articles = _resolve_corpus_articles_path(env)
        doc_ids = _select_embedded_frontmatter_doc_ids_from_corpus(articles)

    limit = getattr(args, "limit", None)
    if limit is not None and limit >= 0:
        doc_ids = doc_ids[:limit]

    if not doc_ids:
        print(
            "No candidate doc ids — the corpus scan found no Influx-authored "
            "docs with an embedded frontmatter block."
        )
        return 0

    # ── Apply-mode subset narrowing ──
    if args.apply:
        if id_list:
            # --id names the targets explicitly; no extra --yes required.
            confirmed: set[str] = set(doc_ids)
        else:
            confirmed = set(args.yes or [])
            if args.yes_to_all:
                confirmed.update(doc_ids)
            unknown = confirmed - set(doc_ids)
            if unknown:
                print(
                    "Warning: --yes ids not present in the candidate set: "
                    + ", ".join(sorted(unknown))
                )
                confirmed -= unknown
            if not confirmed:
                sys.exit("No matching --yes ids; nothing to strip.")
        doc_ids = [d for d in doc_ids if d in confirmed]

    # ── Re-exec under uv if needed (lithos_client imports mcp) ──
    _ensure_project_runtime_or_reexec()
    lithos_url = _read_lithos_url(args, env)

    mode = "Apply" if args.apply else "Dry-run"
    suffix = "+lithos_write" if args.apply else " (no writes)"
    print(
        f"{mode} mode: processing {len(doc_ids)} doc id(s) via "
        f"lithos_read{suffix} on {lithos_url}\n"
    )

    import asyncio

    counts: dict[str, int] = {}

    async def _drive() -> None:
        client = _make_lithos_client(lithos_url)
        try:
            for doc_id in doc_ids:
                outcome, reason, plan = await _process_one_embedded_frontmatter_doc(
                    client=client,
                    doc_id=doc_id,
                    apply=args.apply,
                )
                counts[outcome] = counts.get(outcome, 0) + 1
                line = f"{outcome:>30}  doc_id={doc_id}  {reason}"
                if plan is not None:
                    line += (
                        f"  content_bytes={plan['old_content_len']}"
                        f"→{plan['new_content_len']}"
                    )
                print(line)
        finally:
            await client.close()

    asyncio.run(_drive())

    print()
    print("Summary: " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not args.apply:
        print(
            "\nTo strip these docs, re-run with:\n"
            "    --apply --yes-to-all       (strip every 'would_strip' doc)\n"
            "    --apply --yes <doc-id>     (strip specific docs, repeatable)\n"
            "    --apply --id <doc-id>      (skip corpus scan, repeatable)"
        )
    # Exit non-zero only on genuine failures.
    return 0 if counts.get(_EMBEDDED_FM_OUTCOME_FAILED, 0) == 0 else 1


# ── feed-yield (operational: notes ingested per RSS feed) ────────────


def _resolve_config_path(env: dict[str, str]) -> Path:
    """Resolve the on-disk ``influx.toml`` path.

    ``${INFLUX_DATA_PATH}/influx.toml`` is the convention the env files
    already use for the ``[lithos] url`` lookup in ``_read_lithos_url``.
    """
    data_path = env.get("INFLUX_DATA_PATH")
    if not data_path:
        sys.exit(
            "Cannot resolve influx.toml: set $INFLUX_DATA_PATH in the env "
            "file (the directory containing influx.toml)."
        )
    path = Path(data_path) / "influx.toml"
    if not path.exists():
        sys.exit(f"influx.toml not found at {path}")
    return path


_FORUM_FEED_MARKERS = ("forum", "new topics", "latest forum")


def _is_forum_feed(name: str, url: str) -> bool:
    """Heuristic: does this feed publish discussion/forum threads?"""
    blob = f"{name} {url}".lower()
    return any(marker in blob for marker in _FORUM_FEED_MARKERS)


def _configured_rss_feeds(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map ``feed-slug`` → ``{profile, name, url, forum}`` for every RSS feed.

    The slug is derived from the feed NAME via the same
    :func:`influx.slugs.slugify_feed_name` the writer tags notes with
    (``feed-slug:<slug>``), so the result joins directly against the
    corpus scan.
    """
    from influx.slugs import slugify_feed_name

    feeds: dict[str, dict[str, Any]] = {}
    profiles = config.get("profiles")
    if not isinstance(profiles, list):
        return feeds
    for prof in profiles:
        if not isinstance(prof, dict):
            continue
        pname = str(prof.get("name", "?"))
        sources = prof.get("sources")
        rss = (sources or {}).get("rss") if isinstance(sources, dict) else None
        if not isinstance(rss, list):
            continue
        for entry in rss:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", ""))
            url = str(entry.get("url", ""))
            slug = slugify_feed_name(name)
            if not slug:
                continue
            feeds[slug] = {
                "profile": pname,
                "name": name,
                "url": url,
                "forum": _is_forum_feed(name, url),
            }
    return feeds


def _scan_feed_yield(
    articles_path: Path, *, since_iso: str | None = None
) -> dict[str, dict[str, Any]]:
    """Count notes per ``feed-slug:`` tag across the on-disk corpus.

    Returns ``slug → {count, recent, latest}`` where *count* is the
    all-time note total, *recent* is the count with ``created_at`` on or
    after *since_iso* (when provided), and *latest* is the most recent
    ``created_at`` date (``YYYY-MM-DD``).  Date comparison is on the
    ``YYYY-MM-DD`` prefix, which is lexicographically ordered.
    """
    import yaml

    from influx.canonical_note import NoteParseError, _split_frontmatter

    fs_re = re.compile(r"feed-slug:([a-z0-9-]+)")
    out: dict[str, dict[str, Any]] = {}
    if not articles_path.exists():
        return out
    for md in articles_path.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            fm_raw, _rest = _split_frontmatter(text)
        except NoteParseError:
            continue
        try:
            meta = yaml.safe_load(fm_raw)
        except yaml.YAMLError:
            continue
        if not isinstance(meta, dict):
            continue
        tags = meta.get("tags") or []
        if not isinstance(tags, list):
            continue
        created = str(meta.get("created_at", "") or "")[:10]
        for tag in tags:
            match = fs_re.fullmatch(str(tag))
            if not match:
                continue
            slug = match.group(1)
            rec = out.setdefault(slug, {"count": 0, "recent": 0, "latest": ""})
            rec["count"] += 1
            if created > rec["latest"]:
                rec["latest"] = created
            if since_iso is not None and created >= since_iso:
                rec["recent"] += 1
    return out


def cmd_feed_yield(args: argparse.Namespace) -> int:
    """Report notes-ingested-per-RSS-feed from the on-disk corpus.

    Joins the configured feed list (``influx.toml``) against per-feed
    note counts (the ``feed-slug:`` tag the writer stamps on every
    note), so an operator can see which feeds earn their place, which
    produce nothing (broken/dead), and which have gone silent recently.
    Pure read of on-disk notes + config — no Lithos / docker / HTTP.
    """
    import datetime as _dt
    import tomllib

    env = _load_env(args.env)
    cfg_path = _resolve_config_path(env)
    articles = _resolve_corpus_articles_path(env)

    with cfg_path.open("rb") as fh:
        config = tomllib.load(fh)

    feeds = _configured_rss_feeds(config)
    since_days = int(getattr(args, "since_days", 30) or 30)
    since_iso = (
        (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=since_days)).date().isoformat()
    )
    yields = _scan_feed_yield(articles, since_iso=since_iso)

    for slug, meta in feeds.items():
        y = yields.get(slug, {})
        meta["count"] = int(y.get("count", 0))
        meta["recent"] = int(y.get("recent", 0))
        meta["latest"] = str(y.get("latest", "") or "")

    rows = list(feeds.values())
    profile_filter = getattr(args, "profile", None)
    if profile_filter:
        rows = [r for r in rows if r["profile"] == profile_filter]

    sort_key = getattr(args, "sort", "yield")
    if sort_key == "recent":
        rows.sort(key=lambda r: (r["recent"], r["count"], r["profile"]))
    elif sort_key == "name":
        rows.sort(key=lambda r: (r["profile"], r["name"].lower()))
    else:  # "yield" — ascending so dead weight surfaces first
        rows.sort(key=lambda r: (r["count"], r["recent"], r["profile"]))

    orphans = {s: y for s, y in yields.items() if s not in feeds}

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "since_days": since_days,
                    "feeds": rows,
                    "orphans": [
                        {"slug": s, **y}
                        for s, y in sorted(
                            orphans.items(), key=lambda x: -x[1]["count"]
                        )
                    ],
                },
                indent=2,
            )
        )
        return 0

    print(
        f"Feed yield (notes per RSS feed) — corpus={articles}  "
        f"recent window={since_days}d\n"
    )
    print(f"{'tot':>5} {'recent':>6} {'latest':>10} {'F':1} {'profile':16} feed")
    print("-" * 86)
    for r in rows:
        print(
            f"{r['count']:>5} {r['recent']:>6} "
            f"{(r['latest'] or '  never'):>10} "
            f"{'F' if r['forum'] else ' '} {r['profile']:16} {r['name'][:36]}"
        )

    zero = [r for r in rows if r["count"] == 0]
    silent = [r for r in rows if r["count"] > 0 and r["recent"] == 0]
    print()
    print(
        f"Summary: {len(rows)} feeds | zero-yield (never produced): "
        f"{len(zero)} | silent (no notes in {since_days}d): {len(silent)}"
    )
    if orphans:
        print("\nfeed-slugs in corpus NOT in current config (removed/renamed feeds):")
        for s, y in sorted(orphans.items(), key=lambda x: -x[1]["count"]):
            print(f"  {y['count']:>5}  latest {y['latest']}  {s}")
    return 0


async def _fetch_invalid_source_notes(
    *,
    lithos_url: str,
    note_ids: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch notes carrying ``influx:source-invalid`` (issue #162).

    When *note_ids* is provided, fetches those specific notes via
    ``lithos_read`` and skips the tag-based list scan.  Otherwise
    lists every note tagged ``influx:source-invalid`` (across all
    profiles — this is operator cleanup, not a per-profile sweep)
    and reads each one to obtain the full body for inference.
    """
    client = _make_lithos_client(lithos_url)
    try:
        if note_ids:
            ids = list(note_ids)
        else:
            body = await client.list_notes_body(
                tags=["influx:source-invalid"],
                limit=limit,
            )
            items = body.get("items", [])
            ids = [str(item.get("id", "")) for item in items if item.get("id")]

        notes: list[dict[str, Any]] = []
        for note_id in ids:
            try:
                doc = await client.read_note(note_id=note_id)
            except (KeyboardInterrupt, SystemExit):
                # Operator-initiated cancellation must propagate so
                # the script can stop cleanly instead of looping over
                # remaining ids.
                raise
            except BaseException as exc:  # noqa: BLE001
                chain = _format_exception_chain(exc)
                print(
                    f"warning: read failed for {note_id}: {chain} — skipping",
                    file=sys.stderr,
                )
                continue
            notes.append(doc)
        return notes
    finally:
        await client.close()


async def _apply_invalid_source_action(
    *,
    lithos_url: str,
    note: dict[str, Any],
    new_tags: list[str],
    agent: str,
) -> tuple[str, str]:
    """Rewrite *note* with *new_tags* via ``lithos_write`` (issue #162).

    Returns ``(outcome, reason)`` where ``outcome`` is one of
    ``"applied"`` (rewrite succeeded), ``"already_clean"`` (the tag set
    was already in the target shape — no rewrite needed), or
    ``"refused"`` (write failed; ``reason`` carries the diagnostic).

    Mirrors the per-id confirmation shape of :func:`_delete_squatter`
    so the operator workflow is consistent across cleanup subcommands.

    Safety boundary (PR #170 review)
    --------------------------------
    Refuses any note whose existing tag list does NOT contain
    ``influx:source-invalid``.  The ``--id`` targeting shortcut
    bypasses the tag-filtered ``lithos_list`` scan, so a healthy note
    could otherwise be fetched, classified, and rewritten — for an
    operator cleanup tool the blast radius is too large.  Refusing at
    the write boundary makes the eligibility check unmissable from
    every caller path.
    """
    existing_tags = [t for t in (note.get("tags") or []) if isinstance(t, str)]
    if "influx:source-invalid" not in existing_tags:
        return (
            "refused",
            "note is not tagged influx:source-invalid; apply refuses to rewrite "
            "outside the invalid-source-metadata cleanup scope",
        )
    if sorted(existing_tags) == sorted(new_tags):
        return "already_clean", "no tag change required"

    client = _make_lithos_client(lithos_url)
    try:
        try:
            result = await client.write_note(
                title=str(note.get("title", "")),
                content=str(note.get("content", "")),
                agent=agent,
                path=str(note.get("path", "")),
                source_url=str(note.get("source_url", "")),
                tags=list(new_tags),
                confidence=float(note.get("confidence") or 0.0),
                note_type=str(note.get("note_type") or "summary"),
                namespace=str(note.get("namespace") or "influx"),
            )
        except (KeyboardInterrupt, SystemExit):
            # Operator cancellation must abort the apply pass cleanly
            # rather than being demoted to a "refused" result.
            raise
        except BaseException as exc:  # noqa: BLE001
            return "refused", f"write failed: {_format_exception_chain(exc)}"
        status = getattr(result, "status", "") or getattr(result, "outcome", "")
        if str(status) in {"created", "updated"}:
            return "applied", str(status)
        # Surface anything else (slug_collision survivor, invalid_input, …)
        # so the operator can decide how to proceed.
        return "refused", f"unexpected write status: {status!r} ({result!r})"
    finally:
        await client.close()


def cmd_invalid_source(args: argparse.Namespace) -> int:
    """Audit and clean up notes tagged ``influx:source-invalid`` (issue #162).

    Default mode (no ``--apply``): scans Lithos for notes carrying the
    invalid-source-metadata terminal tag and prints a per-note action
    recommendation.  Read-only — no writes.

    ``--apply`` enables the rewrite path:

    * Recoverable notes (URL / path / id hints support inference) are
      *reconstructed*: the ``source:*`` tag is backfilled, in-band
      terminal flags are dropped, and ``influx:repair-needed`` is
      re-armed so the next sweep retries text extraction cleanly.
    * Unrecoverable notes are *tombstoned*: an ``influx:tombstone``
      tag is added on top of the existing terminal state so the note
      is filterable as "operator-cleaned".

    ``--yes <doc-id>`` (repeatable) or ``--yes-to-all`` are required
    alongside ``--apply``, mirroring the ``squatters`` subcommand's
    confirmation discipline.
    """
    env = _load_env(args.env)
    _ensure_project_runtime_or_reexec()
    lithos_url = _read_lithos_url(args, env)

    import asyncio

    from influx.audit_invalid_source import (
        audit_notes,
        format_audit_report,
        reconstruct_tags,
        tombstone_tags,
    )

    notes = asyncio.run(
        _fetch_invalid_source_notes(
            lithos_url=lithos_url,
            note_ids=list(args.id or []),
            limit=args.limit,
        )
    )
    findings = audit_notes(notes)

    # PR #170 review: when ``--id`` is used, the helper bypasses the
    # tag-filtered ``lithos_list`` scan.  Surface any fetched note that
    # does not actually carry ``influx:source-invalid`` so the operator
    # sees the ineligibility before reading the (possibly misleading)
    # RECONSTRUCT / TOMBSTONE recommendation — and so it's clear why
    # ``--apply`` will refuse below.
    ineligible_ids = [
        f.note_id for f in findings if "influx:source-invalid" not in f.tags
    ]
    if ineligible_ids:
        print(
            "WARNING: the following notes do not carry "
            "'influx:source-invalid' and are INELIGIBLE for this workflow.  "
            "--apply will refuse them.  Suspected cause: an --id targeting a "
            "note that was never in the invalid-source-metadata terminal "
            "state, or was already operator-cleaned.\n"
        )
        for nid in ineligible_ids:
            print(f"  INELIGIBLE  {nid}")
        print()

    print(format_audit_report(findings))

    if not args.apply:
        print(
            "Default mode is read-only.  To apply the recommended action, "
            "re-run with:\n"
            "    --apply --yes <doc-id>      (per-id confirmation, repeatable)\n"
            "    --apply --id <doc-id>       (skip list scan; targets one note)\n"
            "    --apply --yes-to-all        (apply to every note listed above)"
        )
        return 0

    # ── Apply path ─────────────────────────────────────────────────
    if args.id:
        confirmed = {str(i) for i in args.id}
    else:
        confirmed = set(args.yes or [])
        if args.yes_to_all:
            confirmed.update(f.note_id for f in findings)
        if not confirmed:
            sys.exit(
                "--apply requires at least one --yes <doc-id>, --yes-to-all, "
                "or --id <doc-id>.  Aborted."
            )

    finding_by_id = {f.note_id: f for f in findings}
    note_by_id = {str(n.get("id", "")): n for n in notes}

    unknown = confirmed - finding_by_id.keys()
    if unknown:
        print(
            "Warning: --yes ids not present in audit results: "
            + ", ".join(sorted(unknown))
        )
        confirmed -= unknown
    if not confirmed:
        sys.exit("No matching ids; nothing to apply.")

    applied = 0
    already_clean = 0
    refused = 0
    for note_id in sorted(confirmed):
        finding = finding_by_id[note_id]
        note = note_by_id[note_id]
        if finding.recoverable:
            assert finding.inferred_source is not None  # narrowed
            new_tags = reconstruct_tags(note, inferred_source=finding.inferred_source)
            action_label = "RECONSTRUCT"
        else:
            new_tags = tombstone_tags(note)
            action_label = "TOMBSTONE"

        outcome, reason = asyncio.run(
            _apply_invalid_source_action(
                lithos_url=lithos_url,
                note=note,
                new_tags=new_tags,
                agent=args.agent,
            )
        )
        if outcome == "applied":
            print(f"{action_label}  doc_id={note_id}  ({reason})")
            applied += 1
        elif outcome == "already_clean":
            print(f"SKIPPED       doc_id={note_id}  ({reason})")
            already_clean += 1
        else:
            print(f"REFUSED       doc_id={note_id}  reason={reason}")
            refused += 1

    print()
    print(f"Summary: applied={applied} already_clean={already_clean} refused={refused}")
    return 0 if refused == 0 else 1


def cmd_promotion_gate(args: argparse.Namespace) -> int:
    """Evaluate the promotion gate against recent ledger entries (issue #165).

    Pure read of ``runs.jsonl`` plus the pure :mod:`influx.promotion_gate`
    classifier — no Lithos connection, no docker logs, no admin HTTP.
    Exits ``0`` on PASS, ``1`` on FAIL so the subcommand can drive a
    staging-promotion CI step directly.
    """
    _ensure_project_runtime_or_reexec()
    from influx.promotion_gate import (
        PromotionGateConfig,
        evaluate_promotion_gate,
        format_gate_summary,
    )

    env = _load_env(args.env)
    state = _state_dir(env)
    runs = _read_runs_jsonl(state)
    # ``_read_runs_jsonl`` returns oldest-first; the gate's window
    # semantics need newest-first.
    runs.reverse()

    config = PromotionGateConfig(
        window_runs=int(args.window),
        max_expected_lossy_ratio=float(args.max_lossy_ratio),
        min_runs_required=int(args.min_runs),
    )
    result = evaluate_promotion_gate(runs, config)
    print(format_gate_summary(result, config))
    return 0 if result.passed else 1


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
            "Must be combined with --yes <doc-id>, --yes-to-all, or --id."
        ),
    )
    p_squatters.add_argument(
        "--yes",
        action="append",
        metavar="DOC_ID",
        help=(
            "confirm deletion of a specific squatter (repeatable). "
            "Required with --apply unless --yes-to-all or --id is set."
        ),
    )
    p_squatters.add_argument(
        "--id",
        action="append",
        metavar="DOC_ID",
        help=(
            "act on a known squatter doc-id directly, bypassing the docker "
            "log scan (repeatable; no --since/--tail required).  Without "
            "--apply, fetches the doc via lithos_read and reports a preview.  "
            "With --apply, deletes after the existing safety check.  "
            "--id <X> is treated as both 'list this id' and 'confirm "
            "deletion of X'; no additional --yes <X> is required."
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

    p_rewrite = sub.add_parser(
        "rewrite-legacy-notes",
        help=(
            "rewrite legacy Influx notes (pre-fix residue) whose doc-level "
            "tags/source_url/confidence are empty but whose content carries "
            "an embedded YAML frontmatter block (issue #176).  Default mode "
            "is read-only; --apply rewrites via lithos_write."
        ),
    )
    p_rewrite.add_argument(
        "--apply",
        action="store_true",
        help=(
            "actually rewrite the docs (default: dry-run / read-only).  "
            "Must be combined with --yes <doc-id>, --yes-to-all, or --id."
        ),
    )
    p_rewrite.add_argument(
        "--yes",
        action="append",
        metavar="DOC_ID",
        help=(
            "confirm rewrite of a specific doc (repeatable).  Required with "
            "--apply unless --yes-to-all or --id is set."
        ),
    )
    p_rewrite.add_argument(
        "--yes-to-all",
        action="store_true",
        help=(
            "rewrite every candidate listed in the read-only plan.  "
            "Mutually exclusive with --yes."
        ),
    )
    p_rewrite.add_argument(
        "--id",
        action="append",
        metavar="DOC_ID",
        help=(
            "act on a known doc-id directly, bypassing the "
            "unresolved-slug-collisions.jsonl scan (repeatable).  Without "
            "--apply, fetches each doc and reports a planned rewrite line.  "
            "With --apply, rewrites without an extra --yes."
        ),
    )
    p_rewrite.add_argument(
        "--corpus-scan",
        action="store_true",
        dest="corpus_scan",
        help=(
            "select candidate docs by walking the on-disk Lithos articles "
            "subtree (every Influx-authored doc with empty doc-level "
            "source_url), instead of reading the slug-collision backlog "
            "file.  Catches recoverable docs that haven't collided yet "
            "(issue #181).  Mutually exclusive with --id."
        ),
    )
    p_rewrite.add_argument(
        "--lithos-url",
        help=(
            "override LITHOS_URL from the env file.  Useful when targeting a "
            "non-default lithos instance."
        ),
    )
    p_rewrite.set_defaults(func=cmd_rewrite_legacy_notes)

    p_strip = sub.add_parser(
        "strip-embedded-frontmatter",
        help=(
            "strip the redundant embedded YAML frontmatter block from legacy "
            "Influx note bodies whose doc-level metadata is already valid "
            "(issue #225).  Distinct from rewrite-legacy-notes, which recovers "
            "EMPTY doc-level metadata.  Default mode is read-only / audit; "
            "--apply rewrites content via lithos_write."
        ),
    )
    p_strip.add_argument(
        "--apply",
        action="store_true",
        help=(
            "actually strip the embedded block (default: dry-run / audit).  "
            "Must be combined with --yes <doc-id>, --yes-to-all, or --id."
        ),
    )
    p_strip.add_argument(
        "--yes",
        action="append",
        metavar="DOC_ID",
        help=(
            "confirm strip of a specific doc (repeatable).  Required with "
            "--apply unless --yes-to-all or --id is set."
        ),
    )
    p_strip.add_argument(
        "--yes-to-all",
        action="store_true",
        help="strip every candidate listed in the read-only plan.",
    )
    p_strip.add_argument(
        "--id",
        action="append",
        metavar="DOC_ID",
        help=(
            "act on a known doc-id directly, bypassing the corpus scan "
            "(repeatable).  Without --apply, reports a planned strip line; "
            "with --apply, strips without an extra --yes."
        ),
    )
    p_strip.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the number of candidate docs processed (staged rollout).",
    )
    p_strip.add_argument(
        "--lithos-url",
        help="override LITHOS_URL from the env file.",
    )
    p_strip.set_defaults(func=cmd_strip_embedded_frontmatter)

    p_yield = sub.add_parser(
        "feed-yield",
        help=(
            "notes-ingested-per-RSS-feed from the on-disk corpus — joins "
            "influx.toml feeds against the feed-slug: note tags to surface "
            "dead/broken feeds (zero-yield), gone-silent feeds, and the "
            "real top producers.  Read-only."
        ),
    )
    p_yield.add_argument("--profile", help="filter to a single profile")
    p_yield.add_argument(
        "--since-days",
        type=int,
        default=30,
        dest="since_days",
        help="recent-window size in days for the 'recent' column (default: 30)",
    )
    p_yield.add_argument(
        "--sort",
        choices=["yield", "recent", "name"],
        default="yield",
        help="sort order (default: yield — ascending, dead weight first)",
    )
    p_yield.add_argument(
        "--json", action="store_true", help="emit JSON instead of a table"
    )
    p_yield.set_defaults(func=cmd_feed_yield)

    p_invalid = sub.add_parser(
        "invalid-source",
        help=(
            "audit / clean up notes tagged 'influx:source-invalid' "
            "(persisted notes with empty or garbled source metadata, "
            "issue #162).  Default is read-only; --apply enables rewrite."
        ),
    )
    p_invalid.add_argument(
        "--id",
        action="append",
        metavar="DOC_ID",
        help=(
            "act on a known note-id directly, bypassing the lithos_list scan "
            "(repeatable).  Without --apply, fetches each note and reports "
            "the recommended action.  With --apply, applies that action "
            "without an extra --yes."
        ),
    )
    p_invalid.add_argument(
        "--limit",
        type=int,
        help=(
            "cap the number of notes returned by the lithos_list scan (default: no cap)"
        ),
    )
    p_invalid.add_argument(
        "--apply",
        action="store_true",
        help=(
            "actually apply the recommended action (reconstruct for "
            "recoverable notes, tombstone for unrecoverable ones).  "
            "Default is read-only audit.  Must be combined with --yes "
            "<doc-id>, --yes-to-all, or --id."
        ),
    )
    p_invalid.add_argument(
        "--yes",
        action="append",
        metavar="DOC_ID",
        help=(
            "with --apply, confirm a specific doc-id for rewrite "
            "(repeatable).  Required unless --yes-to-all or --id is used."
        ),
    )
    p_invalid.add_argument(
        "--yes-to-all",
        action="store_true",
        help=(
            "with --apply, apply the recommended action to every note "
            "listed in the audit.  Use after reviewing the read-only output."
        ),
    )
    p_invalid.add_argument(
        "--agent",
        default="influx-diagnose",
        help=(
            "agent name passed to lithos_write for the audit trail "
            "(default: influx-diagnose)"
        ),
    )
    p_invalid.add_argument(
        "--lithos-url",
        help=(
            "override the Lithos MCP/SSE URL.  Default resolution: "
            "$LITHOS_URL, then [lithos] url in $INFLUX_DATA_PATH/influx.toml."
        ),
    )
    p_invalid.set_defaults(func=cmd_invalid_source)

    p_gate = sub.add_parser(
        "promotion-gate",
        help=(
            "evaluate recent runs against a configurable severity policy "
            "(issue #165); exits 0 on PASS, 1 on FAIL"
        ),
    )
    p_gate.add_argument(
        "--window",
        type=int,
        default=20,
        help=("number of recent scheduled completed runs to evaluate (default: 20)"),
    )
    p_gate.add_argument(
        "--max-lossy-ratio",
        type=float,
        default=0.5,
        help=(
            "fail the gate when the fraction of expected_lossy runs in "
            "the window is strictly greater than this value.  A ratio "
            "exactly equal to the threshold passes (default: 0.5)."
        ),
    )
    p_gate.add_argument(
        "--min-runs",
        type=int,
        default=5,
        help=(
            "minimum scheduled completed runs required in the window to "
            "evaluate at all (default: 5).  Below this the gate reports "
            "'insufficient_runs' rather than a spurious pass / fail."
        ),
    )
    p_gate.set_defaults(func=cmd_promotion_gate)

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
