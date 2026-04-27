"""CLI entry point with argparse dispatcher.

Provides subcommands for the v1 CLI surface: ``validate-config``,
``serve``, ``run``, ``backfill``, and ``migrate-notes``.

Running ``python -m influx`` with no subcommand prints help and exits
with a non-zero status.
"""

from __future__ import annotations

import argparse
import sys

from influx.config import AppConfig, load_config
from influx.errors import InfluxError

# FR-CLI-7 exit-code policy
EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILURE = 2
EXIT_USAGE = 64


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="influx",
        description="Influx — research-feed ingestion toolkit.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "validate-config",
        help="Load influx.toml, validate, and print the effective config.",
    )

    # FR-CLI-6: migrate-notes
    sub.add_parser(
        "migrate-notes",
        help="Print the current note schema version and exit.",
    )

    # FR-CLI-2: serve takes no flags
    sub.add_parser(
        "serve",
        help="Start the Influx HTTP API server.",
    )

    # FR-CLI-3: run --profile
    run_parser = sub.add_parser(
        "run",
        help="Run a single ingestion cycle for a profile.",
    )
    run_parser.add_argument(
        "--profile",
        required=True,
        help="Profile name to run ingestion for.",
    )

    # FR-CLI-4: backfill --profile --days/--from/--to [--confirm]
    backfill_parser = sub.add_parser(
        "backfill",
        help="Backfill historical data for a profile.",
    )
    backfill_parser.add_argument(
        "--profile",
        required=True,
        help="Profile name to backfill.",
    )
    backfill_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of days to backfill.",
    )
    backfill_parser.add_argument(
        "--from",
        dest="from_date",
        default=None,
        help="Start date for backfill range (ISO format).",
    )
    backfill_parser.add_argument(
        "--to",
        dest="to_date",
        default=None,
        help="End date for backfill range (ISO format).",
    )
    backfill_parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Confirm large backfill jobs.",
    )

    return parser


def _validate_json_mode_slots(config: AppConfig) -> None:
    """Check JSON-mode compatibility for [models.*] slots with json_mode=true.

    For each slot, constructs an HTTP client using the provider's
    ``base_url`` and API key, sends a minimal chat-completions dry-call
    with ``response_format={"type": "json_object"}``, and verifies the
    provider/model accepts it.  Exits non-zero on any failure.

    The ``json_mode`` flag and slot configuration come from config
    (AC-X-1 — no hardcoded constants).  (FR-CLI-5, §16.4)
    """
    import json
    import os

    from influx.errors import NetworkError
    from influx.http_client import guarded_post_json_fetch

    for slot_name, slot in config.models.items():
        if not slot.json_mode:
            continue

        provider = config.providers.get(slot.provider)
        if provider is None:
            print(
                f"influx: model slot {slot_name!r}: provider "
                f"{slot.provider!r} not defined in [providers]",
                file=sys.stderr,
            )
            sys.exit(EXIT_FAILURE)

        api_key = ""
        if provider.api_key_env:
            api_key = os.environ.get(provider.api_key_env, "")

        headers: dict[str, str] = {**provider.extra_headers}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body = {
            "model": slot.model,
            "messages": [{"role": "user", "content": "1"}],
            "max_tokens": 1,
            "response_format": {"type": "json_object"},
        }

        url = f"{provider.base_url.rstrip('/')}/chat/completions"

        try:
            resp = guarded_post_json_fetch(
                url,
                body,
                headers=headers,
                allow_private_ips=config.security.allow_private_ips,
                max_response_bytes=config.storage.max_download_bytes,
                timeout_seconds=slot.request_timeout,
            )
        except NetworkError as exc:
            print(
                f"influx: model slot {slot_name!r}: JSON-mode "
                f"dry-call to {url} failed: {exc}",
                file=sys.stderr,
            )
            sys.exit(EXIT_FAILURE)

        if resp.status_code >= 400:
            try:
                error_body = json.loads(resp.body.decode("utf-8"))
                msg = error_body.get("error", {}).get(
                    "message", resp.body.decode("utf-8", errors="replace")
                )
            except Exception:
                msg = resp.body.decode("utf-8", errors="replace")
            print(
                f"influx: model slot {slot_name!r}: JSON-mode "
                f"check failed ({resp.status_code}): {msg}",
                file=sys.stderr,
            )
            sys.exit(EXIT_FAILURE)


