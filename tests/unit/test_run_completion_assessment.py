"""The one run-completion assessment drives the ledger, metrics and logs alike.

Proves the seam introduced for Lithos task ``efc53838``: whatever
:class:`influx.run_ledger.RunLedger` persists for a run is exactly what
the ``run_completions`` metric labels and the ``run completed`` /
``run flagged`` log lines say — for success, expected lossiness,
unexpected degradation, skipped runs and failure — and that a run is
finalised exactly once.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from influx.config import (
    AppConfig,
    LithosConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.coordinator import RunKind
from influx.run import RunOutcome, RunPlan
from influx.run_assessment import RunCompletionAssessment, RunEvidence
from influx.run_ledger import RunLedger
from influx.run_service import RunService
from influx.telemetry import (
    current_fetched_total,
    current_invalid_url_rejections,
    current_write_outcomes,
)


def _config() -> AppConfig:
    return AppConfig(
        schedule=ScheduleConfig(
            cron="0 6 * * *", timezone="UTC", misfire_grace_seconds=1
        ),
        lithos=LithosConfig(url="http://example.invalid/sse"),
        profiles=[ProfileConfig(name="alpha")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="t"),
            tier1_enrich=PromptEntryConfig(text="t"),
            tier3_extract=PromptEntryConfig(text="t"),
        ),
    )


def _plan(kind: RunKind = RunKind.SCHEDULED) -> RunPlan:
    return RunPlan(profile="alpha", kind=kind)


@pytest.fixture
def completions() -> Any:
    """``run_completions`` as a mock counter so the label set can be read back."""
    counter = MagicMock()
    with patch("influx.metrics.run_completions", return_value=counter):
        yield counter


def _completion_labels(counter: Any) -> dict[str, str]:
    assert counter.add.call_count == 1, counter.add.call_args_list
    return counter.add.call_args.args[1]


def _completed_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [r.message for r in caplog.records if r.message.startswith("run completed")]
    assert len(lines) == 1, lines
    return lines[0]


class TestLedgerMetricAndLogAgree:
    async def test_success(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, completions: Any
    ) -> None:
        ledger = RunLedger(tmp_path)
        service = RunService(config=_config(), ledger=ledger)
        with (
            caplog.at_level(logging.INFO, logger="influx.run_service"),
            patch(
                "influx.run.Run.execute",
                new_callable=AsyncMock,
                return_value=RunOutcome(sources_checked=3, ingested=2),
            ),
        ):
            await service.execute(_plan())

        entry = ledger.recent()[0]
        assert (entry["degraded"], entry["degradation_severity"]) == (False, "success")
        assert _completion_labels(completions) == {
            "profile": "alpha",
            "run_type": "scheduled",
            "outcome": "success",
            "severity": "success",
        }
        assert "degraded=False severity=success reasons=" in _completed_line(caplog)
        assert not [r for r in caplog.records if r.message.startswith("run flagged")]

    async def test_expected_lossy_burst_reason_degrades_everywhere(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, completions: Any
    ) -> None:
        """``invalid_url_rejections`` (#177) alone is expected lossiness.

        Before the assessment owned the outcome, the ledger said
        ``degraded=True`` while the metric said ``outcome=success`` and
        the log said ``degraded=False`` for this reason.
        """
        ledger = RunLedger(tmp_path)
        service = RunService(config=_config(), ledger=ledger)

        async def body(self: Any) -> RunOutcome:
            fetched = current_fetched_total.get()
            rejected = current_invalid_url_rejections.get()
            assert fetched is not None and rejected is not None
            fetched[0] += 40
            rejected[0] += 30
            return RunOutcome(sources_checked=10, ingested=4, fetched_total=40)

        with (
            caplog.at_level(logging.INFO, logger="influx.run_service"),
            patch("influx.run.Run.execute", new=body),
        ):
            await service.execute(_plan())

        entry = ledger.recent()[0]
        assert entry["degraded_reasons"] == ["invalid_url_rejections"]
        assert (entry["degraded"], entry["degradation_severity"]) == (
            True,
            "expected_lossy",
        )
        assert _completion_labels(completions)["outcome"] == "degraded"
        assert _completion_labels(completions)["severity"] == "expected_lossy"
        line = _completed_line(caplog)
        assert (
            "degraded=True severity=expected_lossy reasons=invalid_url_rejections"
            in line
        )
        flagged = [r for r in caplog.records if r.message.startswith("run flagged")]
        assert len(flagged) == 1
        assert "run flagged invalid_url_rejections" in flagged[0].message
        assert "invalid_url_rejections=30 fetched_total=40" in flagged[0].message

    async def test_unexpected_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, completions: Any
    ) -> None:
        ledger = RunLedger(tmp_path)
        service = RunService(config=_config(), ledger=ledger)

        async def body(self: Any) -> RunOutcome:
            writes = current_write_outcomes.get()
            assert writes is not None
            writes[("slug_collision", "rss")] = 2
            return RunOutcome(sources_checked=3, ingested=1)

        with (
            caplog.at_level(logging.INFO, logger="influx.run_service"),
            patch("influx.run.Run.execute", new=body),
        ):
            await service.execute(_plan())

        entry = ledger.recent()[0]
        assert entry["degraded_reasons"] == ["invalid_note_state"]
        assert entry["degradation_severity"] == "unexpected_failure"
        labels = _completion_labels(completions)
        assert (labels["outcome"], labels["severity"]) == (
            "degraded",
            "unexpected_failure",
        )
        assert (
            "severity=unexpected_failure reasons=invalid_note_state"
            in _completed_line(caplog)
        )
        flagged = [r for r in caplog.records if r.message.startswith("run flagged")]
        assert "by_kind=slug_collision=2" in flagged[0].message

    async def test_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, completions: Any
    ) -> None:
        ledger = RunLedger(tmp_path)
        probe_loop = MagicMock()
        probe_loop.lithos_circuit_open.return_value = True
        probe_loop.lcma_tools_unavailable.return_value = False
        service = RunService(config=_config(), ledger=ledger, probe_loop=probe_loop)

        with caplog.at_level(logging.INFO, logger="influx.run_service"):
            outcome = await service.execute(_plan())

        assert outcome.skipped is True
        entry = ledger.recent()[0]
        assert (entry["status"], entry["error"]) == ("skipped", "lithos_unhealthy")
        assert (entry["degraded"], entry["degradation_severity"]) == (
            False,
            "not_applicable",
        )
        labels = _completion_labels(completions)
        assert (labels["outcome"], labels["severity"]) == ("skipped", "not_applicable")
        assert [r.message for r in caplog.records if "run skipped" in r.message]
        assert not [r for r in caplog.records if r.message.startswith("run completed")]

    async def test_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, completions: Any
    ) -> None:
        ledger = RunLedger(tmp_path)
        service = RunService(config=_config(), ledger=ledger)

        async def boom(*args: Any, **kwargs: Any) -> RunOutcome:
            raise RuntimeError("body crashed")

        with (
            caplog.at_level(logging.INFO, logger="influx.run_service"),
            patch("influx.run.Run.execute", side_effect=boom),
            pytest.raises(RuntimeError, match="body crashed"),
        ):
            await service.execute(_plan())

        entry = ledger.recent()[0]
        assert entry["status"] == "failed"
        assert (entry["degraded"], entry["degradation_severity"]) == (
            False,
            "not_applicable",
        )
        labels = _completion_labels(completions)
        assert (labels["outcome"], labels["severity"]) == ("failure", "not_applicable")
        assert [r for r in caplog.records if r.message.startswith("run failed")]


class TestFinalisedExactlyOnce:
    async def test_metric_failure_after_complete_does_not_refinalise(
        self, tmp_path: Path, completions: Any
    ) -> None:
        """A problem *after* ``ledger.complete`` must not reach ``ledger.fail``.

        Previously the completion code ran inside the failure scope, so a
        raising metric would have appended a second, ``failed`` entry for
        the same run.
        """
        ledger = RunLedger(tmp_path)
        service = RunService(config=_config(), ledger=ledger)
        completions.add.side_effect = OSError("exporter down")

        with (
            patch(
                "influx.run.Run.execute",
                new_callable=AsyncMock,
                return_value=RunOutcome(sources_checked=1, ingested=1),
            ),
            pytest.raises(OSError, match="exporter down"),
        ):
            await service.execute(_plan())

        entries = ledger.recent()
        assert [e["status"] for e in entries] == ["completed"]
        assert ledger.active_runs() == []

    async def test_contextvars_reset_after_failure(self, tmp_path: Path) -> None:
        ledger = RunLedger(tmp_path)
        service = RunService(config=_config(), ledger=ledger)

        async def boom(*args: Any, **kwargs: Any) -> RunOutcome:
            raise RuntimeError("x")

        with (
            patch("influx.run.Run.execute", side_effect=boom),
            pytest.raises(RuntimeError),
        ):
            await service.execute(_plan())

        assert current_fetched_total.get() is None
        assert current_write_outcomes.get() is None


def _zero_ingestion_run(
    ledger: RunLedger, *, run_id: str, kind: str
) -> RunCompletionAssessment:
    ledger.start(run_id=run_id, profile="alpha", kind=kind, run_range=None)
    return ledger.complete(
        run_id=run_id,
        evidence=RunEvidence(sources_checked=5, ingested=0, fetched_total=5),
    )


class TestScheduledOnlyHeuristics:
    @pytest.mark.parametrize("kind", ["manual", "backfill", "inbox"])
    def test_non_scheduled_kinds_never_ratchet(self, tmp_path: Path, kind: str) -> None:
        """Two zero-ingestion runs of a non-scheduled kind are not a stall."""
        ledger = RunLedger(tmp_path)
        _zero_ingestion_run(ledger, run_id="r-1", kind=kind)
        second = _zero_ingestion_run(ledger, run_id="r-2", kind=kind)
        assert second.reasons == ()
        assert second.outcome == "success"

    def test_scheduled_kind_ratchets(self, tmp_path: Path) -> None:
        ledger = RunLedger(tmp_path)
        _zero_ingestion_run(ledger, run_id="r-1", kind="scheduled")
        second = _zero_ingestion_run(ledger, run_id="r-2", kind="scheduled")
        assert second.reasons == ("ingestion_stall",)
        assert second.stall_metric_reasons == ("ingestion_stall",)
        assert second.severity == "unexpected_failure"


class TestAssessmentShape:
    def test_terminal_statuses_have_no_reasons_and_not_applicable_severity(
        self,
    ) -> None:
        for status in ("failed", "skipped"):
            assessment = RunCompletionAssessment(
                run_id="r",
                profile="p",
                kind="scheduled",
                status=status,  # type: ignore[arg-type]
                evidence=RunEvidence(),
                reasons=(),
                error="why",
            )
            assert assessment.severity == "not_applicable"
            assert assessment.degraded is False
            assert assessment.top_drivers == ""

    def test_top_drivers_come_from_the_persisted_summary(self) -> None:
        evidence = RunEvidence(
            archive_failures_total=2,
            archive_failures=[
                {"url": "https://ieee.org/a", "tags": ["influx:archive-missing"]},
                {"url": "https://ieee.org/b", "tags": ["influx:archive-missing"]},
            ],
            source_acquisition_errors=[
                {"source": "arxiv", "kind": "timeout", "detail": ""}
            ],
        )
        from influx.run_assessment import build_degradation_summary

        assessment = RunCompletionAssessment(
            run_id="r",
            profile="p",
            kind="scheduled",
            status="completed",
            evidence=evidence,
            reasons=("source_acquisition", "archive_acquisition"),
            degradation_summary=build_degradation_summary(evidence),
        )
        assert assessment.top_drivers == "archive:ieee.org=2;source:arxiv/timeout=1"
        assert assessment.outcome == "degraded"
        assert assessment.severity == "expected_lossy"
        assert assessment.stall_metric_reasons == ()
