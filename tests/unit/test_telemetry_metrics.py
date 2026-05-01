"""Unit tests for the OTEL metrics wrapper (issue #6).

Mirrors :mod:`tests.unit.test_telemetry`'s structure for the trace
wrapper.  Covers:

  (1) Meter is a no-op when ``INFLUX_OTEL_ENABLED`` is unset / false.
  (2) Meter is enabled when ``INFLUX_OTEL_ENABLED=true`` and OTEL
      packages are installed.
  (3) Disabled meter never allocates a per-call instrument: every
      ``counter()`` / ``histogram()`` / ``up_down_counter()`` call
      returns the shared ``_NOOP_INSTRUMENT`` singleton.
  (4) Enabled meter caches instruments by name (the OTEL SDK rejects
      duplicate registrations on the same meter).
  (5) Resource attributes are shared with the tracer (same
      ``service.name`` / ``deployment.environment``).
"""

from __future__ import annotations

import pytest

from influx.telemetry import (
    _NOOP_INSTRUMENT,
    InfluxMeter,
    get_meter,
    get_tracer,
)

try:
    import opentelemetry.sdk.metrics  # noqa: F401

    _has_otel = True
except ImportError:
    _has_otel = False

_needs_otel = pytest.mark.skipif(
    not _has_otel,
    reason="opentelemetry metrics SDK not installed",
)


def _rebuild(monkeypatch: pytest.MonkeyPatch, env_value: str | None) -> None:
    if env_value is None:
        monkeypatch.delenv("INFLUX_OTEL_ENABLED", raising=False)
    else:
        monkeypatch.setenv("INFLUX_OTEL_ENABLED", env_value)


# ── (1) Disabled paths ────────────────────────────────────────────────


class TestMeterDisabled:
    """OTEL off → meter returns shared no-op instruments."""

    def test_meter_disabled_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild(monkeypatch, None)
        meter = get_meter(force_rebuild=True)
        assert not meter.enabled

    def test_meter_disabled_when_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild(monkeypatch, "false")
        meter = get_meter(force_rebuild=True)
        assert not meter.enabled

    def test_disabled_counter_is_shared_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-10-A discipline: disabled meter allocates nothing per call."""
        _rebuild(monkeypatch, "false")
        meter = get_meter(force_rebuild=True)
        a = meter.counter("influx_run_starts_total")
        b = meter.counter("influx_run_completions_total")
        c = meter.histogram("influx_run_duration_seconds")
        d = meter.up_down_counter("influx_active_runs")
        # Every kind returns the SAME shared singleton — proving the
        # disabled body performs no per-instrument allocation.
        assert a is b is c is d is _NOOP_INSTRUMENT

    def test_disabled_instrument_methods_never_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild(monkeypatch, None)
        meter = get_meter(force_rebuild=True)
        counter = meter.counter("influx_run_starts_total")
        counter.add(1)
        counter.add(5, {"profile": "ai"})
        histogram = meter.histogram("influx_run_duration_seconds")
        histogram.record(0.5)
        histogram.record(12.3, {"profile": "web"})
        up_down = meter.up_down_counter("influx_active_runs")
        up_down.add(1, {"profile": "ai"})
        up_down.add(-1, {"profile": "ai"})


# ── (2) Enabled path ──────────────────────────────────────────────────


@_needs_otel
class TestMeterEnabled:
    """OTEL on → meter creates real OTEL instruments."""

    def test_meter_enabled_when_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild(monkeypatch, "true")
        meter = get_meter(force_rebuild=True)
        assert meter.enabled

    def test_enabled_counter_is_real_instrument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild(monkeypatch, "true")
        meter = get_meter(force_rebuild=True)
        counter = meter.counter("influx_test_counter")
        # Real OTEL counters expose ``add``; calling it must not raise
        # even without an exporter wired.
        counter.add(1, {"profile": "ai"})


# ── (3) Instrument caching ───────────────────────────────────────────


@_needs_otel
class TestInstrumentCaching:
    """Same-name lookups return the same OTEL instrument."""

    def test_counter_cached_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild(monkeypatch, "true")
        meter = get_meter(force_rebuild=True)
        a = meter.counter("influx_test_cached_counter")
        b = meter.counter("influx_test_cached_counter")
        assert a is b

    def test_histogram_cached_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild(monkeypatch, "true")
        meter = get_meter(force_rebuild=True)
        a = meter.histogram("influx_test_cached_histogram")
        b = meter.histogram("influx_test_cached_histogram")
        assert a is b

    def test_up_down_counter_cached_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild(monkeypatch, "true")
        meter = get_meter(force_rebuild=True)
        a = meter.up_down_counter("influx_test_cached_up_down")
        b = meter.up_down_counter("influx_test_cached_up_down")
        assert a is b


# ── (4) Shared resource attributes ───────────────────────────────────


@_needs_otel
class TestSharedResourceAttributes:
    """Tracer and meter describe the same service to the collector."""

    def test_service_name_shared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _rebuild(monkeypatch, "true")
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        monkeypatch.delenv("INFLUX_ENVIRONMENT", raising=False)
        tracer = get_tracer(force_rebuild=True)
        meter = get_meter(force_rebuild=True)
        tracer_resource = tracer._tracer.resource  # type: ignore[attr-defined]  # noqa: SLF001
        assert tracer_resource.attributes["service.name"] == "influx"
        assert meter.resource is not None
        assert meter.resource.attributes["service.name"] == "influx"

    def test_deployment_environment_shared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rebuild(monkeypatch, "true")
        monkeypatch.setenv("INFLUX_ENVIRONMENT", "staging")
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        tracer = get_tracer(force_rebuild=True)
        meter = get_meter(force_rebuild=True)
        tracer_resource = tracer._tracer.resource  # type: ignore[attr-defined]  # noqa: SLF001
        assert tracer_resource.attributes["deployment.environment"] == "staging"
        assert meter.resource is not None
        assert meter.resource.attributes["deployment.environment"] == "staging"


# ── (5) Direct InfluxMeter construction ──────────────────────────────


class TestInfluxMeterDirect:
    """Direct InfluxMeter API works independently of the singleton."""

    def test_disabled_meter_constructed_directly(self) -> None:
        meter = InfluxMeter(enabled=False)
        c = meter.counter("influx_test")
        assert c is _NOOP_INSTRUMENT
