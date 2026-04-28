"""Integration tests for the ``serve`` CLI handler (US-008, AC-03-E).

Tests cover:
- ``serve`` starts the full service (ASGI app + scheduler + probe loop)
- Handles SIGINT/SIGTERM with clean shutdown
- Exits with status 0 on clean shutdown
- AC-M1-3: ``/live``, ``/ready``, ``/status`` are bound; scheduler has jobs
- Shutdown is clean: no orphaned threads or scheduler jobs
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent
from typing import Any

import httpx
import pytest

from influx.config import (
    AppConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.service import InfluxService


def _make_config(
    profiles: list[str] | None = None,
    providers: dict[str, Any] | None = None,
    schedule: ScheduleConfig | None = None,
) -> AppConfig:
    """Build a minimal AppConfig for serve tests."""
    profile_names = profiles if profiles is not None else ["ai-robotics"]
    profile_list = [ProfileConfig(name=name) for name in profile_names]
    return AppConfig(
        schedule=schedule or ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=profile_list,
        providers=providers or {},
        security=SecurityConfig(),
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="test"),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
    )


# ── Unit-level tests for _cmd_serve wiring ──────────────────────────


class TestCmdServeWiring:
    """Verify _cmd_serve loads config, validates bind, and configures uvicorn."""

    @staticmethod
    def _patch_uvicorn(
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, Any]:
        """Replace ``uvicorn.Config`` + ``uvicorn.Server`` + ``asyncio.run``
        with capturing stubs so ``_cmd_serve`` returns without binding
        a real socket."""
        import asyncio as asyncio_mod

        import uvicorn

        captured: dict[str, Any] = {}

        real_config = uvicorn.Config

        def capturing_config(app: Any, **kwargs: Any) -> Any:
            captured["app"] = app
            captured["kwargs"] = kwargs
            return real_config(app, **kwargs)

        class FakeServer:
            def __init__(self, cfg: Any) -> None:
                captured["server_config"] = cfg
                self.should_exit = False

            def install_signal_handlers(self) -> None:  # pragma: no cover
                pass

            async def serve(self) -> None:  # pragma: no cover
                return None

        def fake_run(coro: Any) -> None:
            # Close the coroutine so pytest doesn't emit a warning.
            coro.close()

        monkeypatch.setattr(uvicorn, "Config", capturing_config)
        monkeypatch.setattr(uvicorn, "Server", FakeServer)
        monkeypatch.setattr(asyncio_mod, "run", fake_run)
        return captured

    def test_cmd_serve_configures_uvicorn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        influx_config_env: Path,
    ) -> None:
        """_cmd_serve builds a uvicorn Config with the expected host/port."""
        from influx.main import _cmd_serve

        captured = self._patch_uvicorn(monkeypatch)
        monkeypatch.delenv("INFLUX_ADMIN_BIND_HOST", raising=False)
        monkeypatch.delenv("INFLUX_ADMIN_PORT", raising=False)

        _cmd_serve()

        kwargs = captured["kwargs"]
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8080
        assert kwargs["log_level"] == "info"
        assert captured["app"] is not None

    def test_cmd_serve_uses_custom_bind(
        self,
        monkeypatch: pytest.MonkeyPatch,
        influx_config_env: Path,
    ) -> None:
        """_cmd_serve reads custom bind host/port from env."""
        from influx.main import _cmd_serve

        captured = self._patch_uvicorn(monkeypatch)
        monkeypatch.setenv("INFLUX_ADMIN_BIND_HOST", "127.0.0.1")
        monkeypatch.setenv("INFLUX_ADMIN_PORT", "9999")

        _cmd_serve()

        kwargs = captured["kwargs"]
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 9999

    def test_cmd_serve_passes_shutdown_grace(
        self,
        monkeypatch: pytest.MonkeyPatch,
        influx_config_env: Path,
    ) -> None:
        """_cmd_serve passes shutdown_grace_seconds to uvicorn."""
        from influx.main import _cmd_serve

        captured = self._patch_uvicorn(monkeypatch)
        monkeypatch.delenv("INFLUX_ADMIN_BIND_HOST", raising=False)
        monkeypatch.delenv("INFLUX_ADMIN_PORT", raising=False)

        _cmd_serve()

        kwargs = captured["kwargs"]
        # Default shutdown_grace_seconds is 30
        assert kwargs["timeout_graceful_shutdown"] == 30

    def test_cmd_serve_refuses_remote_bind_without_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
        influx_config_env: Path,
    ) -> None:
        """_cmd_serve refuses non-loopback bind when allow_remote_admin is False."""
        from influx.errors import ConfigError
        from influx.main import _cmd_serve

        monkeypatch.setenv("INFLUX_ADMIN_BIND_HOST", "0.0.0.0")

        with pytest.raises(ConfigError, match="not a loopback"):
            _cmd_serve()


# ── End-to-end shutdown bound (Finding 1 regression) ────────────────


_GRACE_ZERO_TOML = dedent("""\
    [influx]
    note_schema_version = 1

    [schedule]
    cron = "0 6 * * *"
    timezone = "UTC"
    misfire_grace_seconds = 3600
    shutdown_grace_seconds = 0

    [[profiles]]
    name = "ai-robotics"

    [prompts.filter]
    text = "Filter {profile_description} {negative_examples} {min_score_in_results}"
    [prompts.tier1_enrich]
    text = "Enrich {title} {abstract} {profile_summary}"
    [prompts.tier3_extract]
    text = "Extract {title} {full_text}"
