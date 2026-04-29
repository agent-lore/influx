"""Tests for Influx structured logging."""

from __future__ import annotations

import io
import json
import logging
import re

import pytest

from influx.logging_config import InfluxJsonFormatter, setup_logging


def _clear_influx_handlers() -> None:
    root = logging.getLogger()
    root.handlers = [
        handler
        for handler in root.handlers
        if not getattr(handler, "_influx_json_handler", False)
    ]


def _last_record(stream: io.StringIO) -> dict[str, object]:
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert lines
    return json.loads(lines[-1])  # type: ignore[no-any-return]


class TestSetupLogging:
    def setup_method(self) -> None:
        _clear_influx_handlers()

    def teardown_method(self) -> None:
        _clear_influx_handlers()

    def test_outputs_lithos_style_json_fields(self) -> None:
        stream = io.StringIO()
        setup_logging(stream=stream)

        logging.getLogger("influx.test").warning("hello")

        record = _last_record(stream)
        assert record["level"] == "WARNING"
        assert record["logger"] == "influx.test"
        assert record["message"] == "hello"
        assert isinstance(record["timestamp"], str)
        assert re.match(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}",
            str(record["timestamp"]),
        )

    def test_extra_fields_are_preserved(self) -> None:
        stream = io.StringIO()
        setup_logging(stream=stream)

        logging.getLogger("influx.test").info(
            "with request",
            extra={"request_id": "req-1", "otelTraceID": "abcd"},
        )

        record = _last_record(stream)
        assert record["request_id"] == "req-1"
        assert record["otelTraceID"] == "abcd"

    def test_text_format_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INFLUX_LOG_FORMAT", "text")
        stream = io.StringIO()
        setup_logging(stream=stream)

        logging.getLogger("influx.test").info("plain")

        line = stream.getvalue().splitlines()[0]
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return
        raise AssertionError("Expected plain text log output")

    def test_exception_info_is_serialized(self) -> None:
        stream = io.StringIO()
        setup_logging(stream=stream)
        logger = logging.getLogger("influx.test")

        try:
            raise ValueError("bad value")
        except ValueError:
            logger.error("failed", exc_info=True)

        record = _last_record(stream)
        assert "ValueError" in str(record["exception"])
        assert "bad value" in str(record["exception"])


def test_formatter_outputs_single_json_line() -> None:
    formatter = InfluxJsonFormatter()
    record = logging.LogRecord(
        name="influx.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    line = formatter.format(record)

    assert "\n" not in line
    assert json.loads(line)["message"] == "hello world"
