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


def _cmd_serve_stub() -> None:
    """Stub handler for serve; replaced by PRD 03."""
    print("[stub] serve not wired yet", file=sys.stderr)
    sys.exit(EXIT_USAGE)


def _cmd_run_stub(args: argparse.Namespace) -> None:
    """Stub handler for run; replaced by a later PRD."""
    print(
        f"[stub] run not wired yet (profile={args.profile})",
        file=sys.stderr,
    )
    sys.exit(EXIT_USAGE)


def _cmd_backfill_stub(args: argparse.Namespace) -> None:
    """Stub handler for backfill; replaced by a later PRD."""
    print(
        f"[stub] backfill not wired yet (profile={args.profile})",
        file=sys.stderr,
    )
    sys.exit(EXIT_USAGE)


def main(argv: list[str] | None = None) -> None:
    """CLI dispatcher.

    Parameters
    ----------
    argv:
        Argument list for testing; defaults to ``sys.argv[1:]``.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        sys.exit(EXIT_USAGE)

    try:
        if args.command == "validate-config":
            _cmd_validate_config()
        elif args.command == "serve":
            _cmd_serve_stub()
        elif args.command == "run":
            _cmd_run_stub(args)
        elif args.command == "backfill":
            _cmd_backfill_stub(args)
    except InfluxError as exc:
        print(f"influx: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILURE)
