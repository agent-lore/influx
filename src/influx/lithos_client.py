"""Lithos MCP client wrapper — SSE transport (PRD 05).

Provides a lazy-connecting SSE-backed client for Lithos tool calls.
The connection is established on first tool-call use and reused for
the duration of the run (FR-MCP-2).

``LITHOS_MCP_TRANSPORT=sse`` is the only supported transport in v1;
any other value raises ``ConfigError`` before a connection is
attempted (FR-MCP-1).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

from influx.errors import ConfigError, LithosError

__all__ = ["LithosClient", "WriteResult"]


@dataclasses.dataclass(frozen=True)
class WriteResult:
    """Result of a ``write_note`` call after envelope handling (FR-MCP-7).

    *status*: ``"created"`` for a successful write, ``"duplicate"``
    for an already-ingested item (caller increments ``dedup_skipped``),
    ``"invalid_input"`` for a malformed payload (logged + skipped).
    """

    status: str
    source_url: str
    detail: str = ""

logger = logging.getLogger(__name__)


class LithosClient:
    """Lazy-connecting SSE-backed MCP client for Lithos.

    The SSE connection is established on first tool-call use (not at
    construction) and reused for the duration of the run (FR-MCP-2).

    Only ``LITHOS_MCP_TRANSPORT=sse`` is supported in v1; any other
    value raises ``ConfigError`` before a connection is attempted
    (FR-MCP-1).
    """

    def __init__(self, *, url: str, transport: str = "sse") -> None:
        if transport != "sse":
            raise ConfigError(
                f"Unsupported LITHOS_MCP_TRANSPORT={transport!r}; "
                "only 'sse' is supported in v1"
            )
        if not url:
            raise ConfigError("LITHOS_URL is required but empty")
        self._url = url
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._connect_lock = asyncio.Lock()

    # Agent identity sent on every (re-)connection (FR-MCP-8).
    _AGENT_REGISTER_ARGS: dict[str, str] = {
        "id": "influx",
        "name": "Influx Pipeline",
        "type": "ingestion-pipeline",
    }

    async def _ensure_connected(self) -> ClientSession:
        """Lazily establish the SSE connection on first use.

        On every new connection (including reconnects after an SSE drop),
        ``lithos_agent_register`` is called automatically so Lithos knows
        the agent identity (FR-MCP-8, AC-05-G).
        """
        if self._session is not None:
            return self._session

        async with self._connect_lock:
            # Double-check after acquiring the lock.
            if self._session is not None:
                return self._session

            stack = AsyncExitStack()
            try:
                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(self._url)
                )
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        client_info=mcp_types.Implementation(
                            name="influx", version="0.1.0"
                        ),
                    )
                )
                await session.initialize()

                # Register with Lithos on every new connection (FR-MCP-8).
                await session.call_tool(
                    "lithos_agent_register", self._AGENT_REGISTER_ARGS
                )
                logger.info(
                    "Registered agent with Lithos (id=%s)",
                    self._AGENT_REGISTER_ARGS["id"],
                )

                self._exit_stack = stack
                self._session = session
                logger.info("Lithos SSE connection established to %s", self._url)
                return session
            except Exception:
                await stack.aclose()
                raise

    async def reconnect(self) -> None:
        """Drop the current SSE connection and re-establish it.

        On the new connection ``lithos_agent_register`` is called again
        automatically (AC-05-G reconnect re-register).
        """
        await self.close()
        await self._ensure_connected()

    async def cache_lookup(
        self, *, query: str | None, source_url: str | None
    ) -> mcp_types.CallToolResult:
        """Look up a note in the Lithos cache (FR-MCP-3, AC-05-A).

        Both *query* and *source_url* are required — the chokepoint
        raises ``LithosError("missing_lookup_arg")`` BEFORE any RPC
        when either argument is ``None`` or an empty string.
        """
        if not query:
            raise LithosError(
                "missing_lookup_arg",
                operation="cache_lookup",
                detail="query is required",
            )
        if not source_url:
            raise LithosError(
                "missing_lookup_arg",
                operation="cache_lookup",
                detail="source_url is required",
            )
        return await self.call_tool(
            "lithos_cache_lookup",
            {"query": query, "source_url": source_url},
        )

    async def write_note(
        self,
        *,
        title: str,
        content: str,
        agent: str = "influx",
        path: str,
        source_url: str,
        tags: list[str],
        confidence: float,
        note_type: str = "summary",
        namespace: str = "influx",
        expires_at: str | None = None,
    ) -> WriteResult:
        """Write a note to Lithos with envelope handling (FR-MCP-6/7).

        Handles ``duplicate`` (treated as hit, no retry) and
        ``invalid_input`` (logged + skipped, no exception) envelopes.
        Returns a :class:`WriteResult` so callers can inspect the
        outcome and increment counters (e.g. ``dedup_skipped``).
        """
        args: dict[str, Any] = {
            "title": title,
            "content": content,
            "agent": agent,
            "path": path,
            "source_url": source_url,
            "tags": tags,
            "confidence": confidence,
            "note_type": note_type,
            "namespace": namespace,
        }
        if expires_at is not None:
            args["expires_at"] = expires_at
        result = await self.call_tool("lithos_write", args)
        return self._parse_write_response(result, source_url=source_url)

    def _parse_write_response(
        self,
        result: mcp_types.CallToolResult,
        *,
        source_url: str,
    ) -> WriteResult:
        """Parse a ``lithos_write`` response and handle envelopes."""
        text = result.content[0].text  # type: ignore[union-attr]
        body = json.loads(text)
        status = body.get("status", "")

        if status == "duplicate":
            return WriteResult(
                status="duplicate", source_url=source_url
            )

        if status == "invalid_input":
            reason = body.get("reason", "unknown")
            logger.warning(
                "lithos_write invalid_input for %s: %s",
                source_url,
                reason,
            )
            return WriteResult(
                status="invalid_input",
                source_url=source_url,
                detail=reason,
            )

        return WriteResult(status=status, source_url=source_url)

    async def list_notes(
        self,
        *,
        tags: list[str],
        limit: int | None = None,
    ) -> mcp_types.CallToolResult:
        """List notes by tag filter (FR-MCP-5).

        Invokes the underlying MCP ``lithos_list`` tool with the provided
        *tags* and optional *limit*.  The server response is returned
        unchanged so callers can inspect titles/IDs directly.
        """
        args: dict[str, Any] = {"tags": tags}
        if limit is not None:
            args["limit"] = limit
        return await self.call_tool("lithos_list", args)

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mcp_types.CallToolResult:
        """Call a Lithos MCP tool, lazily connecting on first use."""
        session = await self._ensure_connected()
        return await session.call_tool(name, arguments)

    async def close(self) -> None:
        """Close the SSE connection if open."""
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._session = None
            self._exit_stack = None
            logger.info("Lithos SSE connection closed")

    @property
    def connected(self) -> bool:
        """Whether the client currently has an active connection."""
        return self._session is not None
