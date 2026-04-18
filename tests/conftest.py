"""Shared pytest fixtures."""

from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture
def influx_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a minimal influx.toml and point ``INFLUX_CONFIG`` at it.

    Env-var overrides are cleared so a developer's local ``.env`` cannot
    silently inject values via ``load_dotenv``.
    """
    data_dir = tmp_path / "data"
    config_path = tmp_path / "influx.toml"
    config_path.write_text(
        dedent(
            f"""
            [influx]
            environment = "test"
            greeting = "Hello"

            [influx.storage]
            data_dir = "{data_dir}"

            [influx.logging]
            level = "info"
            """
        )
    )
    monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
    monkeypatch.setenv("INFLUX_ENVIRONMENT", "")
    monkeypatch.setenv("INFLUX_DATA_DIR", "")
    monkeypatch.setenv("INFLUX_LOG_LEVEL", "")
    return config_path