def _cmd_validate_config() -> None:
    """Load config, print it, check JSON-mode slots, dry-connect to Lithos.

    After config validation, checks JSON-mode compatibility for each
    ``[models.*]`` slot (FR-CLI-5, §16.4), then opens an SSE connection
    to the configured Lithos endpoint and calls ``lithos_agent_register``
    to verify connectivity (AC-05-K).  Exits non-zero on any failure.
    """
    import asyncio

    from influx.lithos_client import LithosClient

    config = load_config()
    print(config.model_dump_json(indent=2))

    # JSON-mode compatibility check for [models.*] slots (FR-CLI-5, §16.4).
    _validate_json_mode_slots(config)

    # Skip Lithos dry-connect if no URL is configured.
    if not config.lithos.url:
        return

    lithos_url = config.lithos.url

    async def _dry_connect() -> None:
        client = LithosClient(
            url=lithos_url,
            transport=config.lithos.transport,
        )
        try:
            # _ensure_connected opens SSE + auto-calls agent_register.
            await client._ensure_connected()  # noqa: SLF001
        finally:
            await client.close()

    try:
        asyncio.run(_dry_connect())
    except Exception as exc:
        print(
            f"influx: Lithos dry-connect failed for {lithos_url}: {exc}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)


def _cmd_migrate_notes() -> None:
    """Print the current note_schema_version from config and exit 0."""
    config = load_config()
    print(f"note_schema_version: {config.influx.note_schema_version}")


def _cmd_serve() -> None:
    """Start the Influx HTTP API server under uvicorn.

    Loads config (with ``check_api_keys=False`` since probes handle
    credential checks), validates the bind address, creates the
    :class:`~influx.service.InfluxService` with lifespan wiring,
    and runs uvicorn.  Blocks until SIGINT or SIGTERM, then performs a
    clean shutdown bounded by ``schedule.shutdown_grace_seconds`` and
    exits with status 0 on either signal (AC-03-E).

    We own the signal handling (via ``loop.add_signal_handler``) instead
    of letting uvicorn install its own ``signal.signal`` handlers, so
    that both SIGINT and SIGTERM return normally from ``server.serve``
    and the process exits 0 after graceful shutdown.

    Process teardown is bounded by ``schedule.shutdown_grace_seconds``:
    after the lifespan-driven ``InfluxService.stop`` returns, any
    remaining tasks are cancelled and given a tiny post-cancel budget
    before the loop is closed — without this, ``asyncio.run``'s built-in
    final cleanup would await lingering tasks unbounded and let total
    process exit exceed the configured grace (AC-03-E).

    Replaces the PRD 02 stub (§5.4, AC-03-E).
    """
    import asyncio
    import contextlib
    import signal as signal_mod

    import uvicorn

    from influx.service import (
        InfluxService,
        resolve_bind_address,
        validate_bind_host,
    )

    app_config = load_config(check_api_keys=False)
    host, port = resolve_bind_address()
    validate_bind_host(host, allow_remote_admin=app_config.security.allow_remote_admin)

    service = InfluxService(app_config, with_lifespan=True)

    uv_config = uvicorn.Config(
        service.app,
        host=host,
        port=port,
        timeout_graceful_shutdown=app_config.schedule.shutdown_grace_seconds,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)
    # Disable uvicorn's built-in ``signal.signal`` handlers so that
    # our asyncio-aware handlers drive graceful shutdown and return 0
    # for both SIGINT and SIGTERM (AC-03-E).
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def _run() -> None:
        loop = asyncio.get_running_loop()

        def _request_shutdown() -> None:
            server.should_exit = True

        for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_shutdown)
            except NotImplementedError:
                # Platforms like Windows fall back to default handling.
                signal_mod.signal(sig, lambda *_: _request_shutdown())

        await server.serve()

    # Own the event loop explicitly rather than using ``asyncio.run``:
    # asyncio.run's final cleanup gathers every remaining task unbounded,
    # which lets a task that swallows cancellation push total process
    # exit time past ``schedule.shutdown_grace_seconds`` (AC-03-E).
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    finally:
        try:
            remaining = [t for t in asyncio.all_tasks(loop) if not t.done()]
            if remaining:
                for task in remaining:
                    task.cancel()
                # ``InfluxService.stop`` has already consumed the
                # configured grace window; give cancelled tasks only a
                # tiny epsilon here so stubborn tasks cannot extend the
                # total serve exit past the shutdown bound.
                with contextlib.suppress(Exception):  # pragma: no cover — defensive
                    loop.run_until_complete(asyncio.wait(remaining, timeout=0.05))
            with contextlib.suppress(Exception):  # pragma: no cover — defensive
                loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def _admin_base_url() -> str:
    """Return the base URL for the running admin service.

    Reads ``INFLUX_ADMIN_PORT`` (default ``8080``) and targets
    ``127.0.0.1`` on loopback (§5.4 of PRD 03).
    """
    import os

    port = os.environ.get("INFLUX_ADMIN_PORT", "8080")
    return f"http://127.0.0.1:{port}"


