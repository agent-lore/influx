"""Unit tests for the service app factory, lifecycle, and bind guard (US-007).

Covers AC-03-D (bind guard), app factory wiring, and start/stop lifecycle.
"""

from __future__ import annotations

from typing import Any

import pytest

from influx.config import (
    AppConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.errors import ConfigError
from influx.service import (
    InfluxService,
    create_app,
    resolve_bind_address,
    validate_bind_host,
)


def _make_config(
    profiles: list[str] | None = None,
    providers: dict[str, Any] | None = None,
    security: SecurityConfig | None = None,
    schedule: ScheduleConfig | None = None,
) -> AppConfig:
    """Build a minimal AppConfig for service tests."""
    profile_names = profiles if profiles is not None else ["ai-robotics"]
    profile_list = [ProfileConfig(name=name) for name in profile_names]
    return AppConfig(
        schedule=schedule or ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=profile_list,
        providers=providers or {},
        security=security or SecurityConfig(),
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="test"),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
    )


# ── Bind guard (AC-03-D) ────────────────────────────────────────────


class TestValidateBindHost:
    """AC-03-D: refuse non-loopback bind host unless allow_remote_admin."""

    def test_loopback_ipv4_accepted(self) -> None:
        """127.0.0.1 is accepted without allow_remote_admin."""
        validate_bind_host("127.0.0.1", allow_remote_admin=False)

    def test_loopback_ipv6_accepted(self) -> None:
        """::1 is accepted without allow_remote_admin."""
        validate_bind_host("::1", allow_remote_admin=False)

    def test_non_loopback_refused_without_flag(self) -> None:
        """0.0.0.0 without allow_remote_admin → ConfigError (AC-03-D)."""
        with pytest.raises(ConfigError, match="not a loopback"):
            validate_bind_host("0.0.0.0", allow_remote_admin=False)

    def test_non_loopback_accepted_with_flag(self) -> None:
        """0.0.0.0 with allow_remote_admin=true → accepted (AC-03-D negative)."""
        validate_bind_host("0.0.0.0", allow_remote_admin=True)

    def test_external_ip_refused(self) -> None:
        """An explicit external IP is refused without the flag."""
        with pytest.raises(ConfigError, match="not a loopback"):
            validate_bind_host("192.168.1.1", allow_remote_admin=False)

    def test_external_ip_accepted_with_flag(self) -> None:
        """An explicit external IP is accepted with the flag."""
        validate_bind_host("192.168.1.1", allow_remote_admin=True)


# ── resolve_bind_address ─────────────────────────────────────────────


class TestResolveBindAddress:
    """Bind address reads from env vars with defaults."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without env vars, defaults to 127.0.0.1:8080."""
        monkeypatch.delenv("INFLUX_ADMIN_BIND_HOST", raising=False)
        monkeypatch.delenv("INFLUX_ADMIN_PORT", raising=False)
        host, port = resolve_bind_address()
        assert host == "127.0.0.1"
        assert port == 8080

    def test_custom_host_and_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env vars override defaults."""
        monkeypatch.setenv("INFLUX_ADMIN_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("INFLUX_ADMIN_PORT", "9090")
        host, port = resolve_bind_address()
        assert host == "0.0.0.0"
        assert port == 9090

    def test_invalid_port_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-integer port → ConfigError."""
        monkeypatch.setenv("INFLUX_ADMIN_PORT", "abc")
        with pytest.raises(ConfigError, match="not a valid integer"):
            resolve_bind_address()


# ── create_app ───────────────────────────────────────────────────────


class TestCreateApp:
    """App factory wires all dependencies onto app.state."""

    def test_app_state_has_dependencies(self) -> None:
        """create_app populates config, coordinator, scheduler, probe_loop."""
        config = _make_config()
        app = create_app(config)
        assert app.state.config is config
        assert app.state.coordinator is not None
        assert app.state.scheduler is not None
        assert app.state.probe_loop is not None

    def test_app_includes_router_routes(self) -> None:
        """The app includes /live, /ready, /status, /runs, /backfills routes."""
        config = _make_config()
        app = create_app(config)
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/live" in paths
        assert "/ready" in paths
        assert "/status" in paths
        assert "/runs" in paths
        assert "/backfills" in paths


# ── InfluxService lifecycle ──────────────────────────────────────────


