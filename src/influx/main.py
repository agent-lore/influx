"""CLI entry point with argparse dispatcher.

Provides subcommands for the v1 CLI surface: ``validate-config``,
``serve``, ``run``, ``backfill``, and ``migrate-notes``.

Running ``python -m influx`` with no subcommand prints help and exits
with a non-zero status.
"""

from __future__ import annotations

import argparse
import sys

from influx.config import load_config
from influx.errors import InfluxError

# FR-CLI-7 exit-code policy
EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILURE = 2
EXIT_USAGE = 64


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="influx",
        description="Influx — research-feed ingestion toolkit.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "validate-config",
        help="Load influx.toml, validate, and print the effective config.",
    )

    # FR-CLI-6: migrate-notes
    sub.add_parser(
        "migrate-notes",
        help="Print the current note schema version and exit.",
    )

    # FR-CLI-2: serve takes no flags
    sub.add_parser(
        "serve",
        help="Start the Influx HTTP API server.",
    )

    # FR-CLI-3: run --profile
    run_parser = sub.add_parser(
        "run",
        help="Run a single ingestion cycle for a profile.",
    )
    run_parser.add_argument(
        "--profile",
        required=True,
        help="Profile name to run ingestion for.",
    )

    # FR-CLI-4: backfill --profile --days/--from/--to [--confirm]
    backfill_parser = sub.add_parser(
        "backfill",
        help="Backfill historical data for a profile.",
    )
    backfill_parser.add_argument(
        "--profile",
        required=True,
        help="Profile name to backfill.",
    )
    backfill_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of days to backfill.",
    )
    backfill_parser.add_argument(
        "--from",
        dest="from_date",
        default=None,
        help="Start date for backfill range (ISO format).",
    )
    backfill_parser.add_argument(
        "--to",
        dest="to_date",
        default=None,
        help="End date for backfill range (ISO format).",
    )
    backfill_parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Confirm large backfill jobs.",
    )

    return parser


def _cmd_validate_config() -> None:
    """Load config, print the effective config model, and exit 0."""
    config = load_config()
    print(config.model_dump_json(indent=2))


def _cmd_migrate_notes() -> None:
    """Print the current note_schema_version from config and exit 0."""
    config = load_config()
    print(f"note_schema_version: {config.influx.note_schema_version}")


def _cmd_serve() -> None:
    """Start the Influx HTTP API server under uvicorn.

    Loads config (with ``check_api_keys=False`` since probes handle
    credential checks), validates the bind address, creates the
    :class:`~influx.service.InfluxService` with lifespan wiring,
    and runs uvicorn.  Blocks until SIGINT/SIGTERM, then performs a
    clean shutdown bounded by ``schedule.shutdown_grace_seconds``.

    Replaces the PRD 02 stub (§5.4, AC-03-E).
    """
    import uvicorn

    from influx.service import (
        InfluxService,
        resolve_bind_address,
        validate_bind_host,
    )

    config = load_config(check_api_keys=False)
    host, port = resolve_bind_address()
    validate_bind_host(
        host, allow_remote_admin=config.security.allow_remote_admin
    )

    service = InfluxService(config, with_lifespan=True)

    uvicorn.run(
        service.app,
        host=host,
        port=port,
        timeout_graceful_shutdown=config.schedule.shutdown_grace_seconds,
        log_level="info",
    )


def _admin_base_url() -> str:
    """Return the base URL for the running admin service.

    Reads ``INFLUX_ADMIN_PORT`` (default ``8080``) and targets
    ``127.0.0.1`` on loopback (§5.4 of PRD 03).
    """
    import os

    port = os.environ.get("INFLUX_ADMIN_PORT", "8080")
    return f"http://127.0.0.1:{port}"


