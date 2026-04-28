"""Unit tests for per-profile rejection-rate logging (US-008).

Covers:
  (1) Cadence at recalibrate_after_runs=3: emission at 3, 6, 9 — not 1, 2, 4, 5, 7, 8
  (2) Negative test: cadence at 5 — not 1-4, emitted at 5
  (3) Per-profile isolation: two profiles emit at independent cadences
  (4) JSON-structured log line with profile name and per-tag rejection-rate map
  (5) AC-M4-4: per-run structured logs include filtered and ingested counts
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from influx.rejection_rate import (
    on_run_complete,
    record_filter_result,
    reset,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset in-memory state before each test."""
    reset()


def _make_config(recalibrate_after_runs: int = 3) -> Any:
    """Build a minimal config mock with the given cadence."""
    config = MagicMock()
    config.feedback.recalibrate_after_runs = recalibrate_after_runs
    config.feedback.negative_examples_per_profile = 20
    return config


def _make_client(rejected_titles: list[str] | None = None) -> Any:
    """Build a mock LithosClient whose list_notes returns given rejection titles."""
    client = MagicMock()
    titles = rejected_titles or []
    items = [{"title": t, "id": f"note-{i}"} for i, t in enumerate(titles)]
    result_text = json.dumps({"items": items})
    mock_content = MagicMock()
    mock_content.text = result_text
    list_result = MagicMock()
    list_result.content = [mock_content]
    client.list_notes = AsyncMock(return_value=list_result)
    return client


async def _simulate_run(
    profile: str,
    *,
    config: Any,
    client: Any,
    items: list[tuple[str, list[str]]] | None = None,
    sources_checked: int = 10,
    ingested: int = 5,
) -> None:
    """Simulate a run: record filter results then call on_run_complete."""
    for title, tags in items or []:
        record_filter_result(profile, title, tags)
    await on_run_complete(
        profile,
        config=config,
        client=client,
        sources_checked=sources_checked,
        ingested=ingested,
    )


# ── (1) Cadence at 3: emission at 3, 6, 9 ────────────────────────────


class TestCadenceAtThree:
    """AC-10-D: rejection-rate log at runs 3, 6, 9 — not 1, 2, 4, 5, 7, 8."""

    @pytest.mark.asyncio
    async def test_emission_cadence(self, caplog: pytest.LogCaptureFixture) -> None:
        config = _make_config(recalibrate_after_runs=3)
        client = _make_client()
        sample_items = [("Paper A", ["cat:cs.AI", "source:arxiv"])]

        emitted_at: list[int] = []
        for run_num in range(1, 10):
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
                await _simulate_run(
                    "test-profile",
                    config=config,
                    client=client,
                    items=sample_items,
                )
            rejection_logs = [
                r
                for r in caplog.records
                if "influx.rejection_rate" in r.getMessage()
                and '"event": "influx.rejection_rate"' in r.getMessage()
            ]
            if rejection_logs:
                emitted_at.append(run_num)

        assert emitted_at == [3, 6, 9]


# ── (2) Negative test: cadence at 5 ──────────────────────────────────


