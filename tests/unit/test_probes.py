"""Tests for the background probe loop and cached state (US-002)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from influx.config import (
    AppConfig,
    LithosConfig,
    PromptEntryConfig,
    PromptsConfig,
    ProviderConfig,
    load_config,
)
from influx.probes import ProbeLoop, ProbeResult, ProbeState

# ── Helper: build a minimal AppConfig with providers ─────────────────


def _make_config(
    providers: dict[str, ProviderConfig] | None = None,
    lithos_url: str = "",
) -> AppConfig:
    """Return a minimal ``AppConfig`` with the given providers."""
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        providers=providers or {},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="f"),
            tier1_enrich=PromptEntryConfig(text="e"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
    )


# ── Lithos stub probe ────────────────────────────────────────────────


class TestLithosProbe:
    """Lithos probe connects to SSE endpoint (US-013)."""

    def test_lithos_ok_with_reachable_server(self, fake_lithos_sse_url: str) -> None:
        """Reachable Lithos SSE server → probe returns ``ok``."""
        cfg = _make_config(lithos_url=fake_lithos_sse_url)
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.lithos.status == "ok"
        assert loop.state.lithos.timestamp > 0

    def test_lithos_degraded_when_unreachable(self) -> None:
        """Unreachable Lithos → probe returns ``degraded`` with reason."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # Port is now unbound — connection will be refused.
        cfg = _make_config(lithos_url=f"http://127.0.0.1:{port}/sse")
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.lithos.status == "degraded"
        assert loop.state.lithos.timestamp > 0
        assert lithos_url_in_detail(loop.state.lithos.detail, port)

    def test_lithos_degraded_when_url_empty(self) -> None:
        """Empty Lithos URL → probe returns ``degraded``."""
        cfg = _make_config(lithos_url="")
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.lithos.status == "degraded"
        assert "not configured" in loop.state.lithos.detail.lower()


def lithos_url_in_detail(detail: str, port: int) -> bool:
    """Helper: verify detail contains the URL (unreachable reason)."""
    return f"127.0.0.1:{port}" in detail or "unreachable" in detail.lower()


# ── LLM-credentials probe ────────────────────────────────────────────


class TestLLMCredentialsProbe:
    """LLM-credentials probe checks configured providers' api_key_env."""

    def test_all_credentials_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Probe passes when all configured providers have their api_key_env set."""
        monkeypatch.setenv("MY_API_KEY", "some-value")
        providers = {
            "my-provider": ProviderConfig(
                base_url="https://api.example.com", api_key_env="MY_API_KEY"
            ),
        }
        cfg = _make_config(providers)
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.llm_credentials.status == "ok"

    def test_missing_credential_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing provider credential → degraded state."""
        monkeypatch.delenv("MISSING_KEY", raising=False)
        providers = {
            "broken-provider": ProviderConfig(
                base_url="https://api.example.com", api_key_env="MISSING_KEY"
            ),
        }
        cfg = _make_config(providers)
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.llm_credentials.status == "degraded"
        assert "MISSING_KEY" in loop.state.llm_credentials.detail

    def test_keyless_provider_skipped(self) -> None:
        """Providers with api_key_env='' are skipped (keyless, e.g. Ollama)."""
        providers = {
            "ollama": ProviderConfig(base_url="http://localhost:11434", api_key_env=""),
        }
        cfg = _make_config(providers)
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.llm_credentials.status == "ok"

    def test_mixed_providers_keyless_and_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mix of keyless and keyed providers — all ok when keyed vars are set."""
        monkeypatch.setenv("OPENAI_KEY", "test-key")
        providers = {
            "ollama": ProviderConfig(base_url="http://localhost:11434", api_key_env=""),
            "openai": ProviderConfig(
                base_url="https://api.openai.com", api_key_env="OPENAI_KEY"
            ),
        }
        cfg = _make_config(providers)
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.llm_credentials.status == "ok"

    def test_mixed_providers_one_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One missing key among several providers → degraded."""
        monkeypatch.setenv("GOOD_KEY", "present")
        monkeypatch.delenv("BAD_KEY", raising=False)
        providers = {
            "good": ProviderConfig(
                base_url="https://a.example.com", api_key_env="GOOD_KEY"
            ),
            "bad": ProviderConfig(
                base_url="https://b.example.com", api_key_env="BAD_KEY"
            ),
        }
        cfg = _make_config(providers)
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.llm_credentials.status == "degraded"
        assert "BAD_KEY" in loop.state.llm_credentials.detail


