"""Tests for the APScheduler setup (US-003)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.coordinator import Coordinator, RunKind
from influx.scheduler import InfluxScheduler, run_profile


def _make_config(
    profiles: list[str] | None = None,
    cron: str = "0 6 * * *",
    timezone: str = "UTC",
    misfire_grace_seconds: int = 3600,
) -> AppConfig:
    """Build a minimal AppConfig for scheduler tests."""
    profile_names = profiles if profiles is not None else ["alpha", "beta"]
    profile_list = [ProfileConfig(name=name) for name in profile_names]
    return AppConfig(
        schedule=ScheduleConfig(
            cron=cron,
            timezone=timezone,
            misfire_grace_seconds=misfire_grace_seconds,
        ),
        profiles=profile_list,
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="test"),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
    )


# ── run_profile stub ────────────────────────────────────────────────


class TestRunProfileStub:
    async def test_run_profile_is_async_noop(self) -> None:
        """run_profile is an async no-op stub in PRD 03."""
        result = await run_profile("alpha", RunKind.SCHEDULED)
        assert result is None

    async def test_run_profile_accepts_range_param(self) -> None:
        """run_profile accepts an optional run_range for backfills."""
        result = await run_profile("alpha", RunKind.BACKFILL, {"days": 7})
        assert result is None


# ── Job registration ────────────────────────────────────────────────


class TestJobRegistration:
    async def test_one_job_per_profile(self) -> None:
        """One scheduler job is registered per profile (AC-M1-3)."""
        config = _make_config(profiles=["alpha", "beta"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)
        sched.start()
        try:
            assert len(sched.jobs) == 2
            job_ids = {j.id for j in sched.jobs}
            assert job_ids == {"profile-alpha", "profile-beta"}
        finally:
            sched.stop()

    async def test_job_settings_max_instances_coalesce_misfire(self) -> None:
        """Jobs use max_instances=1, coalesce=True, misfire from config."""
        config = _make_config(profiles=["alpha"], misfire_grace_seconds=7200)
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)
        sched.start()
        try:
            job = sched.jobs[0]
            assert job.max_instances == 1
            assert job.coalesce is True
            assert job.misfire_grace_time == 7200
        finally:
            sched.stop()

    async def test_no_profiles_yields_no_jobs(self) -> None:
        """An empty profile list produces zero scheduler jobs."""
        config = _make_config(profiles=[])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)
        sched.start()
        try:
            assert len(sched.jobs) == 0
        finally:
            sched.stop()

    async def test_single_profile_one_job(self) -> None:
        config = _make_config(profiles=["solo"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)
        sched.start()
        try:
            assert len(sched.jobs) == 1
            assert sched.jobs[0].id == "profile-solo"
        finally:
            sched.stop()


# ── Lock integration ────────────────────────────────────────────────


class TestSchedulerLockIntegration:
    async def test_fire_acquires_and_releases_lock(self) -> None:
        """Scheduled fire acquires lock around run_profile, then releases."""
        config = _make_config(profiles=["alpha"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        lock_held_during_run = False

        async def spy_run_profile(
            profile: str, kind: Any, run_range: Any = None
        ) -> None:
            nonlocal lock_held_during_run
            lock_held_during_run = coord.is_busy(profile)

        with patch(
            "influx.scheduler.run_profile", side_effect=spy_run_profile
        ):
            await sched._fire_profile("alpha")

        assert lock_held_during_run is True
        assert coord.is_busy("alpha") is False

    async def test_fire_conflict_does_not_crash(self) -> None:
        """Same-profile lock conflict is handled without crashing (FR-SCHED-3)."""
        config = _make_config(profiles=["alpha"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        # Pre-acquire the lock to simulate an in-flight run.
        await coord.try_acquire("alpha")
        assert coord.is_busy("alpha") is True

        # Fire should NOT raise — ProfileBusyError is caught.
        await sched._fire_profile("alpha")

        # Lock still held by the original acquirer.
        assert coord.is_busy("alpha") is True
        coord.release("alpha")

    async def test_fire_releases_lock_on_run_profile_error(self) -> None:
        """Lock is released even when run_profile raises."""
        config = _make_config(profiles=["alpha"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        async def failing_run(
            profile: str, kind: Any, run_range: Any = None
        ) -> None:
            raise RuntimeError("boom")

        with (
            patch("influx.scheduler.run_profile", side_effect=failing_run),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await sched._fire_profile("alpha")

        assert coord.is_busy("alpha") is False


# ── Cross-profile parallelism (AC-M3-1) ────────────────────────────


class TestCrossProfileParallelism:
    async def test_two_profiles_fire_concurrently(self) -> None:
        """Both profiles execute via run_profile through the coordinator."""
        config = _make_config(profiles=["alpha", "beta"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        fired: list[str] = []

        async def spy_run_profile(
            profile: str, kind: Any, run_range: Any = None
        ) -> None:
            fired.append(profile)

        with patch(
            "influx.scheduler.run_profile", side_effect=spy_run_profile
        ):
            await asyncio.gather(
                sched._fire_profile("alpha"),
                sched._fire_profile("beta"),
            )

        assert sorted(fired) == ["alpha", "beta"]
        assert coord.is_busy("alpha") is False
        assert coord.is_busy("beta") is False

    async def test_same_profile_overlap_one_accepted(self) -> None:
        """Two fires for the same profile -> exactly one execution (FR-SCHED-3)."""
        config = _make_config(profiles=["alpha"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        fired_count = 0

        async def spy_run_profile(
            profile: str, kind: Any, run_range: Any = None
        ) -> None:
            nonlocal fired_count
            fired_count += 1
            await asyncio.sleep(0)  # Yield to let the other task attempt

        with patch(
            "influx.scheduler.run_profile", side_effect=spy_run_profile
        ):
            await asyncio.gather(
                sched._fire_profile("alpha"),
                sched._fire_profile("alpha"),
            )

        assert fired_count == 1
