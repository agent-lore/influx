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
    initial_jitter_seconds: int = 0,
    inter_profile_gap_seconds: int = 0,
) -> AppConfig:
    """Build a minimal AppConfig for scheduler tests."""
    profile_names = profiles if profiles is not None else ["alpha", "beta"]
    profile_list = [ProfileConfig(name=name) for name in profile_names]
    return AppConfig(
        schedule=ScheduleConfig(
            cron=cron,
            timezone=timezone,
            misfire_grace_seconds=misfire_grace_seconds,
            initial_jitter_seconds=initial_jitter_seconds,
            inter_profile_gap_seconds=inter_profile_gap_seconds,
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
    async def test_single_tick_dispatcher_for_all_profiles(self) -> None:
        """One ``influx-tick`` dispatcher job is registered for the run.

        Review finding 1: per-profile dedup must be scoped to the entire
        cron tick.  The scheduler now registers a single dispatcher job
        that fans out to all profiles, so ``len(jobs) == 1`` regardless
        of profile count.
        """
        config = _make_config(profiles=["alpha", "beta"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)
        sched.start()
        try:
            assert len(sched.jobs) == 1
            assert sched.jobs[0].id == "influx-tick"
        finally:
            sched.stop()

    async def test_job_settings_max_instances_coalesce_misfire(self) -> None:
        """The cron-registered callable is the thin dispatcher, not the
        fan-out itself.  The dispatcher returns immediately, so APScheduler
        never gates a slow tick; same-profile non-overlap is enforced
        solely by the coordinator (review finding)."""
        config = _make_config(profiles=["alpha"], misfire_grace_seconds=7200)
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)
        sched.start()
        try:
            job = sched.jobs[0]
            assert job.id == "influx-tick"
            # Cron fires the thin dispatcher, NOT the fan-out body.
            # Bound-method identity is not stable across attribute access
            # (each access creates a fresh bound method), so verify the
            # underlying function and bound instance instead of using `is`.
            assert job.func.__self__ is sched
            assert job.func.__func__ is InfluxScheduler._cron_dispatch
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

    async def test_single_profile_one_dispatcher_job(self) -> None:
        config = _make_config(profiles=["solo"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)
        sched.start()
        try:
            assert len(sched.jobs) == 1
            assert sched.jobs[0].id == "influx-tick"
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
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            nonlocal lock_held_during_run
            lock_held_during_run = coord.is_busy(profile)

        with patch("influx.scheduler.run_profile", side_effect=spy_run_profile):
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
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            raise RuntimeError("boom")

        with (
            patch("influx.scheduler.run_profile", side_effect=failing_run),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await sched._fire_profile("alpha")

        assert coord.is_busy("alpha") is False


# ── Tick-overlap regression (review finding) ──────────────────────


class TestTickOverlapDoesNotBlockUnrelatedProfiles:
    """A slow profile in tick N must not block unrelated profiles in
    tick N+1.  Same-profile non-overlap is enforced by the coordinator,
    not by APScheduler's ``max_instances`` cap.
    """

    async def test_tick2_runs_profile_b_while_tick1_alpha_blocks(self) -> None:
        """Cross-tick non-blocking under #87 sequential semantics.

        Profiles within a single tick now run sequentially in declared
        config order, so beta from tick1 cannot overtake a stuck alpha
        in the same tick.  The cross-tick guarantee still holds though:
        tick2 fires while tick1's alpha is blocked, sees alpha as busy
        (skip via the coordinator), and proceeds to run beta — proving
        APScheduler is not the gate, only the coordinator is.
        """
        config = _make_config(profiles=["alpha", "beta"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        started: list[str] = []
        alpha_started = asyncio.Event()
        alpha_block = asyncio.Event()

        async def slow_run(
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            started.append(profile)
            if profile == "alpha":
                alpha_started.set()
                # alpha from tick1 stays in flight until released.
                await alpha_block.wait()

        with patch("influx.scheduler.run_profile", side_effect=slow_run):
            tick1 = asyncio.create_task(sched._fire_tick())
            # Wait for tick1's alpha to actually start and hold the lock.
            await asyncio.wait_for(alpha_started.wait(), timeout=1.0)
            assert coord.is_busy("alpha") is True
            assert coord.is_busy("beta") is False
            # Sequential: tick1's beta has NOT started yet — it queues
            # behind the still-running alpha.
            assert started == ["alpha"]

            # Tick2 fires while tick1's alpha is still running.  Its alpha
            # is skipped (busy); its beta runs because beta's lock is free.
            tick2 = asyncio.create_task(sched._fire_tick())
            await asyncio.wait_for(tick2, timeout=1.0)

            # tick2 ran beta exactly once, skipped alpha entirely.
            assert started.count("beta") == 1
            assert started.count("alpha") == 1
            assert coord.is_busy("alpha") is True

            # Release alpha so tick1 can finish (which then runs tick1's beta).
            alpha_block.set()
            await asyncio.wait_for(tick1, timeout=1.0)

        # By the time both ticks complete: alpha ran once (tick1, tick2 skipped),
        # beta ran twice (tick2 first while tick1 was blocked, tick1 after).
        assert started.count("alpha") == 1
        assert started.count("beta") == 2
        assert coord.is_busy("alpha") is False
        assert coord.is_busy("beta") is False

    async def test_cron_dispatch_returns_immediately_and_tracks_task(
        self,
    ) -> None:
        """``_cron_dispatch`` is a thin dispatcher: it spawns the fan-out
        task on ``active_tasks`` and returns.  Even with many overlapping
        ticks, no APScheduler instance slot is held for the fan-out — so
        only the coordinator gates same-profile non-overlap (review finding).
        """
        config = _make_config(profiles=["alpha", "beta", "gamma"])
        coord = Coordinator()
        active_tasks: set[asyncio.Task[Any]] = set()
        sched = InfluxScheduler(config, coord, active_tasks=active_tasks)

        # Pre-acquire alpha externally so EVERY tick's alpha sub-task
        # must be skipped via ProfileBusyError.  This proves the
        # coordinator — not APScheduler — is the gate: if APScheduler
        # were limiting overlap (e.g. ``max_instances`` saturating),
        # tick 3+ would not even reach the coordinator and beta/gamma
        # in those ticks would never run.
        assert await coord.try_acquire("alpha") is True

        started: list[str] = []

        async def fast_run(
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            started.append(profile)

        try:
            with patch("influx.scheduler.run_profile", side_effect=fast_run):
                # Fire FIVE overlapping ticks back-to-back.  The dispatcher
                # must return synchronously each time without waiting on
                # the fan-out.  If APScheduler were the long-running
                # overlap gate, tick 3+ would never make it past dispatch.
                tick_tasks: list[asyncio.Task[None]] = []
                for _ in range(5):
                    tick_tasks.append(await sched._cron_dispatch())

                # Five fan-out tasks were spawned and tracked on
                # active_tasks — i.e. the dispatcher created each task
                # and registered it without ever blocking on the fan-out.
                assert len(tick_tasks) == 5
                assert set(tick_tasks) == active_tasks

                # Drain so each tick's fan-out can run to completion.
                await asyncio.wait_for(asyncio.gather(*tick_tasks), timeout=2.0)

            # Beta and gamma ran on EVERY tick because the coordinator,
            # not APScheduler, decides what runs; alpha was skipped on
            # every tick because the external hold blocks the lock.
            assert started.count("alpha") == 0
            assert started.count("beta") == 5
            assert started.count("gamma") == 5
            # All fan-out tasks finished and unregistered from active_tasks.
            assert active_tasks == set()
            # alpha lock is still held by the external pre-acquire.
            assert coord.is_busy("alpha") is True
        finally:
            coord.release("alpha")
        assert coord.is_busy("alpha") is False

    async def test_per_tick_factory_isolates_fetch_cache_across_ticks(
        self,
    ) -> None:
        """Each tick gets a fresh fetch cache from the factory, so cron
        tick N+1's begin_fire scope does not see tick N's data even when
        the dispatcher runs concurrently."""
        config = _make_config(profiles=["alpha"])
        coord = Coordinator()

        class _CountingCache:
            def __init__(self) -> None:
                self.begin_count = 0
                self.end_count = 0

            def begin_fire(self) -> None:
                self.begin_count += 1

            def end_fire(self) -> None:
                self.end_count += 1

        produced_caches: list[_CountingCache] = []

        async def noop_provider(
            profile: str, kind: Any, run_range: Any, filter_prompt: str
        ) -> list[Any]:
            del profile, kind, run_range, filter_prompt
            return []

        def factory() -> tuple[Any, _CountingCache]:
            cache = _CountingCache()
            produced_caches.append(cache)
            return noop_provider, cache

        sched = InfluxScheduler(
            config,
            coord,
            item_provider_factory=factory,
        )

        async def fake_run(
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            del profile, kind, run_range

        with patch("influx.scheduler.run_profile", side_effect=fake_run):
            await sched._fire_tick()
            await sched._fire_tick()

        # Two ticks → two distinct caches, each begun + ended exactly once.
        assert len(produced_caches) == 2
        assert produced_caches[0] is not produced_caches[1]
        for cache in produced_caches:
            assert cache.begin_count == 1
            assert cache.end_count == 1


# ── Sequential within-tick fan-out + jitter / gap (issue #87) ──────


class TestSequentialFanOutWithJitterAndGap:
    """Issue #87: avoid arXiv hour-boundary 429 clusters.

    Profiles within a single cron tick now run **sequentially in
    declared config order** with an optional gap between them, after an
    optional initial jitter at the start of the tick.  Both knobs live
    under ``[schedule]`` and default to 0 for backwards-compatible
    behaviour.  The shared ``begin_fire`` / ``end_fire`` window around
    the whole fan-out is preserved so cross-profile fetch dedup
    (R-8 / AC-09-D) still holds.
    """

    async def test_profiles_run_in_declared_config_order(self) -> None:
        """Sequential, in the order profiles appear in ``[[profiles]]``."""
        config = _make_config(profiles=["alpha", "beta", "gamma"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        order: list[str] = []

        async def spy(profile: str, kind: Any, run_range: Any = None, **_: Any) -> None:
            order.append(profile)

        with patch("influx.scheduler.run_profile", side_effect=spy):
            await sched._fire_tick()

        assert order == ["alpha", "beta", "gamma"]

    async def test_initial_jitter_sleeps_before_first_profile(self) -> None:
        """``initial_jitter_seconds`` produces one sleep at tick start."""
        config = _make_config(
            profiles=["alpha", "beta"],
            initial_jitter_seconds=30,
        )
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        sleep_calls: list[float] = []
        order: list[str] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            order.append(f"sleep:{seconds}")

        async def spy(profile: str, kind: Any, run_range: Any = None, **_: Any) -> None:
            order.append(profile)

        with (
            patch(
                "influx.scheduler.random.uniform",
                return_value=17.0,
            ),
            patch("influx.scheduler.asyncio.sleep", side_effect=fake_sleep),
            patch("influx.scheduler.run_profile", side_effect=spy),
        ):
            await sched._fire_tick()

        # The jitter sleep is the first thing that happens, before any
        # profile runs.
        assert order[0] == "sleep:17.0"
        assert order[1:] == ["alpha", "beta"]
        # No inter-profile gap configured → only the jitter sleep fires.
        assert sleep_calls == [17.0]

    async def test_zero_jitter_skips_sleep(self) -> None:
        """``initial_jitter_seconds = 0`` does not call sleep."""
        config = _make_config(
            profiles=["alpha"],
            initial_jitter_seconds=0,
        )
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        async def spy(profile: str, kind: Any, run_range: Any = None, **_: Any) -> None:
            pass

        with (
            patch("influx.scheduler.asyncio.sleep", side_effect=fake_sleep),
            patch("influx.scheduler.run_profile", side_effect=spy),
        ):
            await sched._fire_tick()

        assert sleep_calls == []

    async def test_inter_profile_gap_sleeps_between_profiles(self) -> None:
        """``inter_profile_gap_seconds`` sleeps between each pair, not before
        the first profile and not after the last."""
        config = _make_config(
            profiles=["alpha", "beta", "gamma"],
            inter_profile_gap_seconds=42,
        )
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        events: list[str] = []

        async def fake_sleep(seconds: float) -> None:
            events.append(f"sleep:{seconds}")

        async def spy(profile: str, kind: Any, run_range: Any = None, **_: Any) -> None:
            events.append(profile)

        with (
            patch("influx.scheduler.asyncio.sleep", side_effect=fake_sleep),
            patch("influx.scheduler.run_profile", side_effect=spy),
        ):
            await sched._fire_tick()

        # Gap fires between profile pairs only — not before alpha,
        # not after gamma.
        assert events == [
            "alpha",
            "sleep:42",
            "beta",
            "sleep:42",
            "gamma",
        ]

    async def test_zero_gap_runs_back_to_back(self) -> None:
        """``inter_profile_gap_seconds = 0`` does not insert sleeps."""
        config = _make_config(
            profiles=["alpha", "beta"],
            inter_profile_gap_seconds=0,
        )
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        events: list[str] = []

        async def fake_sleep(seconds: float) -> None:
            events.append(f"sleep:{seconds}")

        async def spy(profile: str, kind: Any, run_range: Any = None, **_: Any) -> None:
            events.append(profile)

        with (
            patch("influx.scheduler.asyncio.sleep", side_effect=fake_sleep),
            patch("influx.scheduler.run_profile", side_effect=spy),
        ):
            await sched._fire_tick()

        assert events == ["alpha", "beta"]

    async def test_profile_failure_does_not_abort_remaining_profiles(
        self,
    ) -> None:
        """One profile raising must not skip subsequent profiles in the tick."""
        config = _make_config(profiles=["alpha", "beta", "gamma"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        ran: list[str] = []

        async def maybe_fail(
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            ran.append(profile)
            if profile == "beta":
                raise RuntimeError("beta exploded")

        with patch("influx.scheduler.run_profile", side_effect=maybe_fail):
            await sched._fire_tick()

        assert ran == ["alpha", "beta", "gamma"]
        # Locks released for every profile despite the middle one raising.
        for name in ("alpha", "beta", "gamma"):
            assert coord.is_busy(name) is False

    async def test_fetch_cache_window_brackets_full_sequential_fanout(
        self,
    ) -> None:
        """The single shared ``begin_fire`` / ``end_fire`` window is preserved.

        Sequential within-tick fan-out must still bracket all profiles
        in one cache scope so cross-profile fetch dedup (R-8, AC-09-D)
        keeps working.
        """
        config = _make_config(
            profiles=["alpha", "beta"],
            inter_profile_gap_seconds=0,
        )
        coord = Coordinator()

        events: list[str] = []

        class _RecordingCache:
            def begin_fire(self) -> None:
                events.append("begin_fire")

            def end_fire(self) -> None:
                events.append("end_fire")

        sched = InfluxScheduler(
            config,
            coord,
            fetch_cache=_RecordingCache(),
        )

        async def spy(profile: str, kind: Any, run_range: Any = None, **_: Any) -> None:
            events.append(profile)

        with patch("influx.scheduler.run_profile", side_effect=spy):
            await sched._fire_tick()

        assert events == ["begin_fire", "alpha", "beta", "end_fire"]


# ── Cross-profile parallelism (AC-M3-1) ────────────────────────────


class TestCrossProfileParallelism:
    async def test_two_profiles_fire_concurrently(self) -> None:
        """Both profiles execute via run_profile through the coordinator."""
        config = _make_config(profiles=["alpha", "beta"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        fired: list[str] = []

        async def spy_run_profile(
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            fired.append(profile)

        with patch("influx.scheduler.run_profile", side_effect=spy_run_profile):
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
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            nonlocal fired_count
            fired_count += 1
            await asyncio.sleep(0)  # Yield to let the other task attempt

        with patch("influx.scheduler.run_profile", side_effect=spy_run_profile):
            await asyncio.gather(
                sched._fire_profile("alpha"),
                sched._fire_profile("alpha"),
            )

        assert fired_count == 1


# ── SweepWriteError → readiness latch (US-011, finding #5) ───────────


class TestSweepWriteErrorMarksReadinessDegraded:
    """``SweepWriteError`` from the sweep flips the probe-loop latch."""

    async def test_sweep_write_error_marks_repair_failure(self) -> None:
        """When repair_sweep raises SweepWriteError, mark the probe latch."""
        from influx.repair import SweepWriteError

        config = _make_config(profiles=["alpha"])

        class _FakeProbeLoop:
            def __init__(self) -> None:
                self.marked = False
                self.cleared = False
                self.detail = ""

            def mark_repair_write_failure(
                self, *, profile: str = "", detail: str = ""
            ) -> None:
                self.marked = True
                self.detail = detail or profile

            def clear_repair_write_failure(self) -> None:
                self.cleared = True

        probe_loop = _FakeProbeLoop()

        async def _failing_sweep(*args: Any, **kwargs: Any) -> None:
            raise SweepWriteError(
                "abort",
                operation="lithos_write",
                detail="version_conflict_unresolved",
            )

        # Patch the LithosClient close so the test doesn't need a real
        # connection.
        class _NoopClient:
            async def close(self) -> None: ...

            async def list_archive_terminal_arxiv_ids(
                self, *, profile: str
            ) -> frozenset[str]:
                return frozenset()

            async def task_create(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                txt = _json.dumps({"task_id": "noop-task"})
                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(type="text", text=txt),
                    ],
                )

            async def task_create_body(self, **kwargs: Any) -> dict[str, Any]:
                del kwargs
                return {"task_id": "noop-task"}

            async def task_complete(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                txt = _json.dumps({"status": "completed"})
                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(type="text", text=txt),
                    ],
                )

        with (
            patch("influx.run.repair_sweep", side_effect=_failing_sweep),
            patch("influx.run.repair_sweep", side_effect=_failing_sweep),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            pytest.raises(SweepWriteError),
        ):
            await run_profile(
                "alpha",
                RunKind.SCHEDULED,
                config=config,
                item_provider=None,  # default provider used internally
                probe_loop=probe_loop,
            )

        assert probe_loop.marked is True
        assert probe_loop.cleared is False

    async def test_successful_sweep_clears_repair_failure(self) -> None:
        """Successful sweep clears the latch."""
        config = _make_config(profiles=["alpha"])

        class _FakeProbeLoop:
            def __init__(self) -> None:
                self.cleared = False

            def mark_repair_write_failure(
                self, *, profile: str = "", detail: str = ""
            ) -> None:
                pass

            def clear_repair_write_failure(self) -> None:
                self.cleared = True

        probe_loop = _FakeProbeLoop()

        async def _ok_sweep(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        class _NoopClient:
            async def close(self) -> None: ...

            async def list_archive_terminal_arxiv_ids(
                self, *, profile: str
            ) -> frozenset[str]:
                return frozenset()

            async def cache_lookup_for_item(self, **kwargs: Any) -> Any:
                # Should not be called — empty provider.
                raise AssertionError("unexpected cache lookup")

            async def task_create(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                txt = _json.dumps({"task_id": "noop-task"})
                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(type="text", text=txt),
                    ],
                )

            async def task_create_body(self, **kwargs: Any) -> dict[str, Any]:
                del kwargs
                return {"task_id": "noop-task"}

            async def task_complete(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                txt = _json.dumps({"status": "completed"})
                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(type="text", text=txt),
                    ],
                )

        # Patch build_negative_examples_block to a no-op so the run can
        # complete cleanly with the empty default item provider.
        async def _empty_neg_block(*args: Any, **kwargs: Any) -> str:
            return ""

        with (
            patch("influx.run.repair_sweep", side_effect=_ok_sweep),
            patch("influx.run.repair_sweep", side_effect=_ok_sweep),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=_empty_neg_block,
            ),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=_empty_neg_block,
            ),
            patch("influx.service.post_run_webhook_hook"),
        ):
            await run_profile(
                "alpha",
                RunKind.SCHEDULED,
                config=config,
                item_provider=None,
                probe_loop=probe_loop,
            )

        assert probe_loop.cleared is True

    async def test_backfill_does_not_touch_repair_latch(self) -> None:
        """Backfills skip the sweep entirely; latch is neither marked nor cleared."""
        config = _make_config(profiles=["alpha"])

        class _FakeProbeLoop:
            def __init__(self) -> None:
                self.marked = False
                self.cleared = False

            def mark_repair_write_failure(
                self, *, profile: str = "", detail: str = ""
            ) -> None:
                self.marked = True

            def clear_repair_write_failure(self) -> None:
                self.cleared = True

        probe_loop = _FakeProbeLoop()

        class _NoopClient:
            async def list_archive_terminal_arxiv_ids(
                self, *, profile: str
            ) -> frozenset[str]:
                return frozenset()

            async def task_create(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                txt = _json.dumps({"task_id": "noop-task"})
                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(type="text", text=txt),
                    ],
                )

            async def task_create_body(self, **kwargs: Any) -> dict[str, Any]:
                del kwargs
                return {"task_id": "noop-task"}

            async def task_complete(self, **kwargs: Any) -> Any: ...

            async def close(self) -> None: ...

        async def _empty_neg_block(*args: Any, **kwargs: Any) -> str:
            return ""

        with (
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=_empty_neg_block,
            ),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=_empty_neg_block,
            ),
            patch("influx.service.post_run_webhook_hook"),
        ):
            await run_profile(
                "alpha",
                RunKind.BACKFILL,
                {"days": 7},
                config=config,
                item_provider=None,
                probe_loop=probe_loop,
            )

        assert probe_loop.marked is False
        assert probe_loop.cleared is False


# ── Scheduled-fire repair_sweep invocation (US-014, finding #2) ──────


class TestScheduledFireInvokesRepairSweep:
    """Scheduled fires drive ``InfluxScheduler._fire_profile`` and run the sweep.

    Finding #2: the existing US-014 positive test only proves the manual
    ``POST /runs`` path.  This test drives the actual scheduled-fire
    code path (``_fire_profile`` calling ``run_profile`` with
    ``RunKind.SCHEDULED``) with a spy on ``repair_sweep`` and asserts
    exactly one call for the profile.
    """

    async def test_scheduled_fire_invokes_repair_sweep_once(self) -> None:
        config = _make_config(profiles=["alpha"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        sweep_calls: list[tuple[str, RunKind]] = []

        async def spy_sweep(profile: str, **kwargs: Any) -> list[Any]:
            # Capture the kind from the surrounding run.  We can infer
            # SCHEDULED from the call site (``_fire_profile`` always
            # passes ``RunKind.SCHEDULED``); record the profile name.
            sweep_calls.append((profile, RunKind.SCHEDULED))
            return []

        async def empty_neg_block(*args: Any, **kwargs: Any) -> str:
            return ""

        class _NoopClient:
            async def close(self) -> None: ...

            async def list_archive_terminal_arxiv_ids(
                self, *, profile: str
            ) -> frozenset[str]:
                return frozenset()

            async def task_create(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                txt = _json.dumps({"task_id": "noop-task"})
                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(type="text", text=txt),
                    ],
                )

            async def task_create_body(self, **kwargs: Any) -> dict[str, Any]:
                del kwargs
                return {"task_id": "noop-task"}

            async def task_complete(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                txt = _json.dumps({"status": "completed"})
                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(type="text", text=txt),
                    ],
                )

        with (
            patch("influx.run.repair_sweep", side_effect=spy_sweep),
            patch("influx.run.repair_sweep", side_effect=spy_sweep),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=empty_neg_block,
            ),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=empty_neg_block,
            ),
            patch("influx.service.post_run_webhook_hook"),
        ):
            await sched._fire_profile("alpha")

        assert sweep_calls == [("alpha", RunKind.SCHEDULED)]
        assert coord.is_busy("alpha") is False

    async def test_scheduled_fire_uses_run_kind_scheduled(self) -> None:
        """``_fire_profile`` calls ``run_profile`` with ``RunKind.SCHEDULED``."""
        config = _make_config(profiles=["alpha"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        observed_kind: list[RunKind] = []

        async def spy_run_profile(
            profile: str, kind: RunKind, run_range: Any = None, **_: Any
        ) -> None:
            observed_kind.append(kind)

        with patch(
            "influx.scheduler.run_profile",
            side_effect=spy_run_profile,
        ):
            await sched._fire_profile("alpha")

        assert observed_kind == [RunKind.SCHEDULED]


# ── AC-X-1: filter tunables actually shape behaviour ─────────────────


class TestNegativeExampleMaxTitleCharsWired:
    """``filter.negative_example_max_title_chars`` is threaded into
    ``build_negative_examples_block`` so the configured tunable
    actually shapes the rendered negative-examples block (AC-X-1)."""

    async def test_max_title_chars_passed_to_feedback_helper(self) -> None:
        from influx.config import FilterTuningConfig

        config = _make_config(profiles=["alpha"])
        # Replace the default filter tuning with a non-default value
        # so a test failure here can only be explained by scheduler
        # threading the configured value through.
        config = config.model_copy(
            update={
                "filter": FilterTuningConfig(negative_example_max_title_chars=42),
            }
        )

        captured_kwargs: list[dict[str, Any]] = []

        async def fake_neg_block(*args: Any, **kwargs: Any) -> str:
            captured_kwargs.append(kwargs)
            return ""

        class _NoopClient:
            async def close(self) -> None: ...

            async def list_archive_terminal_arxiv_ids(
                self, *, profile: str
            ) -> frozenset[str]:
                return frozenset()

            async def task_create(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                txt = _json.dumps({"task_id": "noop-task"})
                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(type="text", text=txt),
                    ],
                )

            async def task_create_body(self, **kwargs: Any) -> dict[str, Any]:
                del kwargs
                return {"task_id": "noop-task"}

            async def task_complete(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                txt = _json.dumps({"status": "completed"})
                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(type="text", text=txt),
                    ],
                )

        async def _ok_sweep(*args: Any, **kwargs: Any) -> list[Any]:
            return []

        with (
            patch("influx.run.repair_sweep", side_effect=_ok_sweep),
            patch("influx.run.repair_sweep", side_effect=_ok_sweep),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=fake_neg_block,
            ),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=fake_neg_block,
            ),
            patch("influx.service.post_run_webhook_hook"),
        ):
            await run_profile(
                "alpha",
                RunKind.SCHEDULED,
                config=config,
                item_provider=None,
            )

        assert captured_kwargs, "build_negative_examples_block was not called"
        assert captured_kwargs[0]["max_title_chars"] == 42


# ── #40: Lithos circuit breaker short-circuit ─────────────────────────


class TestLithosCircuitBreakerShortCircuit:
    """``run_profile`` short-circuits when ``probe_loop.lithos_circuit_open()``."""

    async def test_short_circuits_without_calling_provider(self, tmp_path: Any) -> None:
        """Open breaker → no provider invocation, ledger entry is ``skipped``."""
        from influx.run_ledger import RunLedger

        config = _make_config(profiles=["staging-robotics"])
        # Override storage so the ledger writes under tmp_path.
        config = config.model_copy(
            update={
                "storage": config.storage.model_copy(
                    update={"state_dir": str(tmp_path)}
                )
            }
        )

        provider_called = False

        async def spy_provider(
            profile: str,
            kind: RunKind,
            run_range: dict[str, str | int] | None,
            filter_prompt: str,
        ) -> list[dict[str, Any]]:
            nonlocal provider_called
            provider_called = True
            return []

        # Stub probe loop reporting the breaker open.
        class StubProbeLoop:
            lithos_unhealthy_consecutive = 5

            def lithos_circuit_open(self, *, threshold: int = 3) -> bool:
                return True

        ledger = RunLedger(tmp_path)
        result = await run_profile(
            "staging-robotics",
            RunKind.SCHEDULED,
            config=config,
            item_provider=spy_provider,
            probe_loop=StubProbeLoop(),
            run_ledger=ledger,
        )

        assert result is None
        assert provider_called is False, (
            "Lithos circuit breaker must short-circuit BEFORE the item "
            "provider runs — otherwise we burn LLM tokens against a "
            "write path that will fail"
        )
        # The ledger entry must reflect the skip.
        entries = ledger.recent()
        assert len(entries) == 1
        assert entries[0]["status"] == "skipped"
        assert entries[0]["error"] == "lithos_unhealthy"

    async def test_breaker_closed_proceeds_normally(self, tmp_path: Any) -> None:
        """Closed breaker → provider IS called (existing path unchanged)."""
        from influx.run_ledger import RunLedger

        config = _make_config(profiles=["staging-robotics"])
        config = config.model_copy(
            update={
                "storage": config.storage.model_copy(
                    update={"state_dir": str(tmp_path)}
                )
            }
        )

        provider_called = False

        async def spy_provider(
            profile: str,
            kind: RunKind,
            run_range: dict[str, str | int] | None,
            filter_prompt: str,
        ) -> list[dict[str, Any]]:
            nonlocal provider_called
            provider_called = True
            return []

        class StubProbeLoop:
            lithos_unhealthy_consecutive = 0

            def lithos_circuit_open(self, *, threshold: int = 3) -> bool:
                return False

        ledger = RunLedger(tmp_path)
        # The body still calls into LithosClient; the test would need
        # full mocking to run end-to-end.  Here we only assert the
        # short-circuit DOES NOT fire — the body's normal failure path
        # is exercised in the fuller integration tests.
        import contextlib

        from influx.errors import ConfigError, LCMAError, LithosError

        # Expected: the body tries to connect to a real Lithos URL which
        # isn't available in this unit test.  Either of the raised types
        # is acceptable; what matters is that we got past the breaker.
        with contextlib.suppress(
            ConfigError, LCMAError, LithosError, ConnectionError, OSError
        ):
            await run_profile(
                "staging-robotics",
                RunKind.SCHEDULED,
                config=config,
                item_provider=spy_provider,
                probe_loop=StubProbeLoop(),
                run_ledger=ledger,
            )

        # The ledger entry exists and is NOT skipped (the short-circuit
        # would have written a ``skipped`` row before any error).
        entries = ledger.recent()
        # Either a failed entry or no terminal entry yet — neither
        # should be ``skipped``.
        assert all(e.get("status") != "skipped" for e in entries)


class TestLcmaToolsUnavailableShortCircuit:
    """``run_profile`` skips when the LCMA-tools probe latch is set (#69).

    Probe-time tool-availability check (`lcma_tools_unavailable()`)
    replaces the legacy mid-run `LCMAError("unknown_tool")` latch.
    """

    async def test_skips_when_lcma_tools_latch_set(self, tmp_path: Any) -> None:
        """Latch set → no provider invocation, ledger reflects the skip."""
        from influx.run_ledger import RunLedger

        config = _make_config(profiles=["staging-robotics"])
        config = config.model_copy(
            update={
                "storage": config.storage.model_copy(
                    update={"state_dir": str(tmp_path)}
                )
            }
        )

        provider_called = False

        async def spy_provider(
            profile: str,
            kind: RunKind,
            run_range: dict[str, str | int] | None,
            filter_prompt: str,
        ) -> list[dict[str, Any]]:
            nonlocal provider_called
            provider_called = True
            return []

        class StubProbeLoop:
            lithos_unhealthy_consecutive = 0

            def lithos_circuit_open(self, *, threshold: int = 3) -> bool:
                return False

            def lcma_tools_unavailable(self) -> bool:
                return True

        ledger = RunLedger(tmp_path)
        result = await run_profile(
            "staging-robotics",
            RunKind.SCHEDULED,
            config=config,
            item_provider=spy_provider,
            probe_loop=StubProbeLoop(),
            run_ledger=ledger,
        )

        assert result is None
        assert provider_called is False, (
            "LCMA-tools-unavailable latch must short-circuit BEFORE the "
            "item provider runs — the deployment is misconfigured and any "
            "lithos_task_create call would fail anyway."
        )
        entries = ledger.recent()
        assert len(entries) == 1
        assert entries[0]["status"] == "skipped"
        assert entries[0]["error"] == "lcma_tools_unavailable"


class TestManualRunDispatchesToRunModule:
    """Manual runs (#59) dispatch through ``influx.run.Run.execute()``.

    Replaces the legacy ``_run_profile_body`` inline path for
    ``RunKind.MANUAL``; the Run module's five-stage executor now owns
    the body for both scheduled and manual runs.  Backfills stay on
    the legacy path until #60.
    """

    async def test_manual_run_routes_through_run_module(self, tmp_path: Any) -> None:
        """A manual run hits ``influx.run`` bindings, not the legacy body."""
        from influx.run_ledger import RunLedger

        config = _make_config(profiles=["alpha"])
        config = config.model_copy(
            update={
                "storage": config.storage.model_copy(
                    update={"state_dir": str(tmp_path)}
                )
            }
        )

        legacy_sweep_called = False
        run_sweep_called = False

        async def _legacy_sweep(*args: Any, **kwargs: Any) -> list[Any]:
            nonlocal legacy_sweep_called
            legacy_sweep_called = True
            return []

        async def _run_sweep(*args: Any, **kwargs: Any) -> list[Any]:
            nonlocal run_sweep_called
            run_sweep_called = True
            return []

        class _NoopClient:
            async def close(self) -> None: ...

            async def list_archive_terminal_arxiv_ids(
                self, *, profile: str
            ) -> frozenset[str]:
                return frozenset()

            async def task_create(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(
                            type="text",
                            text=_json.dumps({"task_id": "manual-task"}),
                        ),
                    ],
                )

            async def task_create_body(self, **kwargs: Any) -> dict[str, Any]:
                del kwargs
                return {"task_id": "manual-task"}

            async def task_complete(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(
                            type="text",
                            text=_json.dumps({"status": "completed"}),
                        ),
                    ],
                )

        async def _empty_neg_block(*args: Any, **kwargs: Any) -> str:
            return ""

        ledger = RunLedger(tmp_path)
        with (
            patch("influx.run.repair_sweep", side_effect=_legacy_sweep),
            patch("influx.run.repair_sweep", side_effect=_run_sweep),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=_empty_neg_block,
            ),
            patch("influx.service.post_run_webhook_hook"),
        ):
            await run_profile(
                "alpha",
                RunKind.MANUAL,
                config=config,
                item_provider=None,
                run_ledger=ledger,
            )

        # The Run module's repair sweep ran; the legacy scheduler.repair_sweep
        # was NOT called -- proving the dispatch routed manual runs through
        # influx.run rather than the legacy inline body.
        assert run_sweep_called is True, (
            "Manual runs must dispatch through influx.run.Run.execute()"
        )
        assert legacy_sweep_called is False, (
            "The legacy _run_profile_body path must not run for manual kind"
        )


class TestBackfillRunDispatchesToRunModule:
    """Backfills (#60) dispatch through ``influx.run.Run.execute()``.

    Uses ``RunPlan(skip_repair=True, skip_cache_hits=True,
    notify=False)`` so the Run module's stages skip the repair
    sweep, skip cache-hit items, and skip the post-run webhook.
    """

    async def test_backfill_routes_through_run_module_with_correct_flags(
        self, tmp_path: Any
    ) -> None:
        """A backfill builds a backfill-shaped RunPlan and skips repair."""
        from influx.run_ledger import RunLedger

        config = _make_config(profiles=["alpha"])
        config = config.model_copy(
            update={
                "storage": config.storage.model_copy(
                    update={"state_dir": str(tmp_path)}
                )
            }
        )

        run_sweep_called = False

        async def _run_sweep(*args: Any, **kwargs: Any) -> list[Any]:
            nonlocal run_sweep_called
            run_sweep_called = True
            return []

        webhook_called = False

        def _webhook(*args: Any, **kwargs: Any) -> None:
            nonlocal webhook_called
            webhook_called = True

        class _NoopClient:
            async def close(self) -> None: ...

            async def list_archive_terminal_arxiv_ids(
                self, *, profile: str
            ) -> frozenset[str]:
                return frozenset()

            async def task_create(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(
                            type="text",
                            text=_json.dumps({"task_id": "bf-task"}),
                        ),
                    ],
                )

            async def task_create_body(self, **kwargs: Any) -> dict[str, Any]:
                del kwargs
                return {"task_id": "bf-task"}

            async def task_complete(self, **kwargs: Any) -> Any:
                import json as _json

                from mcp import types as _mcp_types

                return _mcp_types.CallToolResult(
                    content=[
                        _mcp_types.TextContent(
                            type="text",
                            text=_json.dumps({"status": "completed"}),
                        ),
                    ],
                )

        async def _empty_neg_block(*args: Any, **kwargs: Any) -> str:
            return ""

        ledger = RunLedger(tmp_path)
        with (
            patch("influx.run.repair_sweep", side_effect=_run_sweep),
            patch("influx.run.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.run.build_negative_examples_block",
                side_effect=_empty_neg_block,
            ),
            patch("influx.service.post_run_webhook_hook", side_effect=_webhook),
        ):
            await run_profile(
                "alpha",
                RunKind.BACKFILL,
                run_range={"days": 7},
                config=config,
                item_provider=None,
                run_ledger=ledger,
            )

        # FR-REP-2: backfill skips repair sweep entirely.
        assert run_sweep_called is False, (
            "Backfill must build a RunPlan with skip_repair=True so "
            "the repair stage is bypassed."
        )
        # FR-NOT-4: backfill skips the post-run webhook.
        assert webhook_called is False, (
            "Backfill must build a RunPlan with notify=False so the "
            "Finalise stage doesn't call post_run_webhook_hook."
        )
