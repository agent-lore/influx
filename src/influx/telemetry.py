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
    "SourceCooldownSkip",
    "SourceRetryCounts",
    "SpanWrapper",
    "current_archive_terminal_arxiv_ids",
    "current_fetched_total",
    "current_filter_errors",
    "current_invalid_url_rejections",
    "current_run_id",
    "current_source_acquisition_errors",
    "current_source_cooldown_skips",
    "current_source_retry_counts",
    "get_meter",
    "get_tracer",
    "record_fetched_items",
    "record_filter_error",
    "record_invalid_url_rejection",
    "record_source_acquisition_error",
    "record_source_cooldown_skip",
    "record_source_retry",
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


# Issue #146: a cooldown-suppressed source fetch is recorded *separately*
# from the swallowed-acquisition-error path so the run ledger can mark
# the run degraded with ``source_cooldown_skip`` instead of
# ``source_acquisition``.  Same shape as :data:`SourceAcquisitionError`
# (source / kind / detail dicts) so existing JSONL consumers can scan a
# uniform schema.
SourceCooldownSkip = dict[str, str]


current_source_cooldown_skips: ContextVar[list[SourceCooldownSkip] | None] = ContextVar(
    "current_source_cooldown_skips",
    default=None,
)


def record_source_cooldown_skip(
    *,
    source: str,
    kind: str,
    detail: str,
) -> None:
    """Append a cooldown-suppressed source fetch to the current run's record.

    Distinct from :func:`record_source_acquisition_error`: a cooldown
    skip means Influx *chose* not to call upstream because a recent
    burst of 429s tripped the source-specific cooldown state machine
    (issue #146).  Surfaced on the run-ledger entry as
    ``source_cooldown_skip`` and as the dedicated degraded reason of
    the same name.  Safe to call outside a run context — silently
    no-ops when :data:`current_source_cooldown_skips` is unset.
    """
    skips = current_source_cooldown_skips.get()
    if skips is None:
        return
    skips.append(
        SourceCooldownSkip(
            {
                "source": source,
                "kind": kind,
                "detail": detail[:300],
            }
        )
    )


# Per-run counter of source-fetch retries that the run **recovered from**
# (i.e. retries that did not produce a final swallowed error).  Shape:
# ``{"arxiv": {"rate_limit": 2, "timeout": 1}}``.  Source adapters call
# :func:`record_source_retry` once per retry decision (each non-final
# attempt that the retry loop is about to sleep + retry).  Surfaced on
# the run-ledger entry as ``source_retry_counts`` so operators can
# distinguish "one transient 429 we recovered from" from "we burned the
# entire retry budget" — issue #129.
SourceRetryCounts = dict[str, dict[str, int]]


current_source_retry_counts: ContextVar[SourceRetryCounts | None] = ContextVar(
    "current_source_retry_counts",
    default=None,
)


def record_source_retry(*, source: str, kind: str) -> None:
    """Increment the current run's recovered-retry counter for *source*/*kind*.

    Safe to call outside a run context — silently no-ops when
    :data:`current_source_retry_counts` is unset.  Source adapters call
    this on every retry decision (every attempt that failed but is
    followed by another attempt).  When the retry budget is exhausted
    and the failure is finally swallowed, the source instead calls
    :func:`record_source_acquisition_error`; the two records together
    let an operator see "we retried N times before giving up".
    """
    counts = current_source_retry_counts.get()
    if counts is None:
        return
    by_kind = counts.setdefault(source, {})
    by_kind[kind] = by_kind.get(kind, 0) + 1


# Per-run counter of pre-filter fetched candidates.  Set to ``[0]`` at
# run start by ``run_service.ledger_lifecycle``; source adapters
# increment via :func:`record_fetched_items` after a successful fetch
# (BEFORE the LLM filter runs).  The scheduler reads it at run end so
# the run-ledger entry can split ``fetch_stall`` (no items reached the
# filter) from ``filter_stall`` (items reached the filter, all
# rejected) — issue #85.
#
# A list-of-ints (rather than a bare int) is used so that incrementing
# from the source layer mutates a shared mutable container, mirroring
# the :data:`current_source_acquisition_errors` pattern: callers don't
# have to worry about ContextVar.set semantics on top of an immutable
# integer.
current_fetched_total: ContextVar[list[int] | None] = ContextVar(
    "current_fetched_total",
    default=None,
)


