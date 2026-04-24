"""Tests for the background probe loop and cached state (US-002)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from influx.config import AppConfig, ProviderConfig, load_config
from influx.probes import ProbeLoop, ProbeResult, ProbeState

# ── Helper: build a minimal AppConfig with providers ─────────────────


def _make_config(
    providers: dict[str, ProviderConfig] | None = None,
) -> AppConfig:
    """Return a minimal ``AppConfig`` with the given providers."""
    return AppConfig(
        providers=providers or {},
        prompts={
            "filter": {"text": "f"},
            "tier1_enrich": {"text": "e"},
            "tier3_extract": {"text": "x"},
        },
    )


# ── Lithos stub probe ────────────────────────────────────────────────


class TestLithosProbe:
    """Lithos probe is a stub: ok by default, degraded under env var."""

    def test_lithos_ok_by_default(self) -> None:
        """Default Lithos probe returns ``ok`` (AC: stub returns ok)."""
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.lithos.status == "ok"
        assert loop.state.lithos.timestamp > 0

    def test_lithos_degraded_under_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INFLUX_TEST_LITHOS_DOWN=1 → Lithos probe returns degraded (AC-03-C)."""
        monkeypatch.setenv("INFLUX_TEST_LITHOS_DOWN", "1")
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.lithos.status == "degraded"
        assert loop.state.lithos.timestamp > 0

    def test_lithos_ok_when_env_not_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lithos probe returns ok when INFLUX_TEST_LITHOS_DOWN is unset."""
        monkeypatch.delenv("INFLUX_TEST_LITHOS_DOWN", raising=False)
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.lithos.status == "ok"


# ── LLM-credentials probe ────────────────────────────────────────────


class TestLLMCredentialsProbe:
    """LLM-credentials probe checks configured providers' api_key_env."""

    def test_all_credentials_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_missing_credential_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
            "ollama": ProviderConfig(
                base_url="http://localhost:11434", api_key_env=""
            ),
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
            "ollama": ProviderConfig(
                base_url="http://localhost:11434", api_key_env=""
            ),
            "openai": ProviderConfig(
                base_url="https://api.openai.com", api_key_env="OPENAI_KEY"
            ),
        }
        cfg = _make_config(providers)
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.llm_credentials.status == "ok"

    def test_mixed_providers_one_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_degraded_via_lithos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INFLUX_TEST_LITHOS_DOWN=1 drives overall state to degraded."""
        monkeypatch.setenv("INFLUX_TEST_LITHOS_DOWN", "1")
        cfg = _make_config()
        loop = ProbeLoop(cfg, interval=30.0)
        loop.run_once()
        assert loop.state.overall_status == "degraded"
        assert loop.state.is_ready is False

    def test_degraded_via_missing_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing provider credential drives overall state to degraded."""
        monkeypatch.delenv("NONEXISTENT_KEY", raising=False)
        providers = {
            "p1": ProviderConfig(
                base_url="https://api.example.com", api_key_env="NONEXISTENT_KEY"
            ),
        }
        cfg = _make_config(providers)
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
