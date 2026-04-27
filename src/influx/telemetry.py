"""Opt-in OTEL telemetry wrapper.

Provides a thin API for span creation and attribute setting that is a
complete no-op when:

* ``INFLUX_OTEL_ENABLED`` is unset or ``false`` (FR-OBS-2, AC-10-A), OR
* the ``opentelemetry`` optional packages are not installed (AC-M4-3).

When enabled (``INFLUX_OTEL_ENABLED=true`` **and** OTEL packages are
present), calls delegate to the real ``opentelemetry`` SDK.

The no-op path performs only a boolean check — no object instantiation,
no attribute-setting calls (AC-10-A).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = [
    "InfluxTracer",
    "SpanWrapper",
    "current_run_id",
    "get_tracer",
]

# Context variable for the current run ID — set by ``run_profile()`` so
# that downstream call sites (e.g. filter scorer, source fetchers) can
# attach ``influx.run_id`` to their spans without interface changes.
current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)


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


def _build_tracer() -> InfluxTracer:
    """Construct an ``InfluxTracer`` based on current env + package state."""
    if not _otel_enabled() or not _otel_packages_available():
        return InfluxTracer(enabled=False)

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    service_name = os.environ.get("OTEL_SERVICE_NAME", "influx")
    resource_attrs = _parse_resource_attributes(
        os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    )
    resource_attrs["service.name"] = service_name
    environment = os.environ.get("INFLUX_ENVIRONMENT", "")
    if environment and "deployment.environment" not in resource_attrs:
        resource_attrs["deployment.environment"] = environment
    provider = TracerProvider(resource=Resource.create(resource_attrs))

    if _otlp_endpoint_configured():
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError:
            if not _console_fallback_enabled():
                return InfluxTracer(enabled=True, tracer=provider.get_tracer("influx"))
        else:
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    # Console fallback: emit spans to stdout when no collector is configured
    if _console_fallback_enabled() and not _otlp_endpoint_configured():
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    tracer = provider.get_tracer("influx")
    return InfluxTracer(enabled=True, tracer=tracer)


# Module-level singleton — rebuilt by ``get_tracer(force_rebuild=True)``
# or by tests that need to toggle OTEL on/off.
_tracer: InfluxTracer | None = None


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