def record_fetched_items(count: int) -> None:
    """Add *count* to the current run's pre-filter ``fetched_total`` (#85).

    Safe to call outside a run context — silently no-ops when
    :data:`current_fetched_total` is unset.  Source adapters call this
    once per fetch batch, AFTER a successful fetch and BEFORE the LLM
    filter runs, so the count reflects the raw item population the
    filter saw.

    A source that errors during fetch contributes 0 (don't call this
    on the error path).  Multiple sources on one profile (arXiv +
    RSS) accumulate via successive calls.
    """
    if count <= 0:
        return
    counter = current_fetched_total.get()
    if counter is None:
        return
    counter[0] += count


# Per-run counter of LLM-filter execution failures (FilterScorerError).
# Set to ``[0]`` at run start by ``run_service.ledger_lifecycle``;
# source adapters increment via :func:`record_filter_error` from the
# ``except FilterScorerError`` arm.  Used by the run ledger to
# discriminate ``filter_error`` (the scorer failed — transport, parse,
# or provider error) from ``filter_stall`` (the scorer ran and
# rejected every candidate) — review on PR for #85.
current_filter_errors: ContextVar[list[int] | None] = ContextVar(
    "current_filter_errors",
    default=None,
)


def record_filter_error() -> None:
    """Increment the current run's ``filter_errors_total`` (#85 review).

    Safe to call outside a run context — silently no-ops when
    :data:`current_filter_errors` is unset.  Source adapters call this
    once per ``FilterScorerError`` (one per failed batch / feed).
    Multiple errors in the same run accumulate.
    """
    counter = current_filter_errors.get()
    if counter is None:
        return
    counter[0] += 1


# Per-run counter of source items rejected pre-acquisition because their
# article URL failed syntactic validation (issue #131).  Set to ``[0]`` at
# run start by ``run_service.ledger_lifecycle``; source adapters
# increment via :func:`record_invalid_url_rejection` when
# :func:`influx.urls.classify_article_url` rejects an item link.
#
# Surfaced on the run-ledger entry as ``invalid_url_rejections_total`` so
# operators can distinguish "feed fetched OK, N items rejected because
# their URLs were upstream-malformed" from "archive download failed for a
# valid URL" (which keeps producing ``influx:archive-missing``).
current_invalid_url_rejections: ContextVar[list[int] | None] = ContextVar(
    "current_invalid_url_rejections",
    default=None,
)


def record_invalid_url_rejection(count: int = 1) -> None:
    """Add *count* to the current run's ``invalid_url_rejections_total`` (#131).

    Safe to call outside a run context — silently no-ops when
    :data:`current_invalid_url_rejections` is unset.  Source adapters
    call this when ``classify_article_url`` rejects an item link
    (loopback, private, link-local, multicast, malformed, or
    disallowed scheme).  Multiple rejections in the same run accumulate.
    """
    if count <= 0:
        return
    counter = current_invalid_url_rejections.get()
    if counter is None:
        return
    counter[0] += count


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


def _build_logger_provider() -> Any | None:
    """Construct an OTEL ``LoggerProvider`` based on env + package state.

    Mirrors :func:`_build_tracer` and :func:`_build_meter`: shares the
    ``INFLUX_OTEL_ENABLED`` toggle, the ``OTEL_EXPORTER_OTLP_ENDPOINT``
    configuration, and the same resource-attribute set, so traces,
    metrics, and logs always describe the same service from the
    collector's point of view.

    Returns ``None`` when OTEL is disabled or the OTEL packages are not
    importable — callers must treat ``None`` as "do not attach any OTEL
    log handler" (AC-10-A discipline: the disabled path must not
    construct any OTEL objects).
    """
    if not _otel_enabled():
        return None
    if not _otel_packages_available():
        return None

    try:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.warning("OTEL logs SDK not installed; logs will not be exported")
        return None

    resource_attrs = _build_resource_attributes()
    provider = LoggerProvider(resource=Resource.create(resource_attrs))

    if _otlp_endpoint_configured():
        try:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                OTLPLogExporter,
            )
        except ImportError:
            if not _console_fallback_enabled():
                logger.warning(
                    "OTEL enabled but OTLP HTTP log exporter is not installed; "
                    "logs will not be exported"
                )
                return provider
        else:
            provider.add_log_record_processor(
                BatchLogRecordProcessor(OTLPLogExporter())
            )
            logger.info(
                "OTEL OTLP log exporter configured endpoint=%s logs_endpoint=%s",
                os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
                os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", ""),
            )

    if _console_fallback_enabled() and not _otlp_endpoint_configured():
        from opentelemetry.sdk._logs.export import ConsoleLogExporter

        provider.add_log_record_processor(BatchLogRecordProcessor(ConsoleLogExporter()))
        logger.info("OTEL console log exporter configured")
    elif not _otlp_endpoint_configured():
        logger.warning("OTEL enabled but no log exporter endpoint is configured")

    return provider


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
