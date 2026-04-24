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
