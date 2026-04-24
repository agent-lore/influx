"""CLI entry point with argparse dispatcher.

Provides the ``validate-config`` subcommand that loads and validates
``influx.toml``, prints the effective config, and exits 0 on success
or non-zero on error.

Running ``python -m influx`` with no subcommand prints help and exits
with a non-zero status.
"""

from __future__ import annotations

import argparse
import sys

from influx.config import load_config
from influx.errors import InfluxError


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

    return parser


def _cmd_validate_config() -> None:
    """Load config, print the effective config model, and exit 0."""
    config = load_config()
    print(config.model_dump_json(indent=2))


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
        sys.exit(2)

    try:
        if args.command == "validate-config":
            _cmd_validate_config()
    except InfluxError as exc:
        print(f"influx: {exc}", file=sys.stderr)
        sys.exit(1)
