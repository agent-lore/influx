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
from influx.feedback import (
    build_negative_examples_block,
    fetch_rejection_titles,
)
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
        # Queue of override responses for lithos_read (FIFO).
        self.read_responses: list[str] = []
        # Queue of override responses for lithos_cache_lookup (FIFO).
        self.cache_lookup_responses: list[str] = []
        # Queue of override responses for lithos_list (FIFO).
        self.list_responses: list[str] = []
        # LCMA tool response queues (PRD 08).
        self.retrieve_responses: list[str] = []
        self.edge_upsert_responses: list[str] = []
        self.task_create_responses: list[str] = []
        self.task_complete_responses: list[str] = []
        # When True, lithos_retrieve raises to simulate unknown_tool (FR-LCMA-6).
        self.raise_on_retrieve: bool = False
        self._register_tools()

    def _register_tools(self) -> None:
        import json as _json

        calls = self.calls
        write_responses = self.write_responses
        read_responses = self.read_responses
        cache_lookup_responses = self.cache_lookup_responses
        list_responses = self.list_responses
        retrieve_responses = self.retrieve_responses
        edge_upsert_responses = self.edge_upsert_responses
        task_create_responses = self.task_create_responses
        task_complete_responses = self.task_complete_responses
        server_self = self  # capture for closures that need mutable flags

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
        async def lithos_cache_lookup(query: str = "", source_url: str = "") -> str:
            calls.append(
                ("lithos_cache_lookup", {"query": query, "source_url": source_url})
            )
            if cache_lookup_responses:
                return cache_lookup_responses.pop(0)
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
            id: str | None = None,
            expected_version: int | None = None,
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
                        "id": id,
                        "expected_version": expected_version,
                    },
                )
            )
            if write_responses:
                return write_responses.pop(0)
            return '{"status": "created"}'

        @self._mcp.tool(name="lithos_read")
        async def lithos_read(id: str = "") -> str:
            calls.append(("lithos_read", {"id": id}))
            if read_responses:
                return read_responses.pop(0)
            return '{"id": "", "content": "", "tags": [], "version": 1}'

        @self._mcp.tool(name="lithos_list")
        async def lithos_list(
            tags: list[str] | None = None,
            limit: int | None = None,
            order_by: str | None = None,
            order: str | None = None,
        ) -> str:
            calls.append(
                (
                    "lithos_list",
                    {
                        "tags": tags or [],
                        "limit": limit,
                        "order_by": order_by,
                        "order": order,
                    },
                )
            )
            import json

            if list_responses:
                return list_responses.pop(0)
            # Default: return items matching tags for test purposes.
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

        # ── LCMA tools (PRD 08) ──────────────────────────────────────

        @self._mcp.tool(name="lithos_retrieve")
        async def lithos_retrieve(
            query: str = "",
            limit: int = 5,
            agent_id: str = "",
            task_id: str = "",
            tags: list[str] | None = None,
        ) -> str:
            calls.append(
                (
                    "lithos_retrieve",
                    {
                        "query": query,
                        "limit": limit,
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "tags": tags or [],
                    },
                )
            )
            if server_self.raise_on_retrieve:
                raise ValueError("unknown tool: lithos_retrieve")
            if retrieve_responses:
                return retrieve_responses.pop(0)
            return _json.dumps({"results": []})

        @self._mcp.tool(name="lithos_edge_upsert")
        async def lithos_edge_upsert(
            type: str = "",
            source_note_id: str = "",
            target_note_id: str = "",
            evidence: dict[str, Any] | None = None,
        ) -> str:
            calls.append(
                (
                    "lithos_edge_upsert",
                    {
                        "type": type,
                        "source_note_id": source_note_id,
                        "target_note_id": target_note_id,
                        "evidence": evidence,
                    },
                )
            )
            if edge_upsert_responses:
                return edge_upsert_responses.pop(0)
            return _json.dumps({"status": "ok"})

        @self._mcp.tool(name="lithos_task_create")
        async def lithos_task_create(
            title: str = "",
            agent: str = "",
            tags: list[str] | None = None,
        ) -> str:
            calls.append(
                (
                    "lithos_task_create",
                    {"title": title, "agent": agent, "tags": tags or []},
                )
            )
            if task_create_responses:
                return task_create_responses.pop(0)
            return _json.dumps({"task_id": "task-001"})

        @self._mcp.tool(name="lithos_task_complete")
        async def lithos_task_complete(
            task_id: str = "",
            agent: str = "",
            outcome: str | None = None,
        ) -> str:
            calls.append(
                (
                    "lithos_task_complete",
                    {"task_id": task_id, "agent": agent, "outcome": outcome},
                )
            )
            if task_complete_responses:
                return task_complete_responses.pop(0)
            return _json.dumps({"status": "completed"})

    def start(self) -> None:
        app = self._mcp.sse_app()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._uvicorn_server.run, daemon=True)
        self._thread.start()
        # Wait until the server is accepting connections.
        self._wait_for_ready()

    def _wait_for_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"Fake Lithos server did not start within {timeout}s")

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
    fake_lithos_server.read_responses.clear()
    fake_lithos_server.cache_lookup_responses.clear()
    fake_lithos_server.list_responses.clear()


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

    async def test_not_connected_at_construction(self, fake_lithos_url: str) -> None:
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
            list_calls = [c for c in fake_lithos_server.calls if c[0] == "lithos_list"]
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
            list_calls = [c for c in fake_lithos_server.calls if c[0] == "lithos_list"]
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
            await client.list_notes(tags=["arxiv-id:2601.12345"], limit=5)
            list_calls = [c for c in fake_lithos_server.calls if c[0] == "lithos_list"]
            assert len(list_calls) == 1
            assert list_calls[0][1]["limit"] == 5
        finally:
            await client.close()

    async def test_order_by_and_order_not_forwarded_to_current_lithos(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Ordering args are accepted but not sent to current Lithos."""
        client = LithosClient(url=fake_lithos_url)
        try:
            await client.list_notes(
                tags=["influx:repair-needed", "profile:ai-robotics"],
                limit=100,
                order_by="updated_at",
                order="asc",
            )
            list_calls = [c for c in fake_lithos_server.calls if c[0] == "lithos_list"]
            assert len(list_calls) == 1
            assert list_calls[0][1]["tags"] == [
                "influx:repair-needed",
                "profile:ai-robotics",
            ]
            assert list_calls[0][1]["limit"] == 100
            assert list_calls[0][1]["order_by"] is None
            assert list_calls[0][1]["order"] is None
        finally:
            await client.close()

    async def test_order_by_omitted_when_none(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """order_by and order are not sent when None (backward compat)."""
        client = LithosClient(url=fake_lithos_url)
        try:
            await client.list_notes(tags=["some-tag"])
            list_calls = [c for c in fake_lithos_server.calls if c[0] == "lithos_list"]
            assert len(list_calls) == 1
            assert list_calls[0][1]["order_by"] is None
            assert list_calls[0][1]["order"] is None
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
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 1
            payload = write_calls[0][1]
            assert payload["title"] == "Attention Is All You Need"
            assert payload["content"] == ("# Summary\nTransformer architecture paper.")
            assert payload["agent"] == "influx"
            assert payload["path"] == "papers/arxiv/2026/03"
            assert payload["source_url"] == ("https://arxiv.org/abs/1706.03762")
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
            assert result.source_url == ("https://arxiv.org/abs/1706.03762")
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
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 1
            assert write_calls[0][1]["expires_at"] == ("2026-04-30T00:00:00Z")
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
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
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
        fake_lithos_server.write_responses.append('{"status": "duplicate"}')
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
            assert result.source_url == ("https://arxiv.org/abs/2601.00001")
        finally:
            await client.close()

    async def test_duplicate_no_second_write(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """duplicate: exactly one lithos_write call, no retry."""
        fake_lithos_server.write_responses.append('{"status": "duplicate"}')
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
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
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
        fake_lithos_server.write_responses.append('{"status": "duplicate"}')
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
            assert result.source_url == ("https://arxiv.org/abs/2601.00004")
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


class TestWriteEnvelopeUnknownStatus:
    """Undocumented statuses (e.g. ``status='error'``) must surface a diagnosable
    detail and a WARNING log so operators can root-cause from logs alone.
    """

    async def test_error_status_captures_detail(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """status='error' surfaces the response ``reason`` as WriteResult.detail."""
        fake_lithos_server.write_responses.append(
            '{"status": "error", "reason": "lithos says no"}'
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Broken item",
                content="# Summary\nContent.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.99999",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            assert result.status == "error"
            assert result.detail == "lithos says no"
            assert result.source_url == "https://arxiv.org/abs/2601.99999"
        finally:
            await client.close()

    async def test_unknown_status_logs_warning_with_structured_extra(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Undocumented status emits a WARNING carrying status, source_url, detail."""
        import logging

        fake_lithos_server.write_responses.append(
            '{"status": "error", "detail": "constraint failure"}'
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            with caplog.at_level(logging.WARNING, logger="influx.lithos_client"):
                await client.write_note(
                    title="Broken item 2",
                    content="# Summary\nContent.",
                    path="papers/arxiv/2026/03",
                    source_url="https://arxiv.org/abs/2601.99998",
                    tags=["profile:ml-research"],
                    confidence=0.8,
                )
            matching = [
                r
                for r in caplog.records
                if r.levelname == "WARNING"
                and "lithos_write returned non-success" in r.getMessage()
            ]
            assert matching, (
                f"expected warning, got {[r.getMessage() for r in caplog.records]}"
            )
            r = matching[0]
            assert getattr(r, "lithos_status", None) == "error"
            assert getattr(r, "source_url", None) == "https://arxiv.org/abs/2601.99998"
            assert getattr(r, "detail", None) == "constraint failure"
        finally:
            await client.close()

    async def test_slug_collision_masqueraded_as_error_is_rerouted(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A ``status='error'`` body whose ``reason`` describes a slug clash
        must be re-routed through the existing slug-collision retry, so the
        write succeeds with a disambiguated title (staging incident
        2026-05-01).
        """
        import logging

        slug_msg = (
            "Slug 'attention-is-all-you-need' already in use by "
            "document 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'"
        )
        fake_lithos_server.write_responses.extend(
            [
                f'{{"status": "error", "reason": "{slug_msg}"}}',
                '{"status": "created"}',
            ]
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            with caplog.at_level(logging.WARNING, logger="influx.lithos_client"):
                result = await client.write_note(
                    title="Attention Is All You Need",
                    content="# Summary\nTransformer paper.",
                    path="papers/arxiv/2026/03",
                    source_url="https://arxiv.org/abs/1706.03762",
                    tags=["profile:ml-research"],
                    confidence=0.9,
                )
            assert result.status == "created"
            # Retry must have run with the disambiguating title suffix.
            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2
            assert write_calls[1][1]["title"] == (
                "Attention Is All You Need [arXiv 1706.03762]"
            )
            # The reroute itself is logged so operators can spot Lithos
            # still mis-classifying these.
            assert any(
                "slug-collision masquerading as error" in r.getMessage()
                for r in caplog.records
            )
        finally:
            await client.close()


# ── Write envelopes — slug_collision (FR-MCP-7, AC-05-D) ──────────


class TestWriteEnvelopeSlugCollision:
    """``slug_collision`` envelope: retry once with title suffix (AC-05-D)."""

    async def test_arxiv_slug_collision_retry_with_suffix(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """arXiv URL: retry with ` [arXiv <id>]` suffix → succeeds."""
        fake_lithos_server.write_responses.extend(
            [
                '{"status": "slug_collision"}',
                '{"status": "created"}',
            ]
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Attention Is All You Need",
                content="# Summary\nTransformer paper.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/1706.03762",
                tags=["profile:ml-research"],
                confidence=0.9,
            )
            assert result.status == "created"

            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2
            assert write_calls[0][1]["title"] == ("Attention Is All You Need")
            assert write_calls[1][1]["title"] == (
                "Attention Is All You Need [arXiv 1706.03762]"
            )
        finally:
            await client.close()

    async def test_web_slug_collision_retry_with_host_suffix(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Web/RSS URL: retry with ` [<host>]` suffix → succeeds."""
        fake_lithos_server.write_responses.extend(
            [
                '{"status": "slug_collision"}',
                '{"status": "created"}',
            ]
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Great Blog Post",
                content="# Summary\nBlog content.",
                path="papers/web/2026/03",
                source_url="https://example.com/blog/great-post",
                tags=["profile:ml-research"],
                confidence=0.7,
            )
            assert result.status == "created"

            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2
            assert write_calls[0][1]["title"] == "Great Blog Post"
            assert write_calls[1][1]["title"] == ("Great Blog Post [example.com]")
        finally:
            await client.close()

    async def test_second_slug_collision_skips(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Second slug_collision: skip item, no further retry."""
        fake_lithos_server.write_responses.extend(
            [
                '{"status": "slug_collision"}',
                '{"status": "slug_collision"}',
            ]
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Duplicate Slug",
                content="# Summary\nContent.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.99999",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            assert result.status == "slug_collision"

            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2  # No third attempt
        finally:
            await client.close()

    async def test_second_slug_collision_logs(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Second slug_collision: warning logged with source_url."""
        import logging

        fake_lithos_server.write_responses.extend(
            [
                '{"status": "slug_collision"}',
                '{"status": "slug_collision"}',
            ]
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            with caplog.at_level(logging.WARNING):
                await client.write_note(
                    title="Dup Slug Log",
                    content="# Summary\nContent.",
                    path="papers/arxiv/2026/03",
                    source_url="https://arxiv.org/abs/2601.88888",
                    tags=["profile:ml-research"],
                    confidence=0.8,
                )
            assert "slug_collision" in caplog.text
            assert "2601.88888" in caplog.text
        finally:
            await client.close()


# ── Write envelopes — version_conflict (FR-MCP-7, AC-05-E) ────────


class TestWriteEnvelopeVersionConflict:
    """``version_conflict``: re-read + tag-merge + user-notes, retry once."""

    async def test_version_conflict_reread_merge_retry(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """First conflict: re-read, merge tags + user notes, retry succeeds."""
        import json as _json

        fake_lithos_server.write_responses.extend(
            [
                '{"status": "version_conflict", "note_id": "note-042"}',
                '{"status": "updated"}',
            ]
        )
        fake_lithos_server.read_responses.append(
            _json.dumps(
                {
                    "id": "note-042",
                    "content": (
                        "# Summary\nOld content.\n\n"
                        "## User Notes\nMy custom annotations."
                    ),
                    "tags": [
                        "profile:ml-research",
                        "user-custom-tag",
                        "influx:rejected:other-profile",
                    ],
                    "version": 3,
                }
            )
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Updated Paper",
                content="# Summary\nNew content.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.11111",
                tags=["profile:ml-research", "source:arxiv"],
                confidence=0.9,
            )
            assert result.status == "updated"

            # Verify lithos_read was called with note_id.
            read_calls = [c for c in fake_lithos_server.calls if c[0] == "lithos_read"]
            assert len(read_calls) == 1
            assert read_calls[0][1]["id"] == "note-042"

            # Verify the retry write has merged tags.
            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2
            retry_payload = write_calls[1][1]
            retry_tags = retry_payload["tags"]
            # Existing tags preserved + new tags present.
            assert "profile:ml-research" in retry_tags
            assert "source:arxiv" in retry_tags
            assert "user-custom-tag" in retry_tags
            assert "influx:rejected:other-profile" in retry_tags

            # Verify user notes preserved in content.
            assert "## User Notes" in retry_payload["content"]
            assert "My custom annotations" in retry_payload["content"]
            # New content is also present.
            assert "New content" in retry_payload["content"]

            # Verify version info forwarded.
            assert retry_payload["expected_version"] == 3
            assert retry_payload["id"] == "note-042"
        finally:
            await client.close()

    async def test_version_conflict_preserves_user_notes_block(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """User Notes block from existing note replaces any in new content."""
        import json as _json

        fake_lithos_server.write_responses.extend(
            [
                '{"status": "version_conflict", "note_id": "note-043"}',
                '{"status": "updated"}',
            ]
        )
        existing_user_notes = (
            "## User Notes\n"
            "Important: this paper is referenced in our Q3 review.\n"
            "Follow up with team lead."
        )
        fake_lithos_server.read_responses.append(
            _json.dumps(
                {
                    "id": "note-043",
                    "content": f"# Summary\nOld.\n\n{existing_user_notes}",
                    "tags": ["profile:ml-research"],
                    "version": 5,
                }
            )
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            # New content has its own ## User Notes that should be replaced.
            await client.write_note(
                title="Paper X",
                content="# Summary\nRefreshed.\n\n## User Notes\n",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.22222",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            retry_content = write_calls[1][1]["content"]
            assert "Refreshed" in retry_content
            assert "Important: this paper is referenced" in retry_content
            assert "Follow up with team lead" in retry_content
        finally:
            await client.close()

    async def test_second_version_conflict_skips(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Second version_conflict: skip item, no further retry."""
        import json as _json

        fake_lithos_server.write_responses.extend(
            [
                '{"status": "version_conflict", "note_id": "note-044"}',
                '{"status": "version_conflict", "note_id": "note-044"}',
            ]
        )
        fake_lithos_server.read_responses.append(
            _json.dumps(
                {
                    "id": "note-044",
                    "content": "# Summary\nContent.",
                    "tags": ["profile:ml-research"],
                    "version": 7,
                }
            )
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Conflict Item",
                content="# Summary\nNew.",
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.33333",
                tags=["profile:ml-research"],
                confidence=0.8,
            )
            assert result.status == "version_conflict"

            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2  # No third attempt
        finally:
            await client.close()

    async def test_second_version_conflict_logs(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Second version_conflict: warning logged with source_url."""
        import json as _json
        import logging

        fake_lithos_server.write_responses.extend(
            [
                '{"status": "version_conflict", "note_id": "note-045"}',
                '{"status": "version_conflict", "note_id": "note-045"}',
            ]
        )
        fake_lithos_server.read_responses.append(
            _json.dumps(
                {
                    "id": "note-045",
                    "content": "# Summary\nContent.",
                    "tags": [],
                    "version": 1,
                }
            )
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            with caplog.at_level(logging.WARNING):
                await client.write_note(
                    title="Conflict Log",
                    content="# Summary\nNew.",
                    path="papers/arxiv/2026/03",
                    source_url="https://arxiv.org/abs/2601.44444",
                    tags=["profile:ml-research"],
                    confidence=0.8,
                )
            assert "version_conflict" in caplog.text
            assert "2601.44444" in caplog.text
        finally:
            await client.close()


# ── Write envelopes — content_too_large (§9.7, AC-05-F) ───────────


_CONTENT_WITH_TIERS = (
    "# Summary\nTransformer paper.\n\n"
    "## Full Text\n\n"
    "### Introduction\nIntro text.\n\n"
    "### Methods\nMethods text.\n\n"
    "## Claims\n- Claim 1\n\n"
    "## User Notes\nKeep this."
)


class TestWriteEnvelopeContentTooLarge:
    """``content_too_large``: trim Tier 2 retry + create-path skip."""

    async def test_first_retry_drops_tier2_and_succeeds(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """First content_too_large → drop Tier 2, retry → succeeds."""
        fake_lithos_server.write_responses.extend(
            [
                '{"status": "content_too_large"}',
                '{"status": "created"}',
            ]
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Big Paper",
                content=_CONTENT_WITH_TIERS,
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.50001",
                tags=["profile:ml-research"],
                confidence=0.9,
            )
            assert result.status == "created"

            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2

            # First call has full content.
            assert "## Full Text" in write_calls[0][1]["content"]
            # Retry has Tier 2 removed but Tier 1 + Tier 3 kept.
            retry_content = write_calls[1][1]["content"]
            assert "## Full Text" not in retry_content
            assert "Transformer paper" in retry_content
            assert "## Claims" in retry_content
            assert "## User Notes" in retry_content
        finally:
            await client.close()

    async def test_create_path_skip_on_second_content_too_large(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Second content_too_large + no existing note → skip + count + log."""
        import logging

        fake_lithos_server.write_responses.extend(
            [
                '{"status": "content_too_large"}',
                '{"status": "content_too_large"}',
            ]
        )
        # No existing note: cache_lookup returns miss (default).
        client = LithosClient(url=fake_lithos_url)
        try:
            with caplog.at_level(logging.WARNING):
                result = await client.write_note(
                    title="Huge Paper",
                    content=_CONTENT_WITH_TIERS,
                    path="papers/arxiv/2026/03",
                    source_url="https://arxiv.org/abs/2601.50002",
                    tags=["profile:ml-research"],
                    confidence=0.9,
                )

            # Status indicates skipped for counter.
            assert result.status == "content_too_large_skipped"
            assert result.detail == "create_path"
            assert result.source_url == ("https://arxiv.org/abs/2601.50002")

            # Exactly 2 write attempts (original + Tier-2-trimmed retry).
            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2

            # cache_lookup was called to check for existing note.
            lookup_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_cache_lookup"
            ]
            assert len(lookup_calls) == 1

            # Warning logged with source_url.
            assert "content_too_large" in caplog.text
            assert "2601.50002" in caplog.text
        finally:
            await client.close()

    async def test_create_path_no_note_persisted(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Create path: no degraded placeholder note is invented."""
        fake_lithos_server.write_responses.extend(
            [
                '{"status": "content_too_large"}',
                '{"status": "content_too_large"}',
            ]
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Giant Paper",
                content=_CONTENT_WITH_TIERS,
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.50003",
                tags=["profile:ml-research"],
                confidence=0.9,
            )
            assert result.status == "content_too_large_skipped"

            # Only 2 lithos_write calls — no third "placeholder" write.
            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2
        finally:
            await client.close()


# ── Write envelopes — content_too_large repair path (§9.7, AC-05-F) ─


class TestWriteEnvelopeContentTooLargeRepairPath:
    """``content_too_large`` repair path: Tier-1-only retry + repair-needed tag."""

    async def test_repair_path_tier1_retry_succeeds(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Repair path: Tier-1-only retry succeeds.

        Second content_too_large with existing note triggers
        repair-path Tier-1-only retry.  Asserts repair-needed
        tag present and existing tags preserved (AC-05-F).
        """
        import json as _json

        # 1st write → content_too_large
        # 2nd write (Tier 2 dropped) → content_too_large
        # 3rd write (Tier 1 only, repair) → updated
        fake_lithos_server.write_responses.extend(
            [
                '{"status": "content_too_large"}',
                '{"status": "content_too_large"}',
                '{"status": "updated"}',
            ]
        )
        # cache_lookup returns hit → existing note found (repair path).
        # Existing rejection guards a *different* profile so the canonical
        # merge contract (FR-NOTE-6) preserves profile:ml-research.
        fake_lithos_server.cache_lookup_responses.append(
            _json.dumps(
                {
                    "hit": True,
                    "id": "note-repair-001",
                    "tags": [
                        "profile:ml-research",
                        "user-custom-tag",
                        "influx:rejected:robotics",
                    ],
                }
            )
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Large Paper",
                content=_CONTENT_WITH_TIERS,
                path="papers/arxiv/2026/03",
                source_url="https://arxiv.org/abs/2601.60001",
                tags=["profile:ml-research", "source:arxiv"],
                confidence=0.9,
            )
            assert result.status == "updated"

            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            # 3 writes: original, Tier-2-dropped retry, Tier-1-only repair.
            assert len(write_calls) == 3

            # First call has full content.
            assert "## Full Text" in write_calls[0][1]["content"]
            # Second call has Tier 2 dropped, Tier 3 kept.
            assert "## Full Text" not in write_calls[1][1]["content"]
            assert "## Claims" in write_calls[1][1]["content"]

            # Third call (repair): Tier 1 only — no Tier 2 or Tier 3.
            repair_payload = write_calls[2][1]
            repair_content = repair_payload["content"]
            assert "## Full Text" not in repair_content
            assert "## Claims" not in repair_content
            assert "## Datasets & Benchmarks" not in repair_content
            assert "## Builds On" not in repair_content
            assert "## Open Questions" not in repair_content
            # Tier 1 content preserved.
            assert "Transformer paper" in repair_content

            # influx:repair-needed tag present.
            repair_tags = repair_payload["tags"]
            assert "influx:repair-needed" in repair_tags
            # Existing tags preserved via canonical merge (FR-NOTE-5/6/7/8).
            assert "profile:ml-research" in repair_tags
            assert "source:arxiv" in repair_tags
            assert "user-custom-tag" in repair_tags
            assert "influx:rejected:robotics" in repair_tags
        finally:
            await client.close()

    async def test_repair_path_tier1_fails_leaves_existing_untouched(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Repair path Tier-1 also too large.

        Existing note untouched, counter incremented, no abort,
        updated_at unchanged (AC-05-F repair path).
        """
        import json as _json
        import logging

        # 1st write → content_too_large
        # 2nd write (Tier 2 dropped) → content_too_large
        # 3rd write (Tier 1 only, repair) → content_too_large
        fake_lithos_server.write_responses.extend(
            [
                '{"status": "content_too_large"}',
                '{"status": "content_too_large"}',
                '{"status": "content_too_large"}',
            ]
        )
        # cache_lookup returns hit → existing note found (repair path).
        fake_lithos_server.cache_lookup_responses.append(
            _json.dumps(
                {
                    "hit": True,
                    "id": "note-repair-002",
                    "tags": ["profile:ml-research"],
                }
            )
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            with caplog.at_level(logging.WARNING):
                result = await client.write_note(
                    title="Enormous Paper",
                    content=_CONTENT_WITH_TIERS,
                    path="papers/arxiv/2026/03",
                    source_url="https://arxiv.org/abs/2601.60002",
                    tags=["profile:ml-research"],
                    confidence=0.9,
                )

            # Status indicates skipped with repair-path detail.
            assert result.status == "content_too_large_skipped"
            assert result.detail == "repair_path_tier1_failed"
            assert result.source_url == ("https://arxiv.org/abs/2601.60002")

            # Exactly 3 write attempts (original + Tier-2 retry + Tier-1 repair).
            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 3

            # No further writes after the third failure — existing note untouched.
            # (No 4th "overwrite" or "placeholder" call.)

            # Warning logged with source_url.
            assert "content_too_large" in caplog.text
            assert "2601.60002" in caplog.text
            assert "repair path" in caplog.text.lower()
        finally:
            await client.close()


# ── Feedback ingestion (FR-FB-1..3, AC-05-H) ─────────────────────


class TestFeedbackFetch:
    """Feedback-fetch helper via lithos_list (FR-FB-1, FR-FB-2)."""

    async def test_fetch_rejection_titles_happy_path(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """3 rejection items with titles → 3 titles returned."""
        import json as _json

        fake_lithos_server.list_responses.append(
            _json.dumps(
                {
                    "items": [
                        {"id": "r1", "title": "Bad Paper A"},
                        {"id": "r2", "title": "Bad Paper B"},
                        {"id": "r3", "title": "Bad Paper C"},
                    ]
                }
            )
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            titles = await fetch_rejection_titles(
                client,
                profile="ml-research",
                limit=20,
            )
            assert titles == [
                "Bad Paper A",
                "Bad Paper B",
                "Bad Paper C",
            ]
            # Verify correct lithos_list call was made.
            list_calls = [c for c in fake_lithos_server.calls if c[0] == "lithos_list"]
            assert len(list_calls) == 1
            assert list_calls[0][1]["tags"] == ["influx:rejected:ml-research"]
            assert list_calls[0][1]["limit"] == 20
        finally:
            await client.close()

    async def test_fetch_titles_fallback_to_read(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Item without title triggers lithos_read fallback (FR-FB-2)."""
        import json as _json

        fake_lithos_server.list_responses.append(
            _json.dumps(
                {
                    "items": [
                        {"id": "r1", "title": "Has Title"},
                        {"id": "r2"},  # no title
                    ]
                }
            )
        )
        fake_lithos_server.read_responses.append(
            _json.dumps(
                {
                    "id": "r2",
                    "title": "Fetched Via Read",
                    "content": "",
                    "tags": [],
                    "version": 1,
                }
            )
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            titles = await fetch_rejection_titles(
                client,
                profile="ai",
                limit=10,
            )
            assert titles == ["Has Title", "Fetched Via Read"]
            # Verify lithos_read was called for the missing-title item.
            read_calls = [c for c in fake_lithos_server.calls if c[0] == "lithos_read"]
            assert len(read_calls) == 1
            assert read_calls[0][1]["id"] == "r2"
        finally:
            await client.close()

    async def test_fetch_titles_empty_result(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Empty lithos_list response → empty titles list."""
        import json as _json

        fake_lithos_server.list_responses.append(_json.dumps({"items": []}))
        client = LithosClient(url=fake_lithos_url)
        try:
            titles = await fetch_rejection_titles(
                client,
                profile="ai",
                limit=20,
            )
            assert titles == []
        finally:
            await client.close()

    async def test_build_negative_examples_block_seam(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """AC-05-H (seam): 3 rejection items → formatted block via seam."""
        import json as _json

        fake_lithos_server.list_responses.append(
            _json.dumps(
                {
                    "items": [
                        {"id": "r1", "title": "Bad Paper A"},
                        {"id": "r2", "title": "Bad Paper B"},
                        {"id": "r3", "title": "Bad Paper C"},
                    ]
                }
            )
        )
        client = LithosClient(url=fake_lithos_url)
        try:
            block = await build_negative_examples_block(
                client,
                profile="ml-research",
                limit=20,
                max_title_chars=200,
            )
            expected = (
                '- "Bad Paper A" (rejected)\n'
                '- "Bad Paper B" (rejected)\n'
                '- "Bad Paper C" (rejected)'
            )
            assert block == expected
        finally:
            await client.close()


class TestFeedbackTagIntegrity:
    """Influx never synthesizes ``influx:rejected:<profile>`` (FR-FB-3).

    Verifies behaviorally that Influx's write wrappers do not
    introduce new ``influx:rejected:<profile>`` tags; they are
    authored solely by Lens.
    """

    async def test_write_does_not_synthesize_rejected_tag(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """A normal write never introduces an influx:rejected:* tag."""
        client = LithosClient(url=fake_lithos_url)
        try:
            await client.write_note(
                title="Good Paper",
                content="# Summary\nGreat paper.",
                path="papers/arxiv/2026/04",
                source_url="https://arxiv.org/abs/2601.99999",
                tags=[
                    "profile:ml-research",
                    "arxiv-id:2601.99999",
                    "source:arxiv",
                ],
                confidence=0.85,
            )
            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 1
            written_tags = write_calls[0][1]["tags"]
            # No influx:rejected:* tag synthesized by Influx.
            assert not any(t.startswith("influx:rejected:") for t in written_tags)
        finally:
            await client.close()

    async def test_tag_merge_preserves_existing_rejected_tag(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """version_conflict re-write preserves existing rejected tags."""
        import json as _json

        # First write returns version_conflict.
        fake_lithos_server.write_responses.append(
            _json.dumps(
                {
                    "status": "version_conflict",
                    "note_id": "note-rejected-001",
                }
            )
        )
        # Read returns a note with an existing rejected tag.
        fake_lithos_server.read_responses.append(
            _json.dumps(
                {
                    "id": "note-rejected-001",
                    "title": "Existing Paper",
                    "content": "# Summary\nOld content.",
                    "tags": [
                        "profile:ml-research",
                        "influx:rejected:robotics",
                        "source:arxiv",
                    ],
                    "version": 2,
                }
            )
        )
        # Retry write succeeds.
        fake_lithos_server.write_responses.append('{"status": "updated"}')

        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Existing Paper",
                content="# Summary\nNew content.",
                path="papers/arxiv/2026/04",
                source_url="https://arxiv.org/abs/2601.88888",
                tags=[
                    "profile:ml-research",
                    "arxiv-id:2601.88888",
                    "source:arxiv",
                ],
                confidence=0.9,
            )
            assert result.status == "updated"
            # The retry write should contain the merged tags.
            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            # 2 writes: original + retry after version_conflict.
            assert len(write_calls) == 2
            retry_tags = write_calls[1][1]["tags"]
            # Existing influx:rejected:robotics is preserved via merge.
            assert "influx:rejected:robotics" in retry_tags
        finally:
            await client.close()

    async def test_version_conflict_replaces_stale_influx_owned_tags(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Stale Influx-owned tags (e.g. source:rss) are fully replaced.

        FR-NOTE-5: Influx-owned prefix tags from the existing note must
        not survive when the new write supplies a fresh value.  The
        canonical :func:`influx.notes.merge_tags` contract is enforced
        at the version_conflict retry chokepoint.
        """
        import json as _json

        fake_lithos_server.write_responses.extend(
            [
                '{"status": "version_conflict", "note_id": "note-stale-001"}',
                '{"status": "updated"}',
            ]
        )
        fake_lithos_server.read_responses.append(
            _json.dumps(
                {
                    "id": "note-stale-001",
                    "content": "# Summary\nOld content.",
                    "tags": [
                        "source:rss",
                        "arxiv-id:old.0001",
                        "profile:ml-research",
                        "user-custom-tag",
                    ],
                    "version": 4,
                }
            )
        )

        client = LithosClient(url=fake_lithos_url)
        try:
            result = await client.write_note(
                title="Updated Paper",
                content="# Summary\nNew content.",
                path="papers/arxiv/2026/04",
                source_url="https://arxiv.org/abs/2601.77777",
                tags=[
                    "profile:ml-research",
                    "source:arxiv",
                    "arxiv-id:2601.77777",
                ],
                confidence=0.9,
            )
            assert result.status == "updated"

            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2
            retry_tags = write_calls[1][1]["tags"]

            # New Influx-owned tags present.
            assert "source:arxiv" in retry_tags
            assert "arxiv-id:2601.77777" in retry_tags
            # Stale Influx-owned tags fully replaced.
            assert "source:rss" not in retry_tags
            assert "arxiv-id:old.0001" not in retry_tags
            # External + profile + rejection tags preserved.
            assert "user-custom-tag" in retry_tags
            assert "profile:ml-research" in retry_tags
        finally:
            await client.close()

    async def test_version_conflict_rejection_guard_blocks_profile(
        self,
        fake_lithos_url: str,
        fake_lithos_server: FakeLithosServer,
        clear_fake_calls: None,
    ) -> None:
        """Rejection guard: ``influx:rejected:<p>`` blocks ``profile:<p>``.

        FR-NOTE-6: a rejected profile tag on the existing note prevents
        the matching ``profile:<p>`` tag from being re-added on rewrite.
        """
        import json as _json

        fake_lithos_server.write_responses.extend(
            [
                '{"status": "version_conflict", "note_id": "note-reject-002"}',
                '{"status": "updated"}',
            ]
        )
        fake_lithos_server.read_responses.append(
            _json.dumps(
                {
                    "id": "note-reject-002",
                    "content": "# Summary\nOld.",
                    "tags": [
                        "profile:robotics",
                        "influx:rejected:robotics",
                    ],
                    "version": 1,
                }
            )
        )

        client = LithosClient(url=fake_lithos_url)
        try:
            await client.write_note(
                title="Rejected Paper",
                content="# Summary\nNew.",
                path="papers/arxiv/2026/04",
                source_url="https://arxiv.org/abs/2601.66666",
                tags=["profile:robotics", "source:arxiv"],
                confidence=0.7,
            )
            write_calls = [
                c for c in fake_lithos_server.calls if c[0] == "lithos_write"
            ]
            assert len(write_calls) == 2
            retry_tags = write_calls[1][1]["tags"]
            # Rejection guard blocks profile:robotics.
            assert "profile:robotics" not in retry_tags
            assert "influx:rejected:robotics" in retry_tags
            assert "source:arxiv" in retry_tags
        finally:
            await client.close()
