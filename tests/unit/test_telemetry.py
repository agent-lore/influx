"""Unit tests for the OTEL telemetry wrapper (US-001).

Covers:
  (1) OTEL is off by default — no-op when INFLUX_OTEL_ENABLED unset (FR-OBS-2)
  (2) No-op when INFLUX_OTEL_ENABLED=false (FR-OBS-2)
  (3) Enabled when INFLUX_OTEL_ENABLED=true and OTEL packages installed
  (4) No-op when OTEL packages simulated absent via import-stub (AC-M4-3)
  (5) No-op paths never raise
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from unittest.mock import MagicMock

import pytest

from influx.telemetry import InfluxTracer, SpanWrapper, get_tracer

# ── Helpers ────────────────────────────────────────────────────────────


def _rebuild_tracer(monkeypatch: pytest.MonkeyPatch, env_value: str | None) -> None:
    """Set or clear INFLUX_OTEL_ENABLED and force a tracer rebuild."""
    if env_value is None:
        monkeypatch.delenv("INFLUX_OTEL_ENABLED", raising=False)
    else:
        monkeypatch.setenv("INFLUX_OTEL_ENABLED", env_value)


# ── (1) OTEL off by default (FR-OBS-2) ────────────────────────────────


class TestOtelOffByDefault:
    """FR-OBS-2: wrapper is a no-op when INFLUX_OTEL_ENABLED is not set."""

    def test_tracer_disabled_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild_tracer(monkeypatch, None)
        tracer = get_tracer(force_rebuild=True)
        assert not tracer.enabled

    def test_span_is_noop_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild_tracer(monkeypatch, None)
        tracer = get_tracer(force_rebuild=True)
        with tracer.span("influx.run") as s:
            # Should silently accept attributes without error
            s.set_attribute("influx.profile", "test")
            s.set_attributes({"influx.run_id": "abc"})


# ── (2) No-op when INFLUX_OTEL_ENABLED=false (FR-OBS-2) ───────────────


class TestOtelExplicitlyDisabled:
    """FR-OBS-2: wrapper is a no-op when INFLUX_OTEL_ENABLED=false."""

    def test_tracer_disabled_when_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild_tracer(monkeypatch, "false")
        tracer = get_tracer(force_rebuild=True)
        assert not tracer.enabled

    def test_tracer_disabled_when_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild_tracer(monkeypatch, "0")
        tracer = get_tracer(force_rebuild=True)
        assert not tracer.enabled

    def test_span_is_noop_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild_tracer(monkeypatch, "false")
        tracer = get_tracer(force_rebuild=True)
        with tracer.span("influx.filter", attributes={"influx.profile": "p"}) as s:
            s.set_attribute("influx.item_count", 10)


# ── (3) Enabled when INFLUX_OTEL_ENABLED=true + packages installed ─────


class TestOtelEnabled:
    """Wrapper creates real spans when OTEL is enabled and packages are present."""

    def test_tracer_enabled_when_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild_tracer(monkeypatch, "true")
        tracer = get_tracer(force_rebuild=True)
        assert tracer.enabled

    def test_tracer_enabled_when_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild_tracer(monkeypatch, "1")
        tracer = get_tracer(force_rebuild=True)
        assert tracer.enabled

    def test_enabled_span_creates_real_span(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With OTEL enabled, the wrapper creates a real OTEL span."""
        _rebuild_tracer(monkeypatch, "true")

        from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
        from opentelemetry.sdk.trace.export import (
            SimpleSpanProcessor,
            SpanExporter,
            SpanExportResult,
        )

        # Simple collecting exporter for tests
        collected: list[ReadableSpan] = []

        class _CollectingExporter(SpanExporter):
            def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
                collected.extend(spans)
                return SpanExportResult.SUCCESS

            def shutdown(self) -> None:
                pass

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_CollectingExporter()))

        test_tracer = InfluxTracer(
            enabled=True,
            tracer=provider.get_tracer("influx-test"),
        )

        with test_tracer.span("influx.run", attributes={"influx.profile": "ai"}) as s:
            s.set_attribute("influx.run_id", "test-123")

        assert len(collected) == 1
        assert collected[0].name == "influx.run"
        assert collected[0].attributes is not None
        assert collected[0].attributes.get("influx.profile") == "ai"
        assert collected[0].attributes.get("influx.run_id") == "test-123"


