"""Tests for US-014: Backfill exclusion — POST /backfills skips repair sweep.

Verifies:
- Positive: a scheduled run for a profile invokes repair.sweep exactly once
- AC-06-D: a POST /backfills request does NOT invoke repair.sweep
"""

from __future__ import annotations

import time
from collections.abc import Generator, Iterable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from influx.config import (
    AppConfig,
    FeedbackConfig,
    LithosConfig,
    NotificationsConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.coordinator import Coordinator, RunKind
from influx.http_api import router
from influx.probes import ProbeLoop
from influx.scheduler import InfluxScheduler
from tests.contract.test_lithos_client import FakeLithosServer

# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def fake_lithos() -> Generator[FakeLithosServer, None, None]:
    server = FakeLithosServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def fake_lithos_url(fake_lithos: FakeLithosServer) -> str:
    return f"http://127.0.0.1:{fake_lithos.port}/sse"


@pytest.fixture(autouse=True)
def clear_fakes(fake_lithos: FakeLithosServer) -> None:
    fake_lithos.calls.clear()
    fake_lithos.write_responses.clear()
    fake_lithos.read_responses.clear()
    fake_lithos.cache_lookup_responses.clear()
    fake_lithos.list_responses.clear()


# ── Helpers ────────────────────────────────────────────────────────


def _make_config(*, lithos_url: str) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="AI & Robotics",
                thresholds=ProfileThresholds(notify_immediate=8),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(
                text=(
                    "Filter: {profile_description} "
                    "{negative_examples} "
                    "{min_score_in_results}"
                ),
            ),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        feedback=FeedbackConfig(negative_examples_per_profile=20),
    )


def _make_item_provider(
    items: list[dict[str, Any]] | None = None,
) -> Any:
    async def provider(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> Iterable[dict[str, Any]]:
        del profile, kind, run_range, filter_prompt
        return list(items or [])

    return provider


def _make_app(config: AppConfig) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    coordinator = Coordinator()
    scheduler = InfluxScheduler(config, coordinator)
    probe_loop = ProbeLoop(config, interval=30.0)
    probe_loop.run_once()
    app.state.config = config
    app.state.coordinator = coordinator
    app.state.scheduler = scheduler
    app.state.probe_loop = probe_loop
    app.state.active_tasks = set()  # type: ignore[assignment]
    app.state.item_provider = _make_item_provider()
    return app


def _wait_for_idle(
    coordinator: Coordinator,
    profile: str,
    timeout: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not coordinator.is_busy(profile):
            return
        time.sleep(0.05)
    raise TimeoutError(f"Profile {profile!r} still busy after {timeout}s")


# ── Tests ──────────────────────────────────────────────────────────


class TestBackfillExclusion:
    """US-014: POST /backfills MUST NOT call the sweep."""

    def test_scheduled_run_invokes_sweep_once(
        self,
        fake_lithos_url: str,
    ) -> None:
        """Positive: a manual POST /runs invokes repair.sweep once."""
        config = _make_config(lithos_url=fake_lithos_url)
        app = _make_app(config)

        # MANUAL runs dispatch through ``influx.run.Run.execute()`` since
        # #59, so the sweep call lives at the ``influx.run`` binding.
        with patch(
            "influx.run.repair_sweep",
            new_callable=AsyncMock,
        ) as mock_sweep:
            with TestClient(app) as tc:
                resp = tc.post("/runs", json={"profile": "ai-robotics"})
                assert resp.status_code == 202
                _wait_for_idle(app.state.coordinator, "ai-robotics")

            mock_sweep.assert_called_once()
            call_args = mock_sweep.call_args
            assert call_args[0][0] == "ai-robotics"

    def test_backfill_does_not_invoke_sweep(
        self,
        fake_lithos_url: str,
    ) -> None:
        """AC-06-D: POST /backfills does NOT invoke repair.sweep."""
        config = _make_config(lithos_url=fake_lithos_url)
        app = _make_app(config)

        with patch(
            "influx.scheduler.repair_sweep",
            new_callable=AsyncMock,
        ) as mock_sweep:
            with TestClient(app) as tc:
                resp = tc.post(
                    "/backfills",
                    json={"profile": "ai-robotics", "days": 7, "confirm": True},
                )
                assert resp.status_code == 202
                _wait_for_idle(app.state.coordinator, "ai-robotics")

            mock_sweep.assert_not_called()

    def test_default_app_wiring_invokes_sweep(
        self,
        fake_lithos_url: str,
    ) -> None:
        """Finding #1: default ``create_app`` wiring runs the sweep.

        With the production-default arXiv item provider wired in, a
        manual run through ``POST /runs`` must still execute the repair
        sweep — production scheduled and manual runs cannot rely on a
        test-injected provider to enter the sweep code path.  Uses the
        real ``service.create_app`` factory rather than the test-only
        ``_make_app`` shim.
        """
        from influx.service import create_app

        config = _make_config(lithos_url=fake_lithos_url)
        # Disable the arXiv source so the default provider yields zero
        # items without needing a mocked HTTP layer; this test only
        # cares that the sweep runs, not that any items are written.
        for profile in config.profiles:
            profile.sources.arxiv.enabled = False

        app = create_app(config)
        # The default wiring now installs the production arXiv item
        # provider; with arXiv disabled it short-circuits to zero items.
        assert app.state.item_provider is not None

        with patch(
            "influx.run.repair_sweep",
            new_callable=AsyncMock,
        ) as mock_sweep:
            with TestClient(app) as tc:
                resp = tc.post("/runs", json={"profile": "ai-robotics"})
                assert resp.status_code == 202
                _wait_for_idle(app.state.coordinator, "ai-robotics")

            mock_sweep.assert_called_once()
            assert mock_sweep.call_args[0][0] == "ai-robotics"
