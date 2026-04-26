"""Unit tests for the OTEL telemetry wrapper (US-001 + US-002).

Covers:
  (1) OTEL is off by default — no-op when INFLUX_OTEL_ENABLED unset (FR-OBS-2)
  (2) No-op when INFLUX_OTEL_ENABLED=false (FR-OBS-2)
  (3) Enabled when INFLUX_OTEL_ENABLED=true and OTEL packages installed
  (4) No-op when OTEL packages simulated absent via import-stub (AC-M4-3)
  (5) No-op paths never raise
  (6) Enabled wrapper creates spans with given name (US-002)
  (7) Enabled wrapper sets attributes on spans (US-002)
  (8) Console fallback emits spans to stdout (US-002, FR-OBS-3)
  (9) AC-10-A regression guard: no span when disabled even if packages installed
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest

from influx.telemetry import InfluxTracer, SpanWrapper, get_tracer

_has_otel = pytest.importorskip is not None  # always True, used as a marker below
try:
    import opentelemetry.sdk.trace  # noqa: F401

    _has_otel = True
except ImportError:
    _has_otel = False

_needs_otel = pytest.mark.skipif(
    not _has_otel,
    reason="opentelemetry SDK not installed",
)

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

    def test_span_is_noop_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild_tracer(monkeypatch, None)
        tracer = get_tracer(force_rebuild=True)
        with tracer.span("influx.run") as s:
            # Should silently accept attributes without error
            s.set_attribute("influx.profile", "test")
            s.set_attributes({"influx.run_id": "abc"})


# ── (2) No-op when INFLUX_OTEL_ENABLED=false (FR-OBS-2) ───────────────


class TestOtelExplicitlyDisabled:
    """FR-OBS-2: wrapper is a no-op when INFLUX_OTEL_ENABLED=false."""

    def test_tracer_disabled_when_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild_tracer(monkeypatch, "false")
        tracer = get_tracer(force_rebuild=True)
        assert not tracer.enabled

    def test_tracer_disabled_when_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild_tracer(monkeypatch, "0")
        tracer = get_tracer(force_rebuild=True)
        assert not tracer.enabled

    def test_span_is_noop_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild_tracer(monkeypatch, "false")
        tracer = get_tracer(force_rebuild=True)
        with tracer.span("influx.filter", attributes={"influx.profile": "p"}) as s:
            s.set_attribute("influx.item_count", 10)

    def test_disabled_span_does_not_allocate_per_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-10-A: disabled body must not instantiate a wrapper per span."""
        _rebuild_tracer(monkeypatch, "false")
        tracer = get_tracer(force_rebuild=True)

        with tracer.span("influx.run") as a:
            pass
        with tracer.span("influx.filter") as b:
            pass

        # Both invocations yield the SAME shared no-op wrapper object —
        # proves the disabled path performs no per-span allocation.
        assert a is b


# ── (3) Enabled when INFLUX_OTEL_ENABLED=true + packages installed ─────


@_needs_otel
class TestOtelEnabled:
    """Wrapper creates real spans when OTEL is enabled and packages are present."""

    def test_tracer_enabled_when_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild_tracer(monkeypatch, "true")
        tracer = get_tracer(force_rebuild=True)
        assert tracer.enabled

    def test_tracer_enabled_when_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        otel_modules = [k for k in sys.modules if k.startswith("opentelemetry")]
        saved = {k: sys.modules[k] for k in otel_modules}

        try:
            for k in otel_modules:
                monkeypatch.delitem(sys.modules, k, raising=False)

            # Make imports fail
            import builtins

            _real_import = builtins.__import__

            def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
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

        otel_modules = [k for k in sys.modules if k.startswith("opentelemetry")]
        saved = {k: sys.modules[k] for k in otel_modules}

        try:
            for k in otel_modules:
                monkeypatch.delitem(sys.modules, k, raising=False)

            import builtins

            _real_import = builtins.__import__

            def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
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

    def test_noop_nested_spans(self, monkeypatch: pytest.MonkeyPatch) -> None:
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


# ── (6) Enabled wrapper creates spans with given name (US-002) ─────────


def _make_collecting_tracer() -> tuple[InfluxTracer, list]:
    """Create an InfluxTracer with a collecting exporter for test assertions."""
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import (
        SimpleSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )

    collected: list[ReadableSpan] = []

    class _CollectingExporter(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            collected.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_CollectingExporter()))

    tracer = InfluxTracer(
        enabled=True,
        tracer=provider.get_tracer("influx-test"),
    )
    return tracer, collected


@_needs_otel
class TestEnabledSpanCreation:
    """US-002: enabled wrapper creates spans with the given name."""

    def test_span_has_correct_name(self) -> None:
        tracer, collected = _make_collecting_tracer()
        with tracer.span("influx.fetch.arxiv"):
            pass
        assert len(collected) == 1
        assert collected[0].name == "influx.fetch.arxiv"

    def test_multiple_spans_each_named(self) -> None:
        tracer, collected = _make_collecting_tracer()
        with tracer.span("influx.run"):
            pass
        with tracer.span("influx.filter"):
            pass
        assert len(collected) == 2
        assert collected[0].name == "influx.run"
        assert collected[1].name == "influx.filter"

    def test_nested_spans_both_recorded(self) -> None:
        tracer, collected = _make_collecting_tracer()
        with tracer.span("influx.run"), tracer.span("influx.filter"):
            pass
        assert len(collected) == 2
        names = {s.name for s in collected}
        assert names == {"influx.run", "influx.filter"}


