"""Tests for the argparse CLI dispatcher in ``influx.main``."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from influx.main import main


class TestValidateConfigSuccess:
    """AC-M1-1: ``validate-config`` on a valid config exits 0 and prints."""

    def test_valid_config_exits_zero_and_prints_json(
        self,
        influx_config_env: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert influx_config_env.exists()

        main(["validate-config"])

        captured = capsys.readouterr()
        assert captured.out.strip().startswith("{")
        assert '"profiles"' in captured.out


class TestValidateConfigInvalidProfile:
    """AC-M1-2: ``validate-config`` on an invalid profile name exits non-zero."""

    def test_invalid_profile_name_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bad_toml = dedent("""\
            [[profiles]]
            name = "INVALID_NAME"

            [prompts.filter]
            text = "{profile_description} {negative_examples} {min_score_in_results}"
            [prompts.tier1_enrich]
            text = "{title} {abstract} {profile_summary}"
            [prompts.tier3_extract]
            text = "{title} {full_text}"
        """)
        config_path = tmp_path / "influx.toml"
        config_path.write_text(bad_toml)
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        with pytest.raises(SystemExit) as exc_info:
            main(["validate-config"])

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "INVALID_NAME" in captured.err


class TestNoSubcommand:
    """AC-X-5: no subcommand prints help and exits non-zero."""

    def test_no_subcommand_prints_help_and_exits_nonzero(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main([])

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        # Help output should mention the program and available commands.
        combined = captured.out + captured.err
        assert "validate-config" in combined
