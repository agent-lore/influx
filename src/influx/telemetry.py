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

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = [
    "InfluxTracer",
    "SourceAcquisitionError",
    "SpanWrapper",
    "current_archive_terminal_arxiv_ids",
    "current_run_id",
    "current_source_acquisition_errors",
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
    if not _otel_enabled():
        logger.info("OTEL disabled: INFLUX_OTEL_ENABLED is not true")
        return InfluxTracer(enabled=False)
    if not _otel_packages_available():
        logger.warning("OTEL disabled: opentelemetry packages are not installed")
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