def _cmd_run(args: argparse.Namespace) -> None:
    """POST to ``/runs`` on the running service and print the request_id.

    Exit codes (§5.4 of PRD 03):
        0 — accepted (``202``)
        1 — profile busy (``409``)
        2 — network error
    """
    import httpx

    url = f"{_admin_base_url()}/runs"
    payload: dict[str, object] = {"profile": args.profile}

    try:
        resp = httpx.post(url, json=payload, timeout=10.0)
    except httpx.ConnectError:
        print(
            f"influx: could not connect to service at {url}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)
    except httpx.HTTPError as exc:
        print(f"influx: network error: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILURE)

    if resp.status_code == 202:
        body = resp.json()
        print(body["request_id"])
        sys.exit(EXIT_SUCCESS)
    elif resp.status_code == 409:
        body = resp.json()
        print(
            f"influx: profile {body.get('profile', args.profile)!r} is busy",
            file=sys.stderr,
        )
        sys.exit(EXIT_PARTIAL)
    else:
        print(
            f"influx: unexpected response {resp.status_code}: {resp.text}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)


def _cmd_backfill(args: argparse.Namespace) -> None:
    """POST to ``/backfills`` on the running service.

    Handles the ``confirm_required`` reprompt flow: when the server
    returns ``400`` with ``reason="confirm_required"`` and ``--confirm``
    was NOT passed, prints the estimate and exits ``64`` (usage error).
    When ``--confirm`` IS passed and a ``confirm_required`` response
    arrives, re-POSTs with ``confirm: true`` added to the body.

    Exit codes (§5.4 of PRD 03):
        0  — accepted (``202``)
        1  — profile busy (``409``)
        2  — network error
        64 — confirm_required without ``--confirm``
    """
    from influx.backfill import BackfillRangeError, validate_backfill_range

    # FR-BF-1: enforce mutual exclusivity of --days vs --from/--to at the
    # CLI level so the user gets a clear error before any network call.
    try:
        validate_backfill_range(
            days=args.days,
            date_from=args.from_date,
            date_to=args.to_date,
        )
    except BackfillRangeError as exc:
        print(f"influx: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    import httpx

    url = f"{_admin_base_url()}/backfills"
    # Do NOT include ``confirm`` in the initial request even when
    # ``--confirm`` was passed — per §5.4 of PRD 03, the CLI must first
    # observe the server's ``confirm_required`` response and only then
    # re-POST with ``"confirm": true`` added.  This preserves the
    # server-enforced reprompt contract end-to-end.
    payload: dict[str, object] = {"profile": args.profile}

    if args.days is not None:
        payload["days"] = args.days
    if args.from_date is not None:
        payload["from"] = args.from_date
    if args.to_date is not None:
        payload["to"] = args.to_date

    try:
        resp = httpx.post(url, json=payload, timeout=10.0)
    except httpx.ConnectError:
        print(
            f"influx: could not connect to service at {url}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)
    except httpx.HTTPError as exc:
        print(f"influx: network error: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILURE)

    if resp.status_code == 202:
        body = resp.json()
        print(body["request_id"])
        sys.exit(EXIT_SUCCESS)
    elif resp.status_code == 400:
        body = resp.json()
        if body.get("reason") == "confirm_required":
            estimated = body.get("estimated_items", "unknown")
            if args.confirm:
                # Re-POST with confirm=true added (only on retry).
                retry_payload = {**payload, "confirm": True}
                try:
                    resp2 = httpx.post(url, json=retry_payload, timeout=10.0)
                except httpx.HTTPError as exc:
                    print(
                        f"influx: network error on retry: {exc}",
                        file=sys.stderr,
                    )
                    sys.exit(EXIT_FAILURE)
                if resp2.status_code == 202:
                    body2 = resp2.json()
                    print(body2["request_id"])
                    sys.exit(EXIT_SUCCESS)
                else:
                    print(
                        f"influx: unexpected response on retry "
                        f"{resp2.status_code}: {resp2.text}",
                        file=sys.stderr,
                    )
                    sys.exit(EXIT_FAILURE)
            else:
                print(
                    f"Estimated {estimated} items. Re-run with --confirm to proceed.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_USAGE)
        else:
            print(
                f"influx: bad request: {resp.text}",
                file=sys.stderr,
            )
            sys.exit(EXIT_FAILURE)
    elif resp.status_code == 409:
        body = resp.json()
        print(
            f"influx: profile {body.get('profile', args.profile)!r} is busy",
            file=sys.stderr,
        )
        sys.exit(EXIT_PARTIAL)
    else:
        print(
            f"influx: unexpected response {resp.status_code}: {resp.text}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)


_KNOWN_COMMANDS = frozenset(
    {
        "validate-config",
        "migrate-notes",
        "serve",
        "run",
        "backfill",
    }
)


def main(argv: list[str] | None = None) -> None:
    """CLI dispatcher.

    Parameters
    ----------
    argv:
        Argument list for testing; defaults to ``sys.argv[1:]``.
    """
    parser = _build_parser()

    # Intercept unknown subcommands before argparse (which exits 2).
    effective_argv = argv if argv is not None else sys.argv[1:]
    if (
        effective_argv
        and not effective_argv[0].startswith("-")
        and effective_argv[0] not in _KNOWN_COMMANDS
    ):
        print(
            f"influx: unknown command {effective_argv[0]!r}",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        sys.exit(EXIT_USAGE)

    try:
        if args.command == "validate-config":
            _cmd_validate_config()
        elif args.command == "migrate-notes":
            _cmd_migrate_notes()
        elif args.command == "serve":
            _cmd_serve()
        elif args.command == "run":
            _cmd_run(args)
        elif args.command == "backfill":
            _cmd_backfill(args)
    except InfluxError as exc:
        print(f"influx: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILURE)