# ── ProbeState aggregate ─────────────────────────────────────────────


class TestProbeState:
    """ProbeState aggregates individual probe results."""

    def test_initial_state_is_starting(self) -> None:
        """Before first probe, overall status is 'starting'."""
        state = ProbeState()
        assert state.overall_status == "starting"
        assert state.is_ready is True  # no failures yet, just no data

    def test_all_ok_means_ready(self) -> None:
        """When both probes are ok, state is ready and status is ok."""
        state = ProbeState(
            lithos=ProbeResult(status="ok", timestamp=1.0),
            llm_credentials=ProbeResult(status="ok", timestamp=1.0),
        )
        assert state.is_ready is True
        assert state.overall_status == "ok"

    def test_lithos_degraded_means_not_ready(self) -> None:
        """Degraded Lithos → not ready, overall status degraded."""
        state = ProbeState(
            lithos=ProbeResult(status="degraded", timestamp=1.0),
            llm_credentials=ProbeResult(status="ok", timestamp=1.0),
        )
        assert state.is_ready is False
        assert state.overall_status == "degraded"

    def test_llm_degraded_means_not_ready(self) -> None:
        """Degraded LLM credentials → not ready, overall status degraded."""
        state = ProbeState(
            lithos=ProbeResult(status="ok", timestamp=1.0),
            llm_credentials=ProbeResult(status="degraded", timestamp=1.0),
        )
        assert state.is_ready is False
        assert state.overall_status == "degraded"


# ── Timestamps ────────────────────────────────────────────────────────


class TestTimestamps:
    """Probe results include timestamps for staleness checks."""

    def test_timestamps_are_recorded(self) -> None:
        """run_once() populates non-zero timestamps on both probes."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.lithos.timestamp > 0
        assert loop.state.llm_credentials.timestamp > 0

    def test_timestamps_update_on_subsequent_runs(self) -> None:
        """Subsequent run_once() calls update timestamps."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        t1 = loop.state.lithos.timestamp
        loop.run_once()
        t2 = loop.state.lithos.timestamp
        assert t2 >= t1


# ── Staleness (US-002 stale-cache requirement) ────────────────────────


class TestStaleProbeCache:
    """Cached probe results that exceed ``max_age`` are treated as stale.

    A stale cache must NOT count as ready (US-002), even if the last
    cached status was ``ok``.
    """

    def test_fresh_cache_is_ready(self) -> None:
        """Recent timestamps → is_ready stays True."""
        import time as time_mod

        now = time_mod.monotonic()
        state = ProbeState(
            lithos=ProbeResult(status="ok", timestamp=now),
            llm_credentials=ProbeResult(status="ok", timestamp=now),
            max_age=60.0,
        )
        assert state.is_stale() is False
        assert state.is_ready is True
        assert state.overall_status == "ok"

    def test_stale_cache_is_not_ready(self) -> None:
        """Timestamps older than max_age → stale → not ready, degraded."""
        import time as time_mod

        old = time_mod.monotonic() - 1_000.0
        state = ProbeState(
            lithos=ProbeResult(status="ok", timestamp=old),
            llm_credentials=ProbeResult(status="ok", timestamp=old),
            max_age=60.0,
        )
        assert state.is_stale() is True
        assert state.is_ready is False
        assert state.overall_status == "degraded"

    def test_max_age_zero_disables_staleness_check(self) -> None:
        """max_age=0.0 → staleness check disabled (back-compat)."""
        import time as time_mod

        old = time_mod.monotonic() - 1_000.0
        state = ProbeState(
            lithos=ProbeResult(status="ok", timestamp=old),
            llm_credentials=ProbeResult(status="ok", timestamp=old),
            max_age=0.0,
        )
        assert state.is_stale() is False
        assert state.is_ready is True

    def test_starting_state_is_not_stale(self) -> None:
        """Before the first probe cycle, is_stale() returns False."""
        state = ProbeState(max_age=60.0)
        assert state.is_stale() is False
        assert state.overall_status == "starting"

    def test_probe_loop_sets_max_age_from_interval(self) -> None:
        """ProbeLoop defaults max_age to 3× interval."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.max_age == 90.0

    def test_probe_loop_custom_max_age(self) -> None:
        """ProbeLoop accepts an explicit max_age override."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0, max_age=120.0)
        loop.run_once()
        assert loop.state.max_age == 120.0

    def test_stale_cache_forces_degraded_status(self) -> None:
        """After enough wall-clock time, cached results go stale."""
        import time as time_mod

        cfg = _make_config()
        # Very short max_age so the next call is already stale.
        loop = ProbeLoop(cfg, interval=30.0, max_age=0.01)
        loop.run_once()
        # Allow the cached timestamp to age past max_age.
        time_mod.sleep(0.05)
        assert loop.state.is_stale() is True
        assert loop.state.is_ready is False
        assert loop.state.overall_status == "degraded"


