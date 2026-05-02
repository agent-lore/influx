"""Opt-in OTEL telemetry wrapper.

Provides a thin API for span creation, metric recording, and attribute
setting that is a complete no-op when:

* ``INFLUX_OTEL_ENABLED`` is unset or ``false`` (FR-OBS-2, AC-10-A), OR
* the ``opentelemetry`` optional packages are not installed (AC-M4-3).

When enabled (``INFLUX_OTEL_ENABLED=true`` **and** OTEL packages are
present), calls delegate to the real ``opentelemetry`` SDK.

The no-op path performs only a boolean check — no object instantiation,
no attribute-setting calls (AC-10-A).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = [
    "InfluxMeter",
    "InfluxTracer",
    "SourceAcquisitionError",
    "SpanWrapper",
    "current_archive_terminal_arxiv_ids",
    "current_run_id",
    "current_source_acquisition_errors",
    "get_meter",
    "get_tracer",
    "record_source_acquisition_error",
]

# Context variable for the current run ID — set by ``run_profile()`` so
# that downstream call sites (e.g. filter scorer, source fetchers) can
# attach ``influx.run_id`` to their spans without interface changes.
current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)


# Concrete shape of a source-acquisition error record:
#   {"source": "arxiv" | "rss" | ..., "kind": "oversize" | "timeout" |
#    "ssrf" | ..., "detail": "<short diagnostic>"}
# Plain dicts so they round-trip through ``json.dumps`` in the run
# ledger without a custom encoder.
SourceAcquisitionError = dict[str, str]


# Context variable carrying any source-acquisition failures the current
# run has swallowed without aborting.  ``run_profile()`` sets it to an
# empty list at run start; providers append on ``NetworkError`` paths
# that today return zero items silently (issue #20).  The scheduler
# reads it before writing the ledger entry so a degraded run is no
# longer indistinguishable from a quiet window.
current_source_acquisition_errors: ContextVar[list[SourceAcquisitionError] | None] = (
    ContextVar("current_source_acquisition_errors", default=None)
)


# Per-run set of arxiv-ids whose Lithos notes carry
# ``influx:archive-terminal``.  Populated once at the start of each
# scheduled / manual run by ``_run_profile_body`` after the LithosClient
# is connected; consulted by ``build_arxiv_note_item`` so that papers
# whose archive download has already been terminal-flipped (per the
# repair sweep cap added in PR #15) are not re-downloaded on every
# run (issue #14).  Defaults to the empty frozenset so behaviour
# outside a run context (CLI smoke commands, unit tests) is unchanged.
current_archive_terminal_arxiv_ids: ContextVar[frozenset[str]] = ContextVar(
    "current_archive_terminal_arxiv_ids",
    default=frozenset(),
)


def record_source_acquisition_error(
    *,
    source: str,
    kind: str,
    detail: str,
) -> None:
    """Append a swallowed source-fetch failure to the current run's record.

    Safe to call outside a run context — silently no-ops when
    :data:`current_source_acquisition_errors` is unset.  Callers
    should still emit their existing structured WARNING log; this
    helper only adds the run-ledger linkage.
    """
    errors = current_source_acquisition_errors.get()
    if errors is None:
        return
    errors.append(
        SourceAcquisitionError(
            {
                "source": source,
                "kind": kind,
                "detail": detail[:300],
            }
        )
    )


logger = logging.getLogger(__name__)


def _otel_enabled() -> bool:
    """Return ``True`` only when the env var explicitly enables OTEL."""
    return os.environ.get("INFLUX_OTEL_ENABLED", "").lower() in ("true", "1", "yes")


def _otel_packages_available() -> bool:
    """Return ``True`` when the core OTEL packages can be imported."""
    try:
        import opentelemetry.sdk.trace  # noqa: F401
        import opentelemetry.trace  # noqa: F401

        return True
    except ImportError:
        return False


# ── No-op implementations ─────────────────────────────────────────────


class _NoOpSpan:
    """Minimal no-op span — attribute setting is a no-op."""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        pass

    def set_attributes(self, attributes: dict[str, Any]) -> None:  # noqa: ARG002
        pass


_NOOP_SPAN = _NoOpSpan()


class SpanWrapper:
    """Thin wrapper around an OTEL span (or no-op)."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        self._span.set_attribute(key, value)

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        if hasattr(self._span, "set_attributes"):
            self._span.set_attributes(attributes)
        else:
            for k, v in attributes.items():
                self._span.set_attribute(k, v)


# Module-level no-op SpanWrapper reused across every disabled span call so
# the disabled body never instantiates a wrapper per invocation (AC-10-A).
_NOOP_SPAN_WRAPPER = SpanWrapper(_NOOP_SPAN)


