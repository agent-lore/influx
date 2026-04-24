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
) -> AppConfig:
    """Build a minimal AppConfig for service tests."""
    profile_names = profiles if profiles is not None else ["ai-robotics"]
    profile_list = [ProfileConfig(name=name) for name in profile_names]
    return AppConfig(
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
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
