"""Contract tests for LCMA wrappers on LithosClient (PRD 08, US-003).

Exercises happy-path calls for the four LCMA tools and unknown_tool
error translation for ``lithos_retrieve`` and ``lithos_edge_upsert``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Generator
from typing import Any

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from influx.errors import LCMAError
from influx.lithos_client import LithosClient

# ── Fake Lithos server with LCMA tools ────────────────────────────


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLCMAServer:
    """Minimal MCP server exposing the four LCMA tools + agent_register."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.port = _find_free_port()
        self._mcp = FastMCP("fake-lithos-lcma")
        self._uvicorn_server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        # Per-tool response queues (FIFO).
        self.retrieve_responses: list[str] = []
        self.edge_upsert_responses: list[str] = []
        self.task_create_responses: list[str] = []
        self.task_complete_responses: list[str] = []
        self._register_tools()

    def _register_tools(self) -> None:
        calls = self.calls
        retrieve_responses = self.retrieve_responses
        edge_upsert_responses = self.edge_upsert_responses
        task_create_responses = self.task_create_responses
        task_complete_responses = self.task_complete_responses

        @self._mcp.tool(name="lithos_agent_register")
        async def lithos_agent_register(
            id: str = "", name: str = "", type: str = ""
        ) -> str:
            calls.append(
                ("lithos_agent_register", {"id": id, "name": name, "type": type})
            )
            return '{"registered": true}'

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
            if retrieve_responses:
                return retrieve_responses.pop(0)
            return json.dumps({"results": []})

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
            return json.dumps({"status": "ok"})

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
            return json.dumps({"task_id": "task-001"})

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
            return json.dumps({"status": "completed"})

    def start(self) -> None:
        app = self._mcp.sse_app()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._uvicorn_server.run, daemon=True)
        self._thread.start()
        self._wait_for_ready()

    def _wait_for_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"Fake LCMA server did not start within {timeout}s")

    def stop(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


class FakeMinimalServer:
    """MCP server that does NOT register LCMA tools — for unknown_tool tests."""

    def __init__(self) -> None:
        self.port = _find_free_port()
        self._mcp = FastMCP("fake-lithos-minimal")
        self._uvicorn_server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

        @self._mcp.tool(name="lithos_agent_register")
        async def lithos_agent_register(
            id: str = "", name: str = "", type: str = ""
        ) -> str:
            return '{"registered": true}'

    def start(self) -> None:
        app = self._mcp.sse_app()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._uvicorn_server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("Fake minimal server did not start")

    def stop(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def lcma_server() -> Generator[FakeLCMAServer, None, None]:
    server = FakeLCMAServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def lcma_url(lcma_server: FakeLCMAServer) -> str:
    return f"http://127.0.0.1:{lcma_server.port}/sse"


@pytest.fixture
def clear_lcma_calls(lcma_server: FakeLCMAServer) -> None:
    lcma_server.calls.clear()
    lcma_server.retrieve_responses.clear()
    lcma_server.edge_upsert_responses.clear()
    lcma_server.task_create_responses.clear()
    lcma_server.task_complete_responses.clear()


@pytest.fixture(scope="module")
def minimal_server() -> Generator[FakeMinimalServer, None, None]:
    server = FakeMinimalServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def minimal_url(minimal_server: FakeMinimalServer) -> str:
    return f"http://127.0.0.1:{minimal_server.port}/sse"


# ── Happy-path tests ─────────────────────────────────────────────


class TestRetrieveHappyPath:
    """``LithosClient.retrieve`` forwards args and returns result."""

    async def test_retrieve_reaches_server(
        self,
        lcma_url: str,
        lcma_server: FakeLCMAServer,
        clear_lcma_calls: None,
    ) -> None:
        client = LithosClient(url=lcma_url)
        try:
            result = await client.retrieve(
                query="test query",
                limit=5,
                agent_id="influx",
                task_id="task-123",
                tags=["profile:ai"],
            )
            assert result is not None
            assert not result.isError
            retrieve_calls = [
                c for c in lcma_server.calls if c[0] == "lithos_retrieve"
            ]
            assert len(retrieve_calls) == 1
            assert retrieve_calls[0][1] == {
                "query": "test query",
                "limit": 5,
                "agent_id": "influx",
                "task_id": "task-123",
                "tags": ["profile:ai"],
            }
        finally:
            await client.close()

    async def test_retrieve_returns_results(
        self,
        lcma_url: str,
        lcma_server: FakeLCMAServer,
        clear_lcma_calls: None,
    ) -> None:
        lcma_server.retrieve_responses.append(
            json.dumps(
                {
                    "results": [
                        {
                            "title": "Related Note",
                            "score": 0.85,
                            "receipt_id": "r-001",
                        }
                    ]
                }
            )
        )
        client = LithosClient(url=lcma_url)
        try:
            result = await client.retrieve(
                query="test",
                limit=5,
                agent_id="influx",
                task_id="t-1",
                tags=[],
            )
            text = result.content[0].text  # type: ignore[union-attr]
            body = json.loads(text)
            assert len(body["results"]) == 1
            assert body["results"][0]["score"] == 0.85
        finally:
            await client.close()


class TestEdgeUpsertHappyPath:
    """``LithosClient.edge_upsert`` forwards args and returns result."""

    async def test_edge_upsert_reaches_server(
        self,
        lcma_url: str,
        lcma_server: FakeLCMAServer,
        clear_lcma_calls: None,
    ) -> None:
        client = LithosClient(url=lcma_url)
        try:
            result = await client.edge_upsert(
                type="related_to",
                source_note_id="note-a",
                target_note_id="note-b",
                evidence={
                    "kind": "lithos_retrieve",
                    "score": 0.85,
                    "receipt_id": "r-001",
                },
            )
            assert result is not None
            assert not result.isError
            edge_calls = [
                c for c in lcma_server.calls if c[0] == "lithos_edge_upsert"
            ]
            assert len(edge_calls) == 1
            assert edge_calls[0][1]["type"] == "related_to"
        finally:
            await client.close()


class TestTaskCreateHappyPath:
    """``LithosClient.task_create`` forwards args and returns result."""

    async def test_task_create_reaches_server(
        self,
        lcma_url: str,
        lcma_server: FakeLCMAServer,
        clear_lcma_calls: None,
    ) -> None:
        client = LithosClient(url=lcma_url)
        try:
            result = await client.task_create(
                title="Influx run ai-robotics 2026-04-26",
                agent="influx",
                tags=["influx:run", "profile:ai-robotics"],
            )
            assert result is not None
            assert not result.isError
            text = result.content[0].text  # type: ignore[union-attr]
            body = json.loads(text)
            assert "task_id" in body

            create_calls = [
                c for c in lcma_server.calls if c[0] == "lithos_task_create"
            ]
            assert len(create_calls) == 1
            assert create_calls[0][1] == {
                "title": "Influx run ai-robotics 2026-04-26",
                "agent": "influx",
                "tags": ["influx:run", "profile:ai-robotics"],
            }
        finally:
            await client.close()


class TestTaskCompleteHappyPath:
    """``LithosClient.task_complete`` forwards args and returns result."""

    async def test_task_complete_reaches_server(
        self,
        lcma_url: str,
        lcma_server: FakeLCMAServer,
        clear_lcma_calls: None,
    ) -> None:
        client = LithosClient(url=lcma_url)
        try:
            result = await client.task_complete(
                task_id="task-001",
                agent="influx",
                outcome="success",
            )
            assert result is not None
            assert not result.isError

            complete_calls = [
                c for c in lcma_server.calls if c[0] == "lithos_task_complete"
            ]
            assert len(complete_calls) == 1
            assert complete_calls[0][1] == {
                "task_id": "task-001",
                "agent": "influx",
                "outcome": "success",
            }
        finally:
            await client.close()

    async def test_task_complete_outcome_optional(
        self,
        lcma_url: str,
        lcma_server: FakeLCMAServer,
        clear_lcma_calls: None,
    ) -> None:
        """outcome is optional and defaults to None."""
        client = LithosClient(url=lcma_url)
        try:
            result = await client.task_complete(
                task_id="task-002",
                agent="influx",
            )
            assert not result.isError
            complete_calls = [
                c for c in lcma_server.calls if c[0] == "lithos_task_complete"
            ]
            assert len(complete_calls) == 1
            assert complete_calls[0][1]["outcome"] is None
        finally:
            await client.close()


# ── Unknown-tool tests ────────────────────────────────────────────


class TestRetrieveUnknownTool:
    """``LithosClient.retrieve`` raises ``LCMAError("unknown_tool")``
    when the server does not support the tool."""

    async def test_retrieve_unknown_tool(self, minimal_url: str) -> None:
        client = LithosClient(url=minimal_url)
        try:
            with pytest.raises(LCMAError, match="unknown_tool"):
                await client.retrieve(
                    query="test",
                    limit=5,
                    agent_id="influx",
                    task_id="t-1",
                    tags=[],
                )
        finally:
            await client.close()


class TestEdgeUpsertUnknownTool:
    """``LithosClient.edge_upsert`` raises ``LCMAError("unknown_tool")``
    when the server does not support the tool."""

    async def test_edge_upsert_unknown_tool(self, minimal_url: str) -> None:
        client = LithosClient(url=minimal_url)
        try:
            with pytest.raises(LCMAError, match="unknown_tool"):
                await client.edge_upsert(
                    type="related_to",
                    evidence={"kind": "test"},
                )
        finally:
            await client.close()


# ── cache_lookup non-regression ───────────────────────────────────


class TestCacheLookupNonRegression:
    """Existing ``cache_lookup`` contract (FR-MCP-3, R-7) is preserved."""

    async def test_cache_lookup_both_args_still_required(
        self,
        lcma_url: str,
    ) -> None:
        """cache_lookup still rejects missing args (non-regression)."""
        from influx.errors import LithosError

        client = LithosClient(url=lcma_url)
        try:
            with pytest.raises(LithosError, match="missing_lookup_arg"):
                await client.cache_lookup(query=None, source_url="https://x")
            with pytest.raises(LithosError, match="missing_lookup_arg"):
                await client.cache_lookup(query="q", source_url="")
        finally:
            await client.close()
