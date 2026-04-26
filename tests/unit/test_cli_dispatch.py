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
            main(
                [
                    "backfill",
                    "--profile",
                    "web-tech",
                    "--from",
                    "2026-01-01",
                    "--to",
                    "2026-01-31",
                ]
            )

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
            main(
                [
                    "backfill",
                    "--profile",
                    "ai-robotics",
                    "--days",
                    "365",
                ]
            )

        assert exc_info.value.code == EXIT_USAGE
        captured = capsys.readouterr()
        assert "5000" in captured.err
        assert "--confirm" in captured.err

    @respx.mock
    def test_backfill_confirm_required_with_confirm_reposts(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """confirm_required + --confirm → re-POST with confirm=true (AC-M3-8).

        Also verifies the mandated sequence (§5.4 of PRD 03): the first
        request MUST NOT carry ``confirm``; only the retry issued after
        the server replies ``confirm_required`` is allowed to add it.
        """
        import json as json_mod

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
            main(
                [
                    "backfill",
                    "--profile",
                    "ai-robotics",
                    "--days",
                    "365",
                    "--confirm",
                ]
            )

        assert exc_info.value.code == EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "bf-confirmed-789" in captured.out
        # Verify two requests were made.
        assert route.call_count == 2

        # First request MUST NOT include ``confirm`` — the reprompt flow
        # is driven by the server's response, not by the CLI pre-empting it.
        first_body = json_mod.loads(route.calls[0].request.content)
        assert "confirm" not in first_body, (
            f"First POST must not contain 'confirm': {first_body}"
        )
        assert first_body == {"profile": "ai-robotics", "days": 365}

        # Second request (retry) MUST include ``confirm: true``.
        second_body = json_mod.loads(route.calls[1].request.content)
        assert second_body.get("confirm") is True, (
            f"Retry POST must include 'confirm: true': {second_body}"
        )
        assert second_body["profile"] == "ai-robotics"
        assert second_body["days"] == 365

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
            main(
                [
                    "backfill",
                    "--profile",
                    "ai-robotics",
                    "--days",
                    "7",
                ]
            )

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
            main(
                [
                    "backfill",
                    "--profile",
                    "ai-robotics",
                    "--days",
                    "7",
                ]
            )

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

    @respx.mock
    def test_validate_config_still_works(
        self,
        influx_config_env: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Mock the provider endpoint for JSON-mode dry-call.
        respx.post("https://api.test.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "{}"},
                            "finish_reason": "stop",
                            "index": 0,
                        }
                    ]
                },
            )
        )
        main(["validate-config"])

        captured = capsys.readouterr()
        assert captured.out.strip().startswith("{")


def _write_config_with_model_slot(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
    provider_url: str,
    *,
    json_mode: bool = True,
) -> None:
    """Write a minimal config with one model slot pointing at *provider_url*."""
    from pathlib import Path
    from textwrap import dedent

    jm = "true" if json_mode else "false"
    config_path = Path(str(tmp_path)) / "influx.toml"
    config_path.write_text(
        dedent(f"""\
            [providers.fake-provider]
            base_url = "{provider_url}"
            api_key_env = "FAKE_PROVIDER_KEY"

            [models.filter]
            provider = "fake-provider"
            model = "fake-model"
            json_mode = {jm}

            [prompts.filter]
            text = "f"
            [prompts.tier1_enrich]
            text = "e"
            [prompts.tier3_extract]
            text = "x"
        """)
    )
    monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "test-key")