# ── ProbeLoop lifecycle ──────────────────────────────────────────────


class TestProbeLoopLifecycle:
    """ProbeLoop start/stop and interval validation."""

    def test_interval_must_be_at_least_30(self) -> None:
        """Interval < 30s raises ValueError."""
        cfg = _make_config()
        with pytest.raises(ValueError, match="30"):
            ProbeLoop(cfg, interval=10.0)

    async def test_start_populates_state_immediately(self) -> None:
        """After start(), cached state is populated (not still starting)."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        await loop.start()
        try:
            assert loop.state.overall_status != "starting"
            assert loop.state.lithos.timestamp > 0
        finally:
            await loop.stop()

    async def test_stop_cancels_task(self) -> None:
        """stop() cleanly cancels the background task."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        await loop.start()
        await loop.stop()
        assert loop._task is None

    async def test_double_start_is_idempotent(self) -> None:
        """Calling start() twice doesn't create a second task."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        await loop.start()
        task1 = loop._task
        await loop.start()
        task2 = loop._task
        assert task1 is task2
        await loop.stop()

    async def test_double_stop_is_safe(self) -> None:
        """Calling stop() when not started doesn't raise."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        await loop.stop()  # no-op, should not raise


# ── Degraded state driven by both probes ─────────────────────────────


class TestDegradedState:
    """The probe can be driven into a degraded state by either probe."""

    def test_degraded_via_lithos_unreachable(self) -> None:
        """Unreachable Lithos SSE endpoint drives overall state to degraded."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = _make_config(lithos_url=f"http://127.0.0.1:{port}/sse")
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.overall_status == "degraded"
        assert loop.state.is_ready is False

    def test_degraded_via_missing_credential(
        self, fake_lithos_sse_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing provider credential drives overall state to degraded."""
        monkeypatch.delenv("NONEXISTENT_KEY", raising=False)
        providers = {
            "p1": ProviderConfig(
                base_url="https://api.example.com", api_key_env="NONEXISTENT_KEY"
            ),
        }
        cfg = _make_config(providers, lithos_url=fake_lithos_sse_url)
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.overall_status == "degraded"
        assert loop.state.is_ready is False


# ── config.py: load_config with check_api_keys=False ─────────────────


