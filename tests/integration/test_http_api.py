"""Integration tests for the HTTP API endpoints (US-004, US-005, US-006).

Uses FastAPI's ``TestClient`` (backed by httpx) to exercise
``/live``, ``/ready``, ``/status``, ``POST /runs``, and
``POST /backfills`` against a wired-up app with real coordinator,
scheduler, and probe loop instances.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from influx.config import (
    AppConfig,
    LithosConfig,
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
    lithos_url: str = "",
) -> AppConfig:
    """Build a minimal AppConfig for HTTP API tests."""
    profile_names = profiles if profiles is not None else ["ai-robotics"]
    profile_list = [ProfileConfig(name=name) for name in profile_names]
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
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
def app_with_state(fake_lithos_sse_url: str) -> FastAPI:
    """Create a FastAPI app with router, coordinator, scheduler, and probe loop."""
    config = _make_config(
        profiles=["ai-robotics", "web-tech"],
        lithos_url=fake_lithos_sse_url,
    )
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
    ) -> None:
        """Degraded probes ��� 503 + ready=false."""
        import socket

        # Switch to unreachable Lithos URL to force probe degradation.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        app_with_state.state.config.lithos.url = (
            f"http://127.0.0.1:{port}/sse"
        )
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


# ── AC-M1-11 (probe + /status): Lithos unreachable → degraded ────────


class TestDegradedStateLithosUnreachable:
    """Lithos unreachable → /ready 503, /status degraded (AC-M1-11 probe side)."""

    @pytest.fixture
    def app_lithos_down(self) -> FastAPI:
        """App configured with an unreachable Lithos URL."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        config = _make_config(
            profiles=["ai-robotics", "web-tech"],
            lithos_url=f"http://127.0.0.1:{port}/sse",
        )
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
        return app

    def test_lithos_down_ready_503(self, app_lithos_down: FastAPI) -> None:
        """Lithos unreachable → /ready returns 503."""
        with TestClient(app_lithos_down) as tc:
            resp = tc.get("/ready")
        assert resp.status_code == 503

    def test_lithos_down_status_degraded(
        self, app_lithos_down: FastAPI
    ) -> None:
        """/status reports degraded with lithos dependency not ok (AC-M1-11)."""
        with TestClient(app_lithos_down) as tc:
            body = tc.get("/status").json()
        assert body["status"] == "degraded"
        assert body["dependencies"]["lithos"]["status"] == "degraded"


# ── POST /runs (US-005) ─────────────────────────────────────────────


