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

from influx.errors import ConfigError, LithosError
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
        # Queue of override responses for lithos_write (FIFO).
        # When non-empty, the next response is popped and returned
        # instead of the default ``{"status": "created"}``.
        self.write_responses: list[str] = []
        self._register_tools()

    def _register_tools(self) -> None:
        calls = self.calls
        write_responses = self.write_responses

        @self._mcp.tool(name="lithos_ping")
        async def lithos_ping() -> str:
            calls.append(("lithos_ping", {}))
            return "pong"

        @self._mcp.tool(name="lithos_agent_register")
        async def lithos_agent_register(
            id: str = "", name: str = "", type: str = ""
        ) -> str:
            calls.append(
                (
                    "lithos_agent_register",
                    {"id": id, "name": name, "type": type},
                )
            )
            return '{"registered": true}'

        @self._mcp.tool(name="lithos_cache_lookup")
        async def lithos_cache_lookup(
            query: str = "", source_url: str = ""
        ) -> str:
            calls.append(
                ("lithos_cache_lookup", {"query": query, "source_url": source_url})
            )
            return '{"hit": false, "stale_exists": false}'

        @self._mcp.tool(name="lithos_write")
        async def lithos_write(
            title: str = "",
            content: str = "",
            agent: str = "",
            path: str = "",
            source_url: str = "",
            tags: list[str] | None = None,
            confidence: float = 0.0,
            note_type: str = "",
            namespace: str = "",
            expires_at: str | None = None,
        ) -> str:
            calls.append(
                (
                    "lithos_write",
                    {
                        "title": title,
                        "content": content,
                        "agent": agent,
                        "path": path,
                        "source_url": source_url,
                        "tags": tags or [],
                        "confidence": confidence,
                        "note_type": note_type,
                        "namespace": namespace,
                        "expires_at": expires_at,
                    },
                )
            )
            if write_responses:
                return write_responses.pop(0)
            return '{"status": "created"}'

        @self._mcp.tool(name="lithos_list")
        async def lithos_list(
            tags: list[str] | None = None,
            limit: int | None = None,
        ) -> str:
            calls.append(
                ("lithos_list", {"tags": tags or [], "limit": limit})
            )
            import json

            # Return items matching tags for test purposes.
            if tags and any(t.startswith("arxiv-id:") for t in tags):
                return json.dumps(
                    {
                        "items": [
                            {
                                "id": "note-001",
                                "title": "Attention Is All You Need",
                                "tags": tags,
                            }
                        ]
                    }
                )
            return json.dumps({"items": []})

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
    """Clear recorded calls and response overrides before each test."""
    fake_lithos_server.calls.clear()
    fake_lithos_server.write_responses.clear()


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
            # agent_register fires on connect, then our ping.
            assert len(fake_lithos_server.calls) == 2
            assert fake_lithos_server.calls[0][0] == "lithos_agent_register"
            assert fake_lithos_server.calls[1][0] == "lithos_ping"
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
            # First call establishes connection (agent_register + ping).
            await client.call_tool("lithos_ping")
            assert client.connected

            # Capture the session identity after first connect.
            session_after_first = client._session  # noqa: SLF001

            # Second call reuses the same session (no reconnect).
            await client.call_tool("lithos_ping")
            assert client._session is session_after_first  # noqa: SLF001
            # agent_register + 2 × ping = 3 total calls.
            assert len(fake_lithos_server.calls) == 3
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


# ── Agent registration (FR-MCP-8, AC-05-G) ────────────────────────


_EXPECTED_REGISTER_PAYLOAD = {
    "id": "influx",
    "name": "Influx Pipeline",
    "type": "ingestion-pipeline",
}