class _NoOpInstrument:
    """Shared no-op metric instrument.

    Mirrors :class:`_NoOpSpan`: when OTEL is disabled the meter returns
    this singleton so that increment / record sites do zero work and
    allocate no objects (AC-10-A discipline extended to metrics).
    """

    __slots__ = ()

    def add(self, value: float, attributes: dict[str, Any] | None = None) -> None:  # noqa: ARG002
        pass

    def record(self, value: float, attributes: dict[str, Any] | None = None) -> None:  # noqa: ARG002
        pass


_NOOP_INSTRUMENT = _NoOpInstrument()


class InfluxTracer:
    """Tracer that wraps OTEL or falls back to no-op.

    Usage::

        tracer = get_tracer()
        with tracer.span("influx.run", attributes={"influx.profile": "ai"}) as s:
            s.set_attribute("influx.item_count", 42)
    """

    __slots__ = ("_enabled", "_tracer")

    def __init__(self, *, enabled: bool = False, tracer: Any = None) -> None:
        self._enabled = enabled
        self._tracer = tracer

    @property
    def enabled(self) -> bool:
        return self._enabled

    @contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[SpanWrapper]:
        """Start a span as a context manager.

        When disabled, yields a shared module-level no-op wrapper —
        no SpanWrapper instantiation, no OTEL calls (AC-10-A).
        """
        if not self._enabled:
            yield _NOOP_SPAN_WRAPPER
            return

        # OTEL is enabled — delegate to the real tracer
        real_tracer = self._tracer
        ctx = real_tracer.start_as_current_span(name, attributes=attributes)
        with ctx as otel_span:
            yield SpanWrapper(otel_span)


def _console_fallback_enabled() -> bool:
    """Return ``True`` when the console fallback exporter is requested."""
    return os.environ.get("INFLUX_OTEL_CONSOLE_FALLBACK", "").lower() in (
        "true",
        "1",
        "yes",
    )


def _otlp_endpoint_configured() -> bool:
    """Return ``True`` when an OTLP collector endpoint is configured."""
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""))


def _parse_resource_attributes(value: str) -> dict[str, str]:
    """Parse OTEL_RESOURCE_ATTRIBUTES-style ``key=value`` pairs."""
    attrs: dict[str, str] = {}
    for pair in value.split(","):
        if not pair.strip() or "=" not in pair:
            continue
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key:
            attrs[key] = raw.strip()
    return attrs


def _build_resource_attributes() -> dict[str, str]:
    """Build the OTEL resource attributes shared by traces and metrics.

    The attribute set is identical for every signal so dashboards can
    correlate runs, spans, and metrics by ``service.name`` /
    ``deployment.environment`` without per-signal divergence.
    """
    service_name = os.environ.get("OTEL_SERVICE_NAME", "influx")
    resource_attrs = _parse_resource_attributes(
        os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    )
    resource_attrs["service.name"] = service_name
    environment = os.environ.get("INFLUX_ENVIRONMENT", "")
    if environment and "deployment.environment" not in resource_attrs:
        resource_attrs["deployment.environment"] = environment
    return resource_attrs


def _build_tracer() -> InfluxTracer:
    """Construct an ``InfluxTracer`` based on current env + package state."""
    if not _otel_enabled():
        logger.info("OTEL disabled: INFLUX_OTEL_ENABLED is not true")
        return InfluxTracer(enabled=False)
    if not _otel_packages_available():
        logger.warning("OTEL disabled: opentelemetry packages are not installed")
        return InfluxTracer(enabled=False)

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    resource_attrs = _build_resource_attributes()
    provider = TracerProvider(resource=Resource.create(resource_attrs))

    if _otlp_endpoint_configured():
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError:
            if not _console_fallback_enabled():
                logger.warning(
                    "OTEL enabled but OTLP HTTP exporter is not installed; "
                    "spans will not be exported"
                )
                return InfluxTracer(enabled=True, tracer=provider.get_tracer("influx"))
        else:
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            logger.info(
                "OTEL OTLP trace exporter configured endpoint=%s traces_endpoint=%s",
                os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
                os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ""),
            )

    # Console fallback: emit spans to stdout when no collector is configured
    if _console_fallback_enabled() and not _otlp_endpoint_configured():
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        logger.info("OTEL console span exporter configured")
    elif not _otlp_endpoint_configured():
        logger.warning("OTEL enabled but no exporter endpoint is configured")

    tracer = provider.get_tracer("influx")
    return InfluxTracer(enabled=True, tracer=tracer)


