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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = [
    "InfluxTracer",
    "SpanWrapper",
    "get_tracer",
]


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

        When disabled, yields a no-op wrapper immediately (no OTEL calls).
        """
        if not self._enabled:
            yield SpanWrapper(_NOOP_SPAN)
            return

        # OTEL is enabled — delegate to the real tracer
        real_tracer = self._tracer
        ctx = real_tracer.start_as_current_span(name, attributes=attributes)
        with ctx as otel_span:
            yield SpanWrapper(otel_span)


def _build_tracer() -> InfluxTracer:
    """Construct an ``InfluxTracer`` based on current env + package state."""
    if not _otel_enabled() or not _otel_packages_available():
        return InfluxTracer(enabled=False)

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("influx")
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