class TestAgentRegister:
    """``lithos_agent_register`` fires on connect and on reconnect."""

    async def test_register_on_first_tool_call(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """First tool-call triggers exactly one agent_register with correct payload."""
        client = LithosClient(url=fake_lithos_url)
        try:
            await client.call_tool("lithos_ping")

            register_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_agent_register"
            ]
            assert len(register_calls) == 1
            assert register_calls[0][1] == _EXPECTED_REGISTER_PAYLOAD
        finally:
            await client.close()

    async def test_no_register_before_tool_call(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """No agent_register call is sent before any tool-call use."""
        client = LithosClient(url=fake_lithos_url)
        try:
            # Client constructed but no tool call yet.
            assert not client.connected
            register_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_agent_register"
            ]
            assert len(register_calls) == 0
        finally:
            await client.close()

    async def test_register_again_after_reconnect(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """SSE drop + reconnect triggers a second agent_register call."""
        client = LithosClient(url=fake_lithos_url)
        try:
            # First connection — one register.
            await client.call_tool("lithos_ping")
            register_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_agent_register"
            ]
            assert len(register_calls) == 1

            # Simulate SSE drop + reconnect.
            await client.reconnect()

            register_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_agent_register"
            ]
            assert len(register_calls) == 2
            # Both calls carry identical payload.
            assert register_calls[0][1] == _EXPECTED_REGISTER_PAYLOAD
            assert register_calls[1][1] == _EXPECTED_REGISTER_PAYLOAD
        finally:
            await client.close()


# ── Cache lookup chokepoint (FR-MCP-3, AC-05-A) ───────────────────