class TestCadenceAtFive:
    """Negative: cadence at 5 — not emitted on 1-4, emitted on 5."""

    @pytest.mark.asyncio
    async def test_not_emitted_before_threshold(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _make_config(recalibrate_after_runs=5)
        client = _make_client()
        sample_items = [("Paper X", ["cat:cs.RO"])]

        for run_num in range(1, 5):
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
                await _simulate_run(
                    "test-profile",
                    config=config,
                    client=client,
                    items=sample_items,
                )
            rejection_logs = [
                r
                for r in caplog.records
                if '"event": "influx.rejection_rate"' in r.getMessage()
            ]
            assert not rejection_logs, f"Unexpected emission at run {run_num}"

    @pytest.mark.asyncio
    async def test_emitted_at_threshold(self, caplog: pytest.LogCaptureFixture) -> None:
        config = _make_config(recalibrate_after_runs=5)
        client = _make_client()
        sample_items = [("Paper X", ["cat:cs.RO"])]

        # Runs 1-4: no emission (but still execute to increment counter).
        for _ in range(4):
            await _simulate_run(
                "test-profile",
                config=config,
                client=client,
                items=sample_items,
            )

        # Run 5: should emit.
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
            await _simulate_run(
                "test-profile",
                config=config,
                client=client,
                items=sample_items,
            )
        rejection_logs = [
            r
            for r in caplog.records
            if '"event": "influx.rejection_rate"' in r.getMessage()
        ]
        assert len(rejection_logs) == 1


# ── (3) Per-profile isolation ─────────────────────────────────────────


class TestPerProfileIsolation:
    """Two profiles emit at independent cadences."""

    @pytest.mark.asyncio
    async def test_independent_counters(self, caplog: pytest.LogCaptureFixture) -> None:
        config = _make_config(recalibrate_after_runs=3)
        client = _make_client()
        items_a = [("Alpha Paper", ["cat:cs.AI"])]
        items_b = [("Beta Paper", ["cat:cs.RO"])]

        # Profile A: 3 runs → should emit at run 3.
        for _ in range(2):
            await _simulate_run(
                "profile-a", config=config, client=client, items=items_a
            )

        # Profile B: 1 run — should not emit.
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
            await _simulate_run(
                "profile-b", config=config, client=client, items=items_b
            )
        b_rejection = [
            r
            for r in caplog.records
            if '"event": "influx.rejection_rate"' in r.getMessage()
            and '"profile": "profile-b"' in r.getMessage()
        ]
        assert not b_rejection, "Profile B should not emit at its 1st run"

        # Profile A run 3: should emit.
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
            await _simulate_run(
                "profile-a", config=config, client=client, items=items_a
            )
        a_rejection = [
            r
            for r in caplog.records
            if '"event": "influx.rejection_rate"' in r.getMessage()
            and '"profile": "profile-a"' in r.getMessage()
        ]
        assert len(a_rejection) == 1, "Profile A should emit at its 3rd run"

        # Profile B still hasn't hit cadence (only 1 run so far).
        b_rejection_all = [
            r
            for r in caplog.records
            if '"event": "influx.rejection_rate"' in r.getMessage()
            and '"profile": "profile-b"' in r.getMessage()
        ]
        assert not b_rejection_all


# ── (4) JSON structure and per-tag rejection rates ────────────────────


class TestRejectionRateJson:
    """Log line is JSON-structured with profile and per-tag rejection-rate map."""

    @pytest.mark.asyncio
    async def test_json_structure_with_rejection_rates(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify JSON contains profile, rejection_rates with correct rates."""
        config = _make_config(recalibrate_after_runs=1)
        # Two items: Paper A has cat:cs.AI, Paper B has cat:cs.AI + cat:cs.RO.
        # Paper A is rejected by user.
        client = _make_client(rejected_titles=["Paper A"])

        record_filter_result("my-profile", "Paper A", ["cat:cs.AI", "source:arxiv"])
        record_filter_result(
            "my-profile",
            "Paper B",
            ["cat:cs.AI", "cat:cs.RO", "source:arxiv"],
        )

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
            await on_run_complete(
                "my-profile",
                config=config,
                client=client,
                sources_checked=10,
                ingested=5,
            )

        rejection_logs = [
            r
            for r in caplog.records
            if '"event": "influx.rejection_rate"' in r.getMessage()
        ]
        assert len(rejection_logs) == 1

        payload = json.loads(rejection_logs[0].getMessage())
        assert payload["event"] == "influx.rejection_rate"
        assert payload["profile"] == "my-profile"
        rates = payload["rejection_rates"]
        assert isinstance(rates, dict)

        # cat:cs.AI: 1 rejected out of 2 = 0.5
        assert rates["cat:cs.AI"] == 0.5
        # cat:cs.RO: 0 rejected out of 1 = 0.0
        assert rates["cat:cs.RO"] == 0.0
        # source:arxiv: 1 rejected out of 2 = 0.5
        assert rates["source:arxiv"] == 0.5

    @pytest.mark.asyncio
    async def test_no_rejections_all_zero_rates(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When no items were rejected, all rates should be 0.0."""
        config = _make_config(recalibrate_after_runs=1)
        client = _make_client(rejected_titles=[])

        record_filter_result("clean-profile", "Paper X", ["cat:cs.AI"])
        record_filter_result("clean-profile", "Paper Y", ["cat:cs.AI", "cat:cs.RO"])

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
            await on_run_complete(
                "clean-profile",
                config=config,
                client=client,
                sources_checked=5,
                ingested=2,
            )

        rejection_logs = [
            r
            for r in caplog.records
            if '"event": "influx.rejection_rate"' in r.getMessage()
        ]
        payload = json.loads(rejection_logs[0].getMessage())
        for rate in payload["rejection_rates"].values():
            assert rate == 0.0

    @pytest.mark.asyncio
    async def test_tag_store_cleared_after_emission(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After emission, tag store is cleared for next window."""
        config = _make_config(recalibrate_after_runs=1)
        client = _make_client(rejected_titles=[])

        # Run 1: record items and emit.
        record_filter_result("p", "Paper A", ["tag1"])
        await on_run_complete(
            "p", config=config, client=client, sources_checked=1, ingested=1
        )

        # Run 2: record different items and emit.
        record_filter_result("p", "Paper B", ["tag2"])
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
            await on_run_complete(
                "p", config=config, client=client, sources_checked=1, ingested=1
            )

        rejection_logs = [
            r
            for r in caplog.records
            if '"event": "influx.rejection_rate"' in r.getMessage()
        ]
        payload = json.loads(rejection_logs[0].getMessage())
        # Only tag2 should appear (tag1 was in the previous window).
        assert "tag2" in payload["rejection_rates"]
        assert "tag1" not in payload["rejection_rates"]


# ── (5) AC-M4-4: per-run filtered + ingested counts ──────────────────


class TestPerRunStructuredLog:
    """AC-M4-4: per-run structured logs include filtered and ingested counts."""

    @pytest.mark.asyncio
    async def test_per_run_stats_emitted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # High cadence to avoid rejection log.
        config = _make_config(recalibrate_after_runs=100)
        client = _make_client()

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
            await on_run_complete(
                "stats-profile",
                config=config,
                client=client,
                sources_checked=20,
                ingested=8,
            )

        stats_logs = [
            r for r in caplog.records if '"event": "influx.run.stats"' in r.getMessage()
        ]
        assert len(stats_logs) == 1
        payload = json.loads(stats_logs[0].getMessage())
        assert payload["event"] == "influx.run.stats"
        assert payload["profile"] == "stats-profile"
        assert payload["filtered"] == 12  # 20 - 8
        assert payload["ingested"] == 8
        assert payload["sources_checked"] == 20

    @pytest.mark.asyncio
    async def test_per_run_stats_emitted_every_run(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Stats log is emitted every run, not just at cadence boundaries."""
        config = _make_config(recalibrate_after_runs=5)
        client = _make_client()

        stats_count = 0
        for _ in range(3):
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="influx.rejection_rate"):
                await on_run_complete(
                    "p",
                    config=config,
                    client=client,
                    sources_checked=10,
                    ingested=3,
                )
            stats_logs = [
                r
                for r in caplog.records
                if '"event": "influx.run.stats"' in r.getMessage()
            ]
            stats_count += len(stats_logs)
        assert stats_count == 3
