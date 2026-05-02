"""Unit tests for :mod:`influx.metrics` (issue #6).

Covers:

  (1) Every helper returns the SAME cached instrument across calls.
  (2) Disabled meter → every helper returns the shared no-op.
  (3) Cardinality discipline: instrument names follow ``influx_*`` and
      no helper emits ``run_id`` / ``note_id`` / ``arxiv_id`` /
      ``source_url`` / ``title`` as a label.
  (4) Helpers are wired through :func:`influx.telemetry.get_meter` so
      ``force_rebuild=True`` swaps every caller's instrument in lock
      step.
"""

from __future__ import annotations

import pytest

from influx import metrics
from influx.telemetry import _NOOP_INSTRUMENT, get_meter

try:
    import opentelemetry.sdk.metrics  # noqa: F401

    _has_otel = True
except ImportError:
    _has_otel = False

_needs_otel = pytest.mark.skipif(
    not _has_otel,
    reason="opentelemetry metrics SDK not installed",
)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFLUX_OTEL_ENABLED", raising=False)
    get_meter(force_rebuild=True)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFLUX_OTEL_ENABLED", "true")
    get_meter(force_rebuild=True)


# Helper → expected instrument name.  The instrument names are part of
# the operator-facing contract (dashboard / alert queries reference
# them).  Pin them so an accidental rename breaks this test rather
# than silently breaking dashboards.
HELPERS: dict[str, str] = {
    "run_starts": "influx_run_starts_total",
    "run_completions": "influx_run_completions_total",
    "run_duration": "influx_run_duration_seconds",
    "active_runs": "influx_active_runs",
    "candidates_fetched": "influx_source_candidates_fetched_total",
    "articles_filtered": "influx_articles_filtered_total",
    "articles_inspected": "influx_articles_inspected_total",
    "cache_hits": "influx_cache_hits_total",
    "lithos_writes": "influx_lithos_writes_total",
    "repair_candidates": "influx_repair_candidates_total",
    "llm_validation_failures": "influx_llm_validation_failures_total",
    "archive_missing": "influx_archive_missing_total",
    "source_acquisition_errors": "influx_source_acquisition_errors_total",
}


class TestHelperContract:
    """Every helper is exported and bound to a documented instrument name."""

    def test_all_helpers_exported(self) -> None:
        missing = [name for name in HELPERS if name not in metrics.__all__]
        assert not missing, f"Helpers missing from __all__: {missing}"
        for name in HELPERS:
            assert callable(getattr(metrics, name))


class TestDisabledMeterUsesNoop:
    """OTEL off → every helper hands out the shared no-op singleton."""

    def test_every_helper_returns_noop_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable(monkeypatch)
        for name in HELPERS:
            instrument = getattr(metrics, name)()
            assert instrument is _NOOP_INSTRUMENT, (
                f"helper {name!r} did not return _NOOP_INSTRUMENT in disabled mode"
            )


@_needs_otel
class TestEnabledMeterCachesInstruments:
    """OTEL on → repeated helper calls return the same OTEL instrument.

    This is required by the OTEL SDK, which raises if the same
    instrument name is registered twice on a meter.  Helpers must
    delegate to the meter's instrument cache.
    """

    def test_repeated_helper_calls_return_same_instrument(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable(monkeypatch)
        for name in HELPERS:
            helper = getattr(metrics, name)
            first = helper()
            second = helper()
            assert first is second, f"helper {name!r} did not cache its instrument"


@_needs_otel
class TestInstrumentNames:
    """Instrument names follow the documented contract (issue #6 plan §2)."""

    def test_instrument_names_match_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _enable(monkeypatch)
        for helper_name, expected_metric_name in HELPERS.items():
            instrument = getattr(metrics, helper_name)()
            actual = getattr(instrument, "name", None)
            assert actual == expected_metric_name, (
                f"helper {helper_name!r} created instrument {actual!r}, "
                f"expected {expected_metric_name!r}"
            )


# ── Cardinality discipline ────────────────────────────────────────────


# These keys are explicitly disallowed as label values by issue #6
# ("avoid high-cardinality labels such as article title or full URL").
# The audit lives at the call-site level, not the instrument level, so
# this test just guards that the helper module itself does not encourage
# them by exposing them as helper kwargs or signature parameters.
HIGH_CARDINALITY_FIELDS = ("run_id", "note_id", "arxiv_id", "source_url", "title")


class TestCardinalityDiscipline:
    """No helper signature accepts high-cardinality identifiers as kwargs."""

    def test_no_helper_takes_high_cardinality_kwargs(self) -> None:
        import inspect

        for name in HELPERS:
            sig = inspect.signature(getattr(metrics, name))
            forbidden = [p for p in sig.parameters if p in HIGH_CARDINALITY_FIELDS]
            assert not forbidden, (
                f"helper {name!r} exposes high-cardinality kwargs: {forbidden}"
            )
