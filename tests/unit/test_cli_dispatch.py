"""Tests for the v1 CLI argparse surface and exit-code policy."""

from __future__ import annotations

import httpx
import pytest
import respx

from influx.main import EXIT_FAILURE, EXIT_PARTIAL, EXIT_SUCCESS, EXIT_USAGE, main

# ── serve ─────────────────────────────────────────────────────────────


class TestServeSubcommand:
    """serve subcommand dispatches to the real serve handler."""

    def test_serve_calls_cmd_serve(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """serve routes to _cmd_serve (no longer a stub)."""
        called = False

        def fake_serve() -> None:
            nonlocal called
            called = True

        monkeypatch.setattr("influx.main._cmd_serve", fake_serve)
        main(["serve"])
        assert called

    def test_serve_takes_no_flags(self) -> None:
        """serve rejects unknown flags (argparse enforces no extra args)."""
        with pytest.raises(SystemExit) as exc_info:
            main(["serve", "--unknown-flag"])

        assert exc_info.value.code != 0


# ── run ───────────────────────────────────────────────────────────────


class TestRunSubcommand:
    """run subcommand is a real HTTP client of POST /runs."""

    @respx.mock
    def test_run_happy_path_exit_0(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """202 accepted → exit 0 + printed request_id."""
        respx.post("http://127.0.0.1:8080/runs").mock(
            return_value=httpx.Response(
                202,
                json={
                    "status": "accepted",
                    "request_id": "abc-123",
                    "kind": "manual",
                    "scope": "ai-robotics",
                    "submitted_at": "2026-04-24T00:00:00+00:00",
                },
            )
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["run", "--profile", "ai-robotics"])

        assert exc_info.value.code == EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "abc-123" in captured.out

    @respx.mock
    def test_run_conflict_exit_1(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """409 profile_busy → exit 1."""
        respx.post("http://127.0.0.1:8080/runs").mock(
            return_value=httpx.Response(
                409,
                json={"reason": "profile_busy", "profile": "ai-robotics"},
            )
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["run", "--profile", "ai-robotics"])

        assert exc_info.value.code == EXIT_PARTIAL
        captured = capsys.readouterr()
        assert "busy" in captured.err.lower()

    @respx.mock
    def test_run_network_error_exit_2(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Network error → exit 2."""
        respx.post("http://127.0.0.1:8080/runs").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["run", "--profile", "ai-robotics"])

        assert exc_info.value.code == EXIT_FAILURE
        captured = capsys.readouterr()
        assert "connect" in captured.err.lower()

    def test_run_requires_profile(self) -> None:
        """run without --profile exits with an error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["run"])

        assert exc_info.value.code != 0

    @respx.mock
    def test_run_uses_admin_port_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """run reads INFLUX_ADMIN_PORT from env."""
        monkeypatch.setenv("INFLUX_ADMIN_PORT", "9999")
        respx.post("http://127.0.0.1:9999/runs").mock(
            return_value=httpx.Response(
                202,
                json={
                    "status": "accepted",
                    "request_id": "port-test-id",
                    "kind": "manual",
                    "scope": "ai-robotics",
                    "submitted_at": "2026-04-24T00:00:00+00:00",
                },
            )
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["run", "--profile", "ai-robotics"])

        assert exc_info.value.code == EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "port-test-id" in captured.out


# ── backfill ──────────────────────────────────────────────────────────


class TestBackfillSubcommand:
    """backfill subcommand is a real HTTP client of POST /backfills."""

    @respx.mock
    def test_backfill_happy_path_with_days(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """202 accepted with --days → exit 0 + printed request_id."""
        respx.post("http://127.0.0.1:8080/backfills").mock(
            return_value=httpx.Response(
                202,
                json={
                    "status": "accepted",
                    "request_id": "bf-123",
                    "kind": "backfill",
                    "scope": "ai-robotics",
                    "submitted_at": "2026-04-24T00:00:00+00:00",
                },
            )
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["backfill", "--profile", "ai-robotics", "--days", "7"])

        assert exc_info.value.code == EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "bf-123" in captured.out

    @respx.mock
    def test_backfill_happy_path_with_date_range(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """202 accepted with --from/--to → exit 0."""
        respx.post("http://127.0.0.1:8080/backfills").mock(
            return_value=httpx.Response(
                202,
                json={
                    "status": "accepted",
                    "request_id": "bf-range-456",
                    "kind": "backfill",
                    "scope": "web-tech",
                    "submitted_at": "2026-04-24T00:00:00+00:00",
                },
            )
        )

        with pytest.raises(SystemExit) as exc_info:
            main([
                "backfill",
                "--profile", "web-tech",
                "--from", "2026-01-01",
                "--to", "2026-01-31",
            ])

        assert exc_info.value.code == EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "bf-range-456" in captured.out

    @respx.mock
    def test_backfill_confirm_required_without_confirm_exit_64(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """confirm_required without --confirm → exit 64 + estimate printed."""
        respx.post("http://127.0.0.1:8080/backfills").mock(
            return_value=httpx.Response(
                400,
                json={
                    "reason": "confirm_required",
                    "estimated_items": 5000,
                },
            )
        )

        with pytest.raises(SystemExit) as exc_info:
            main([
                "backfill",
                "--profile", "ai-robotics",
                "--days", "365",
            ])

        assert exc_info.value.code == EXIT_USAGE
        captured = capsys.readouterr()
        assert "5000" in captured.err
        assert "--confirm" in captured.err

    @respx.mock
    def test_backfill_confirm_required_with_confirm_reposts(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """confirm_required + --confirm → re-POST with confirm=true (AC-M3-8)."""
        route = respx.post("http://127.0.0.1:8080/backfills")
        route.side_effect = [
            httpx.Response(
                400,
                json={
                    "reason": "confirm_required",
                    "estimated_items": 5000,
                },
            ),
            httpx.Response(
                202,
                json={
                    "status": "accepted",
                    "request_id": "bf-confirmed-789",
                    "kind": "backfill",
                    "scope": "ai-robotics",
                    "submitted_at": "2026-04-24T00:00:00+00:00",
                },
            ),
        ]

        with pytest.raises(SystemExit) as exc_info:
            main([
                "backfill",
                "--profile", "ai-robotics",
                "--days", "365",
                "--confirm",
            ])

        assert exc_info.value.code == EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "bf-confirmed-789" in captured.out
        # Verify two requests were made.
        assert route.call_count == 2

    @respx.mock
    def test_backfill_conflict_exit_1(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """409 profile_busy → exit 1."""
        respx.post("http://127.0.0.1:8080/backfills").mock(
            return_value=httpx.Response(
                409,
                json={"reason": "profile_busy", "profile": "ai-robotics"},
            )
        )

        with pytest.raises(SystemExit) as exc_info:
            main([
                "backfill",
                "--profile", "ai-robotics",
                "--days", "7",
            ])

        assert exc_info.value.code == EXIT_PARTIAL
        captured = capsys.readouterr()
        assert "busy" in captured.err.lower()

    @respx.mock
    def test_backfill_network_error_exit_2(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Network error → exit 2."""
        respx.post("http://127.0.0.1:8080/backfills").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        with pytest.raises(SystemExit) as exc_info:
            main([
                "backfill",
                "--profile", "ai-robotics",
                "--days", "7",
            ])

        assert exc_info.value.code == EXIT_FAILURE
        captured = capsys.readouterr()
        assert "connect" in captured.err.lower()

    def test_backfill_requires_profile(self) -> None:
        """backfill without --profile exits with an error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["backfill"])

        assert exc_info.value.code != 0


# ── validate-config ───────────────────────────────────────────────────


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


# ── migrate-notes ─────────────────────────────────────────────────────


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


# ── unknown / no subcommand ───────────────────────────────────────────


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