class InfluxMeter:
    """Meter that wraps OTEL or falls back to no-op.

    Mirrors :class:`InfluxTracer`.  Instruments are created lazily and
    cached, so the second call to ``counter("influx_run_starts_total")``
    returns the same underlying OTEL ``Counter`` — required by the OTEL
    SDK, which raises if the same instrument name is registered twice
    on a meter.

    When disabled the meter returns the shared :data:`_NOOP_INSTRUMENT`
    so increment sites pay only a hash lookup, not an OTEL SDK call.
    """

    __slots__ = (
        "_counters",
        "_enabled",
        "_histograms",
        "_meter",
        "_resource",
        "_up_down_counters",
    )

    def __init__(
        self,
        *,
        enabled: bool = False,
        meter: Any = None,
        resource: Any = None,
    ) -> None:
        self._enabled = enabled
        self._meter = meter
        self._resource = resource
        self._counters: dict[str, Any] = {}
        self._up_down_counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}

    @property
    def resource(self) -> Any:
        """OTEL ``Resource`` attached to this meter (or ``None`` when disabled).

        Exposed so tests can verify ``service.name`` /
        ``deployment.environment`` without poking at private SDK
        attributes.
        """
        return self._resource

    @property
    def enabled(self) -> bool:
        return self._enabled

    def counter(self, name: str, *, unit: str = "1", description: str = "") -> Any:
        """Return (and cache) a monotonic counter instrument."""
        if not self._enabled:
            return _NOOP_INSTRUMENT
        cached = self._counters.get(name)
        if cached is not None:
            return cached
        instrument = self._meter.create_counter(
            name=name,
            unit=unit,
            description=description,
        )
        self._counters[name] = instrument
        return instrument

    def up_down_counter(
        self, name: str, *, unit: str = "1", description: str = ""
    ) -> Any:
        """Return (and cache) an up-down counter instrument."""
        if not self._enabled:
            return _NOOP_INSTRUMENT
        cached = self._up_down_counters.get(name)
        if cached is not None:
            return cached
        instrument = self._meter.create_up_down_counter(
            name=name,
            unit=unit,
            description=description,
        )
        self._up_down_counters[name] = instrument
        return instrument

    def histogram(self, name: str, *, unit: str = "1", description: str = "") -> Any:
        """Return (and cache) a histogram instrument."""
        if not self._enabled:
            return _NOOP_INSTRUMENT
        cached = self._histograms.get(name)
        if cached is not None:
            return cached
        instrument = self._meter.create_histogram(
            name=name,
            unit=unit,
            description=description,
        )
        self._histograms[name] = instrument
        return instrument


def _build_meter() -> InfluxMeter:
    """Construct an ``InfluxMeter`` based on current env + package state.

    Parallels :func:`_build_tracer`: shares the ``INFLUX_OTEL_ENABLED``
    toggle, the ``OTEL_EXPORTER_OTLP_ENDPOINT`` configuration, and the
    same resource-attribute set, so traces and metrics always describe
    the same service from the collector's point of view.
    """
    if not _otel_enabled():
        return InfluxMeter(enabled=False)
    if not _otel_packages_available():
        return InfluxMeter(enabled=False)

    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.warning("OTEL metrics SDK not installed; metrics will not be exported")
        return InfluxMeter(enabled=False)

    resource_attrs = _build_resource_attributes()
    readers: list[Any] = []

    if _otlp_endpoint_configured():
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
        except ImportError:
            if not _console_fallback_enabled():
                logger.warning(
                    "OTEL enabled but OTLP HTTP metric exporter is not installed; "
                    "metrics will not be exported"
                )
        else:
            readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
            logger.info(
                "OTEL OTLP metric exporter configured endpoint=%s metrics_endpoint=%s",
                os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
                os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", ""),
            )

    if _console_fallback_enabled() and not _otlp_endpoint_configured():
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

        readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))
        logger.info("OTEL console metric exporter configured")

    resource = Resource.create(resource_attrs)
    provider = MeterProvider(
        resource=resource,
        metric_readers=readers,
    )
    return InfluxMeter(
        enabled=True,
        meter=provider.get_meter("influx"),
        resource=resource,
    )


# Module-level singletons — rebuilt by ``get_tracer(force_rebuild=True)``
# / ``get_meter(force_rebuild=True)`` or by tests that need to toggle
# OTEL on/off between cases.
_tracer: InfluxTracer | None = None
_meter: InfluxMeter | None = None


def get_tracer(*, force_rebuild: bool = False) -> InfluxTracer:
    """Return the module-level ``InfluxTracer`` singleton.

    Parameters
    ----------
    force_rebuild:
        When ``True``, discard the cached tracer and rebuild from the
        current environment.  Useful in tests that toggle
        ``INFLUX_OTEL_ENABLED`` between cases.
    """
    global _tracer  # noqa: PLW0603
    if _tracer is None or force_rebuild:
        _tracer = _build_tracer()
    return _tracer


def get_meter(*, force_rebuild: bool = False) -> InfluxMeter:
    """Return the module-level ``InfluxMeter`` singleton.

    Mirrors :func:`get_tracer` so tests that toggle OTEL on/off can
    rebuild both signals from the same environment in one place.
    """
    global _meter  # noqa: PLW0603
    if _meter is None or force_rebuild:
        _meter = _build_meter()
    return _meter
