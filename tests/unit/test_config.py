"""Tests for TOML config loading, discovery order, and env-var overrides."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from influx.config import AppConfig, load_config
from influx.errors import ConfigError

# Minimal valid v0.7 TOML (prompts required).
_MINIMAL_TOML = dedent("""\
    [prompts.filter]
    text = "f {profile_description} {negative_examples} {min_score_in_results}"

    [prompts.tier1_enrich]
    text = "e {title} {abstract} {profile_summary}"

    [prompts.tier3_extract]
    text = "x {title} {full_text}"
""")


def _write_config(directory: Path, content: str = _MINIMAL_TOML) -> Path:
    """Write TOML content to ``influx.toml`` inside *directory*."""
    p = directory / "influx.toml"
    p.write_text(content)
    return p


# ── INFLUX_CONFIG env-var respected ──────────────────────────────────


class TestInfluxConfigEnvVar:
    """INFLUX_CONFIG takes precedence over discovery fallback (FR-CFG-2)."""

    def test_influx_config_env_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _write_config(tmp_path)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        cfg = load_config()

        assert isinstance(cfg, AppConfig)

    def test_influx_config_missing_file_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "nonexistent.toml"
        monkeypatch.setenv("INFLUX_CONFIG", str(missing))

        with pytest.raises(ConfigError, match="INFLUX_CONFIG"):
            load_config()

    def test_explicit_path_arg_bypasses_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a *path* argument is given, INFLUX_CONFIG is ignored."""
        config_path = _write_config(tmp_path)
        monkeypatch.setenv("INFLUX_CONFIG", "/does/not/exist.toml")

        cfg = load_config(path=config_path)

        assert isinstance(cfg, AppConfig)


# ── Fallback discovery ordering ──────────────────────────────────────


