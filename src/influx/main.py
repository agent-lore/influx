"""Entry point stub.

Loads configuration via :func:`influx.config.load_config` and prints
a summary.  This module will be replaced by an argparse dispatcher
in US-009.
"""

from __future__ import annotations

import sys

from influx.config import load_config
from influx.errors import InfluxError


def main() -> None:
    """Load config and print a status line.

    Exits with code 1 and a message on stderr when config cannot be
    loaded, so shell callers see a non-zero status.
    """
    try:
        config = load_config()
    except InfluxError as exc:
        print(f"influx: {exc}", file=sys.stderr)
        sys.exit(1)

    n_profiles = len(config.profiles)
    print(f"Influx v0.7 config OK — {n_profiles} profile(s) loaded")
