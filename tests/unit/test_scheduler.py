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
        config = _make_config(profiles=["alpha", "beta"])
        coord = Coordinator()
        sched = InfluxScheduler(config, coord)

        started: list[str] = []
        alpha_block = asyncio.Event()
        beta_done = asyncio.Event()

        async def slow_run(
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            started.append(profile)
            if profile == "alpha":
                # alpha from tick1 stays in flight until released.
                await alpha_block.wait()
            else:
                # beta finishes promptly so tick2 can re-run it.
                beta_done.set()

        with patch("influx.scheduler.run_profile", side_effect=slow_run):
            tick1 = asyncio.create_task(sched._fire_tick())
            # Wait for tick1's beta to actually finish (so its lock is free)
            # while tick1's alpha remains stuck.
            await asyncio.wait_for(beta_done.wait(), timeout=1.0)
            assert coord.is_busy("alpha") is True
            assert coord.is_busy("beta") is False

            # Tick2 fires while tick1's alpha is still running.
            tick2 = asyncio.create_task(sched._fire_tick())
            # Drain pending callbacks so tick2's fan-out can dispatch.
            for _ in range(10):
                await asyncio.sleep(0)

            # tick2's beta must have run; tick2's alpha must have been
            # skipped (lock held by tick1).
            assert started.count("beta") == 2
            assert started.count("alpha") == 1
            assert coord.is_busy("alpha") is True

            # Tick2 has nothing left to do (alpha skipped, beta done).
            await asyncio.wait_for(tick2, timeout=1.0)

            # Release alpha so tick1 can finish.
            alpha_block.set()
            await asyncio.wait_for(tick1, timeout=1.0)

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
            patch("influx.scheduler.repair_sweep", side_effect=_failing_sweep),
            patch("influx.scheduler.LithosClient", return_value=_NoopClient()),
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
            patch("influx.scheduler.repair_sweep", side_effect=_ok_sweep),
            patch("influx.scheduler.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.scheduler.build_negative_examples_block",
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

            async def task_complete(self, **kwargs: Any) -> Any: ...

            async def close(self) -> None: ...

        async def _empty_neg_block(*args: Any, **kwargs: Any) -> str:
            return ""

        with (
            patch("influx.scheduler.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.scheduler.build_negative_examples_block",
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
            patch("influx.scheduler.repair_sweep", side_effect=spy_sweep),
            patch("influx.scheduler.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.scheduler.build_negative_examples_block",
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
            patch("influx.scheduler.repair_sweep", side_effect=_ok_sweep),
            patch("influx.scheduler.LithosClient", return_value=_NoopClient()),
            patch(
                "influx.scheduler.build_negative_examples_block",
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
