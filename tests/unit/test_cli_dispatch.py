"""Tests for the v1 CLI argparse surface and exit-code policy."""

from __future__ import annotations

import pytest

from influx.main import EXIT_SUCCESS, EXIT_USAGE, main


class TestServeSubcommand:
    """serve subcommand routes to its stub handler."""

    def test_serve_exits_64(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["serve"])

        assert exc_info.value.code == EXIT_USAGE
        captured = capsys.readouterr()
        assert "stub" in captured.err.lower()

    def test_serve_takes_no_flags(self) -> None:
        """serve rejects unknown flags (argparse enforces no extra args)."""
        with pytest.raises(SystemExit) as exc_info:
            main(["serve", "--unknown-flag"])

        assert exc_info.value.code != 0


class TestRunSubcommand:
    """run subcommand routes to its stub handler."""

    def test_run_with_profile_exits_64(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["run", "--profile", "ai-robotics"])

        assert exc_info.value.code == EXIT_USAGE
        captured = capsys.readouterr()
        assert "stub" in captured.err.lower()
        assert "ai-robotics" in captured.err

    def test_run_requires_profile(self) -> None:
        """run without --profile exits with an error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["run"])

        assert exc_info.value.code != 0


class TestBackfillSubcommand:
    """backfill subcommand routes to its stub handler."""

    def test_backfill_with_profile_exits_64(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["backfill", "--profile", "web-tech"])

        assert exc_info.value.code == EXIT_USAGE
        captured = capsys.readouterr()
        assert "stub" in captured.err.lower()
        assert "web-tech" in captured.err

    def test_backfill_requires_profile(self) -> None:
        """backfill without --profile exits with an error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["backfill"])

        assert exc_info.value.code != 0

    def test_backfill_accepts_all_flags(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """backfill accepts --days, --from, --to, --confirm."""
        with pytest.raises(SystemExit) as exc_info:
            main([
                "backfill",
                "--profile", "ai-robotics",
                "--days", "7",
                "--from", "2026-01-01",
                "--to", "2026-01-07",
                "--confirm",
            ])

        assert exc_info.value.code == EXIT_USAGE


class TestValidateConfigUnchanged:
    """validate-config continues to route to the existing validator."""

    def test_validate_config_still_works(
        self,
        influx_config_env: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["validate-config"])

        captured = capsys.readouterr()
        assert captured.out.strip().startswith("{")


class TestMigrateNotes:
    """AC-02-F: migrate-notes prints note_schema_version and exits 0."""

    def test_migrate_notes_prints_version_and_exits_0(
        self,
        influx_config_env: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["migrate-notes"])

        captured = capsys.readouterr()
        assert captured.out.strip() == "note_schema_version: 1"

    def test_migrate_notes_uses_config_value(
        self,
        tmp_path: object,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """migrate-notes reads note_schema_version from config."""
        from pathlib import Path
        from textwrap import dedent

        config_path = Path(str(tmp_path)) / "influx.toml"
        config_path.write_text(dedent("""\
            [influx]
            note_schema_version = 42

            [prompts.filter]
            text = "f"
            [prompts.tier1_enrich]
            text = "e"
            [prompts.tier3_extract]
            text = "x"
        """))
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))

        main(["migrate-notes"])

        captured = capsys.readouterr()
        assert captured.out.strip() == "note_schema_version: 42"


class TestUnknownSubcommand:
    """AC-02-E: unknown subcommand exits 64."""

    def test_unknown_subcommand_exits_64(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["foobar"])

        assert exc_info.value.code == EXIT_USAGE
        captured = capsys.readouterr()
        assert "unknown command" in captured.err.lower()

    def test_another_unknown_exits_64(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["deploy"])

        assert exc_info.value.code == EXIT_USAGE


class TestNoSubcommand:
    """AC-X-5 regression: no subcommand prints help and exits non-zero."""

    def test_no_args_prints_help_and_exits_nonzero(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main([])

        assert exc_info.value.code != EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "usage" in captured.err.lower() or "influx" in captured.err.lower()