""")


class TestCmdServeShutdownBound:
    """Finding 1 regression: total ``_cmd_serve`` exit time must stay
    within ``schedule.shutdown_grace_seconds`` even when an in-flight
    task swallows cancellation.

    Without manual event-loop ownership in ``_cmd_serve``,
    ``asyncio.run``'s final cleanup pass would gather every remaining
    task unbounded and let a stubborn task push the process exit past
    the configured grace (AC-03-E).
    """

    def test_cmd_serve_bounds_total_exit_on_stubborn_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """End-to-end regression: stubborn post-cancel task must not
        extend the total serve exit time past the configured grace.
        """
        import asyncio as asyncio_mod
        import time

        import uvicorn

        # grace=0 config so any extension of the post-shutdown wait
        # shows up as extra wall-clock time.
        config_path = tmp_path / "influx.toml"
        config_path.write_text(_GRACE_ZERO_TOML)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.setenv("INFLUX_ADMIN_BIND_HOST", "127.0.0.1")
        monkeypatch.setenv("INFLUX_ADMIN_PORT", "0")

        captured: dict[str, Any] = {}

        class FakeServer:
            """Simulates uvicorn: enters/exits the app's lifespan,
            injects a stubborn task in-between so ``InfluxService.stop``
            has to cancel it on the way out."""

            def __init__(self, cfg: Any) -> None:
                self.config = cfg
                self.should_exit = False

            def install_signal_handlers(self) -> None:
                pass

            async def serve(self) -> None:
                app = self.config.app
                lifespan_cm = app.router.lifespan_context(app)
                await lifespan_cm.__aenter__()
                try:

                    async def stubborn() -> None:
                        # Repeatedly swallow every CancelledError for a
                        # full 1.0s so ``asyncio.run``'s unbounded final
                        # cleanup pass has something to actually wait for.
                        deadline = time.monotonic() + 1.0
                        while time.monotonic() < deadline:
                            try:
                                await asyncio_mod.sleep(0.05)
                            except asyncio_mod.CancelledError:
                                current = asyncio_mod.current_task()
                                if current is not None:
                                    current.uncancel()
                                continue
                        captured["stubborn_done"] = True

                    loop = asyncio_mod.get_event_loop()
                    task = loop.create_task(stubborn())
                    app.state.active_tasks.add(task)
                    task.add_done_callback(app.state.active_tasks.discard)
                    captured["stubborn_task"] = task
                    await asyncio_mod.sleep(0)  # let it actually start
                finally:
                    # Triggers InfluxService.stop() under the lifespan.
                    await lifespan_cm.__aexit__(None, None, None)

        monkeypatch.setattr(uvicorn, "Server", FakeServer)

        from influx.main import _cmd_serve

        # Tolerance allows for bookkeeping (loop teardown, shutdown_asyncgens,
        # the tiny post-cancel epsilon inside _cmd_serve); a full 1.0s
        # stubborn linger would blow through this bound without the fix.
        tolerance = 0.5

        start = time.monotonic()
        _cmd_serve()
        elapsed = time.monotonic() - start

        assert elapsed < tolerance, (
            f"_cmd_serve took {elapsed:.3f}s with shutdown_grace_seconds=0; "
            f"expected < {tolerance:.3f}s — a stubborn post-cancel task "
            "must not extend total serve exit past the configured grace"
        )


# ── Lifespan integration ────────────────────────────────────────────


class TestServiceLifespan:
    """InfluxService.lifespan starts and stops the service correctly."""

    async def test_lifespan_starts_and_stops_service(self) -> None:
        """Lifespan context manager calls start() on enter and stop() on exit."""
        config = _make_config()
        svc = InfluxService(config, with_lifespan=True)

        async with svc.lifespan(svc.app):
            # Service should be started — scheduler has jobs
            assert len(svc.scheduler.jobs) > 0
            assert svc.probe_loop.state.overall_status != "starting"

        # After exit — scheduler stopped
        assert len(svc.scheduler.jobs) == 0

    async def test_lifespan_ac_m1_3_endpoints_and_jobs(self) -> None:
        """AC-M1-3: /live, /ready, /status are bound; tick dispatcher exists.

        After review finding 1 the scheduler registers a single
        ``influx-tick`` dispatcher job that fans out to all profiles, so
        the per-source FetchCache scope covers the whole cron tick.
        """
        config = _make_config(profiles=["ai-robotics", "web-tech"])
        svc = InfluxService(config, with_lifespan=True)

        async with svc.lifespan(svc.app):
            # Endpoints are bound
            paths = {getattr(r, "path", None) for r in svc.app.routes}
            assert "/live" in paths
            assert "/ready" in paths
            assert "/status" in paths

            # Single tick-dispatcher job that fans out to all profiles.
            job_ids = {j.id for j in svc.scheduler.jobs}
            assert "influx-tick" in job_ids
            assert len(svc.scheduler.jobs) == 1


# ── Subprocess integration test (AC-03-E) ───────────────────────────


_MINIMAL_TOML = dedent("""\
    [influx]
    note_schema_version = 1

    [schedule]
    cron = "0 6 * * *"
    timezone = "UTC"
    misfire_grace_seconds = 3600
    shutdown_grace_seconds = 5

    [[profiles]]
    name = "ai-robotics"

    [prompts.filter]
    text = "Filter {profile_description} {negative_examples} {min_score_in_results}"
    [prompts.tier1_enrich]
    text = "Enrich {title} {abstract} {profile_summary}"
    [prompts.tier3_extract]
    text = "Extract {title} {full_text}"