class TestCacheLookupChokepoint:
    """``cache_lookup`` enforces both query and source_url (AC-05-A)."""

    async def test_empty_query_raises_before_rpc(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Empty string query raises LithosError; zero RPCs sent."""
        client = LithosClient(url=fake_lithos_url)
        try:
            with pytest.raises(LithosError, match="missing_lookup_arg"):
                await client.cache_lookup(query="", source_url="https://x")
            # No connection was made, so zero calls total.
            lookup_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_cache_lookup"
            ]
            assert len(lookup_calls) == 0
        finally:
            await client.close()

    async def test_none_query_raises_before_rpc(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """None query raises LithosError; zero RPCs sent."""
        client = LithosClient(url=fake_lithos_url)
        try:
            with pytest.raises(LithosError, match="missing_lookup_arg"):
                await client.cache_lookup(query=None, source_url="https://x")
            lookup_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_cache_lookup"
            ]
            assert len(lookup_calls) == 0
        finally:
            await client.close()

    async def test_empty_source_url_raises_before_rpc(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Empty source_url raises LithosError; zero RPCs sent."""
        client = LithosClient(url=fake_lithos_url)
        try:
            with pytest.raises(LithosError, match="missing_lookup_arg"):
                await client.cache_lookup(query="some query", source_url="")
            lookup_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_cache_lookup"
            ]
            assert len(lookup_calls) == 0
        finally:
            await client.close()

    async def test_none_source_url_raises_before_rpc(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """None source_url raises LithosError; zero RPCs sent."""
        client = LithosClient(url=fake_lithos_url)
        try:
            with pytest.raises(LithosError, match="missing_lookup_arg"):
                await client.cache_lookup(query="some query", source_url=None)
            lookup_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_cache_lookup"
            ]
            assert len(lookup_calls) == 0
        finally:
            await client.close()

    async def test_happy_path_reaches_server(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Well-formed cache_lookup reaches server; response forwarded."""
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.cache_lookup(
                query="Attention Is All You Need",
                source_url="https://arxiv.org/abs/1706.03762",
            )
            # The wrapper forwards the Lithos response unchanged.
            assert result is not None
            # Verify the fake server received the correct arguments.
            lookup_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_cache_lookup"
            ]
            assert len(lookup_calls) == 1
            assert lookup_calls[0][1] == {
                "query": "Attention Is All You Need",
                "source_url": "https://arxiv.org/abs/1706.03762",
            }
            # Response content should contain the fake server's response.
            assert len(result.content) > 0
            text_content = result.content[0]
            assert text_content.type == "text"
            assert "hit" in text_content.text  # type: ignore[union-attr]
        finally:
            await client.close()


# ── List wrapper (FR-MCP-5) ────────────────────────────────────────


class TestListNotes:
    """``list_notes`` invokes ``lithos_list`` and forwards response."""

    async def test_happy_path_with_tags(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """List call with tags reaches server; items envelope returned."""
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.list_notes(
                tags=["arxiv-id:2601.12345"],
            )
            # Verify the fake server received the call.
            list_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_list"
            ]
            assert len(list_calls) == 1
            assert list_calls[0][1]["tags"] == ["arxiv-id:2601.12345"]

            # Response content should contain the items envelope.
            assert len(result.content) > 0
            text = result.content[0]
            assert text.type == "text"
            import json

            body = json.loads(text.text)  # type: ignore[union-attr]
            assert "items" in body
            assert len(body["items"]) == 1
            assert body["items"][0]["title"] == "Attention Is All You Need"
        finally:
            await client.close()

    async def test_empty_result_propagated(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Empty list response is propagated (not None or error)."""
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.list_notes(
                tags=["nonexistent-tag:xyz"],
            )
            # Verify server received the call.
            list_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_list"
            ]
            assert len(list_calls) == 1

            # Response should be an empty items list, not None.
            assert result is not None
            assert len(result.content) > 0
            text = result.content[0]
            assert text.type == "text"
            import json

            body = json.loads(text.text)  # type: ignore[union-attr]
            assert body == {"items": []}
        finally:
            await client.close()

    async def test_limit_forwarded(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Limit parameter is forwarded to the server."""
        client = LithosClient(url=fake_lithos_url)
        try:
            await client.list_notes(
                tags=["arxiv-id:2601.12345"], limit=5
            )
            list_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_list"
            ]
            assert len(list_calls) == 1
            assert list_calls[0][1]["limit"] == 5
        finally:
            await client.close()


# ── Write wrapper (FR-MCP-6) ──────────────────────────────────────


class TestWriteNote:
    """``write_note`` invokes ``lithos_write`` with FR-MCP-6 fields."""

    async def test_happy_path_arxiv_item(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Sample arXiv write reaches server with documented field set."""
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Attention Is All You Need",
                content="# Summary\nTransformer architecture paper.",
                agent="influx",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/1706.03762",
                tags=[
                    "profile:ml-research",
                    "arxiv-id:1706.03762",
                    "source:arxiv",
                ],
                confidence=0.9,
                note_type="summary",
                namespace="influx",
            )

            # Verify the fake server received the call.
            write_calls = [
                c
                for c in fake_lithos_server.calls
                if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 1
            payload = write_calls[0][1]
            assert payload["title"] == "Attention Is All You Need"
            assert payload["content"] == (
                "# Summary\nTransformer architecture paper."
            )
            assert payload["agent"] == "influx"
            assert payload["path"] == "papers/arxiv/2026/03"
            assert payload["source_url"] == (
                "https://arxiv.org/abs/1706.03762"
            )
            assert payload["tags"] == [
                "profile:ml-research",
                "arxiv-id:1706.03762",
                "source:arxiv",
            ]
            assert payload["confidence"] == 0.9
            assert payload["note_type"] == "summary"
            assert payload["namespace"] == "influx"
            assert payload["expires_at"] is None

            # WriteResult surfaces the status for caller counters.
            assert result.status == "created"
            assert result.source_url == (
                "https://arxiv.org/abs/1706.03762"
            )
        finally:
            await client.close()

    async def test_expires_at_forwarded(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Optional expires_at is forwarded when provided."""
        client = LithosClient(url=fake_lithos_url)
        try:
            await client.write_note(
                title="Repair note",
                content="# Summary\nNeeds repair.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.99999",
                tags=["influx:repair-needed"],
                confidence=0.5,
                expires_at="2026-04-30T00:00:00Z",
            )

            write_calls = [
                c
                for c in fake_lithos_server.calls
                if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 1
            assert write_calls[0][1]["expires_at"] == (
                "2026-04-30T00:00:00Z"
            )
        finally:
            await client.close()

    async def test_expires_at_omitted_when_none(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """expires_at is not sent when None (normal writes)."""
        client = LithosClient(url=fake_lithos_url)
        try:
            await client.write_note(
                title="Normal note",
                content="# Summary\nContent.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.00001",
                tags=["profile:ml-research"],
                confidence=0.8,
            )

            write_calls = [
                c
                for c in fake_lithos_server.calls
                if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 1
            # expires_at is None in the recorded payload because
            # the wrapper did not include it in the tool call args.
            assert write_calls[0][1]["expires_at"] is None
        finally:
            await client.close()


# ── Write envelopes — duplicate & invalid_input (FR-MCP-7) ────────


class TestWriteEnvelopeDuplicate:
    """``duplicate`` envelope is treated as hit (AC-05-C)."""

    async def test_duplicate_treated_as_hit(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """duplicate: no error, status surfaces for dedup_skipped."""
        fake_lithos_server.write_responses.append(
            '{"status": "duplicate"}'
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Already exists",
                content="# Summary\nContent.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.00001",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            assert result.status == "duplicate"
            assert result.source_url == (
                "https://arxiv.org/abs/2601.00001"
            )
        finally:
            await client.close()

    async def test_duplicate_no_second_write(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """duplicate: exactly one lithos_write call, no retry."""
        fake_lithos_server.write_responses.append(
            '{"status": "duplicate"}'
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            await client.write_note(
                title="Dup item",
                content="# Summary\nContent.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.00002",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            write_calls = [
                c
                for c in fake_lithos_server.calls
                if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 1
        finally:
            await client.close()

    async def test_duplicate_dedup_skipped_countable(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Caller can count dedup_skipped via result.status."""
        fake_lithos_server.write_responses.append(
            '{"status": "duplicate"}'
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Counted dup",
                content="# Summary\nContent.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.00003",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            # Caller increments dedup_skipped based on status.
            dedup_skipped = 0
            if result.status == "duplicate":
                dedup_skipped += 1
            assert dedup_skipped == 1
        finally:
            await client.close()


class TestWriteEnvelopeInvalidInput:
    """``invalid_input`` envelope: logged + skipped (FR-MCP-7)."""

    async def test_invalid_input_returns_skip_status(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """invalid_input: no exception, status='invalid_input'."""
        fake_lithos_server.write_responses.append(
            '{"status": "invalid_input", "reason": "bad payload"}'
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Bad item",
                content="# Summary\nContent.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.00004",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            assert result.status == "invalid_input"
            assert result.detail == "bad payload"
            assert result.source_url == (
                "https://arxiv.org/abs/2601.00004"
            )
        finally:
            await client.close()

    async def test_invalid_input_logged(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """invalid_input: source_url and reason are logged."""
        import logging

        fake_lithos_server.write_responses.append(
            '{"status": "invalid_input", "reason": "missing field"}'
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            with caplog.at_level(logging.WARNING):
                await client.write_note(
                    title="Bad item 2",
                    content="# Summary\nContent.",
                    path="papers/arxiv/2026/03",
                    source_url="https://arxiv.org/abs/2601.00005",
                    tags=["profile:ml-research"],
                    confidence=0.8,
                )
            assert "2601.00005" in caplog.text
            assert "missing field" in caplog.text
        finally:
            await client.close()

    async def test_invalid_input_no_abort(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """invalid_input does not raise — run continues."""
        fake_lithos_server.write_responses.append(
            '{"status": "invalid_input", "reason": "bad"}'
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            # Should NOT raise an exception.
            result = await client.write_note(
                title="Skipped item",
                content="# Summary\nContent.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.00006",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            assert result.status == "invalid_input"
            # Subsequent writes still work.
            result2 = await client.write_note(
                title="Good item",
                content="# Summary\nContent.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.00007",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            assert result2.status == "created"
        finally:
            await client.close()
