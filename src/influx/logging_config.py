"""Structured logging configuration for Influx.

Influx follows the same operational logging shape as Lithos: JSON logs
by default with ``timestamp``, ``level``, ``logger``, and ``message``
fields, plus any caller-provided ``extra`` fields.  Set
``INFLUX_LOG_FORMAT=text`` for local plain-text logs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

__all__ = ["InfluxJsonFormatter", "setup_logging"]

_HANDLER_MARKER = "_influx_json_handler"
_STANDARD_LOG_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class InfluxJsonFormatter(logging.Formatter):
    """Single-line JSON formatter compatible with Lithos log fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="seconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


def setup_logging(level: int = logging.INFO, stream: Any = None) -> None:
    """Install structured logging on the root logger.

    Repeated calls are idempotent unless the previously installed stream
    was closed, matching the Lithos closed-stream recovery behavior used
    by CLI tests.
    """
    root = logging.getLogger()

    survivors: list[logging.Handler] = []
    replace = False
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            handler_stream = getattr(handler, "stream", None)
            if handler_stream is not None and getattr(handler_stream, "closed", False):
                replace = True
                continue
        survivors.append(handler)
    root.handlers = survivors

    if not replace:
        for handler in root.handlers:
            if getattr(handler, _HANDLER_MARKER, False):
                root.setLevel(level)
                return

    if stream is None:
        stream = sys.stderr

    handler = logging.StreamHandler(stream)
    setattr(handler, _HANDLER_MARKER, True)

    log_format = os.environ.get("INFLUX_LOG_FORMAT", "json").lower()
    if log_format == "text":
        formatter: logging.Formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    else:
        formatter = InfluxJsonFormatter()
    handler.setFormatter(formatter)

    root.setLevel(level)
    root.addHandler(handler)