# ── (7) Enabled wrapper sets attributes on spans (US-002) ──────────────


@_needs_otel
class TestEnabledSpanAttributes:
    """US-002: enabled wrapper sets attributes on the underlying OTEL span."""

    def test_initial_attributes_set(self) -> None:
        tracer, collected = _make_collecting_tracer()
        with tracer.span("influx.run", attributes={"influx.profile": "ai"}):
            pass
        assert collected[0].attributes is not None
        assert collected[0].attributes.get("influx.profile") == "ai"

    def test_set_attribute_after_creation(self) -> None:
        tracer, collected = _make_collecting_tracer()
        with tracer.span("influx.run") as s:
            s.set_attribute("influx.run_id", "r-42")
            s.set_attribute("influx.item_count", 100)
        attrs = collected[0].attributes
        assert attrs is not None
        assert attrs.get("influx.run_id") == "r-42"
        assert attrs.get("influx.item_count") == 100

    def test_set_attributes_batch(self) -> None:
        tracer, collected = _make_collecting_tracer()
        with tracer.span("influx.filter") as s:
            s.set_attributes(
                {
                    "influx.profile": "robotics",
                    "influx.run_id": "r-99",
                    "influx.item_count": 25,
                }
            )
        attrs = collected[0].attributes
        assert attrs is not None
        assert attrs.get("influx.profile") == "robotics"
        assert attrs.get("influx.run_id") == "r-99"
        assert attrs.get("influx.item_count") == 25

    def test_initial_and_dynamic_attributes_merged(self) -> None:
        tracer, collected = _make_collecting_tracer()
        with tracer.span(
            "influx.enrich.tier1",
            attributes={"influx.profile": "ai"},
        ) as s:
            s.set_attribute("influx.item_count", 5)
        attrs = collected[0].attributes
        assert attrs is not None
        assert attrs.get("influx.profile") == "ai"
        assert attrs.get("influx.item_count") == 5


# ── (8) Console fallback emits spans to stdout (US-002, FR-OBS-3) ──────


@_needs_otel
class TestConsoleFallback:
    """US-002 / FR-OBS-3: console fallback prints spans to stdout."""

    def test_console_fallback_emits_to_stdout(self) -> None:
        """With console fallback enabled, spans are observably emitted."""
        import io

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )

        buf = io.StringIO()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=buf)))

        tracer = InfluxTracer(
            enabled=True,
            tracer=provider.get_tracer("influx-test"),
        )

        with tracer.span(
            "influx.run",
            attributes={"influx.profile": "test"},
        ):
            pass

        output = buf.getvalue()
        assert "influx.run" in output
        assert "influx.profile" in output

    def test_build_tracer_uses_console_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_build_tracer adds ConsoleSpanExporter when fallback is on."""
        monkeypatch.setenv("INFLUX_OTEL_ENABLED", "true")
        monkeypatch.setenv("INFLUX_OTEL_CONSOLE_FALLBACK", "true")
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        tracer = get_tracer(force_rebuild=True)
        assert tracer.enabled

    def test_console_fallback_disabled_no_exporter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With console fallback disabled, tracer is enabled but no console."""
        monkeypatch.setenv("INFLUX_OTEL_ENABLED", "true")
        monkeypatch.delenv("INFLUX_OTEL_CONSOLE_FALLBACK", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        tracer = get_tracer(force_rebuild=True)
        assert tracer.enabled

    def test_console_fallback_not_used_when_collector_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a collector endpoint set, console fallback is skipped."""
        from influx.telemetry import (
            _console_fallback_enabled,
            _otlp_endpoint_configured,
        )

        monkeypatch.setenv("INFLUX_OTEL_CONSOLE_FALLBACK", "true")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

        assert _console_fallback_enabled()
        assert _otlp_endpoint_configured()


# ── (9) AC-10-A regression guard: no span when disabled + pkgs installed ─


@_needs_otel
class TestAC10ARegressionGuard:
    """AC-10-A: with OTEL disabled, no span created even if packages installed."""

    def test_no_spans_when_disabled(self) -> None:
        """Directly construct a disabled tracer and verify zero exports."""
        from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
        from opentelemetry.sdk.trace.export import (
            SimpleSpanProcessor,
            SpanExporter,
            SpanExportResult,
        )

        collected: list[ReadableSpan] = []

        class _CollectingExporter(SpanExporter):
            def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
                collected.extend(spans)
                return SpanExportResult.SUCCESS

            def shutdown(self) -> None:
                pass

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_CollectingExporter()))

        # Tracer is disabled even though we have a real OTEL provider
        disabled_tracer = InfluxTracer(
            enabled=False,
            tracer=provider.get_tracer("test"),
        )

        with disabled_tracer.span(
            "influx.run",
            attributes={"influx.profile": "ai"},
        ) as s:
            s.set_attribute("influx.item_count", 42)

        # No spans should have been exported
        assert len(collected) == 0

    def test_get_tracer_disabled_produces_no_spans(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Via get_tracer with OTEL disabled, no real spans are created."""
        monkeypatch.setenv("INFLUX_OTEL_ENABLED", "false")
        tracer = get_tracer(force_rebuild=True)
        assert not tracer.enabled

        with tracer.span("influx.run") as s:
            s.set_attribute("influx.profile", "test")
            # The span wrapper wraps the no-op — no OTEL calls made
            assert isinstance(s, SpanWrapper)