""")


class TestServeSubprocess:
    """AC-03-E: serve starts, handles signals, shuts down cleanly."""

    @pytest.fixture
    def serve_env(self, tmp_path: Path) -> dict[str, str]:
        """Prepare env vars for a subprocess serve invocation."""
        config_path = tmp_path / "influx.toml"
        config_path.write_text(_MINIMAL_TOML)

        env = os.environ.copy()
        env["INFLUX_CONFIG"] = str(config_path)
        env["INFLUX_ADMIN_BIND_HOST"] = "127.0.0.1"
        env["INFLUX_ADMIN_PORT"] = "0"  # Will be overridden per test
        return env

    def _find_free_port(self) -> int:
        """Find an available TCP port."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _wait_for_live(self, port: int, timeout: float = 10.0) -> bool:
        """Poll /live until it responds or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/live", timeout=1.0)
                if resp.status_code == 200:
                    return True
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
                pass
            time.sleep(0.2)
        return False

    def test_serve_sigint_clean_shutdown_exit_0(
        self,
        serve_env: dict[str, str],
    ) -> None:
        """AC-03-E: serve starts, SIGINT triggers clean shutdown, exit 0."""
        port = self._find_free_port()
        serve_env["INFLUX_ADMIN_PORT"] = str(port)

        proc = subprocess.Popen(
            [sys.executable, "-m", "influx", "serve"],
            env=serve_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            assert self._wait_for_live(port), "Server did not start in time"

            # Verify endpoints are accessible (AC-M1-3)
            resp = httpx.get(f"http://127.0.0.1:{port}/live", timeout=2.0)
            assert resp.status_code == 200

            resp = httpx.get(f"http://127.0.0.1:{port}/ready", timeout=2.0)
            assert resp.status_code in (200, 503)  # Depends on probe state

            resp = httpx.get(f"http://127.0.0.1:{port}/status", timeout=2.0)
            assert resp.status_code == 200
            body = resp.json()
            assert "status" in body
            assert "version" in body

            # Send SIGINT for clean shutdown
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=15)

            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_serve_sigterm_clean_shutdown_exit_0(
        self,
        serve_env: dict[str, str],
    ) -> None:
        """AC-03-E: serve handles SIGTERM with a clean shutdown and exit 0.

        ``_cmd_serve`` installs its own asyncio-level signal handlers
        (so SIGTERM drives ``server.should_exit = True`` and returns
        normally), so the process must exit 0 after graceful shutdown
        — not ``-15`` from the signal.
        """
        port = self._find_free_port()
        serve_env["INFLUX_ADMIN_PORT"] = str(port)

        proc = subprocess.Popen(
            [sys.executable, "-m", "influx", "serve"],
            env=serve_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            assert self._wait_for_live(port), "Server did not start in time"

            # Send SIGTERM for clean shutdown
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=15)

            assert proc.returncode == 0, (
                f"Expected exit 0 on SIGTERM, got {proc.returncode}"
            )
            # Verify clean shutdown happened (uvicorn logs)
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            assert "Application shutdown complete" in stderr
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_serve_scheduler_has_profile_jobs(
        self,
        serve_env: dict[str, str],
    ) -> None:
        """AC-M1-3: started service has scheduler jobs for enabled profiles."""
        port = self._find_free_port()
        serve_env["INFLUX_ADMIN_PORT"] = str(port)

        proc = subprocess.Popen(
            [sys.executable, "-m", "influx", "serve"],
            env=serve_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            assert self._wait_for_live(port), "Server did not start in time"

            # /status should show profile info
            resp = httpx.get(f"http://127.0.0.1:{port}/status", timeout=2.0)
            assert resp.status_code == 200
            body = resp.json()
            assert "profiles" in body
            assert "ai-robotics" in body["profiles"]

            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=15)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