class TestInfluxService:
    """InfluxService start/stop lifecycle."""

    def test_service_exposes_app(self) -> None:
        """Service .app is a FastAPI instance."""
        config = _make_config()
        svc = InfluxService(config)
        assert svc.app is not None
        assert svc.config is config

    async def test_start_and_stop(self) -> None:
        """Service can start and stop cleanly."""
        config = _make_config()
        svc = InfluxService(config)

        await svc.start()
        # Scheduler should have registered jobs.
        assert len(svc.scheduler.jobs) > 0
        # Probe loop should have run at least once (state is populated).
        assert svc.probe_loop.state.overall_status != "starting"

        await svc.stop()
        # Scheduler should be shut down — no more jobs.
        assert len(svc.scheduler.jobs) == 0

    async def test_double_start_is_idempotent(self) -> None:
        """Calling start() twice does not crash."""
        config = _make_config()
        svc = InfluxService(config)
        await svc.start()
        await svc.start()  # should be a no-op
        await svc.stop()

    async def test_stop_without_start_is_safe(self) -> None:
        """Calling stop() before start() does not crash."""
        config = _make_config()
        svc = InfluxService(config)
        await svc.stop()  # should be a no-op

    async def test_stop_awaits_in_flight_http_tasks(self) -> None:
        """Shutdown waits for HTTP-triggered tasks to complete within grace."""
        import asyncio

        config = _make_config(
            schedule=ScheduleConfig(shutdown_grace_seconds=2),
        )
        svc = InfluxService(config)
        await svc.start()

        completed = asyncio.Event()
        cancel_observed = asyncio.Event()

        async def in_flight_work() -> None:
            try:
                # Simulate quick HTTP-triggered work that finishes
                # well inside the grace window.
                await asyncio.sleep(0.1)
                completed.set()
            except asyncio.CancelledError:
                cancel_observed.set()
                raise

        task = asyncio.get_event_loop().create_task(in_flight_work())
        svc.app.state.active_tasks.add(task)
        task.add_done_callback(svc.app.state.active_tasks.discard)

        await svc.stop()

        assert completed.is_set(), "Task should have finished within the grace window"
        assert not cancel_observed.is_set(), (
            "Task should not have been cancelled within the grace window"
        )

    async def test_stop_awaits_http_triggered_work_on_real_app(self) -> None:
        """Regression for Finding 1: ``_spawn_tracked_task`` must register
        HTTP-triggered tasks on the existing (possibly empty) set on
        ``app.state`` so ``InfluxService.stop`` can actually await them.
        """
        import asyncio
        from unittest.mock import patch

        import httpx

        config = _make_config(
            schedule=ScheduleConfig(shutdown_grace_seconds=2),
        )
        svc = InfluxService(config)
        await svc.start()

        completed = asyncio.Event()
        cancel_observed = asyncio.Event()

        async def slow_run_profile(
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            try:
                await asyncio.sleep(0.1)  # well inside the grace window
                completed.set()
            except asyncio.CancelledError:
                cancel_observed.set()
                raise

        transport = httpx.ASGITransport(app=svc.app)
        with patch("influx.http_api.run_profile", slow_run_profile):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post("/runs", json={"profile": "ai-robotics"})
                assert resp.status_code == 202

            # Let the spawned task actually start.
            await asyncio.sleep(0)

            # The real bug: with the empty-set replacement, this would
            # stay at 0 because the task went into a throwaway local set.
            assert len(svc.app.state.active_tasks) >= 1, (
                "_spawn_tracked_task must register task on app.state.active_tasks"
            )

            await svc.stop()

        assert completed.is_set(), (
            "HTTP-triggered task should finish within shutdown grace window"
        )
        assert not cancel_observed.is_set(), (
            "HTTP-triggered task should not be cancelled within grace window"
        )

    async def test_stop_awaits_scheduler_fire_within_grace(self) -> None:
        """Regression for Finding 2: scheduled fires must be awaited up to
        ``schedule.shutdown_grace_seconds`` instead of cancelled immediately.
        """
        import asyncio
        from unittest.mock import patch

        config = _make_config(
            schedule=ScheduleConfig(shutdown_grace_seconds=2),
        )
        svc = InfluxService(config)
        await svc.start()

        fired = asyncio.Event()
        completed = asyncio.Event()
        cancel_observed = asyncio.Event()

        async def long_run_profile(
            profile: str, kind: Any, run_range: Any = None, **_: Any
        ) -> None:
            fired.set()
            try:
                await asyncio.sleep(0.2)  # well inside the grace window
                completed.set()
            except asyncio.CancelledError:
                cancel_observed.set()
                raise

        with patch("influx.scheduler.run_profile", long_run_profile):
            # Directly invoke _cron_dispatch to simulate a scheduler fire
            # — avoids waiting for cron while still exercising the real
            # tick-dispatcher path that registers the fan-out task on
            # active_tasks (review finding 1).  ``_cron_dispatch`` returns
            # the spawned background task; that task is what shutdown
            # must await within the grace window.
            fire_task = await svc.scheduler._cron_dispatch()
            await fired.wait()

            # Task must be tracked so shutdown can await it.
            assert fire_task in svc.app.state.active_tasks

            await svc.stop()

        assert completed.is_set(), (
            "Scheduled fire should complete within the grace window, "
            "not be cancelled immediately"
        )
        assert not cancel_observed.is_set(), (
            "Scheduled fire should NOT observe CancelledError within grace"
        )

    async def test_stop_cancels_tasks_that_exceed_grace(self) -> None:
        """Tasks that exceed schedule.shutdown_grace_seconds are cancelled."""
        import asyncio

        # Bound shutdown tightly so the test runs fast but the task
        # deliberately exceeds the grace window.
        config = _make_config(
            schedule=ScheduleConfig(shutdown_grace_seconds=0),
        )
        svc = InfluxService(config)
        await svc.start()

        cancel_observed = asyncio.Event()

        async def blocked_work() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancel_observed.set()
                raise

        task = asyncio.get_event_loop().create_task(blocked_work())
        svc.app.state.active_tasks.add(task)
        task.add_done_callback(svc.app.state.active_tasks.discard)

        # Give the task a moment to actually start
        await asyncio.sleep(0)

        await svc.stop()

        assert task.cancelled() or cancel_observed.is_set(), (
            "Task that exceeded grace should have been cancelled"
        )

    async def test_stop_returns_within_bound_even_if_cancellation_is_slow(
        self,
    ) -> None:
        """stop() must not block past schedule.shutdown_grace_seconds when a
        task catches CancelledError and lingers.

        With ``grace=0`` the total shutdown wait must be near-zero — no
        extra fixed post-cancel window may be added on top of the
        configured grace.
        """
        import asyncio
        import time

        config = _make_config(
            schedule=ScheduleConfig(shutdown_grace_seconds=0),
        )
        svc = InfluxService(config)
        await svc.start()

        released = asyncio.Event()

        async def stubborn_work() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                # Swallow cancellation and linger well past any bounded
                # wait stop() might perform.
                try:
                    await asyncio.sleep(1.0)
                finally:
                    released.set()

        task = asyncio.get_event_loop().create_task(stubborn_work())
        svc.app.state.active_tasks.add(task)
        task.add_done_callback(svc.app.state.active_tasks.discard)

        await asyncio.sleep(0)  # let the task actually start

        # Tolerance covers bookkeeping (scheduler.pause(), probe_loop.stop(),
        # logger calls); any post-cancel wait tied to a fixed sleep would
        # blow past this bound when grace=0.
        tolerance = 0.05

        try:
            start = time.monotonic()
            await svc.stop()
            elapsed = time.monotonic() - start
        finally:
            # Cleanup: make sure the lingering task is drained so it
            # doesn't leak into other tests.
            if not task.done():
                await asyncio.wait({task}, timeout=2.0)

        assert elapsed < tolerance, (
            f"stop() took {elapsed:.3f}s with grace=0; expected < "
            f"{tolerance:.3f}s — any fixed post-cancel wait would exceed "
            "the configured grace bound"
        )
        assert not released.is_set() or task.done(), (
            "stop() returned before the stubborn task finished lingering, "
            "which is the expected bounded behaviour"
        )


# ── Config schema extensions ─────────────────────────────────────────


class TestConfigExtensions:
    """SecurityConfig.allow_remote_admin and ScheduleConfig.shutdown_grace_seconds."""

    def test_allow_remote_admin_default_false(self) -> None:
        """allow_remote_admin defaults to False."""
        config = _make_config()
        assert config.security.allow_remote_admin is False

    def test_allow_remote_admin_can_be_set(self) -> None:
        """allow_remote_admin can be explicitly set to True."""
        config = _make_config(
            security=SecurityConfig(allow_remote_admin=True),
        )
        assert config.security.allow_remote_admin is True

    def test_shutdown_grace_seconds_default(self) -> None:
        """shutdown_grace_seconds defaults to 30."""
        config = _make_config()
        assert config.schedule.shutdown_grace_seconds == 30

    def test_shutdown_grace_seconds_custom(self) -> None:
        """shutdown_grace_seconds can be set to a custom value."""
        config = AppConfig(
            schedule=ScheduleConfig(shutdown_grace_seconds=60),
            prompts=PromptsConfig(
                filter=PromptEntryConfig(text="t"),
                tier1_enrich=PromptEntryConfig(text="t"),
                tier3_extract=PromptEntryConfig(text="t"),
            ),
        )
        assert config.schedule.shutdown_grace_seconds == 60
