"""Contract tests for LithosClient against a fake Lithos SSE server.

Exercises lazy-connect + reuse semantics (FR-MCP-2) and validates that
``LITHOS_MCP_TRANSPORT`` must be ``sse`` (FR-MCP-1).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from typing import Any

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from influx.errors import ConfigError
from influx.lithos_client import LithosClient

# ── Fake Lithos SSE server ──────────────────────────────────────────


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLithosServer:
    """Minimal MCP server exposing a ``lithos_ping`` tool for lifecycle tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.port = _find_free_port()
        self._mcp = FastMCP("fake-lithos")
        self._uvicorn_server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._register_tools()

    def _register_tools(self) -> None:
        calls = self.calls

        @self._mcp.tool(name="lithos_ping")
        async def lithos_ping() -> str:
            calls.append(("lithos_ping", {}))
            return "pong"

        @self._mcp.tool(name="lithos_cache_lookup")
        async def lithos_cache_lookup(
            query: str = "", source_url: str = ""
        ) -> str:
            calls.append(
                ("lithos_cache_lookup", {"query": query, "source_url": source_url})
            )
            return '{"hit": false, "stale_exists": false}'

    def start(self) -> None:
        app = self._mcp.sse_app()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._uvicorn_server.run, daemon=True
        )
        self._thread.start()
        # Wait until the server is accepting connections.
        self._wait_for_ready()

    def _wait_for_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.port), timeout=0.5
                ):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(
            f"Fake Lithos server did not start within {timeout}s"
        )

    def stop(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture(scope="module")
def fake_lithos_server() -> Generator[FakeLithosServer, None, None]:
    """Start a fake Lithos SSE server for the test module."""
    server = FakeLithosServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def fake_lithos_url(fake_lithos_server: FakeLithosServer) -> str:
    return f"http://127.0.0.1:{fake_lithos_server.port}/sse"


@pytest.fixture
def clear_fake_calls(fake_lithos_server: FakeLithosServer) -> None:
    """Clear recorded calls before each test."""
    fake_lithos_server.calls.clear()


# ── Construction validation ─────────────────────────────────────────


class TestConstructionValidation:
    """LithosClient validates transport and URL at construction time."""

    def test_rejects_non_sse_transport(self) -> None:
        with pytest.raises(ConfigError, match="only 'sse' is supported"):
            LithosClient(url="http://localhost:1234/sse", transport="stdio")

    def test_rejects_empty_url(self) -> None:
        with pytest.raises(ConfigError, match="LITHOS_URL is required"):
            LithosClient(url="", transport="sse")

    def test_accepts_sse_transport(self) -> None:
        client = LithosClient(url="http://localhost:1234/sse", transport="sse")
        assert not client.connected


# ── Connection lifecycle ────────────────────────────────────────────


class TestConnectionLifecycle:
    """Lazy-connect + reuse semantics (FR-MCP-2)."""

    async def test_not_connected_at_construction(
        self, fake_lithos_url: str
    ) -> None:
        client = LithosClient(url=fake_lithos_url)
        assert not client.connected
        await client.close()

    async def test_connects_on_first_tool_call(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        client = LithosClient(url=fake_lithos_url)
        try:
            assert not client.connected
            await client.call_tool("lithos_ping")
            assert client.connected
            assert len(fake_lithos_server.calls) == 1
            assert fake_lithos_server.calls[0][0] == "lithos_ping"
        finally:
            await client.close()

    async def test_reuses_connection_across_calls(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        client = LithosClient(url=fake_lithos_url)
        try:
            # First call establishes the connection.
            await client.call_tool("lithos_ping")
            assert client.connected

            # Capture the session identity after first connect.
            session_after_first = client._session  # noqa: SLF001

            # Second call reuses the same session (no reconnect).
            await client.call_tool("lithos_ping")
            assert client._session is session_after_first  # noqa: SLF001
            assert len(fake_lithos_server.calls) == 2
        finally:
            await client.close()

    async def test_close_disconnects(
        self, fake_lithos_url: str, clear_fake_calls: None
    ) -> None:
        client = LithosClient(url=fake_lithos_url)
        await client.call_tool("lithos_ping")
        assert client.connected
        await client.close()
        assert not client.connected