# ── (4) No-op when OTEL packages absent via import-stub (AC-M4-3) ──────


class TestOtelPackagesAbsent:
    """AC-M4-3: wrapper remains a no-op when OTEL packages are simulated absent."""

    def test_noop_when_packages_absent_and_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with INFLUX_OTEL_ENABLED=true, no-op when packages missing."""
        _rebuild_tracer(monkeypatch, "true")

        # Simulate missing packages by stubbing imports
        otel_modules = [
            k for k in sys.modules if k.startswith("opentelemetry")
        ]
        saved = {k: sys.modules[k] for k in otel_modules}

        try:
            for k in otel_modules:
                monkeypatch.delitem(sys.modules, k, raising=False)

            # Make imports fail
            import builtins

            _real_import = builtins.__import__

            def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
                if name.startswith("opentelemetry"):
                    raise ImportError(f"Simulated missing: {name}")
                return _real_import(name, *args, **kwargs)

            monkeypatch.setattr(builtins, "__import__", _blocked_import)

            tracer = get_tracer(force_rebuild=True)
            assert not tracer.enabled

            # Span creation is a no-op
            with tracer.span("influx.run") as s:
                s.set_attribute("influx.profile", "test")
        finally:
            # Restore real modules
            for k, v in saved.items():
                sys.modules[k] = v

    def test_noop_when_packages_absent_and_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With OTEL disabled and packages absent, still a no-op."""
        _rebuild_tracer(monkeypatch, "false")

        otel_modules = [
            k for k in sys.modules if k.startswith("opentelemetry")
        ]
        saved = {k: sys.modules[k] for k in otel_modules}

        try:
            for k in otel_modules:
                monkeypatch.delitem(sys.modules, k, raising=False)

            import builtins

            _real_import = builtins.__import__

            def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
                if name.startswith("opentelemetry"):
                    raise ImportError(f"Simulated missing: {name}")
                return _real_import(name, *args, **kwargs)

            monkeypatch.setattr(builtins, "__import__", _blocked_import)

            tracer = get_tracer(force_rebuild=True)
            assert not tracer.enabled

            with tracer.span("influx.filter") as s:
                s.set_attribute("influx.item_count", 5)
        finally:
            for k, v in saved.items():
                sys.modules[k] = v


# ── (5) No-op paths never raise ────────────────────────────────────────


class TestNoOpNeverRaises:
    """Wrapper does not raise in any no-op path with valid wrapper calls."""

    def test_noop_span_with_all_attribute_types(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild_tracer(monkeypatch, None)
        tracer = get_tracer(force_rebuild=True)

        with tracer.span("influx.run", attributes={"a": 1, "b": "x"}) as s:
            s.set_attribute("int_attr", 42)
            s.set_attribute("str_attr", "value")
            s.set_attribute("float_attr", 3.14)
            s.set_attribute("bool_attr", True)
            s.set_attributes({"multi_a": 1, "multi_b": "two"})

    def test_noop_nested_spans(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild_tracer(monkeypatch, None)
        tracer = get_tracer(force_rebuild=True)

        with tracer.span("influx.run") as outer:
            outer.set_attribute("influx.profile", "p")
            with tracer.span("influx.filter") as inner:
                inner.set_attribute("influx.item_count", 10)

    def test_noop_span_wrapper_from_mock(self) -> None:
        """SpanWrapper wraps any object that has set_attribute."""
        mock_span = MagicMock()
        wrapper = SpanWrapper(mock_span)
        wrapper.set_attribute("key", "val")
        mock_span.set_attribute.assert_called_once_with("key", "val")
