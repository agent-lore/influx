"""Hello-world entry point.

Loads configuration via :func:`influx.config.load_config` and prints a
greeting that shows the active environment.
"""

from __future__ import annotations

import sys

from influx.config import load_config
from influx.errors import InfluxError


def main() -> None:
    """Print ``{greeting} from Influx ({environment})``.

    Exits with code 1 and a message on stderr when config cannot be
    loaded, so shell callers see a non-zero status.
    """
    try:
        config = load_config()
    except InfluxError as exc:
        print(f"influx: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"{config.greeting} from Influx ({config.environment})")
