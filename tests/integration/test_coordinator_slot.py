"""Tests for US-015: Coordinator slot integration (AC-06-E).

Verifies:
- The sweep runs inside the same per-profile coordinator slot already
  held by the scheduled or manual run path.
- The lock is held for the union of (sweep + normal fetch), NOT held
  twice for the same profile run.
- Two profiles A and B run serialised within their own slots while
  proceeding independently of each other.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator, Iterable
from typing import Any
from unittest.mock import AsyncMock

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
                name="profile-a",
                description="Profile A",
                thresholds=ProfileThresholds(notify_immediate=8),
            ),
            ProfileConfig(
                name="profile-b",
                description="Profile B",
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


class TestCoordinatorSlotIntegration:
    """US-015 / AC-06-E: sweep runs inside per-profile slot."""

    def test_sweep_runs_inside_coordinator_lock(
        self,
        fake_lithos_url: str,
    ) -> None:
        """The sweep and fetch both observe the coordinator lock held.

        The lock is held for the union of (sweep + normal fetch), not
        acquired twice.
        """
        config = _make_config(lithos_url=fake_lithos_url)
        app = _make_app(config)
        coordinator: Coordinator = app.state.coordinator

        lock_observations: list[bool] = []

        async def _spy_sweep(profile: str, **kwargs: Any) -> list[dict[str, Any]]:
            lock_observations.append(coordinator.is_busy(profile))
            return []

        async def _spy_provider(
            profile: str,
            kind: RunKind,
            run_range: dict[str, str | int] | None,
            filter_prompt: str,
        ) -> Iterable[dict[str, Any]]:
            lock_observations.append(coordinator.is_busy(profile))
            return []

        app.state.item_provider = _spy_provider

        from unittest.mock import patch

        with (
            patch("influx.scheduler.repair_sweep", side_effect=_spy_sweep),
            patch("influx.run.repair_sweep", side_effect=_spy_sweep),
            TestClient(app) as tc,
        ):
            resp = tc.post("/runs", json={"profile": "profile-a"})
            assert resp.status_code == 202
            _wait_for_idle(coordinator, "profile-a")

        # Both sweep and item_provider observed the lock as held.
        assert len(lock_observations) == 2
        assert all(lock_observations), (
            "Both sweep and fetch must see the coordinator lock held"
        )

    def test_lock_held_exactly_once_per_profile_run(
        self,
        fake_lithos_url: str,
    ) -> None:
        """The coordinator lock is acquired once, not twice (sweep+fetch).

        Tracks acquire/release events to prove the lock was held
        continuously, not released between sweep and fetch.
        """
        config = _make_config(lithos_url=fake_lithos_url)
        app = _make_app(config)
        coordinator: Coordinator = app.state.coordinator

        acquire_count = 0
        original_try_acquire = coordinator.try_acquire

        async def _counting_acquire(profile: str) -> bool:
            nonlocal acquire_count
            result = await original_try_acquire(profile)
            if result:
                acquire_count += 1
            return result

        coordinator.try_acquire = _counting_acquire  # type: ignore[assignment]

        sweep_mock = AsyncMock(return_value=[])

        from unittest.mock import patch

        with (
            patch("influx.scheduler.repair_sweep", sweep_mock),
            patch("influx.run.repair_sweep", sweep_mock),
            TestClient(app) as tc,
        ):
            resp = tc.post("/runs", json={"profile": "profile-a"})
            assert resp.status_code == 202
            _wait_for_idle(coordinator, "profile-a")

        # Lock acquired exactly once for the entire run.
        assert acquire_count == 1, f"Expected lock acquired once, got {acquire_count}"

    def test_two_profiles_run_independently(
        self,
        fake_lithos_url: str,
    ) -> None:
        """Profiles A and B run in their own slots independently.

        Sweep+fetch for A runs serialised within A's slot while B's
        run proceeds within B's slot.
        """
        config = _make_config(lithos_url=fake_lithos_url)
        app = _make_app(config)
        coordinator: Coordinator = app.state.coordinator

        # Track which profile's lock is held during each sweep call.
        sweep_observations: dict[str, dict[str, bool]] = {}

        async def _spy_sweep(profile: str, **kwargs: Any) -> list[dict[str, Any]]:
            sweep_observations[profile] = {
                "own_lock": coordinator.is_busy(profile),
            }
            return []

        sweep_mock = AsyncMock(side_effect=_spy_sweep)

        from unittest.mock import patch

        with (
            patch("influx.scheduler.repair_sweep", sweep_mock),
            patch("influx.run.repair_sweep", sweep_mock),
            TestClient(app) as tc,
        ):
            # Launch both profiles.
            resp_a = tc.post("/runs", json={"profile": "profile-a"})
            resp_b = tc.post("/runs", json={"profile": "profile-b"})
            assert resp_a.status_code == 202
            assert resp_b.status_code == 202
            _wait_for_idle(coordinator, "profile-a")
            _wait_for_idle(coordinator, "profile-b")

        # Both profiles' sweeps ran.
        assert "profile-a" in sweep_observations
        assert "profile-b" in sweep_observations

        # Each profile's sweep observed its OWN lock as held.
        assert sweep_observations["profile-a"]["own_lock"] is True
        assert sweep_observations["profile-b"]["own_lock"] is True

    def test_scheduled_fire_holds_lock_for_run_profile(
        self,
        fake_lithos_url: str,
    ) -> None:
        """Scheduled _fire_profile holds lock while run_profile runs.

        Patches run_profile to observe the coordinator lock state,
        verifying that the scheduled path holds the lock for the
        entire run_profile call (which includes the sweep).
        """
        config = _make_config(lithos_url=fake_lithos_url)
        coordinator = Coordinator()
        scheduler = InfluxScheduler(config, coordinator)

        lock_held_during_run: list[bool] = []

        async def _spy_run_profile(profile: str, kind: RunKind, **kwargs: Any) -> None:
            lock_held_during_run.append(coordinator.is_busy(profile))

        from unittest.mock import patch

        with patch(
            "influx.scheduler.run_profile",
            side_effect=_spy_run_profile,
        ):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(scheduler._fire_profile("profile-a"))
            finally:
                loop.close()

        # run_profile observed the lock as held.
        assert len(lock_held_during_run) == 1
        assert lock_held_during_run[0] is True

        # Lock is released after fire completes.
        assert not coordinator.is_busy("profile-a")
