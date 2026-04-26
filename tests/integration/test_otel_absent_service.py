"""AC-M4-3: service starts, serves, and completes a run with OTEL absent.

With OTEL optional packages absent (simulated via import-stub) and OTEL
disabled, the service must start, serve requests, and complete a
representative run — asserting service-level behaviour, not just module
import or wrapper no-op.
"""

from __future__ import annotations

import builtins
import sys
from collections.abc import Generator, Iterable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from influx.config import (
    AppConfig,
    FeedbackConfig,
    LithosConfig,
    NotificationsConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.coordinator import Coordinator, RunKind
from influx.http_api import router
from influx.notifications import ProfileRunResult
from influx.probes import ProbeLoop
from influx.scheduler import InfluxScheduler, ProfileItem, run_profile
from tests.contract.test_lithos_client import FakeLithosServer


def _make_config(lithos_url: str = "") -> AppConfig:
    """Build a minimal AppConfig with OTEL disabled."""
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[ProfileConfig(name="ai-robotics")],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="test"),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        feedback=FeedbackConfig(negative_examples_per_profile=20),
    )


@pytest.fixture(scope="module")
def fake_lithos_mcp() -> Generator[FakeLithosServer, None, None]:
    """Module-scoped fully-functional fake Lithos MCP server."""
    server = FakeLithosServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def fake_lithos_mcp_url(fake_lithos_mcp: FakeLithosServer) -> str:
    return f"http://127.0.0.1:{fake_lithos_mcp.port}/sse"


@pytest.fixture(autouse=True)
def _clear_fakes(fake_lithos_mcp: FakeLithosServer) -> None:
    fake_lithos_mcp.calls.clear()
    fake_lithos_mcp.write_responses.clear()
    fake_lithos_mcp.read_responses.clear()
    fake_lithos_mcp.cache_lookup_responses.clear()
    fake_lithos_mcp.list_responses.clear()


class TestServiceWithOtelAbsent:
    """AC-M4-3: service operates normally when OTEL packages are absent."""

    @pytest.fixture(autouse=True)
    def _stub_otel_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[None, None, None]:
        """Simulate OTEL packages being absent via import-stubbing."""
        monkeypatch.delenv("INFLUX_OTEL_ENABLED", raising=False)

        # Save and remove any loaded opentelemetry modules
        otel_keys = [k for k in sys.modules if k.startswith("opentelemetry")]
        saved = {k: sys.modules[k] for k in otel_keys}
        for k in otel_keys:
            monkeypatch.delitem(sys.modules, k, raising=False)

        _real_import = builtins.__import__

        def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("opentelemetry"):
                raise ImportError(f"Simulated missing: {name}")
            return _real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_import)

        # Force rebuild the telemetry singleton to reflect absent packages
        from influx.telemetry import get_tracer

        get_tracer(force_rebuild=True)

        yield

        # Restore OTEL modules after test
        for k, v in saved.items():
            sys.modules[k] = v
        get_tracer(force_rebuild=True)

    def test_service_boots_and_serves_with_otel_absent(
        self,
        fake_lithos_sse_url: str,
    ) -> None:
        """Service starts, /live and /ready respond, scheduler has jobs."""
        from fastapi import FastAPI

        config = _make_config(lithos_url=fake_lithos_sse_url)
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

        client = TestClient(app)

        # /live must respond 200
        resp = client.get("/live")
        assert resp.status_code == 200

        # /ready must respond (200 or 503 depending on probe state)
        resp = client.get("/ready")
        assert resp.status_code in (200, 503)

        # /status must respond 200 with version info
        resp = client.get("/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "version" in body

    async def test_run_completes_with_otel_absent(
        self,
        fake_lithos_mcp_url: str,
    ) -> None:
        """A representative run completes successfully when OTEL is absent.

        Uses a fully-functional fake Lithos MCP server so the run
        exercises the entire ``run_profile`` lifecycle (task bracketing,
        feedback ingestion, item provider invocation, post-run webhook)
        and returns a ``ProfileRunResult`` — proving the service-level
        AC-M4-3 contract: start + serve + complete a run.
        """
        config = _make_config(lithos_url=fake_lithos_mcp_url)

        async def _empty_provider(
            profile: str,
            kind: RunKind,
            run_range: dict[str, str | int] | None,
            filter_prompt: str,
        ) -> Iterable[ProfileItem]:
            del profile, kind, run_range, filter_prompt
            return ()

        result = await run_profile(
            "ai-robotics",
            RunKind.SCHEDULED,
            config=config,
            item_provider=_empty_provider,
        )

        # The run completes and returns a ProfileRunResult.
        assert isinstance(result, ProfileRunResult)
        assert result.profile == "ai-robotics"
        assert result.stats.sources_checked == 0
        assert result.stats.ingested == 0

        # Verify telemetry remained a no-op throughout the run.
        from influx.telemetry import get_tracer

        tracer = get_tracer()
        assert not tracer.enabled
