"""Integration-style tests for the arXiv 429 cooldown (issue #146).

Covers the wiring between ``ArxivSource.fetch_candidates`` and the
run-level telemetry surface: a cooldown-suppressed fetch records a
``source_cooldown_skip`` entry rather than a generic
``source_acquisition`` error, and the run ledger picks the matching
degraded reason.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ResilienceConfig,
    ScheduleConfig,
)
from influx.coordinator import RunKind
from influx.run_ledger import RunLedger
from influx.sources.arxiv import (
    ArxivCooldownError,
    ArxivSource,
    _record_429_final_failure,
    _reset_arxiv_cooldown_for_tests,
    _reset_fetch_pacing_for_tests,
)
from influx.telemetry import (
    current_run_id,
    current_source_acquisition_errors,
    current_source_cooldown_skips,
)


@pytest.fixture(autouse=True)
def _reset_cooldown_state() -> None:
    _reset_arxiv_cooldown_for_tests()
    _reset_fetch_pacing_for_tests()


def _minimal_config(*, cooldown_threshold: int = 2) -> AppConfig:
    return AppConfig(
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[ProfileConfig(name="ai-robotics")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        resilience=ResilienceConfig(
            arxiv_429_cooldown_threshold=cooldown_threshold,
            arxiv_429_cooldown_seconds=600,
            arxiv_429_max_retries=0,
            max_retries=0,
            arxiv_request_min_interval_seconds=0,
        ),
    )


async def test_cooldown_skip_records_cooldown_skip_not_acquisition_error() -> None:
    """When the cooldown suppresses a fetch the source surfaces the
    skip via :func:`record_source_cooldown_skip`, *not* via the
    generic acquisition-error path.  This is the load-bearing
    distinction operators rely on to triage "we backed off on purpose"
    apart from "we tried and failed".
    """
    config = _minimal_config(cooldown_threshold=1)
    # Engage the cooldown directly so the source.fetch_candidates call
    # short-circuits at ``_fetch_with_retry``'s skip branch.
    _record_429_final_failure(
        config.resilience, classification="rate_limit_upstream_capacity"
    )

    run_token = current_run_id.set("test-run-cooldown")
    errors_token = current_source_acquisition_errors.set([])
    cooldown_token = current_source_cooldown_skips.set([])
    try:
        source = ArxivSource(config)
        result = await source.fetch_candidates(
            profile_cfg=config.profiles[0],
            kind=RunKind.SCHEDULED,
            run_range=None,
        )
        assert list(result) == []
        errors = current_source_acquisition_errors.get() or []
        skips = current_source_cooldown_skips.get() or []
    finally:
        current_run_id.reset(run_token)
        current_source_acquisition_errors.reset(errors_token)
        current_source_cooldown_skips.reset(cooldown_token)

    # Critical assertion: no entry in the generic acquisition error
    # list — the run is degraded for a *different* reason.
    assert errors == []
    assert len(skips) == 1
    skip = skips[0]
    assert skip["source"] == "arxiv"
    assert skip["kind"] == "cooldown_active"
    assert "cooldown active" in skip["detail"].lower()


async def test_cooldown_skips_propagate_into_run_ledger(tmp_path: Any) -> None:
    """End-to-end: a cooldown-suppressed run lands a ledger entry with
    ``degraded=True`` and ``"source_cooldown_skip"`` among the
    degraded reasons, distinct from ``"source_acquisition"``.

    This uses :class:`RunLedger.complete` directly rather than driving
    a real run, because the cooldown wiring lives in the contextvar /
    ledger seam — the run body shape is incidental.
    """
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r1", profile="p", kind="scheduled", run_range=None)

    reasons = ledger.complete(
        run_id="r1",
        sources_checked=0,
        ingested=0,
        fetched_total=0,
        filter_errors_total=0,
        invalid_url_rejections_total=0,
        archive_failures_total=0,
        source_acquisition_errors=[],
        source_cooldown_skips=[
            {
                "source": "arxiv",
                "kind": "cooldown_active",
                "detail": "remaining_seconds=540.0 classification=upstream_capacity",
            }
        ],
    )

    assert "source_cooldown_skip" in reasons
    assert "source_acquisition" not in reasons

    recent = ledger.recent(profile="p")
    assert len(recent) == 1
    entry = recent[0]
    assert entry["degraded"] is True
    assert entry["source_cooldown_skips"] == [
        {
            "source": "arxiv",
            "kind": "cooldown_active",
            "detail": "remaining_seconds=540.0 classification=upstream_capacity",
        }
    ]


async def test_source_acquisition_and_cooldown_skip_can_coexist(tmp_path: Any) -> None:
    """A run that hits both shapes (e.g. arxiv cooldown + rss network
    failure) reports both degraded reasons; one does not mask the other.
    """
    ledger = RunLedger(tmp_path / "state")
    ledger.start(run_id="r1", profile="p", kind="scheduled", run_range=None)

    reasons = ledger.complete(
        run_id="r1",
        sources_checked=0,
        ingested=0,
        fetched_total=0,
        filter_errors_total=0,
        invalid_url_rejections_total=0,
        archive_failures_total=0,
        source_acquisition_errors=[
            {"source": "rss", "kind": "timeout", "detail": "boom"},
        ],
        source_cooldown_skips=[
            {
                "source": "arxiv",
                "kind": "cooldown_active",
                "detail": "remaining_seconds=540.0 classification=upstream_capacity",
            }
        ],
    )

    assert "source_acquisition" in reasons
    assert "source_cooldown_skip" in reasons


async def test_disabled_cooldown_falls_through_to_acquisition_error() -> None:
    """When the disable knob is set (threshold=0), a 429 burst hits
    the generic acquisition-error path — proving the feature can be
    turned off without affecting the legacy degraded-reason wiring.
    """
    config = _minimal_config(cooldown_threshold=0)

    # Force a 429 final-failure path from the underlying fetch.
    from influx.errors import NetworkError

    run_token = current_run_id.set("test-run-disabled")
    errors_token = current_source_acquisition_errors.set([])
    cooldown_token = current_source_cooldown_skips.set([])
    try:
        source = ArxivSource(config)
        reason = "classification=rate_limit_upstream_capacity retry_after_present=false"
        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            side_effect=NetworkError(
                "HTTP 429 from arXiv API",
                url="https://x",
                kind="rate_limit",
                reason=reason,
            ),
        ):
            result = await source.fetch_candidates(
                profile_cfg=config.profiles[0],
                kind=RunKind.SCHEDULED,
                run_range=None,
            )
            assert list(result) == []

        errors = current_source_acquisition_errors.get() or []
        skips = current_source_cooldown_skips.get() or []
    finally:
        current_run_id.reset(run_token)
        current_source_acquisition_errors.reset(errors_token)
        current_source_cooldown_skips.reset(cooldown_token)

    # Disabled — falls through to the legacy path.
    assert skips == []
    assert len(errors) == 1
    # And the refined-kind classification is still surfaced.
    assert errors[0]["kind"] == "rate_limit_upstream_capacity"


async def test_successful_fetch_clears_cooldown_for_next_run() -> None:
    """Regression: once arXiv recovers, the next run must not still
    be in cooldown.  The successful fetch clears the streak eagerly
    so the same process can flip back to NORMAL without waiting for
    the cooldown deadline.
    """
    config = _minimal_config(cooldown_threshold=1)
    # Engage cooldown.
    _record_429_final_failure(config.resilience, classification="rate_limit_local")

    # First fetch (cooldown active): returns empty + records skip.
    run_token = current_run_id.set("test-run-1")
    errors_token = current_source_acquisition_errors.set([])
    cooldown_token = current_source_cooldown_skips.set([])
    try:
        source = ArxivSource(config)
        result1 = await source.fetch_candidates(
            profile_cfg=config.profiles[0],
            kind=RunKind.SCHEDULED,
            run_range=None,
        )
        assert list(result1) == []
        assert (current_source_cooldown_skips.get() or []) != []
    finally:
        current_run_id.reset(run_token)
        current_source_acquisition_errors.reset(errors_token)
        current_source_cooldown_skips.reset(cooldown_token)

    # Now an external party clears the cooldown — e.g. via the
    # success path on a different in-process caller (the cooldown
    # state is shared at the module level on purpose).
    from influx.sources.arxiv import _record_arxiv_fetch_success

    _record_arxiv_fetch_success()

    # Second fetch: cooldown is gone.  The underlying fetch_arxiv is
    # mocked to return zero items, so we should see *no* cooldown
    # skip recorded — the cooldown is no longer active.
    run_token2 = current_run_id.set("test-run-2")
    errors_token2 = current_source_acquisition_errors.set([])
    cooldown_token2 = current_source_cooldown_skips.set([])
    try:
        source = ArxivSource(config)
        with patch("influx.sources.arxiv.fetch_arxiv", return_value=[]):
            result2 = await source.fetch_candidates(
                profile_cfg=config.profiles[0],
                kind=RunKind.SCHEDULED,
                run_range=None,
            )
            assert list(result2) == []
        errors2 = current_source_acquisition_errors.get() or []
        skips2 = current_source_cooldown_skips.get() or []
    finally:
        current_run_id.reset(run_token2)
        current_source_acquisition_errors.reset(errors_token2)
        current_source_cooldown_skips.reset(cooldown_token2)

    # Cooldown cleared — no skip recorded.  And no acquisition error
    # either, because the fetch (mocked) returned cleanly.
    assert skips2 == []
    assert errors2 == []


def test_cooldown_error_subclasses_network_error() -> None:
    """``ArxivCooldownError`` must subclass :class:`NetworkError` so
    existing ``except NetworkError`` branches in callers continue to
    catch the cooldown signal without code changes.
    """
    from influx.errors import NetworkError

    exc = ArxivCooldownError(
        "skipped",
        url="https://x",
        kind="cooldown_active",
        reason="remaining_seconds=10",
    )
    assert isinstance(exc, NetworkError)
    assert exc.kind == "cooldown_active"