def _cmd_run(args: argparse.Namespace) -> None:
    """POST to ``/runs`` on the running service and print the request_id.

    Exit codes (§5.4 of PRD 03):
        0 — accepted (``202``)
        1 — profile busy (``409``)
        2 — network error
    """
    import httpx

    url = f"{_admin_base_url()}/runs"
    payload: dict[str, object] = {"profile": args.profile}

    try:
        resp = httpx.post(url, json=payload, timeout=10.0)
    except httpx.ConnectError:
        print(
            f"influx: could not connect to service at {url}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)
    except httpx.HTTPError as exc:
        print(f"influx: network error: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILURE)

    if resp.status_code == 202:
        body = resp.json()
        print(body["request_id"])
        sys.exit(EXIT_SUCCESS)
    elif resp.status_code == 409:
        body = resp.json()
        print(
            f"influx: profile {body.get('profile', args.profile)!r} is busy",
            file=sys.stderr,
        )
        sys.exit(EXIT_PARTIAL)
    else:
        print(
            f"influx: unexpected response {resp.status_code}: {resp.text}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)


def _cmd_backfill(args: argparse.Namespace) -> None:
    """POST to ``/backfills`` on the running service.

    Handles the ``confirm_required`` reprompt flow: when the server
    returns ``400`` with ``reason="confirm_required"`` and ``--confirm``
    was NOT passed, prints the estimate and exits ``64`` (usage error).
    When ``--confirm`` IS passed and a ``confirm_required`` response
    arrives, re-POSTs with ``confirm: true`` added to the body.

    Exit codes (§5.4 of PRD 03):
        0  — accepted (``202``)
        1  — profile busy (``409``)
        2  — network error
        64 — confirm_required without ``--confirm``
    """
    import httpx

    url = f"{_admin_base_url()}/backfills"
    payload: dict[str, object] = {"profile": args.profile}

    if args.days is not None:
        payload["days"] = args.days
    if args.from_date is not None:
        payload["from"] = args.from_date
    if args.to_date is not None:
        payload["to"] = args.to_date
    if args.confirm:
        payload["confirm"] = True

    try:
        resp = httpx.post(url, json=payload, timeout=10.0)
    except httpx.ConnectError:
        print(
            f"influx: could not connect to service at {url}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)
    except httpx.HTTPError as exc:
        print(f"influx: network error: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILURE)

    if resp.status_code == 202:
        body = resp.json()
        print(body["request_id"])
        sys.exit(EXIT_SUCCESS)
    elif resp.status_code == 400:
        body = resp.json()
        if body.get("reason") == "confirm_required":
            estimated = body.get("estimated_items", "unknown")
            if args.confirm:
                # Re-POST with confirm=true added.
                payload["confirm"] = True
                try:
                    resp2 = httpx.post(url, json=payload, timeout=10.0)
                except httpx.HTTPError as exc:
                    print(
                        f"influx: network error on retry: {exc}",
                        file=sys.stderr,
                    )
                    sys.exit(EXIT_FAILURE)
                if resp2.status_code == 202:
                    body2 = resp2.json()
                    print(body2["request_id"])
                    sys.exit(EXIT_SUCCESS)
                else:
                    print(
                        f"influx: unexpected response on retry "
                        f"{resp2.status_code}: {resp2.text}",
                        file=sys.stderr,
                    )
                    sys.exit(EXIT_FAILURE)
            else:
                print(
                    f"Estimated {estimated} items. "
                    f"Re-run with --confirm to proceed.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_USAGE)
        else:
            print(
                f"influx: bad request: {resp.text}",
                file=sys.stderr,
            )
            sys.exit(EXIT_FAILURE)
    elif resp.status_code == 409:
        body = resp.json()
        print(
            f"influx: profile {body.get('profile', args.profile)!r} is busy",
            file=sys.stderr,
        )
        sys.exit(EXIT_PARTIAL)
    else:
        print(
            f"influx: unexpected response {resp.status_code}: {resp.text}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)


_KNOWN_COMMANDS = frozenset({
    "validate-config",
    "migrate-notes",
    "serve",
    "run",
    "backfill",
})


def main(argv: list[str] | None = None) -> None:
    """CLI dispatcher.

    Parameters
    ----------
    argv:
        Argument list for testing; defaults to ``sys.argv[1:]``.
    """
    parser = _build_parser()

    # Intercept unknown subcommands before argparse (which exits 2).
    effective_argv = argv if argv is not None else sys.argv[1:]
    if (
        effective_argv
        and not effective_argv[0].startswith("-")
        and effective_argv[0] not in _KNOWN_COMMANDS
    ):
        print(
            f"influx: unknown command {effective_argv[0]!r}",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        sys.exit(EXIT_USAGE)

    try:
        if args.command == "validate-config":
            _cmd_validate_config()
        elif args.command == "migrate-notes":
            _cmd_migrate_notes()
        elif args.command == "serve":
            _cmd_serve()
        elif args.command == "run":
            _cmd_run(args)
        elif args.command == "backfill":
            _cmd_backfill(args)
    except InfluxError as exc:
        print(f"influx: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILURE)
