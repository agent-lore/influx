"""Integration tests for the HTTP API endpoints (US-004).

Uses FastAPI's ``TestClient`` (backed by httpx) to exercise
``/live``, ``/ready``, and ``/status`` against a wired-up app with
real coordinator, scheduler, and probe loop instances.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from influx.config import (
    AppConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.coordinator import Coordinator
from influx.http_api import router
from influx.probes import ProbeLoop
from influx.scheduler import InfluxScheduler


def _make_config(
    profiles: list[str] | None = None,
    providers: dict[str, Any] | None = None,
) -> AppConfig:
    """Build a minimal AppConfig for HTTP API tests."""
    profile_names = profiles if profiles is not None else ["ai-robotics"]
    profile_list = [ProfileConfig(name=name) for name in profile_names]
    return AppConfig(
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=profile_list,
        providers=providers or {},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="test"),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
    )


@pytest.fixture
def app_with_state() -> FastAPI:
    """Create a FastAPI app with router, coordinator, scheduler, and probe loop."""
    config = _make_config(profiles=["ai-robotics", "web-tech"])
    app = FastAPI()
    app.include_router(router)

    coordinator = Coordinator()
    scheduler = InfluxScheduler(config, coordinator)
    probe_loop = ProbeLoop(config, interval=30.0)

    # Run one probe cycle so state is populated (not "starting").
    probe_loop.run_once()

    app.state.config = config
    app.state.coordinator = coordinator
    app.state.scheduler = scheduler
    app.state.probe_loop = probe_loop

    return app


@pytest.fixture
def client(app_with_state: FastAPI) -> TestClient:
    """Provide a TestClient for the wired-up app."""
    return TestClient(app_with_state)


# ── GET /live ────────────────────────────────────────────────────────


class TestLive:
    """``GET /live`` returns 200 under normal conditions (FR-HTTP-1)."""

    def test_live_returns_200(self, client: TestClient) -> None:
        resp = client.get("/live")
        assert resp.status_code == 200
        assert resp.json()["live"] is True

    def test_live_body_is_json(self, client: TestClient) -> None:
        resp = client.get("/live")
        assert resp.headers["content-type"] == "application/json"


# ── GET /ready ───────────────────────────────────────────────────────


class TestReady:
    """``GET /ready`` returns 200 when probes pass, 503 when degraded."""

    def test_ready_200_when_probes_pass(self, client: TestClient) -> None:
        """All probes ok → 200 + ready=true (AC-M1-5)."""
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json() == {"ready": True}

    def test_ready_503_when_probes_fail(
        self,
        app_with_state: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Degraded probes → 503 + ready=false."""
        # Force lithos probe to degraded.
        monkeypatch.setenv("INFLUX_TEST_LITHOS_DOWN", "1")
        probe_loop: ProbeLoop = app_with_state.state.probe_loop
        probe_loop.run_once()

        with TestClient(app_with_state) as tc:
            resp = tc.get("/ready")
        assert resp.status_code == 503
        assert resp.json() == {"ready": False}


# ── GET /status ──────────────────────────────────────────────────────


class TestStatus:
    """``GET /status`` returns the documented body shape (FR-HTTP-3)."""

    def test_status_returns_200(self, client: TestClient) -> None:
        resp = client.get("/status")
        assert resp.status_code == 200

    def test_status_body_shape(self, client: TestClient) -> None:
        """Response contains the PRD-mandated subset of fields."""
        body = client.get("/status").json()

        # Top-level fields
        assert body["status"] in {"ok", "degraded", "starting"}
        assert isinstance(body["ready"], bool)
        assert isinstance(body["version"], str)
        assert len(body["version"]) > 0

        # Dependencies
        deps = body["dependencies"]
        assert deps["lithos"]["status"] in {"ok", "degraded"}
        assert deps["llm_credentials"]["status"] in {"ok", "degraded"}

        # Profiles
        profiles = body["profiles"]
        assert "ai-robotics" in profiles
        assert "web-tech" in profiles
        for _name, info in profiles.items():
            assert "next_run_at" in info
            assert "last_run_at" in info
            assert "last_run_status" in info

    def test_status_ok_when_probes_pass(self, client: TestClient) -> None:
        """Healthy probes → status=ok, ready=true."""
        body = client.get("/status").json()
        assert body["status"] == "ok"
        assert body["ready"] is True

    def test_status_version_matches_package(self, client: TestClient) -> None:
        """version field matches influx.__version__."""
        import influx

        body = client.get("/status").json()
        assert body["version"] == influx.__version__

    async def test_status_profiles_next_run_at_non_null_when_scheduler_running(
        self, app_with_state: FastAPI
    ) -> None:
        """next_run_at is non-null when scheduler is running (AC-M1-6)."""
        scheduler: InfluxScheduler = app_with_state.state.scheduler
        scheduler.start()
        try:
            with TestClient(app_with_state) as tc:
                body = tc.get("/status").json()
            for name, info in body["profiles"].items():
                assert info["next_run_at"] is not None, (
                    f"Profile {name!r} should have non-null next_run_at"
                )
        finally:
            scheduler.stop()

    def test_status_profiles_next_run_at_null_when_scheduler_stopped(
        self, client: TestClient
    ) -> None:
        """next_run_at is null when scheduler is not running."""
        body = client.get("/status").json()
        for name, info in body["profiles"].items():
            assert info["next_run_at"] is None, (
                f"Profile {name!r} should have null next_run_at when scheduler stopped"
            )

    async def test_status_currently_running_reflects_coordinator(
        self, app_with_state: FastAPI
    ) -> None:
        """currently_running matches coordinator lock state."""
        coordinator: Coordinator = app_with_state.state.coordinator

        # Acquire lock for ai-robotics.
        await coordinator.try_acquire("ai-robotics")
        try:
            with TestClient(app_with_state) as tc:
                body = tc.get("/status").json()
            assert body["profiles"]["ai-robotics"]["currently_running"] is True
            assert body["profiles"]["web-tech"]["currently_running"] is False
        finally:
            coordinator.release("ai-robotics")


# ── AC-03-C: Degraded state via INFLUX_TEST_LITHOS_DOWN ──────────────


class TestDegradedStateAC03C:
    """AC-03-C: INFLUX_TEST_LITHOS_DOWN=1 → /ready 503, /status degraded."""

    def test_lithos_down_ready_503(
        self,
        app_with_state: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Within one probe cycle, /ready returns 503."""
        monkeypatch.setenv("INFLUX_TEST_LITHOS_DOWN", "1")
        probe_loop: ProbeLoop = app_with_state.state.probe_loop
        probe_loop.run_once()

        with TestClient(app_with_state) as tc:
            resp = tc.get("/ready")
        assert resp.status_code == 503

    def test_lithos_down_status_degraded(
        self,
        app_with_state: FastAPI,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Within one probe cycle, /status.status == 'degraded'."""
        monkeypatch.setenv("INFLUX_TEST_LITHOS_DOWN", "1")
        probe_loop: ProbeLoop = app_with_state.state.probe_loop
        probe_loop.run_once()

        with TestClient(app_with_state) as tc:
            body = tc.get("/status").json()
        assert body["status"] == "degraded"
        assert body["dependencies"]["lithos"]["status"] == "degraded"
