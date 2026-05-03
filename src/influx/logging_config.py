"""Structured logging configuration for Influx.

Influx follows the same operational logging shape as Lithos: JSON logs
by default with ``timestamp``, ``level``, ``logger``, and ``message``
fields, plus any caller-provided ``extra`` fields.  Set
``INFLUX_LOG_FORMAT=text`` for local plain-text logs.

When ``INFLUX_OTEL_ENABLED=true`` the same records are *also* forwarded
to an OTEL ``LoggingHandler`` attached to the root logger, so the
existing ``extra={...}`` fields (``run_id``, ``profile``, ``source_url``,
``status``, ``detail``, …) become OTEL log attributes and ride alongside
spans + metrics in the configured backend (issue #28).  The stderr JSON
handler keeps running unchanged so ``docker logs`` and
``scripts/influx-diagnose.py`` are unaffected.

The OTEL path is strictly opt-in: when the toggle is unset / false, no
OTEL imports run and no ``LoggingHandler`` is constructed (AC-10-A).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from influx.telemetry import _build_logger_provider

__all__ = ["InfluxJsonFormatter", "setup_logging"]

_HANDLER_MARKER = "_influx_json_handler"
_OTEL_HANDLER_MARKER = "_influx_otel_log_handler"
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

    When ``INFLUX_OTEL_ENABLED=true`` an OTEL ``LoggingHandler`` is
    *additionally* attached to the root logger so the same records are
    exported to the OTEL backend.  When OTEL is disabled, no OTEL
    objects are constructed and no OTEL handler is installed
    (AC-10-A discipline).
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

    json_handler_present = any(
        getattr(handler, _HANDLER_MARKER, False) for handler in root.handlers
    )
    if not replace and json_handler_present:
        # Stderr handler is already installed.  Attach the OTEL handler
        # if missing (so a subsequent ``setup_logging`` call after env
        # changes can still wire OTEL), then bail.
        _maybe_attach_otel_log_handler(root, level)
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

    _maybe_attach_otel_log_handler(root, level)


def _maybe_attach_otel_log_handler(root: logging.Logger, level: int) -> None:
    """Attach an OTEL ``LoggingHandler`` when (and only when) OTEL is enabled.

    Strict AC-10-A discipline: the disabled path performs only a single
    boolean check (``INFLUX_OTEL_ENABLED`` via :func:`_build_logger_provider`)
    and never imports the OTEL SDK or constructs a handler object.
    """
    # Skip if an OTEL handler is already present (idempotent re-entry).
    for handler in root.handlers:
        if getattr(handler, _OTEL_HANDLER_MARKER, False):
            return

    provider = _build_logger_provider()
    if provider is None:
        # OTEL disabled or SDK unavailable — no handler attached
        # (AC-10-A: no OTEL objects on the disabled path).
        return

    # Lazy import: this line only runs when OTEL is enabled and the
    # SDK is installed (``_build_logger_provider`` would have returned
    # ``None`` otherwise), so the disabled path never imports this.
    from opentelemetry.sdk._logs import LoggingHandler

    otel_handler = LoggingHandler(level=level, logger_provider=provider)
    setattr(otel_handler, _OTEL_HANDLER_MARKER, True)
    root.addHandler(otel_handler)
