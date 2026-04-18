"""Smoke test for the ``main()`` entry point."""

from pathlib import Path

import pytest

from influx.main import main


def test_main_prints_greeting(
    influx_config_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Silence unused-arg warning — the fixture sets INFLUX_CONFIG.
    assert influx_config_env.exists()

    main()

    captured = capsys.readouterr()
    assert captured.out.strip() == "Hello from Influx (test)"
