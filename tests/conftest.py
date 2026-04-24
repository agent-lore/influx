"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

# Minimal valid v0.7 TOML (prompts section is required by AppConfig).
_MINIMAL_V07_TOML = dedent("""\
    [influx]
    note_schema_version = 1

    [prompts.filter]
    text = "f {profile_description} {negative_examples} {min_score_in_results}"

    [prompts.tier1_enrich]
    text = "e {title} {abstract} {profile_summary}"

    [prompts.tier3_extract]
    text = "x {title} {full_text}"
""")


@pytest.fixture
def influx_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a minimal v0.7 influx.toml and point ``INFLUX_CONFIG`` at it.

    Env-var overrides are cleared so a developer's local ``.env`` cannot
    silently inject values via ``load_dotenv``.
    """
    config_path = tmp_path / "influx.toml"
    config_path.write_text(_MINIMAL_V07_TOML)
    monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
    return config_path
