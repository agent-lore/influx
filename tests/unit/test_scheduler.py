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