class TestLoadConfigRelaxedApiKeys:
    """load_config(check_api_keys=False) allows startup with missing keys."""

    def test_missing_key_no_raise_when_relaxed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With check_api_keys=False, missing api_key_env does not raise."""
        toml = dedent("""\
            [providers.openai]
            base_url = "https://api.openai.com/v1"
            api_key_env = "OPENAI_API_KEY"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = tmp_path / "influx.toml"
        config_path.write_text(toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        cfg = load_config(check_api_keys=False)

        assert cfg.providers["openai"].api_key_env == "OPENAI_API_KEY"

    def test_missing_key_still_raises_when_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With check_api_keys=True (default), missing key still raises."""
        from influx.errors import ConfigError

        toml = dedent("""\
            [providers.openai]
            base_url = "https://api.openai.com/v1"
            api_key_env = "OPENAI_API_KEY"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = tmp_path / "influx.toml"
        config_path.write_text(toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
            load_config(check_api_keys=True)


# ── Repair-write-failure readiness latch (US-011, finding #5) ─────────


class TestRepairWriteFailureLatch:
    """Terminal sweep-write failures degrade readiness.

    Per US-011 (PRD 06 §5.4 failure mode 1), an unresolved
    ``version_conflict`` after the FR-MCP-7 retry — or a generic write
    transport failure — must flip ``/ready`` to degraded until the
    next successful repair sweep clears the latch.
    """

    def test_default_latch_is_clear(self) -> None:
        """Fresh state has no repair-write-failure latch."""
        state = ProbeState()
        assert state.repair_write_failure is False

    def test_latch_makes_state_not_ready(self) -> None:
        """Setting the latch flips is_ready False even with ok probes."""
        import time as time_mod

        now = time_mod.monotonic()
        state = ProbeState(
            lithos=ProbeResult(status="ok", timestamp=now),
            llm_credentials=ProbeResult(status="ok", timestamp=now),
            repair_write_failure=True,
        )
        assert state.is_ready is False
        assert state.overall_status == "degraded"

    def test_clear_latch_returns_ready(self) -> None:
        """Clearing the latch returns is_ready to True (probes ok)."""
        import time as time_mod

        now = time_mod.monotonic()
        state = ProbeState(
            lithos=ProbeResult(status="ok", timestamp=now),
            llm_credentials=ProbeResult(status="ok", timestamp=now),
            repair_write_failure=False,
        )
        assert state.is_ready is True
        assert state.overall_status == "ok"

    def test_probe_loop_mark_and_clear_repair_failure(self) -> None:
        """ProbeLoop exposes mark/clear methods that update state."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        # Clean baseline.
        assert loop.state.repair_write_failure is False

        loop.mark_repair_write_failure(profile="ai-robotics", detail="abort")
        assert loop.state.repair_write_failure is True
        assert loop.state.is_ready is False

        # Latch persists across the next probe cycle until cleared.
        loop.run_once()
        assert loop.state.repair_write_failure is True

        loop.clear_repair_write_failure()
        assert loop.state.repair_write_failure is False


# ── #40: Lithos circuit breaker ──────────────────────────────────────


class TestLithosCircuitBreaker:
    """ProbeLoop tracks consecutive degraded Lithos probes (#40)."""

    def test_starts_closed(self, fake_lithos_sse_url: str) -> None:
        cfg = _make_config(lithos_url=fake_lithos_sse_url)
        loop = ProbeLoop(cfg, interval=30.0)
        assert loop.lithos_unhealthy_consecutive == 0
        assert loop.lithos_circuit_open() is False

    def test_increments_on_degraded_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each degraded probe bumps the consecutive counter."""
        from influx import probes as _probes

        cfg = _make_config(lithos_url="http://127.0.0.1:1/sse")  # unreachable
        # Stub the lithos probe so we don't pay an actual TCP timeout.
        monkeypatch.setattr(
            _probes,
            "_probe_lithos",
            lambda url: ProbeResult(status="degraded", detail="stub", timestamp=1.0),
        )
        loop = ProbeLoop(cfg, interval=30.0)
        for expected in (1, 2, 3):
            loop.run_once()
            assert loop.lithos_unhealthy_consecutive == expected

    def test_opens_at_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default threshold of 3 — breaker stays closed at 2, opens at 3."""
        from influx import probes as _probes

        cfg = _make_config(lithos_url="http://127.0.0.1:1/sse")
        monkeypatch.setattr(
            _probes,
            "_probe_lithos",
            lambda url: ProbeResult(status="degraded", detail="stub", timestamp=1.0),
        )
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        loop.run_once()
        assert loop.lithos_circuit_open() is False
        loop.run_once()
        assert loop.lithos_circuit_open() is True

    def test_threshold_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from influx import probes as _probes

        cfg = _make_config(lithos_url="http://127.0.0.1:1/sse")
        monkeypatch.setattr(
            _probes,
            "_probe_lithos",
            lambda url: ProbeResult(status="degraded", detail="stub", timestamp=1.0),
        )
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.lithos_circuit_open(threshold=1) is True
        assert loop.lithos_circuit_open(threshold=2) is False

    def test_resets_on_first_ok_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Breaker closes automatically on the first ``ok`` probe."""
        from influx import probes as _probes

        cfg = _make_config(lithos_url="http://127.0.0.1:1/sse")
        # Three degraded probes, then one ok — counter must reset.
        responses = iter(
            [
                ProbeResult(status="degraded", detail="x", timestamp=1.0),
                ProbeResult(status="degraded", detail="x", timestamp=2.0),
                ProbeResult(status="degraded", detail="x", timestamp=3.0),
                ProbeResult(status="ok", detail="", timestamp=4.0),
            ]
        )
        monkeypatch.setattr(_probes, "_probe_lithos", lambda url: next(responses))
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        loop.run_once()
        loop.run_once()
        assert loop.lithos_circuit_open() is True
        loop.run_once()
        assert loop.lithos_unhealthy_consecutive == 0
        assert loop.lithos_circuit_open() is False


# ── LCMA tool-availability probe (issue #69) ────────────────────────


class TestLcmaToolsProbe:
    """Probe-time LCMA tool-availability check (issue #69).

    Replaces the legacy mid-run ``LCMAError("unknown_tool")`` latch
    with a probe-driven, non-sticky latch flipped from ``tools/list``.
    """

    @pytest.mark.asyncio
    async def test_all_required_tools_present_clears_latch(self) -> None:
        """Tool list containing every required name → latch cleared."""
        from influx.probes import REQUIRED_LCMA_TOOLS

        async def lister() -> list[str]:
            return list(REQUIRED_LCMA_TOOLS) + ["lithos_write", "lithos_ping"]

        cfg = _make_config(lithos_url="http://127.0.0.1:1/sse")
        loop = ProbeLoop(cfg, interval=30.0, tool_lister=lister)
        await loop.run_once_async()

        assert loop.lcma_tools_unavailable() is False
        assert loop.state.lcma_unknown_tool_failure is False

    @pytest.mark.asyncio
    async def test_missing_required_tool_flips_latch(self) -> None:
        """Tool list missing one required name → latch flips, detail names it."""

        async def lister() -> list[str]:
            return [
                "lithos_write",
                "lithos_task_create",
                "lithos_task_complete",
                "lithos_cache_lookup",
                "lithos_edge_upsert",
                # ``lithos_retrieve`` deliberately missing
            ]

        cfg = _make_config(lithos_url="http://127.0.0.1:1/sse")
        loop = ProbeLoop(cfg, interval=30.0, tool_lister=lister)
        await loop.run_once_async()

        assert loop.lcma_tools_unavailable() is True
        assert loop.state.lcma_unknown_tool_failure is True
        assert "lithos_retrieve" in loop.state.lcma_unknown_tool_failure_detail

    @pytest.mark.asyncio
    async def test_tools_list_transport_error_flips_latch(self) -> None:
        """tools/list raising → latch set with degraded detail (transport error)."""

        async def failing_lister() -> list[str]:
            raise RuntimeError("MCP connection refused")

        cfg = _make_config(lithos_url="http://127.0.0.1:1/sse")
        loop = ProbeLoop(cfg, interval=30.0, tool_lister=failing_lister)
        await loop.run_once_async()

        assert loop.lcma_tools_unavailable() is True
        assert "tools/list failed" in loop.state.lcma_unknown_tool_failure_detail

    @pytest.mark.asyncio
    async def test_no_tool_lister_skips_probe_and_clears_latch(self) -> None:
        """``tool_lister=None`` → probe is a no-op, latch stays cleared."""
        cfg = _make_config(lithos_url="http://127.0.0.1:1/sse")
        loop = ProbeLoop(cfg, interval=30.0, tool_lister=None)
        await loop.run_once_async()

        assert loop.lcma_tools_unavailable() is False
        assert loop.state.lcma_unknown_tool_failure is False

    @pytest.mark.asyncio
    async def test_latch_recovers_when_tools_become_available(self) -> None:
        """Non-sticky behaviour: missing → present across cycles re-clears."""
        from influx.probes import REQUIRED_LCMA_TOOLS

        cycle_state = {"missing_retrieve": True}

        async def lister() -> list[str]:
            base = list(REQUIRED_LCMA_TOOLS)
            if cycle_state["missing_retrieve"]:
                return [t for t in base if t != "lithos_retrieve"]
            return base

        cfg = _make_config(lithos_url="http://127.0.0.1:1/sse")
        loop = ProbeLoop(cfg, interval=30.0, tool_lister=lister)
        await loop.run_once_async()
        assert loop.lcma_tools_unavailable() is True

        # Deployment fixed mid-flight; next probe cycle clears the latch.
        cycle_state["missing_retrieve"] = False
        await loop.run_once_async()
        assert loop.lcma_tools_unavailable() is False
        assert loop.state.lcma_unknown_tool_failure is False
