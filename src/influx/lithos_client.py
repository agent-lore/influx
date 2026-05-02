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
import re
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import urlparse

from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError

from influx.dedup import compose_dedup_query
from influx.errors import ConfigError, LCMAError, LithosError
from influx.notes import (
    NoteParseError,
    ProfileRelevanceEntry,
    merge_profile_relevance_union,
    parse_note,
    parse_profile_relevance,
)
from influx.notes import (
    _render_profile_relevance_body as _render_pr_body,
)
from influx.notes import (
    merge_tags as _canonical_merge_tags,
)

__all__ = ["LithosClient", "WriteResult"]

# Substrings that indicate the MCP server is reporting an unsupported /
# unregistered tool (vs. a runtime error inside a registered tool).
# FastMCP returns "Unknown tool: <name>"; lowlevel JSON-RPC uses
# "Method not found".  Match case-insensitively to be robust against
# minor server-side wording differences.
_UNKNOWN_TOOL_MARKERS: tuple[str, ...] = (
    "unknown tool",
    "method not found",
    "tool not found",
    "no such tool",
)


def _is_unknown_tool_message(message: str | None) -> bool:
    """Return ``True`` when *message* indicates an unsupported tool."""
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _UNKNOWN_TOOL_MARKERS)


def _first_non_empty_str(body: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value among *keys* in *body*."""
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


@dataclasses.dataclass(frozen=True)
class WriteResult:
    """Result of a ``write_note`` call after envelope handling (FR-MCP-7).

    *status*: ``"created"`` / ``"updated"`` for success, ``"duplicate"``
    for an already-ingested item (caller increments ``dedup_skipped``),
    ``"invalid_input"`` for a malformed payload (logged + skipped),
    ``"slug_collision"`` when both retries exhausted (logged + skipped),
    ``"version_conflict"`` when both retries exhausted (logged + skipped),
    ``"content_too_large_skipped"`` when content_too_large exhausted
    all trimming retries (logged + counted + skipped).

    *note_id* carries the Lithos note id from the write envelope on
    successful ``created`` / ``updated`` outcomes so the LCMA layer can
    wire it as the ``source_note_id`` on subsequent ``edge_upsert``
    calls (PRD 08 graph wiring).
    """

    status: str
    source_url: str
    detail: str = ""
    note_id: str = ""


# ── Pure helpers ────────────────────────────────────────────────────

_ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/([^\s?#]+)")
# Matcher for parsing ``existing_id=<id>`` out of the slug_collision
# diagnostic.  Lithos returns UUIDs in production (``[0-9a-f-]+``) but
# tests use friendlier ids like ``doc-dup-1``; accept anything up to a
# whitespace, semicolon, or comma terminator.
_EXISTING_ID_RE = re.compile(r"existing_id=([^\s;,]+)")


def _extract_slug_suffix(source_url: str) -> str:
    """Compute disambiguating title suffix for slug_collision retry.

    arXiv URLs get `` [arXiv <id>]``; all others get `` [<host>]``
    (FR-MCP-7, AC-05-D).
    """
    m = _ARXIV_ID_RE.search(source_url)
    if m:
        return f" [arXiv {m.group(1)}]"
    host = urlparse(source_url).hostname or urlparse(source_url).netloc
    return f" [{host}]"


def _arxiv_id_from_url(source_url: str) -> str | None:
    """Return the arxiv id from a URL like ``https://arxiv.org/abs/2604.28197``."""
    m = _ARXIV_ID_RE.search(source_url)
    return m.group(1) if m else None


def _existing_id_from_detail(detail: str) -> str | None:
    """Parse ``existing_id=<uuid>`` out of a slug_collision detail string (#30)."""
    if not detail:
        return None
    m = _EXISTING_ID_RE.search(detail)
    return m.group(1) if m else None


def _doc_tags(doc: dict[str, Any]) -> list[str]:
    """Extract the tag list from a ``lithos_read`` response.

    Tags live under ``metadata`` in the canonical envelope but some
    code paths (and the diagnose-script preview) read them at the top
    level — be tolerant of both shapes.
    """
    direct = doc.get("tags")
    if isinstance(direct, list):
        return [str(t) for t in direct]
    meta = doc.get("metadata")
    if isinstance(meta, dict):
        nested = meta.get("tags")
        if isinstance(nested, list):
            return [str(t) for t in nested]
    return []


def _doc_source_url(doc: dict[str, Any]) -> str | None:
    """Extract source_url from a ``lithos_read`` response (top-level or metadata)."""
    direct = doc.get("source_url")
    if isinstance(direct, str) and direct:
        return direct
    meta = doc.get("metadata")
    if isinstance(meta, dict):
        nested = meta.get("source_url")
        if isinstance(nested, str) and nested:
            return nested
    return None


@dataclasses.dataclass(frozen=True)
class SquatterClassification:
    """Outcome of inspecting the doc that owns a colliding slug (#31)."""

    kind: str  # "duplicate" | "reclaimable" | "distinct"
    squatter_id: str
    reason: str  # human-readable explanation, surfaced in detail / logs


def _classify_squatter(
    doc: dict[str, Any],
    *,
    squatter_id: str,
    incoming_source_url: str,
) -> SquatterClassification:
    """Classify a slug-squatting Lithos doc against an incoming write (#31).

    Returns one of three outcomes:

    * ``"duplicate"`` — the squatter already represents the same paper
      as the incoming write (matching ``arxiv-id:<id>`` tag, or
      matching ``source_url``).  Lithos's URL/cache dedup should have
      caught this; surfacing it here recovers from that miss.
    * ``"reclaimable"`` — the squatter is an empty residue from an
      aborted prior write (no tags AND no source_url AND no body).
      Safe to delete and retry the original write.
    * ``"distinct"`` — the squatter is a real, distinct doc that
      happens to slugify the same.  The caller should fall back to
      the suffix-retry path; if THAT also collides, the entry goes
      to the unresolved-collisions backlog.

    This function is pure: I/O lives in :meth:`LithosClient._retry_slug_collision`.
    """
    tags = _doc_tags(doc)
    sq_source_url = _doc_source_url(doc)
    body = str(doc.get("content") or "").strip()

    incoming_arxiv_id = _arxiv_id_from_url(incoming_source_url)

    # Match #1: explicit arxiv-id tag equality.
    if incoming_arxiv_id:
        for tag in tags:
            if tag == f"arxiv-id:{incoming_arxiv_id}":
                return SquatterClassification(
                    kind="duplicate",
                    squatter_id=squatter_id,
                    reason=(
                        f"squatter carries arxiv-id:{incoming_arxiv_id} — "
                        "treat as duplicate of the same paper"
                    ),
                )

    # Match #2: source_url equality.
    if sq_source_url and sq_source_url == incoming_source_url:
        return SquatterClassification(
            kind="duplicate",
            squatter_id=squatter_id,
            reason=(
                f"squatter source_url matches incoming ({sq_source_url}) — "
                "treat as duplicate of the same paper"
            ),
        )

    # Reclaim path: empty residue.  Conservative: ALL of the following
    # must hold so we never delete a real note that just shares a slug.
    if not tags and not sq_source_url and not body:
        return SquatterClassification(
            kind="reclaimable",
            squatter_id=squatter_id,
            reason=(
                "squatter has no tags, no source_url, and empty body — "
                "stale residue from an aborted prior write"
            ),
        )

    # Genuinely-distinct paper that happens to slugify the same.
    return SquatterClassification(
        kind="distinct",
        squatter_id=squatter_id,
        reason=(
            f"squatter has its own metadata "
            f"(tags={len(tags)}, source_url={sq_source_url!r}, body_len={len(body)})"
        ),
    )


def _merge_tags(existing_tags: list[str], new_tags: list[str]) -> list[str]:
    """Merge tags using the canonical PRD 04 contract (FR-NOTE-5/6/7/8).

    Delegates to :func:`influx.notes.merge_tags` so that Influx-owned
    tags are fully replaced, ``profile:*`` tags are union-merged with
    the rejection guard, and external tags are preserved verbatim.
    """
    return _canonical_merge_tags(existing_tags=existing_tags, new_tags=new_tags)


_USER_NOTES_MARKER = "## User Notes"


def _preserve_user_notes(existing_content: str, new_content: str) -> str:
    """Merge content, preserving ``## User Notes`` from the existing note.

    The ``## User Notes`` section and everything beneath it in
    *existing_content* replaces any ``## User Notes`` already present
    in *new_content* (AC-05-E).
    """
    idx = existing_content.find(_USER_NOTES_MARKER)
    if idx == -1:
        return new_content
    user_notes_block = existing_content[idx:]

    new_idx = new_content.find(_USER_NOTES_MARKER)
    base = new_content[:new_idx].rstrip() if new_idx != -1 else new_content.rstrip()
    return base + "\n\n" + user_notes_block


_PROFILE_RELEVANCE_MARKER = "## Profile Relevance"


def _merge_profile_relevance_in_content(
    existing_content: str,
    new_content: str,
    merged_tags: list[str],
) -> str:
    """Merge ``## Profile Relevance`` sections from two note contents.

    Parses Profile Relevance entries from both *existing_content* and
    *new_content*, union-merges them (preserving old entries for profiles
    not in the new set), and replaces the ``## Profile Relevance``
    section in *new_content* with the merged result.

    Falls back to *new_content* unchanged when either note cannot be
    parsed (e.g. non-canonical format).
    """
    try:
        existing_parsed = parse_note(existing_content)
        new_parsed = parse_note(new_content)
    except NoteParseError:
        return new_content

    old_entries = parse_profile_relevance(existing_parsed)
    new_entries = parse_profile_relevance(new_parsed)

    if not old_entries:
        return new_content  # Nothing to merge from existing

    merged_entries = merge_profile_relevance_union(
        old_entries=old_entries,
        new_entries=new_entries,
        tags=merged_tags,
    )

    # Replace the ## Profile Relevance section in new_content
    return _replace_profile_relevance_section(new_content, merged_entries)


def _replace_profile_relevance_section(
    content: str,
    entries: list[ProfileRelevanceEntry],
) -> str:
    """Replace the ``## Profile Relevance`` section body in *content*."""
    pr_idx = content.find(_PROFILE_RELEVANCE_MARKER)
    if pr_idx == -1:
        return content

    # Find the end of the Profile Relevance section: the next ## heading
    after_heading = pr_idx + len(_PROFILE_RELEVANCE_MARKER)
    next_h2 = content.find("\n## ", after_heading)

    pr_body = _render_pr_body(entries)
    marker = _PROFILE_RELEVANCE_MARKER
    replacement = f"{marker}\n{pr_body}\n" if pr_body else f"{marker}\n"

    if next_h2 != -1:
        # Replace up to but not including the next ## heading's newline
        return content[:pr_idx] + replacement + "\n" + content[next_h2 + 1 :]
    else:
        # Profile Relevance is the last section — replace to end
        return content[:pr_idx] + replacement


_TIER2_MARKER = "## Full Text"

# Tier 3 section headings (master PRD §7.3).
_TIER3_MARKERS = (
    "## Claims",
    "## Datasets & Benchmarks",
    "## Builds On",
    "## Open Questions",
)


def _drop_tier2(content: str) -> str:
    """Remove the ``## Full Text`` (Tier 2) section from *content*.

    Keeps Tier 1 and Tier 3 sections intact (master PRD §9.7 step 1).
    The Tier 2 section spans from ``## Full Text`` to the next ``##``
    heading (exclusive) or the ``## User Notes`` marker or end-of-string.
    """
    idx = content.find(_TIER2_MARKER)
    if idx == -1:
        return content
    before = content[:idx].rstrip()
    # Find the next ## heading after Tier 2.
    rest = content[idx + len(_TIER2_MARKER) :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    if next_heading is not None:
        after = rest[next_heading.start() :]
        return (before + "\n\n" + after).rstrip()
    return before


def _drop_tier2_and_tier3(content: str) -> str:
    """Remove Tier 2 (``## Full Text``) AND Tier 3 sections from *content*.

    Keeps only Tier 1 sections + ``## User Notes`` (master PRD §9.7
    repair path).  Tier 3 headings: ``## Claims``,
    ``## Datasets & Benchmarks``, ``## Builds On``, ``## Open Questions``.
    """
    # First drop Tier 2.
    result = _drop_tier2(content)
    # Then drop each Tier 3 section.
    for marker in _TIER3_MARKERS:
        idx = result.find(marker)
        if idx == -1:
            continue
        before = result[:idx].rstrip()
        rest = result[idx + len(marker) :]
        next_heading = re.search(r"^## ", rest, re.MULTILINE)
        if next_heading is not None:
            after = rest[next_heading.start() :]
            result = (before + "\n\n" + after).rstrip()
        else:
            result = before
    return result


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

    async def cache_lookup_for_item(
        self,
        *,
        title: str,
        source_url: str | None,
        abstract_or_summary: str | None = None,
    ) -> mcp_types.CallToolResult:
        """Compose dedup query + cache lookup for an arXiv/RSS item.

        Single source-agnostic chokepoint that ensures the
        ``title + first_sentence(abstract_or_summary)`` rule from
        FR-MCP-3 / AC-05-B is always applied identically across arXiv
        and RSS callers.  Raises ``LithosError("missing_lookup_arg")``
        before any RPC when *title* or *source_url* is missing.
        """
        if not title:
            raise LithosError(
                "missing_lookup_arg",
                operation="cache_lookup",
                detail="title is required",
            )
        query = compose_dedup_query(title, abstract_or_summary)
        return await self.cache_lookup(query=query, source_url=source_url)

    async def read_note(self, *, note_id: str) -> dict[str, Any]:
        """Read a note by ID (used for version_conflict re-reads)."""
        result = await self.call_tool("lithos_read", {"id": note_id})
        text = result.content[0].text  # type: ignore[union-attr]
        return json.loads(text)

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

        Handles ``duplicate`` (treated as hit, no retry),
        ``invalid_input`` (logged + skipped, no exception),
        ``slug_collision`` (retry once with disambiguating title suffix,
        AC-05-D), and ``version_conflict`` (re-read + tag-merge +
        user-notes preservation + retry once, AC-05-E).
        Returns a :class:`WriteResult` so callers can inspect the
        outcome and increment counters (e.g. ``dedup_skipped``).
        """
        args: dict[str, Any] = {
            "title": title,
            "content": content,
            "agent": agent,
            "path": path,
            "source_url": source_url,
            "tags": list(tags),
            "confidence": confidence,
            "note_type": note_type,
            "namespace": namespace,
        }
        if expires_at is not None:
            args["expires_at"] = expires_at
        result = await self.call_tool("lithos_write", args)
        parsed = self._parse_write_response(result, source_url=source_url)

        if parsed.status == "slug_collision":
            return await self._retry_slug_collision(
                args, source_url=source_url, initial_collision=parsed
            )

        if parsed.status == "version_conflict":
            return await self._retry_version_conflict(
                args,
                note_id=parsed.detail,
                source_url=source_url,
                original_tags=tags,
            )

        if parsed.status == "content_too_large":
            return await self._retry_content_too_large(
                args,
                source_url=source_url,
                original_tags=tags,
            )

        return parsed

    # ── Slug-collision retry (AC-05-D) ──────────────────────────────

    async def _retry_slug_collision(
        self,
        args: dict[str, Any],
        *,
        source_url: str,
        initial_collision: WriteResult,
    ) -> WriteResult:
        """Recover from slug_collision by inspecting the squatter (#31).

        Strategy (replaces the original AC-05-D ""one suffix retry"" form):

        1. Read the squatter via the ``existing_id`` lithos returned in
           the initial collision envelope.
        2. Classify (:func:`_classify_squatter`):

           * ``duplicate`` — squatter shares the incoming write's
             ``arxiv-id`` or ``source_url``.  Return as ``duplicate``.
           * ``reclaimable`` — squatter is empty residue (no tags, no
             source_url, no body).  Delete it then re-issue the original
             write.
           * ``distinct`` — squatter is a real, different doc that
             happens to slugify the same.  Try the AC-05-D suffix once.
             If THAT also collides, recurse the inspection (in case the
             suffixed slug is itself residue) — once.

        Anything still ``slug_collision`` after the recovery chain is
        returned to the caller, which (in the scheduler) appends to the
        unresolved-collisions backlog file.
        """
        from influx import metrics

        # Round 1: inspect the squatter named in the initial collision.
        recovered = await self._try_recover_collision(
            args,
            source_url=source_url,
            collision=initial_collision,
            metrics_module=metrics,
        )

        if recovered.status != "slug_collision":
            return recovered

        # Round 2: the suffix retry collided too.  One more inspection in
        # case the suffixed-slug squatter is itself a reclaimable residue.
        suffix = _extract_slug_suffix(source_url)
        suffixed_args = {**args, "title": args["title"] + suffix}
        recovered = await self._try_recover_collision(
            suffixed_args,
            source_url=source_url,
            collision=recovered,
            metrics_module=metrics,
            allow_suffix_retry=False,
        )

        if recovered.status == "slug_collision":
            logger.warning(
                "lithos_write slug_collision unresolved after recovery for %s",
                source_url,
            )
        return recovered

    async def _try_recover_collision(
        self,
        args: dict[str, Any],
        *,
        source_url: str,
        collision: WriteResult,
        metrics_module: Any,
        allow_suffix_retry: bool = True,
    ) -> WriteResult:
        """Single recovery round.  Returns the ``WriteResult`` to surface.

        When ``allow_suffix_retry=True`` (round 1) and the squatter is
        ``distinct``, this method issues the AC-05-D suffix retry and
        returns its result (which may itself be a ``slug_collision``
        for the outer to handle in round 2).
        """
        squatter_id = _existing_id_from_detail(collision.detail)
        if not squatter_id:
            # No squatter id to inspect (e.g. older lithos response shape).
            # Fall through to the conservative AC-05-D suffix retry.
            if allow_suffix_retry:
                return await self._suffix_retry(args, source_url=source_url)
            return collision

        try:
            doc = await self.read_note(note_id=squatter_id)
        except Exception:  # noqa: BLE001 — read failure shouldn't crash the write loop
            logger.warning(
                "slug_collision squatter inspection failed for %s id=%s; "
                "falling back to suffix retry",
                source_url,
                squatter_id,
            )
            if allow_suffix_retry:
                return await self._suffix_retry(args, source_url=source_url)
            return collision

        classification = _classify_squatter(
            doc, squatter_id=squatter_id, incoming_source_url=source_url
        )

        if classification.kind == "duplicate":
            metrics_module.slug_collision_dedup_recovery().add(1)
            logger.info(
                "slug_collision recovered as duplicate for %s: %s",
                source_url,
                classification.reason,
            )
            return WriteResult(
                status="duplicate",
                source_url=source_url,
                detail=f"recovered: {classification.reason}",
            )

        if classification.kind == "reclaimable":
            metrics_module.slug_collision_reclaimed().add(1)
            logger.warning(
                "slug_collision reclaimed empty squatter for %s id=%s: %s",
                source_url,
                squatter_id,
                classification.reason,
            )
            await self.call_tool(
                "lithos_delete", {"id": squatter_id, "agent": "influx"}
            )
            # Re-issue the original write — the slug is now free.
            result = await self.call_tool("lithos_write", args)
            return self._parse_write_response(result, source_url=source_url)

        # 'distinct' — fall back to the AC-05-D suffix retry.
        if not allow_suffix_retry:
            return collision
        return await self._suffix_retry(args, source_url=source_url)

    async def _suffix_retry(
        self, args: dict[str, Any], *, source_url: str
    ) -> WriteResult:
        """Single AC-05-D suffix retry for the genuinely-distinct case."""
        suffix = _extract_slug_suffix(source_url)
        retry_args = {**args, "title": args["title"] + suffix}
        result = await self.call_tool("lithos_write", retry_args)
        return self._parse_write_response(result, source_url=source_url)

    # ── Version-conflict retry (AC-05-E) ────────────────────────────

    async def _retry_version_conflict(
        self,
        args: dict[str, Any],
        *,
        note_id: str,
        source_url: str,
        original_tags: list[str],
    ) -> WriteResult:
        """Re-read, merge tags + notes + Profile Relevance (AC-05-E)."""
        existing = await self.read_note(note_id=note_id)
        existing_tags: list[str] = existing.get("tags", [])
        merged_tags = _merge_tags(existing_tags, original_tags)
        existing_content: str = existing.get("content", "")
        merged_content = _preserve_user_notes(existing_content, args["content"])
        # Multi-profile merge: union-merge Profile Relevance entries (FR-NOTE-6)
        merged_content = _merge_profile_relevance_in_content(
            existing_content, merged_content, merged_tags
        )
        retry_args = {
            **args,
            "tags": merged_tags,
            "content": merged_content,
        }
        version = existing.get("version")
        if version is not None:
            retry_args["expected_version"] = version
        if note_id:
            retry_args["id"] = note_id

        result = await self.call_tool("lithos_write", retry_args)
        parsed = self._parse_write_response(result, source_url=source_url)
        if parsed.status == "version_conflict":
            logger.warning(
                "lithos_write version_conflict retry failed for %s",
                source_url,
            )
        return parsed

    # ── Content-too-large retry (§9.7) ──────────────────────────────

    async def _check_existing_note(self, source_url: str) -> dict[str, Any] | None:
        """Check whether an Influx-authored note exists for *source_url*.

        Uses ``lithos_cache_lookup`` with the source URL.  Returns the
        existing note dict if found, ``None`` otherwise.  The detection
        mechanism is a cache lookup by ``source_url`` — implementation-
        defined per AC of US-010.
        """
        result = await self.call_tool(
            "lithos_cache_lookup",
            {"query": source_url, "source_url": source_url},
        )
        text = result.content[0].text  # type: ignore[union-attr]
        body = json.loads(text)
        if body.get("hit"):
            return body
        return None

    async def _retry_content_too_large(
        self,
        args: dict[str, Any],
        *,
        source_url: str,
        original_tags: list[str],
    ) -> WriteResult:
        """Handle ``content_too_large`` per master PRD §9.7.

        Step 1: drop Tier 2 (``## Full Text``), keep Tier 1 + Tier 3,
        retry once.

        Step 2 (on second ``content_too_large``):
        - **Create path** (no existing note): skip + log + count.
        - **Repair path** (existing note): handled by US-011.
        """
        # Step 1: drop Tier 2 and retry.
        trimmed = _drop_tier2(args["content"])
        retry_args = {**args, "content": trimmed}
        result = await self.call_tool("lithos_write", retry_args)
        parsed = self._parse_write_response(result, source_url=source_url)
        if parsed.status != "content_too_large":
            return parsed

        # Step 2: second content_too_large — branch on create vs repair.
        existing = await self._check_existing_note(source_url)
        if existing is None:
            # Create path: skip, no degraded placeholder (AC-05-F).
            logger.warning(
                "lithos_write content_too_large (create path) for %s — skipping item",
                source_url,
            )
            return WriteResult(
                status="content_too_large_skipped",
                source_url=source_url,
                detail="create_path",
            )

        # Repair path: Tier-1-only retry (US-011).
        return await self._retry_content_too_large_repair(
            args,
            source_url=source_url,
            existing=existing,
            original_tags=original_tags,
        )

    async def _retry_content_too_large_repair(
        self,
        args: dict[str, Any],
        *,
        source_url: str,
        existing: dict[str, Any],
        original_tags: list[str],
    ) -> WriteResult:
        """Repair-path Tier-1-only retry (US-011, master PRD §9.7).

        Drops Tier 2 AND Tier 3, tags ``influx:repair-needed``, retries
        once.  If that also fails: leave existing note untouched, count +
        log, no abort, no ``updated_at`` advance.
        """
        tier1_content = _drop_tier2_and_tier3(args["content"])
        existing_tags: list[str] = existing.get("tags", [])
        merged_tags = _merge_tags(
            existing_tags, [*original_tags, "influx:repair-needed"]
        )
        repair_args = {
            **args,
            "content": tier1_content,
            "tags": merged_tags,
        }
        result = await self.call_tool("lithos_write", repair_args)
        parsed = self._parse_write_response(result, source_url=source_url)
        if parsed.status == "content_too_large":
            # Tier 1 alone too large — leave existing note untouched.
            logger.warning(
                "lithos_write content_too_large (repair path, "
                "Tier-1-only) for %s — leaving existing note "
                "untouched",
                source_url,
            )
            return WriteResult(
                status="content_too_large_skipped",
                source_url=source_url,
                detail="repair_path_tier1_failed",
            )
        return parsed

    # ── Response parsing ────────────────────────────────────────────

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
            return WriteResult(status="duplicate", source_url=source_url)

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

        if status == "slug_collision":
            # Lithos's slug_collision envelope carries ``existing_id`` and
            # ``message`` (lithos/server.py); preserve them as ``detail`` so
            # the operator-facing WARNING in scheduler.py can name the
            # squatting note rather than logging an empty string.  See the
            # 2026-05-02 staging incident: the only signal of the colliding
            # doc was thrown away here.
            existing_id = body.get("existing_id", "")
            message = body.get("message", "")
            if existing_id and message:
                detail = f"existing_id={existing_id}; {message}"
            elif existing_id:
                detail = f"existing_id={existing_id}"
            else:
                detail = message
            return WriteResult(
                status="slug_collision",
                source_url=source_url,
                detail=detail,
            )

        if status == "version_conflict":
            note_id = body.get("note_id", "")
            return WriteResult(
                status="version_conflict",
                source_url=source_url,
                detail=note_id,
            )

        if status == "content_too_large":
            return WriteResult(
                status="content_too_large",
                source_url=source_url,
            )

        if status in ("created", "updated"):
            # Success — ``note_id`` is plumbed through so LCMA can use it
            # as the ``source_note_id`` on subsequent ``edge_upsert`` calls.
            return WriteResult(
                status=status,
                source_url=source_url,
                note_id=body.get("note_id", ""),
            )

        # Undocumented / unexpected envelope (e.g. ``status="error"``).
        # Surface whatever diagnostic the server returned so the failure
        # is root-causable from logs alone — see staging incident
        # 2026-04-30 where a bare ``status=error`` left no breadcrumb.
        detail = _first_non_empty_str(body, ("reason", "detail", "error", "message"))
        body_excerpt = "" if detail else json.dumps(body, default=str)[:500]
        logger.warning(
            "lithos_write returned non-success status=%s for %s: %s",
            status or "<empty>",
            source_url,
            detail or body_excerpt,
            extra={
                "lithos_status": status,
                "source_url": source_url,
                "detail": detail,
                "body_excerpt": body_excerpt,
            },
        )
        return WriteResult(
            status=status,
            source_url=source_url,
            detail=detail,
            note_id=body.get("note_id", ""),
        )

    async def list_notes(
        self,
        *,
        tags: list[str],
        limit: int | None = None,
        order_by: str | None = None,
        order: str | None = None,
    ) -> mcp_types.CallToolResult:
        """List notes by tag filter (FR-MCP-5, FR-REP-1).

        Invokes the underlying MCP ``lithos_list`` tool with the provided
        *tags* and optional *limit*.  ``order_by`` and ``order`` are accepted
        for compatibility with callers, but current Lithos does not expose
        server-side ordering on ``lithos_list``; callers that need ordering
        should sort the returned items locally.  The server response is
        returned unchanged so callers can inspect titles/IDs directly.

        Parameters
        ----------
        order_by:
            Field to sort by (e.g. ``"updated_at"``).  Accepted for API
            compatibility but not forwarded to Lithos.
        order:
            Sort direction (``"asc"`` or ``"desc"``).  Accepted for API
            compatibility but not forwarded to Lithos.
        """
        del order_by, order
        args: dict[str, Any] = {"tags": tags}
        if limit is not None:
            args["limit"] = limit
        return await self.call_tool("lithos_list", args)

    async def list_archive_terminal_arxiv_ids(
        self,
        *,
        profile: str,
    ) -> frozenset[str]:
        """Return the arxiv-ids of notes tagged ``influx:archive-terminal``
        for *profile* (issue #14).

        Used by the inspector to short-circuit ``download_archive`` for
        papers whose archive is known to be permanently unfetchable
        (e.g. >100 MB PDFs that already accumulated the cap of counted
        download failures during the repair sweep).  Returns an empty
        frozenset when Lithos is unreachable or returns no items so the
        run continues at worst as today.
        """
        try:
            result = await self.list_notes(
                tags=["influx:archive-terminal", f"profile:{profile}"],
            )
        except (LithosError, McpError):
            logger.warning(
                "list_archive_terminal_arxiv_ids: lithos_list failed for "
                "profile %r; assuming empty terminal set",
                profile,
                exc_info=True,
            )
            return frozenset()

        if getattr(result, "isError", False) is True:
            logger.warning(
                "list_archive_terminal_arxiv_ids: lithos_list returned "
                "isError=True for profile %r; assuming empty terminal set",
                profile,
            )
            return frozenset()

        try:
            text = result.content[0].text  # type: ignore[union-attr]
            body = json.loads(text)
        except (AttributeError, IndexError, json.JSONDecodeError, KeyError):
            logger.warning(
                "list_archive_terminal_arxiv_ids: malformed lithos_list "
                "response for profile %r; assuming empty terminal set",
                profile,
                exc_info=True,
            )
            return frozenset()

        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return frozenset()

        ids: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            tags = item.get("tags")
            if not isinstance(tags, list):
                continue
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("arxiv-id:"):
                    ids.add(tag[len("arxiv-id:") :])
                    break
        return frozenset(ids)

    # ── LCMA wrappers (PRD 08) ──────────────────────────────────────

    async def _call_lcma_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp_types.CallToolResult:
        """Call an LCMA tool, translating unknown-tool failures.

        Translates *only* genuine unsupported-tool failures into
        ``LCMAError("unknown_tool", stage=name)`` (FR-LCMA-6).  Other
        MCP failures — invalid params, internal tool exceptions, output
        validation errors — are surfaced as
        ``LCMAError("call_failed", stage=name, detail=…)`` so callers
        can distinguish deployment misconfiguration from ordinary
        per-call failures and so the US-007 abort/degraded-readiness
        path is reserved for the former.

        Both error variants carry ``stage=name`` so operators can see
        which LCMA tool failed.
        """
        try:
            result = await self.call_tool(name, arguments)
        except McpError as exc:
            err = getattr(exc, "error", None)
            code = getattr(err, "code", None)
            message = getattr(err, "message", None) or str(exc)
            if code == mcp_types.METHOD_NOT_FOUND or _is_unknown_tool_message(message):
                raise LCMAError("unknown_tool", stage=name, detail=message) from exc
            raise LCMAError("call_failed", stage=name, detail=message) from exc

        if result.isError:
            text = ""
            try:
                text = result.content[0].text  # type: ignore[union-attr]
            except (IndexError, AttributeError):
                text = ""
            if _is_unknown_tool_message(text):
                raise LCMAError("unknown_tool", stage=name, detail=text)
            raise LCMAError("call_failed", stage=name, detail=text)
        return result

    async def retrieve(
        self,
        *,
        query: str,
        limit: int,
        agent_id: str,
        task_id: str,
        tags: list[str],
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_retrieve`` (FR-LCMA-2)."""
        return await self._call_lcma_tool(
            "lithos_retrieve",
            {
                "query": query,
                "limit": limit,
                "agent_id": agent_id,
                "task_id": task_id,
                "tags": tags,
            },
        )

    async def edge_upsert(
        self,
        *,
        type: str,
        evidence: dict[str, Any],
        source_note_id: str = "",
        target_note_id: str = "",
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_edge_upsert`` (FR-LCMA-3)."""
        args: dict[str, Any] = {
            "type": type,
            "evidence": evidence,
        }
        if source_note_id:
            args["source_note_id"] = source_note_id
        if target_note_id:
            args["target_note_id"] = target_note_id
        return await self._call_lcma_tool("lithos_edge_upsert", args)

    async def task_create(
        self,
        *,
        title: str,
        agent: str,
        tags: list[str],
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_task_create`` (FR-LCMA-5)."""
        return await self._call_lcma_tool(
            "lithos_task_create",
            {"title": title, "agent": agent, "tags": tags},
        )

    async def task_complete(
        self,
        *,
        task_id: str,
        agent: str,
        outcome: str | None = None,
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_task_complete`` (FR-LCMA-5)."""
        args: dict[str, Any] = {"task_id": task_id, "agent": agent}
        if outcome is not None:
            args["outcome"] = outcome
        return await self._call_lcma_tool("lithos_task_complete", args)

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