class TestFallbackDiscovery:
    """Discovery order: cwd, then home, then /etc."""

    def test_cwd_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First candidate (cwd) wins when INFLUX_CONFIG is unset."""
        monkeypatch.delenv("INFLUX_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        _write_config(tmp_path)

        cfg = load_config()

        assert isinstance(cfg, AppConfig)

    def test_home_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second candidate (~/.influx/influx.toml) wins when cwd has none."""
        monkeypatch.delenv("INFLUX_CONFIG", raising=False)
        # Empty cwd — no influx.toml here.
        empty_cwd = tmp_path / "empty"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)

        # Fake home dir with ~/.influx/influx.toml
        fake_home = tmp_path / "fakehome"
        influx_dir = fake_home / ".influx"
        influx_dir.mkdir(parents=True)
        _write_config(influx_dir)
        monkeypatch.setenv("HOME", str(fake_home))

        cfg = load_config()

        assert isinstance(cfg, AppConfig)

    def test_no_config_found_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("INFLUX_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        # Set HOME to a dir with no .influx/influx.toml
        monkeypatch.setenv("HOME", str(tmp_path / "emptyhome"))
        (tmp_path / "emptyhome").mkdir()

        with pytest.raises(ConfigError, match="No influx.toml found"):
            load_config()


# ── TOML syntax error ────────────────────────────────────────────────


class TestTomlSyntaxError:
    """Invalid TOML raises ConfigError with a meaningful message."""

    def test_invalid_toml_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = tmp_path / "influx.toml"
        bad.write_text("this is [not valid toml ===")
        monkeypatch.setenv("INFLUX_CONFIG", str(bad))

        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config()


# ── Parsed model structure ───────────────────────────────────────────


class TestParsedModel:
    """load_config returns a correctly typed AppConfig."""

    def test_defaults_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(tmp_path)
        monkeypatch.setenv("INFLUX_CONFIG", str(tmp_path / "influx.toml"))

        cfg = load_config()

        assert cfg.influx.note_schema_version == 1
        assert cfg.schedule.cron == "0 6 * * *"
        assert cfg.repair.max_items_per_run == 100

    def test_custom_values_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        toml = dedent("""\
            [influx]
            note_schema_version = 2

            [schedule]
            cron = "0 12 * * *"

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

        cfg = load_config()

        assert cfg.influx.note_schema_version == 2
        assert cfg.schedule.cron == "0 12 * * *"

    def test_schema_validation_error_wraps_as_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A TOML file that violates the pydantic schema raises ConfigError."""
        # Missing required [prompts] section
        bad_toml = tmp_path / "influx.toml"
        bad_toml.write_text("[influx]\nnote_schema_version = 1\n")
        monkeypatch.setenv("INFLUX_CONFIG", str(bad_toml))

        with pytest.raises(ConfigError):
            load_config()


# ── Environment variable overrides (US-006, FR-CFG-3, AC-01-F) ──────


class TestEnvVarOverrides:
    """Env vars listed in §19 with 'Overrides config key' beat TOML values."""

    def test_influx_archive_dir_overrides_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INFLUX_ARCHIVE_DIR overrides storage.archive_dir (path key)."""
        toml = dedent("""\
            [storage]
            archive_dir = "/archive/original"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.setenv("INFLUX_ARCHIVE_DIR", "/override/archive")

        cfg = load_config()

        assert cfg.storage.archive_dir == "/override/archive"

    def test_influx_otel_enabled_overrides_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INFLUX_OTEL_ENABLED overrides telemetry.enabled (bool key)."""
        toml = dedent("""\
            [telemetry]
            enabled = false

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.setenv("INFLUX_OTEL_ENABLED", "true")

        cfg = load_config()

        assert cfg.telemetry.enabled is True

    def test_agent_zero_webhook_url_overrides_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENT_ZERO_WEBHOOK_URL overrides notifications.webhook_url (str)."""
        toml = dedent("""\
            [notifications]
            webhook_url = "https://original.example.com/hook"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.setenv(
            "AGENT_ZERO_WEBHOOK_URL", "https://override.example.com/hook"
        )

        cfg = load_config()

        assert cfg.notifications.webhook_url == "https://override.example.com/hook"

    def test_otel_console_fallback_overrides_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INFLUX_OTEL_CONSOLE_FALLBACK overrides telemetry.console_fallback."""
        toml = dedent("""\
            [telemetry]
            console_fallback = false

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.setenv("INFLUX_OTEL_CONSOLE_FALLBACK", "true")

        cfg = load_config()

        assert cfg.telemetry.console_fallback is True

    def test_unset_env_var_leaves_toml_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset env vars do not affect TOML values."""
        toml = dedent("""\
            [storage]
            archive_dir = "/archive/original"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.delenv("INFLUX_ARCHIVE_DIR", raising=False)

        cfg = load_config()

        assert cfg.storage.archive_dir == "/archive/original"

    def test_env_overrides_create_section_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env override works even if the target section is missing in TOML."""
        config_path = _write_config(tmp_path)  # minimal TOML — no [storage]
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.setenv("INFLUX_ARCHIVE_DIR", "/env-only-archive")

        cfg = load_config()

        assert cfg.storage.archive_dir == "/env-only-archive"

    def test_bool_env_false_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bool env vars with false-like values are parsed as False."""
        toml = dedent("""\
            [telemetry]
            enabled = true

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.setenv("INFLUX_OTEL_ENABLED", "false")

        cfg = load_config()

        assert cfg.telemetry.enabled is False


# ── Profile, RSS, and provider validations (US-007) ─────────────────


class TestProfileNameValidation:
    """Profile names must match ^[a-z][a-z0-9-]{0,31}$ (FR-CFG-4, AC-M1-2)."""

    def test_invalid_profile_name_uppercase_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uppercase profile name raises ConfigError naming the profile."""
        toml = dedent("""\
            [[profiles]]
            name = "MyProfile"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        with pytest.raises(ConfigError, match="MyProfile"):
            load_config()

    def test_invalid_profile_name_starts_with_digit_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Profile name starting with a digit raises ConfigError."""
        toml = dedent("""\
            [[profiles]]
            name = "123abc"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        with pytest.raises(ConfigError, match="123abc"):
            load_config()

    def test_valid_profile_name_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A well-formed profile name passes validation."""
        toml = dedent("""\
            [[profiles]]
            name = "ai-research"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        cfg = load_config()

        assert cfg.profiles[0].name == "ai-research"


class TestRssSourceTagValidation:
    """RSS source_tag must be present and exactly 'rss' or 'blog'."""

    def test_missing_source_tag_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing source_tag on RSS entry raises ConfigError (AC-01-C)."""
        toml = dedent("""\
            [[profiles]]
            name = "test-profile"

            [[profiles.sources.rss]]
            name = "Test Feed"
            url = "https://example.com/feed.xml"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        with pytest.raises(ConfigError):
            load_config()

    def test_invalid_source_tag_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """source_tag='web' raises ConfigError (AC-01-D)."""
        toml = dedent("""\
            [[profiles]]
            name = "test-profile"

            [[profiles.sources.rss]]
            name = "Test Feed"
            url = "https://example.com/feed.xml"
            source_tag = "web"

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        with pytest.raises(ConfigError):
            load_config()


class TestProviderApiKeyEnvValidation:
    """Provider api_key_env validation (FR-CFG-8, AC-01-E)."""

    def test_missing_api_key_env_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset api_key_env env var raises ConfigError (AC-01-E)."""
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
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
            load_config()

    def test_keyless_provider_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """api_key_env='' skips the env check (keyless providers like Ollama)."""
        toml = dedent("""\
            [providers.ollama]
            base_url = "http://localhost:11434"
            api_key_env = ""

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        cfg = load_config()

        assert cfg.providers["ollama"].api_key_env == ""


# ── influx.example.toml round-trip (US-008) ──────────────────────────


class TestExampleTomlRoundTrip:
    """influx.example.toml loads successfully and matches expected structure."""

    @staticmethod
    def _example_path() -> Path:
        """Return the repo-root influx.example.toml path."""
        return Path(__file__).resolve().parents[2] / "influx.example.toml"

    def test_example_toml_loads_successfully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_config() accepts influx.example.toml without error."""
        example = self._example_path()
        assert example.exists(), f"influx.example.toml not found at {example}"

        # Set provider API keys so api_key_env validation passes.
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

        cfg = load_config(path=example)

        assert isinstance(cfg, AppConfig)

    def test_example_toml_has_expected_profiles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Example includes one arXiv profile and one RSS-only profile."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

        cfg = load_config(path=self._example_path())

        assert len(cfg.profiles) == 2
        names = {p.name for p in cfg.profiles}
        assert "ai-robotics" in names
        assert "web-tech" in names

        # ai-robotics has arXiv enabled and RSS feeds
        arxiv_profile = next(p for p in cfg.profiles if p.name == "ai-robotics")
        assert arxiv_profile.sources.arxiv.enabled is True
        assert len(arxiv_profile.sources.rss) >= 1

        # web-tech has arXiv disabled
        rss_profile = next(p for p in cfg.profiles if p.name == "web-tech")
        assert rss_profile.sources.arxiv.enabled is False
        assert len(rss_profile.sources.rss) >= 1

    def test_example_toml_has_providers_and_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Example covers providers, all three model slots, and prompts."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

        cfg = load_config(path=self._example_path())

        # Providers — only OpenAI-compatible providers ship by default
        # because the JSON-mode caller in src/influx/enrich.py speaks
        # OpenAI-compatible /chat/completions only (finding #2).
        assert "openai" in cfg.providers
        assert "openrouter" in cfg.providers

        # Model slots (filter, enrich, extract); each must bind to an
        # OpenAI-compatible provider so the JSON-mode caller works.
        assert "filter" in cfg.models
        assert "enrich" in cfg.models
        assert "extract" in cfg.models
        compatible = {"openai", "openrouter"}
        for slot_name, slot in cfg.models.items():
            assert slot.provider in compatible, (
                f"models.{slot_name} provider {slot.provider!r} is not "
                f"OpenAI-compatible — finding #2"
            )

        # Prompts (filter, tier1_enrich, tier3_extract)
        assert cfg.prompts.filter.text is not None
        assert cfg.prompts.tier1_enrich.text is not None
        assert cfg.prompts.tier3_extract.text is not None

    def test_example_toml_has_repair_and_schema_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Example covers [repair] and influx.note_schema_version."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")

        cfg = load_config(path=self._example_path())

        assert cfg.influx.note_schema_version == 1
        assert cfg.repair.max_items_per_run == 100


# ── Repair config knob (US-002, PRD 06 §5.1 FR-REP-1) ───────────────


class TestRepairConfigKnob:
    """repair.max_items_per_run is independently configurable."""

    def test_custom_max_items_per_run_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting repair.max_items_per_run = 50 validates and is accessible."""
        toml = dedent("""\
            [repair]
            max_items_per_run = 50

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        cfg = load_config()

        assert cfg.repair.max_items_per_run == 50

    def test_repair_and_feedback_independently_configurable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """repair and feedback knobs are independent."""
        toml = dedent("""\
            [repair]
            max_items_per_run = 50

            [feedback]
            negative_examples_per_profile = 30

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        cfg = load_config()

        assert cfg.repair.max_items_per_run == 50
        assert cfg.feedback.negative_examples_per_profile == 30
        repair_val = cfg.repair.max_items_per_run
        feedback_val = cfg.feedback.negative_examples_per_profile
        assert repair_val != feedback_val


# ── Extraction config defaults and overrides (US-003, PRD 07 §5.1) ──


class TestExtractionConfigDefaults:
    """ExtractionConfig defaults match PRD 07 §5.1 (FR-ENR-2, FR-ENR-3, FR-RES-5)."""

    def test_min_html_chars_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """extraction.min_html_chars defaults to 1000."""
        _write_config(tmp_path)
        monkeypatch.setenv("INFLUX_CONFIG", str(tmp_path / "influx.toml"))

        cfg = load_config()

        assert cfg.extraction.min_html_chars == 1000

    def test_min_web_chars_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """extraction.min_web_chars defaults to 500."""
        _write_config(tmp_path)
        monkeypatch.setenv("INFLUX_CONFIG", str(tmp_path / "influx.toml"))

        cfg = load_config()

        assert cfg.extraction.min_web_chars == 500

    def test_strip_tags_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """extraction.strip_tags defaults to [script, iframe, object, embed]."""
        _write_config(tmp_path)
        monkeypatch.setenv("INFLUX_CONFIG", str(tmp_path / "influx.toml"))

        cfg = load_config()

        assert cfg.extraction.strip_tags == ["script", "iframe", "object", "embed"]


class TestExtractionConfigOverrides:
    """User-supplied extraction overrides flow through to the parsed config."""

    def test_override_min_html_chars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom extraction.min_html_chars is reflected on the parsed config."""
        toml = dedent("""\
            [extraction]
            min_html_chars = 2000

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        cfg = load_config()

        assert cfg.extraction.min_html_chars == 2000

    def test_override_min_web_chars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom extraction.min_web_chars is reflected on the parsed config."""
        toml = dedent("""\
            [extraction]
            min_web_chars = 800

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        cfg = load_config()

        assert cfg.extraction.min_web_chars == 800

    def test_override_strip_tags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom extraction.strip_tags is reflected on the parsed config."""
        toml = dedent("""\
            [extraction]
            strip_tags = ["script", "style", "noscript"]

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        cfg = load_config()

        assert cfg.extraction.strip_tags == ["script", "style", "noscript"]

    def test_override_all_extraction_knobs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All three knobs overridden simultaneously are reflected."""
        toml = dedent("""\
            [extraction]
            min_html_chars = 3000
            min_web_chars = 1500
            strip_tags = ["form", "input"]

            [prompts.filter]
            text = "f {profile_description} {negative_examples} {min_score_in_results}"

            [prompts.tier1_enrich]
            text = "e {title} {abstract} {profile_summary}"

            [prompts.tier3_extract]
            text = "x {title} {full_text}"
        """)
        config_path = _write_config(tmp_path, toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        cfg = load_config()

        assert cfg.extraction.min_html_chars == 3000
        assert cfg.extraction.min_web_chars == 1500
        assert cfg.extraction.strip_tags == ["form", "input"]


class TestConftestFixture:
    """The conftest influx_config_env fixture yields a loadable config."""

    def test_fixture_loads_successfully(self, influx_config_env: Path) -> None:
        """The conftest fixture produces a valid multi-section config."""
        cfg = load_config()

        assert isinstance(cfg, AppConfig)
        assert len(cfg.profiles) == 2
        assert "test-provider" in cfg.providers
        assert "filter" in cfg.models
        assert "enrich" in cfg.models
        assert "extract" in cfg.models
        assert cfg.repair.max_items_per_run == 100