class TestPostRuns:
    """``POST /runs`` accepts manual run requests (FR-HTTP-4)."""

    def test_runs_happy_path_202(self, client: TestClient) -> None:
        """Single-profile run returns 202 + documented response body."""
        resp = client.post("/runs", json={"profile": "ai-robotics"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert "request_id" in body
        assert body["kind"] == "manual"
        assert body["scope"] == "ai-robotics"
        assert "submitted_at" in body

    def test_runs_conflict_409(self, app_with_state: FastAPI) -> None:
        """Duplicate submission while run in flight → 409 + profile_busy (AC-M1-8)."""
        coordinator: Coordinator = app_with_state.state.coordinator

        # Simulate an in-flight run by holding the lock.
        loop = asyncio.new_event_loop()
        loop.run_until_complete(coordinator.try_acquire("ai-robotics"))
        try:
            with TestClient(app_with_state) as tc:
                resp = tc.post("/runs", json={"profile": "ai-robotics"})
            assert resp.status_code == 409
            assert resp.json()["reason"] == "profile_busy"
        finally:
            coordinator.release("ai-robotics")
            loop.close()

    def test_runs_both_profile_and_all_returns_422(self, client: TestClient) -> None:
        """Body with both profile and all_profiles → 422 (AC-03-B)."""
        resp = client.post(
            "/runs",
            json={"profile": "ai-robotics", "all_profiles": True},
        )
        assert resp.status_code == 422

    def test_runs_neither_profile_nor_all_returns_422(self, client: TestClient) -> None:
        """Body with neither profile nor all_profiles → 422."""
        resp = client.post("/runs", json={})
        assert resp.status_code == 422

    def test_runs_unknown_profile_returns_422(self, client: TestClient) -> None:
        """Unknown profile name → 422."""
        resp = client.post("/runs", json={"profile": "does-not-exist"})
        assert resp.status_code == 422

    def test_runs_all_profiles_returns_202(self, client: TestClient) -> None:
        """all_profiles=true returns 202 with scope='all'."""
        resp = client.post("/runs", json={"all_profiles": True})
        assert resp.status_code == 202
        body = resp.json()
        assert body["scope"] == "all"
        assert body["kind"] == "manual"

    def test_runs_request_id_is_unique(self, client: TestClient) -> None:
        """Each accepted run gets a unique request_id."""
        r1 = client.post("/runs", json={"profile": "ai-robotics"})
        # Wait briefly for the background task to release the lock.
        import time

        time.sleep(0.05)
        r2 = client.post("/runs", json={"profile": "ai-robotics"})
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["request_id"] != r2.json()["request_id"]


class TestPostRunsSchedulerOverlap:
    """AC-03-A: scheduled fire + POST /runs for same profile → exactly one accepted."""

    async def test_scheduled_and_manual_overlap_same_profile(
        self, app_with_state: FastAPI
    ) -> None:
        """A scheduled fire holds the lock → POST /runs returns 409."""
        coordinator: Coordinator = app_with_state.state.coordinator

        # Simulate a scheduled fire holding the lock.
        await coordinator.try_acquire("ai-robotics")
        try:
            with TestClient(app_with_state) as tc:
                resp = tc.post("/runs", json={"profile": "ai-robotics"})
            assert resp.status_code == 409
            assert resp.json()["reason"] == "profile_busy"
        finally:
            coordinator.release("ai-robotics")

    async def test_cross_profile_runs_allowed(self, app_with_state: FastAPI) -> None:
        """Different profiles can run concurrently."""
        coordinator: Coordinator = app_with_state.state.coordinator

        # Hold lock for ai-robotics.
        await coordinator.try_acquire("ai-robotics")
        try:
            with TestClient(app_with_state) as tc:
                resp = tc.post("/runs", json={"profile": "web-tech"})
            assert resp.status_code == 202
        finally:
            coordinator.release("ai-robotics")
            # Also release web-tech if the run acquired it.
            if coordinator.is_busy("web-tech"):
                coordinator.release("web-tech")


# ── POST /backfills (US-006) ───────────────────────────────────────


class TestPostBackfills:
    """``POST /backfills`` accepts backfill requests (FR-HTTP-5)."""

    def test_backfills_happy_path_with_days(self, client: TestClient) -> None:
        """Single-profile backfill with days returns 202."""
        resp = client.post(
            "/backfills",
            json={"profile": "ai-robotics", "days": 7},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert "request_id" in body
        assert body["kind"] == "backfill"
        assert body["scope"] == "ai-robotics"
        assert "submitted_at" in body

    def test_backfills_happy_path_with_date_range(self, client: TestClient) -> None:
        """Single-profile backfill with from/to returns 202."""
        resp = client.post(
            "/backfills",
            json={
                "profile": "ai-robotics",
                "from": "2026-01-01",
                "to": "2026-01-31",
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["kind"] == "backfill"
        assert body["scope"] == "ai-robotics"

    def test_backfills_all_profiles_returns_202(self, client: TestClient) -> None:
        """all_profiles=true returns 202 with scope='all'."""
        resp = client.post(
            "/backfills",
            json={"all_profiles": True, "days": 7},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["scope"] == "all"
        assert body["kind"] == "backfill"

    def test_backfills_both_profile_and_all_returns_422(
        self, client: TestClient
    ) -> None:
        """Body with both profile and all_profiles → 422."""
        resp = client.post(
            "/backfills",
            json={
                "profile": "ai-robotics",
                "all_profiles": True,
                "days": 7,
            },
        )
        assert resp.status_code == 422

    def test_backfills_neither_scope_returns_422(self, client: TestClient) -> None:
        """Body with neither profile nor all_profiles → 422."""
        resp = client.post("/backfills", json={"days": 7})
        assert resp.status_code == 422

    def test_backfills_both_days_and_range_returns_422(
        self, client: TestClient
    ) -> None:
        """Body with both days and from/to → 422."""
        resp = client.post(
            "/backfills",
            json={
                "profile": "ai-robotics",
                "days": 7,
                "from": "2026-01-01",
                "to": "2026-01-31",
            },
        )
        assert resp.status_code == 422

    def test_backfills_neither_days_nor_range_returns_422(
        self, client: TestClient
    ) -> None:
        """Body with neither days nor from/to → 422."""
        resp = client.post(
            "/backfills",
            json={"profile": "ai-robotics"},
        )
        assert resp.status_code == 422

    def test_backfills_unknown_profile_returns_422(self, client: TestClient) -> None:
        """Unknown profile name → 422."""
        resp = client.post(
            "/backfills",
            json={"profile": "does-not-exist", "days": 7},
        )
        assert resp.status_code == 422

    def test_backfills_conflict_409(self, app_with_state: FastAPI) -> None:
        """Duplicate backfill while run in flight → 409 + profile_busy."""
        coordinator: Coordinator = app_with_state.state.coordinator

        loop = asyncio.new_event_loop()
        loop.run_until_complete(coordinator.try_acquire("ai-robotics"))
        try:
            with TestClient(app_with_state) as tc:
                resp = tc.post(
                    "/backfills",
                    json={"profile": "ai-robotics", "days": 7},
                )
            assert resp.status_code == 409
            assert resp.json()["reason"] == "profile_busy"
        finally:
            coordinator.release("ai-robotics")
            loop.close()


class TestBackfillConfirmRequired:
    """Confirm-required flow when estimator reports > 1000 items (AC-M3-8)."""

    def test_confirm_required_when_estimate_exceeds_1000(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Estimate > 1000 without confirm → 400 + confirm_required."""
        import influx.http_api as http_api_mod

        monkeypatch.setattr(http_api_mod, "_backfill_estimate_override", 5000)
        resp = client.post(
            "/backfills",
            json={"profile": "ai-robotics", "days": 365},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["reason"] == "confirm_required"
        assert body["estimated_items"] == 5000

    def test_confirm_true_accepts_large_estimate(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Estimate > 1000 with confirm=true → 202 accepted."""
        import influx.http_api as http_api_mod

        monkeypatch.setattr(http_api_mod, "_backfill_estimate_override", 5000)
        resp = client.post(
            "/backfills",
            json={
                "profile": "ai-robotics",
                "days": 365,
                "confirm": True,
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["kind"] == "backfill"

    def test_estimate_lte_1000_accepted_without_confirm(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Estimate ≤ 1000 without confirm → 202 accepted (no reprompt)."""
        import influx.http_api as http_api_mod

        monkeypatch.setattr(http_api_mod, "_backfill_estimate_override", 500)
        resp = client.post(
            "/backfills",
            json={"profile": "ai-robotics", "days": 30},
        )
        assert resp.status_code == 202


class TestBackfillSchedulerOverlap:
    """AC-M3-7: backfill does not overlap with scheduled run for same profile."""

    async def test_backfill_blocked_by_scheduled_run(
        self, app_with_state: FastAPI
    ) -> None:
        """A scheduled fire holds the lock → backfill returns 409."""
        coordinator: Coordinator = app_with_state.state.coordinator

        # Simulate a scheduled fire holding the lock.
        await coordinator.try_acquire("ai-robotics")
        try:
            with TestClient(app_with_state) as tc:
                resp = tc.post(
                    "/backfills",
                    json={"profile": "ai-robotics", "days": 7},
                )
            assert resp.status_code == 409
            assert resp.json()["reason"] == "profile_busy"
        finally:
            coordinator.release("ai-robotics")

    async def test_backfill_cross_profile_allowed(
        self, app_with_state: FastAPI
    ) -> None:
        """Backfill for profile Y while profile X is busy → 202."""
        coordinator: Coordinator = app_with_state.state.coordinator

        await coordinator.try_acquire("ai-robotics")
        try:
            with TestClient(app_with_state) as tc:
                resp = tc.post(
                    "/backfills",
                    json={"profile": "web-tech", "days": 7},
                )
            assert resp.status_code == 202
        finally:
            coordinator.release("ai-robotics")
            if coordinator.is_busy("web-tech"):
                coordinator.release("web-tech")