class TestValidateConfigJsonMode:
    """validate-config checks JSON-mode compatibility for [models.*] slots."""

    @respx.mock
    def test_json_mode_capable_exits_zero(
        self,
        tmp_path: object,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Slot with json_mode=true and capable provider exits 0."""
        _write_config_with_model_slot(
            tmp_path, monkeypatch, "https://fake.api.example.com/v1"
        )
        respx.post("https://fake.api.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "{}"},
                            "finish_reason": "stop",
                            "index": 0,
                        }
                    ]
                },
            )
        )

        # Should NOT raise SystemExit — exit 0.
        main(["validate-config"])

        captured = capsys.readouterr()
        assert captured.out.strip().startswith("{")

    @respx.mock
    def test_json_mode_incapable_exits_nonzero(
        self,
        tmp_path: object,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Slot with json_mode=true and incapable provider exits non-zero."""
        _write_config_with_model_slot(
            tmp_path, monkeypatch, "https://fake.api.example.com/v1"
        )
        respx.post("https://fake.api.example.com/v1/chat/completions").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "response_format json_object is not "
                            "supported for this model"
                        )
                    }
                },
            )
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["validate-config"])

        assert exc_info.value.code == EXIT_FAILURE
        captured = capsys.readouterr()
        assert "json-mode" in captured.err.lower()

    @respx.mock
    def test_json_mode_false_skips_check(
        self,
        tmp_path: object,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Slot with json_mode=false does not trigger a dry-call."""
        _write_config_with_model_slot(
            tmp_path,
            monkeypatch,
            "https://fake.api.example.com/v1",
            json_mode=False,
        )
        # No respx route registered — any request would raise.

        main(["validate-config"])

        captured = capsys.readouterr()
        assert captured.out.strip().startswith("{")

    @respx.mock
    def test_json_mode_config_driven_not_hardcoded(
        self,
        tmp_path: object,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC-X-1: json_mode flag comes from config, not hardcoded."""
        from pathlib import Path
        from textwrap import dedent

        # Config with json_mode=true on filter but false on enrich.
        config_path = Path(str(tmp_path)) / "influx.toml"
        config_path.write_text(
            dedent("""\
                [providers.fake-provider]
                base_url = "https://fake.api.example.com/v1"
                api_key_env = "FAKE_PROVIDER_KEY"

                [models.filter]
                provider = "fake-provider"
                model = "fake-model-a"
                json_mode = true

                [models.enrich]
                provider = "fake-provider"
                model = "fake-model-b"
                json_mode = false

                [prompts.filter]
                text = "f"
                [prompts.tier1_enrich]
                text = "e"
                [prompts.tier3_extract]
                text = "x"
            """)
        )
        monkeypatch.setenv("INFLUX_CONFIG", str(config_path))
        monkeypatch.setenv("FAKE_PROVIDER_KEY", "test-key")

        route = respx.post("https://fake.api.example.com/v1/chat/completions")
        route.mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "{}"},
                            "finish_reason": "stop",
                            "index": 0,
                        }
                    ]
                },
            )
        )

        main(["validate-config"])

        # Only the json_mode=true slot (filter) should trigger a dry-call;
        # enrich (json_mode=false) should NOT.
        assert route.call_count == 1
        import json as json_mod

        sent_body = json_mod.loads(route.calls[0].request.content)
        assert sent_body["model"] == "fake-model-a"


def _write_config_with_lithos(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, lithos_url: str
) -> None:
    """Write a minimal config pointing at *lithos_url* and set env."""
    from pathlib import Path
    from textwrap import dedent

    config_path = Path(str(tmp_path)) / "influx.toml"
    config_path.write_text(
        dedent(f"""\
            [lithos]
            url = "{lithos_url}"
            transport = "sse"

            [prompts.filter]
            text = "f"
            [prompts.tier1_enrich]
            text = "e"
            [prompts.tier3_extract]
            text = "x"
        """)
    )
    monkeypatch.setenv("INFLUX_CONFIG", str(config_path))


class TestValidateConfigLithosUnreachable:
    """AC-05-K: Lithos unreachable → exit non-zero naming SSE endpoint."""

    def test_unreachable_lithos_exits_nonzero_with_endpoint(
        self,
        tmp_path: object,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import socket

        # Find a port that nothing is listening on.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        unreachable_url = f"http://127.0.0.1:{port}/sse"
        _write_config_with_lithos(tmp_path, monkeypatch, unreachable_url)

        with pytest.raises(SystemExit) as exc_info:
            main(["validate-config"])

        assert exc_info.value.code == EXIT_FAILURE
        captured = capsys.readouterr()
        # Error message MUST name the configured SSE endpoint (AC-05-K).
        assert unreachable_url in captured.err


class TestValidateConfigLithosDryConnect:
    """Successful dry-connect against a fake Lithos MCP SSE server."""

    def test_dry_connect_success_registers_agent(
        self,
        tmp_path: object,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """validate-config opens SSE + sends agent_register, exits 0."""
        import socket
        import threading
        import time

        import uvicorn
        from mcp.server.fastmcp import FastMCP

        calls: list[tuple[str, dict[str, object]]] = []

        mcp_app = FastMCP("fake-lithos-vc")

        @mcp_app.tool(name="lithos_agent_register")
        async def lithos_agent_register(
            id: str = "", name: str = "", type: str = ""
        ) -> str:
            calls.append(
                ("lithos_agent_register", {"id": id, "name": name, "type": type})
            )
            return '{"registered": true}'

        # Find a free port.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        config = uvicorn.Config(
            mcp_app.sse_app(),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        # Wait for the server to start.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("Fake Lithos server did not start")

        lithos_url = f"http://127.0.0.1:{port}/sse"
        _write_config_with_lithos(tmp_path, monkeypatch, lithos_url)

        try:
            # Should NOT raise SystemExit — exit 0.
            main(["validate-config"])

            captured = capsys.readouterr()
            assert captured.out.strip().startswith("{")

            # The fake server must have received agent_register.
            register_calls = [c for c in calls if c[0] == "lithos_agent_register"]
            assert len(register_calls) >= 1
            payload = register_calls[0][1]
            assert payload["id"] == "influx"
            assert payload["name"] == "Influx Pipeline"
            assert payload["type"] == "ingestion-pipeline"
        finally:
            server.should_exit = True


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
        config_path.write_text(
            dedent("""\
            [influx]
            note_schema_version = 42

            [prompts.filter]
            text = "f"
            [prompts.tier1_enrich]
            text = "e"
            [prompts.tier3_extract]
            text = "x"
        """)
        )
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
