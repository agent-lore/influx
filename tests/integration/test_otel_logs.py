"""End-to-end OTEL log emission test (issue #28).

Mirrors :mod:`tests.integration.test_otel_metrics` but for logs:
configures the logger provider with an in-memory log exporter and emits
representative ``logger.info / warning`` calls, then asserts the
documented attribute keys (``run_id``, ``profile``, ``source_url``,
``status``, ``detail`` …) and resource attributes (``service.name``,
``deployment.environment``) are preserved on the OTEL log records.

A complementary test confirms zero ``LoggingHandler`` instances are
attached to the root logger when OTEL is disabled (AC-10-A discipline
extended to logs).
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip(
    "opentelemetry.sdk._logs",
    reason="OTEL logs SDK required for log integration tests",
)


def _clear_influx_handlers() -> None:
    """Strip any prior Influx-installed handlers between cases."""
    root = logging.getLogger()
    root.handlers = [
        handler
        for handler in root.handlers
        if not getattr(handler, "_influx_json_handler", False)
        and not getattr(handler, "_influx_otel_log_handler", False)
    ]


class TestOtelLogsEnabled:
    """OTEL enabled: log records carry both extras and resource attrs."""

    def setup_method(self) -> None:
        _clear_influx_handlers()

    def teardown_method(self) -> None:
        _clear_influx_handlers()

    def test_log_record_carries_extras_and_resource_attrs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``logger.info("...", extra={...})`` call produces an OTEL
        log record carrying the supplied extras plus ``service.name`` /
        ``deployment.environment`` from the resource."""
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            InMemoryLogRecordExporter,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.resources import Resource

        from influx.logging_config import setup_logging

        monkeypatch.setenv("INFLUX_OTEL_ENABLED", "true")
        monkeypatch.setenv("INFLUX_ENVIRONMENT", "staging")

        # Build an explicit in-memory provider and inject it so we don't
        # depend on the production OTLP exporter being reachable.
        exporter = InMemoryLogRecordExporter()
        provider = LoggerProvider(
            resource=Resource.create(
                {"service.name": "influx", "deployment.environment": "staging"}
            )
        )
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

        # Patch _build_logger_provider so setup_logging picks up our test
        # provider rather than constructing one from env (which would try
        # to wire OTLP HTTP).
        from influx import logging_config

        monkeypatch.setattr(
            logging_config,
            "_build_logger_provider",
            lambda: provider,
        )

        setup_logging()

        logger = logging.getLogger("influx.test")
        logger.info(
            "article write skipped",
            extra={
                "run_id": "run-xyz",
                "profile": "ai-robotics",
                "source_url": "https://example.com/post",
                "status": "duplicate",
                "detail": "already ingested",
            },
        )

        provider.force_flush()

        records = exporter.get_finished_logs()
        assert len(records) >= 1, "Expected at least one OTEL log record"

        # Find the record matching our message
        matching = [
            r for r in records if "article write skipped" in str(r.log_record.body)
        ]
        assert matching, (
            f"Expected a record with body 'article write skipped', got: "
            f"{[str(r.log_record.body) for r in records]}"
        )
        record = matching[-1]

        attrs = dict(record.log_record.attributes or {})
        assert attrs.get("run_id") == "run-xyz"
        assert attrs.get("profile") == "ai-robotics"
        assert attrs.get("source_url") == "https://example.com/post"
        assert attrs.get("status") == "duplicate"
        assert attrs.get("detail") == "already ingested"

        # Resource attributes (service.name, deployment.environment) live
        # on the record's resource, not its per-record attributes.
        resource_attrs = dict(record.resource.attributes or {})
        assert resource_attrs.get("service.name") == "influx"
        assert resource_attrs.get("deployment.environment") == "staging"

    def test_logging_handler_attached_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With OTEL enabled, a ``LoggingHandler`` is attached to the root."""
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import (
            InMemoryLogRecordExporter,
            SimpleLogRecordProcessor,
        )

        from influx import logging_config
        from influx.logging_config import setup_logging

        monkeypatch.setenv("INFLUX_OTEL_ENABLED", "true")

        exporter = InMemoryLogRecordExporter()
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

        monkeypatch.setattr(
            logging_config,
            "_build_logger_provider",
            lambda: provider,
        )

        setup_logging()

        root = logging.getLogger()
        otel_handlers = [h for h in root.handlers if isinstance(h, LoggingHandler)]
        assert len(otel_handlers) >= 1, (
            f"Expected at least one OTEL LoggingHandler on root, got: "
            f"{[type(h).__name__ for h in root.handlers]}"
        )

    def test_stderr_json_handler_still_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enabling OTEL must NOT remove the stderr JSON handler — both run."""
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            InMemoryLogRecordExporter,
            SimpleLogRecordProcessor,
        )

        from influx import logging_config
        from influx.logging_config import setup_logging

        monkeypatch.setenv("INFLUX_OTEL_ENABLED", "true")

        exporter = InMemoryLogRecordExporter()
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

        monkeypatch.setattr(
            logging_config,
            "_build_logger_provider",
            lambda: provider,
        )

        setup_logging()

        root = logging.getLogger()
        stderr_handlers = [
            h for h in root.handlers if getattr(h, "_influx_json_handler", False)
        ]
        assert len(stderr_handlers) == 1, (
            "Stderr JSON handler must remain installed when OTEL is enabled."
        )


class TestOtelLogsDisabled:
    """OTEL disabled → no ``LoggingHandler`` installed (AC-10-A)."""

    def setup_method(self) -> None:
        _clear_influx_handlers()

    def teardown_method(self) -> None:
        _clear_influx_handlers()

    def test_no_otel_handler_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With OTEL disabled, the root logger has no ``LoggingHandler``."""
        from opentelemetry.sdk._logs import LoggingHandler

        from influx.logging_config import setup_logging

        monkeypatch.delenv("INFLUX_OTEL_ENABLED", raising=False)

        setup_logging()

        root = logging.getLogger()
        otel_handlers = [h for h in root.handlers if isinstance(h, LoggingHandler)]
        assert otel_handlers == [], (
            f"Expected zero OTEL LoggingHandler instances when disabled, got: "
            f"{[type(h).__name__ for h in otel_handlers]}"
        )

    def test_no_otel_handler_when_explicitly_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``INFLUX_OTEL_ENABLED=false`` must not install any OTEL handler."""
        from opentelemetry.sdk._logs import LoggingHandler

        from influx.logging_config import setup_logging

        monkeypatch.setenv("INFLUX_OTEL_ENABLED", "false")

        setup_logging()

        root = logging.getLogger()
        otel_handlers = [h for h in root.handlers if isinstance(h, LoggingHandler)]
        assert otel_handlers == []
