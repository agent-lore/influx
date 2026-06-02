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
    parse_note,
    parse_profile_relevance,
)
from influx.notes import (
    merge_tags as _canonical_merge_tags,
)
from influx.renderer import (
    ProfileRelevanceEntry,
    merge_profile_relevance_union,
)
from influx.renderer import (
    _render_profile_relevance_body as _render_pr_body,
)
from influx.urls import normalise_url

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

    arXiv URLs get `` [arXiv <id>]``; inbox local-PDF synthetic URLs
    (``inbox-pdf:sha256:…``, which have no host) get `` [inbox-pdf]``;
    all others get `` [<host>]`` (FR-MCP-7, AC-05-D; inbox §13.3).
    """
    m = _ARXIV_ID_RE.search(source_url)
    if m:
        return f" [arXiv {m.group(1)}]"
    if source_url.startswith("inbox-pdf:"):
        return " [inbox-pdf]"
    host = urlparse(source_url).hostname or urlparse(source_url).netloc
    return f" [{host}]"


def _arxiv_id_from_url(source_url: str) -> str | None:
    """Return the arxiv id from a URL like ``https://arxiv.org/abs/2604.28197``."""
    m = _ARXIV_ID_RE.search(source_url)
    return m.group(1) if m else None


def _existing_id_from_detail(detail: str) -> str | None:
    """Parse ``existing_id=<uuid>`` out of a slug_collision detail string (#30).

    Tolerates both the original single-attempt shape
    ``existing_id=<id>; ...`` and the issue-#32 enriched form
    ``first_existing_id=<A>; ...; retry_existing_id=<B>; ...`` —
    in the latter case the retry id wins because it is the squatter
    that ultimately blocked the write.
    """
    if not detail:
        return None
    # Prefer the retry id when the issue-#32 enriched form is present.
    retry = re.search(r"retry_existing_id=([^\s;,]+)", detail)
    if retry:
        return retry.group(1)
    m = _EXISTING_ID_RE.search(detail)
    return m.group(1) if m else None


def _format_unresolved_detail(
    *,
    first_existing_id: str | None,
    first_slug: str,
    retry_existing_id: str | None,
    retry_slug: str,
    retry_detail: str,
) -> str:
    """Build the issue-#32 detail string enumerating BOTH squatters.

    Format::

        first_existing_id=<A>; first_slug='<unsuffixed>';
        retry_existing_id=<B>; retry_slug='<suffixed>'; <retry message>

    The retry envelope's human-readable ``message`` (everything after
    ``existing_id=...; `` in *retry_detail*) is appended verbatim so the
    operator-facing WARNING still carries Lithos's own diagnostic.
    Either id may be missing on older Lithos response shapes — emit
    ``<missing>`` rather than dropping the field so downstream parsers
    see a stable schema.
    """
    parts: list[str] = [
        f"first_existing_id={first_existing_id or '<missing>'}",
        f"first_slug='{first_slug}'",
        f"retry_existing_id={retry_existing_id or '<missing>'}",
        f"retry_slug='{retry_slug}'",
    ]
    # Strip the leading ``existing_id=<id>; `` from retry_detail so the
    # tail is just Lithos's message, avoiding a duplicated existing_id.
    tail = retry_detail
    if tail.startswith("existing_id="):
        sep = tail.find("; ")
        tail = tail[sep + 2 :] if sep != -1 else ""
    if tail:
        parts.append(tail)
    return "; ".join(parts)


def _doc_tags(doc: dict[str, Any]) -> list[str]:
    """Extract the tag list from a ``lithos_read`` response.

    Tags live under ``metadata`` in the canonical envelope but some
    code paths (and the diagnose-script preview) read them at the top
    level — be tolerant of both shapes.

    Note: docs obtained via :meth:`LithosClient.read_note` are already
    flattened by :func:`_normalise_read_envelope` (#187), so for those the
    top-level branch hits first.  The ``metadata`` fallback here still
    guards any direct caller that passes a raw, un-normalised envelope.
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


# Structured fields that Lithos returns nested under ``metadata`` in a
# ``lithos_read`` response but that Influx reads at the top level — most
# importantly the repair sweep (``influx.repair``), which builds its
# rewrite args from ``note.get("source_url")`` / ``note.get("path")`` /
# ``note.get("tags")``.  Issue #187: when these stayed nested, each sweep
# pass rewrote a repair-needed note with empty source_url / path / tags,
# progressively stripping it into an ``influx:source-invalid`` zombie.
#
# Each entry maps the field to its expected JSON type — a schema-violating
# nested value (e.g. ``metadata.tags`` as a string) is NOT hoisted, mirroring
# the ``isinstance`` guards in ``_doc_tags`` / ``_doc_source_url`` so callers
# that apply ``list(...)`` to a hoisted value can never iterate a stray
# string into character-garbage tags.
_READ_ENVELOPE_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "tags": list,
    "source_url": str,
    "path": str,
    "confidence": (int, float),
    "note_type": str,
    "namespace": str,
    "version": int,
    "author": str,
}


def _normalise_read_envelope(doc: dict[str, Any]) -> dict[str, Any]:
    """Hoist nested ``metadata.*`` fields to the top level of a read doc (#187).

    For each allow-listed field, copy ``metadata[field]`` to the top
    level **only when the top-level value is missing or empty AND the
    nested value has the expected type** — a present, non-empty top-level
    value always wins, so this never clobbers real data and is a no-op on
    already-flat docs.  "Empty" means ``None`` / ``""`` / ``[]`` only, so a
    legitimate ``confidence: 0.0`` or ``version: 0`` is preserved, while an
    empty ``tags: []`` or ``source_url: ""`` is correctly replaced from
    ``metadata``.  List values are copied so the hoisted top-level field
    does not alias ``metadata[field]``.  Mutates and returns *doc* for
    caller convenience.
    """
    meta = doc.get("metadata")
    if not isinstance(meta, dict):
        return doc
    for field, expected in _READ_ENVELOPE_FIELD_TYPES.items():
        if doc.get(field) not in (None, "", []):
            continue  # a present, non-empty top-level value always wins
        value = meta.get(field)
        if field not in meta or not isinstance(value, expected):
            continue  # absent, or schema-violating nested type — skip
        # Copy lists so the hoisted value doesn't alias metadata[field].
        doc[field] = list(value) if isinstance(value, list) else value
    return doc


@dataclasses.dataclass(frozen=True)
class SquatterClassification:
    """Outcome of inspecting the doc that owns a colliding slug (#31)."""

    kind: str  # "duplicate" | "reclaimable" | "distinct"
    squatter_id: str
    reason: str  # human-readable explanation, surfaced in detail / logs


def _safe_normalise_url(url: str) -> str:
    """Return :func:`normalise_url` output, falling back to *url* on error.

    The classifier is on the conflict-recovery hot path; a malformed URL
    must not crash the write loop.  Empty input returns ``""``.
    """
    if not url:
        return ""
    try:
        return normalise_url(url)
    except Exception:  # noqa: BLE001 — defensive: never crash recovery on bad URLs
        return url


def _classify_squatter(
    doc: dict[str, Any],
    *,
    squatter_id: str,
    incoming_source_url: str,
) -> SquatterClassification:
    """Classify a slug-squatting Lithos doc against an incoming write (#31).

    Returns one of three outcomes:

    * ``"duplicate"`` — the squatter already represents the same paper
      as the incoming write (matching ``arxiv-id:<id>`` tag, matching
      ``source_url``, or canonical-URL equivalence).  Lithos's URL/cache
      dedup should have caught this; surfacing it here recovers from
      that miss.
    * ``"reclaimable"`` — the squatter is an empty residue from an
      aborted prior write (no tags AND no source_url AND no body).
      Safe to delete and retry the original write.
    * ``"distinct"`` — the squatter is a real, distinct doc that
      happens to slugify the same.  The caller should fall back to
      the suffix-retry path; if THAT also collides, the entry goes
      to the unresolved-collisions backlog.

    Stable-identity matches (#148) now include:

    * arxiv-id tag equality (existing);
    * exact ``source_url`` equality (existing);
    * canonical-URL equality via :func:`influx.urls.normalise_url`
      (handles scheme case, default ports, tracking params, trailing
      slashes) — catches the staging cases where two writes for the
      same paper differ only in URL normalisation;
    * arxiv id extracted from the squatter's ``source_url`` when no
      explicit ``arxiv-id:`` tag is present — catches squatters whose
      tagset was truncated by an earlier merge but whose ``source_url``
      still names the same paper.

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

    # Match #1b: arxiv-id extracted from the squatter's source_url matches the
    # incoming arxiv-id.  Squatters whose tagset was truncated by an earlier
    # merge can still be identified by URL alone.
    if incoming_arxiv_id and sq_source_url:
        sq_arxiv_id = _arxiv_id_from_url(sq_source_url)
        if sq_arxiv_id == incoming_arxiv_id:
            return SquatterClassification(
                kind="duplicate",
                squatter_id=squatter_id,
                reason=(
                    f"squatter source_url names arxiv-id:{incoming_arxiv_id} — "
                    "treat as duplicate of the same paper"
                ),
            )

    # Match #2: source_url equality (exact or canonical).
    if sq_source_url:
        if sq_source_url == incoming_source_url:
            return SquatterClassification(
                kind="duplicate",
                squatter_id=squatter_id,
                reason=(
                    f"squatter source_url matches incoming ({sq_source_url}) — "
                    "treat as duplicate of the same paper"
                ),
            )
        # Canonical-URL equality: handles scheme case, default ports,
        # tracking params, trailing slashes.  Same paper, different URL
        # shape — Lithos's URL dedup missed it because the stored URL
        # was a slightly different rendering of the same logical link.
        sq_canonical = _safe_normalise_url(sq_source_url)
        incoming_canonical = _safe_normalise_url(incoming_source_url)
        if sq_canonical and incoming_canonical and sq_canonical == incoming_canonical:
            return SquatterClassification(
                kind="duplicate",
                squatter_id=squatter_id,
                reason=(
                    f"squatter source_url is canonical match for incoming "
                    f"({sq_source_url} ≡ {incoming_source_url}) — "
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

    def _result_text(
        self,
        result: mcp_types.CallToolResult,
        *,
        operation: str,
    ) -> str:
        """Extract the first text payload from a tool result."""
        try:
            text = result.content[0].text  # type: ignore[union-attr]
        except (AttributeError, IndexError) as exc:
            raise LithosError(
                "malformed_tool_response",
                operation=operation,
                detail="missing text content",
            ) from exc
        if not isinstance(text, str):
            raise LithosError(
                "malformed_tool_response",
                operation=operation,
                detail="non-string text content",
            )
        return text

    def _result_json_dict(
        self,
        result: mcp_types.CallToolResult,
        *,
        operation: str,
    ) -> dict[str, Any]:
        """Decode a tool result into a JSON object with consistent errors."""
        text = self._result_text(result, operation=operation)
        if getattr(result, "isError", False) is True:
            raise LithosError(
                f"{operation} failed",
                operation=operation,
                detail=text,
            )
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LithosError(
                "malformed_tool_response",
                operation=operation,
                detail="invalid JSON payload",
            ) from exc
        if not isinstance(body, dict):
            raise LithosError(
                "malformed_tool_response",
                operation=operation,
                detail="JSON payload was not an object",
            )
        return body

    async def cache_lookup_body(
        self, *, query: str | None, source_url: str | None
    ) -> dict[str, Any]:
        """Run ``cache_lookup`` and decode the JSON body."""
        return self._result_json_dict(
            await self.cache_lookup(query=query, source_url=source_url),
            operation="cache_lookup",
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

    async def cache_lookup_by_url_body(
        self,
        *,
        source_url: str,
    ) -> dict[str, Any]:
        """Source-URL-only cache lookup (issue #128).

        Sends ``query=source_url`` so the Lithos server matches on
        the exact URL.  Used as a defensive fallback after the primary
        title-based dedup query misses, and as the canonical chokepoint
        for any caller that needs to ask "do you have a note for this
        exact source_url?" (e.g. ``content_too_large`` recovery).

        Raises ``LithosError("missing_lookup_arg")`` before any RPC
        when *source_url* is empty.
        """
        if not source_url:
            raise LithosError(
                "missing_lookup_arg",
                operation="cache_lookup",
                detail="source_url is required",
            )
        return await self.cache_lookup_body(query=source_url, source_url=source_url)

    async def cache_lookup_for_item_body(
        self,
        *,
        title: str,
        source_url: str | None,
        abstract_or_summary: str | None = None,
    ) -> dict[str, Any]:
        """Run ``cache_lookup_for_item`` and decode the JSON body."""
        return self._result_json_dict(
            await self.cache_lookup_for_item(
                title=title,
                source_url=source_url,
                abstract_or_summary=abstract_or_summary,
            ),
            operation="cache_lookup",
        )

    async def read_note(self, *, note_id: str) -> dict[str, Any]:
        """Read a note by ID (used for version_conflict re-reads).

        The returned dict is envelope-normalised (#187): structured
        fields Lithos nests under ``metadata`` are hoisted to the top
        level so downstream top-level ``note.get(...)`` reads — notably
        the repair sweep's rewrite-arg construction — see the correct
        values instead of empties.
        """
        # SINGLE NORMALISATION CHOKEPOINT for the lithos_read tool (#187/#190).
        # This is the only place the raw "lithos_read" tool may be invoked: it
        # is the one call site that runs _normalise_read_envelope, which hoists
        # the metadata-nested fields Lithos returns. Reading the tool anywhere
        # else bypasses normalisation and reintroduces #187 (notes stripped into
        # influx:source-invalid zombies). Always read via read_note(); never
        # invoke the lithos_read tool directly elsewhere. Enforced by
        # tests/unit/test_lithos_read_chokepoint.py.
        result = await self.call_tool("lithos_read", {"id": note_id})
        return _normalise_read_envelope(
            self._result_json_dict(result, operation="read_note")
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

        Handles ``duplicate`` (treated as hit, no retry),
        ``invalid_input`` (logged + skipped, no exception),
        ``slug_collision`` (retry once with disambiguating title suffix,
        AC-05-D), and ``version_conflict`` (re-read + tag-merge +
        user-notes preservation + retry once, AC-05-E).
        Returns a :class:`WriteResult` so callers can inspect the
        outcome and increment counters (e.g. ``dedup_skipped``).

        Strict-mode contract (#178): ``content`` must be body-only
        markdown — no leading ``---``-fenced YAML frontmatter block.
        Lithos owns the outer frontmatter and persists ``tags`` /
        ``source_url`` / ``confidence`` / ``note_type`` / ``namespace``
        from the API parameters on this call (spec §5.1).  A
        ``LithosError`` is raised if ``content`` starts with ``---\\n``
        so a renderer regression that re-introduces the embedded
        frontmatter shape fails loudly at the boundary instead of
        silently doubling the on-disk state.

        Canonical-URL normalization (FR-MCP-4): ``source_url`` is
        normalised via :func:`influx.urls.normalise_url` before being
        forwarded to ``lithos_write``.  Pre-#178 this happened inside
        the rendered frontmatter; after the renderer change, the
        ``write_note`` boundary owns it so callers don't have to
        pre-normalise.  Lithos's internal ``normalize_url`` would
        canonicalise for dedup-map storage anyway, but normalising
        here keeps Influx's own logs / telemetry / ``WriteResult``
        in canonical form and stops dedupe identity from drifting
        for any caller that passes a tracking-param-laden URL.
        """
        if content.startswith("---\n") or content.startswith("---\r\n"):
            raise LithosError(
                "content begins with a '---' frontmatter fence — "
                "the lithos_write contract requires body-only content; "
                "tags / source_url / confidence / note_type / namespace "
                "must be passed as API parameters, not inlined in content "
                "(spec §5.1, issue #178)",
                operation="write_note",
                detail="embedded_frontmatter",
            )
        # FR-MCP-4: canonicalise source_url at the API boundary.  See
        # ``_safe_normalise_url`` — never crash the write loop on a
        # malformed URL, fall through with the raw value so the write
        # at least attempts (Lithos will reject it as invalid_input
        # if it really is unusable).
        canonical_source_url = _safe_normalise_url(source_url)
        args: dict[str, Any] = {
            "title": title,
            "content": content,
            "agent": agent,
            "path": path,
            "source_url": canonical_source_url,
            "tags": list(tags),
            "confidence": confidence,
            "note_type": note_type,
            "namespace": namespace,
        }
        if expires_at is not None:
            args["expires_at"] = expires_at
        result = await self.call_tool("lithos_write", args)
        parsed = self._parse_write_response(result, source_url=canonical_source_url)

        if parsed.status == "slug_collision":
            return await self._retry_slug_collision(
                args, source_url=canonical_source_url, initial_collision=parsed
            )

        if parsed.status == "version_conflict":
            return await self._retry_version_conflict(
                args,
                note_id=parsed.detail,
                source_url=canonical_source_url,
                original_tags=tags,
            )

        if parsed.status == "content_too_large":
            return await self._retry_content_too_large(
                args,
                source_url=canonical_source_url,
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
        """Recover from slug_collision by stable-identity then squatter shape.

        Strategy (issue #148 builds on the #31 squatter-shape dispatch):

        0. **Stable-identity pre-check** (#148): before any squatter
           inspection, run a source-URL-keyed ``lithos_cache_lookup``.
           A hit means Lithos already has a note for this exact source
           URL — slug collision was incidental and the right answer is
           ``duplicate``.  This converts the "Lithos found the conflict
           only at write time" noise into a clean dedupe outcome and
           keeps the ingestion path idempotent for repeated runs.
        1. Read the squatter via the ``existing_id`` lithos returned in
           the initial collision envelope.
        2. Classify (:func:`_classify_squatter`):

           * ``duplicate`` — squatter shares the incoming write's
             ``arxiv-id``, ``source_url``, or canonical URL.  Return as
             ``duplicate``.
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

        first_title = args["title"]

        # Round 0 (#148): stable-identity pre-check.  If Lithos already
        # has a note for this source URL, the slug collision is just
        # noise around an existing duplicate — fold it into the dedup
        # outcome rather than fighting through suffix retries.
        url_recovery = await self._url_identity_recovery(
            source_url=source_url, metrics_module=metrics
        )
        if url_recovery is not None:
            return url_recovery

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
        retry_title = first_title + suffix
        suffixed_args = {**args, "title": retry_title}
        recovered = await self._try_recover_collision(
            suffixed_args,
            source_url=source_url,
            collision=recovered,
            metrics_module=metrics,
            allow_suffix_retry=False,
        )

        if recovered.status == "slug_collision":
            # Issue #32: surface BOTH colliding squatters in the final
            # detail so the operator-facing WARNING / unresolved-collision
            # backlog enumerates the first AND the retry squatter — not
            # just the retry's id.  An operator can then clean both with
            # one command.  The per-attempt envelope details are otherwise
            # lost because each retry overwrites the previous WriteResult.
            recovered = dataclasses.replace(
                recovered,
                detail=_format_unresolved_detail(
                    first_existing_id=_existing_id_from_detail(
                        initial_collision.detail
                    ),
                    first_slug=first_title,
                    retry_existing_id=_existing_id_from_detail(recovered.detail),
                    retry_slug=retry_title,
                    retry_detail=recovered.detail,
                ),
            )
            logger.warning(
                "lithos_write slug_collision unresolved after recovery for %s: %s",
                source_url,
                recovered.detail,
            )
        return recovered

    async def _url_identity_recovery(
        self,
        *,
        source_url: str,
        metrics_module: Any,
    ) -> WriteResult | None:
        """Stable-identity pre-check for slug_collision recovery (#148).

        Issues a ``source_url``-keyed ``lithos_cache_lookup``.  When the
        lookup hits, Lithos already has a note for this exact URL — the
        slug collision is incidental, the correct outcome is
        ``duplicate``.  Returns ``None`` when the lookup misses (caller
        falls through to the existing squatter-shape dispatch) or when
        *source_url* is empty.

        Lookup failures (network, malformed response) return ``None`` so
        the recovery chain still runs — this pre-check is purely a
        latency/noise optimisation, never a correctness gate.
        """
        if not source_url:
            return None
        try:
            body = await self.cache_lookup_by_url_body(source_url=source_url)
        except (LithosError, McpError):
            # Defensive: never crash the write loop because the
            # pre-check failed.  Fall through to squatter inspection.
            logger.warning(
                "slug_collision url-identity pre-check failed for %s; "
                "falling back to squatter inspection",
                source_url,
                exc_info=True,
            )
            return None
        except Exception:  # noqa: BLE001 — see comment above
            logger.warning(
                "slug_collision url-identity pre-check raised unexpectedly "
                "for %s; falling back to squatter inspection",
                source_url,
                exc_info=True,
            )
            return None

        if not body.get("hit"):
            return None

        existing_id = ""
        if isinstance(body, dict):
            for key in ("id", "note_id", "existing_id"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    existing_id = value
                    break

        metrics_module.slug_collision_url_recovery().add(1)
        logger.info(
            "slug_collision recovered via source_url identity for %s "
            "(existing_id=%s) — Lithos already had this URL",
            source_url,
            existing_id or "<unknown>",
        )
        return WriteResult(
            status="duplicate",
            source_url=source_url,
            detail=(
                f"recovered: source_url already in lithos "
                f"(existing_id={existing_id or '<unknown>'})"
            ),
            note_id=existing_id,
        )

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

        Thin wrapper over :meth:`cache_lookup_by_url_body`: returns the
        decoded body when ``hit`` is true, ``None`` otherwise.  The
        detection mechanism is a cache lookup by ``source_url`` —
        implementation-defined per AC of US-010.
        """
        body = await self.cache_lookup_by_url_body(source_url=source_url)
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

    async def list_notes_body(
        self,
        *,
        tags: list[str],
        limit: int | None = None,
        order_by: str | None = None,
        order: str | None = None,
    ) -> dict[str, Any]:
        """Run ``list_notes`` and decode the JSON body."""
        return self._result_json_dict(
            await self.list_notes(
                tags=tags,
                limit=limit,
                order_by=order_by,
                order=order,
            ),
            operation="list_notes",
        )

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
            body = await self.list_notes_body(
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

    async def retrieve_body(
        self,
        *,
        query: str,
        limit: int,
        agent_id: str,
        task_id: str,
        tags: list[str],
    ) -> dict[str, Any]:
        """Run ``retrieve`` and decode the JSON body."""
        return self._result_json_dict(
            await self.retrieve(
                query=query,
                limit=limit,
                agent_id=agent_id,
                task_id=task_id,
                tags=tags,
            ),
            operation="retrieve",
        )

    async def edge_upsert(
        self,
        *,
        from_id: str,
        to_id: str,
        type: str,
        weight: float,
        namespace: str = "influx",
        provenance_actor: str | None = None,
        provenance_type: str | None = None,
        evidence: dict[str, Any] | list[Any] | None = None,
        conflict_state: str | None = None,
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_edge_upsert`` (FR-LCMA-3)."""
        args: dict[str, Any] = {
            "from_id": from_id,
            "to_id": to_id,
            "type": type,
            "weight": weight,
            "namespace": namespace,
        }
        if provenance_actor is not None:
            args["provenance_actor"] = provenance_actor
        if provenance_type is not None:
            args["provenance_type"] = provenance_type
        if evidence is not None:
            args["evidence"] = evidence
        if conflict_state is not None:
            args["conflict_state"] = conflict_state
        return await self._call_lcma_tool("lithos_edge_upsert", args)

    async def task_create(
        self,
        *,
        title: str,
        agent: str,
        tags: list[str],
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_task_create`` (FR-LCMA-5).

        ``description`` + ``metadata`` (inbox submission, ``docs/plans/inbox.md``
        §3) are omitted from the RPC when ``None`` so existing per-Run task
        callers are unaffected.
        """
        args: dict[str, Any] = {"title": title, "agent": agent, "tags": tags}
        if description is not None:
            args["description"] = description
        if metadata is not None:
            args["metadata"] = metadata
        return await self._call_lcma_tool("lithos_task_create", args)

    async def task_create_body(
        self,
        *,
        title: str,
        agent: str,
        tags: list[str],
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run ``task_create`` and decode the JSON body."""
        return self._result_json_dict(
            await self.task_create(
                title=title,
                agent=agent,
                tags=tags,
                description=description,
                metadata=metadata,
            ),
            operation="task_create",
        )

    async def task_complete(
        self,
        *,
        task_id: str,
        agent: str,
        outcome: str | None = None,
        cited_nodes: list[str] | None = None,
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_task_complete`` (FR-LCMA-5).

        ``cited_nodes`` (inbox, ``docs/plans/inbox.md`` §7.2) links the
        task to the canonical note(s) the work produced.  Omitted from the
        RPC when ``None`` so scheduled-run callers are unaffected.
        """
        args: dict[str, Any] = {"task_id": task_id, "agent": agent}
        if outcome is not None:
            args["outcome"] = outcome
        if cited_nodes is not None:
            args["cited_nodes"] = cited_nodes
        return await self._call_lcma_tool("lithos_task_complete", args)

    async def task_complete_body(
        self,
        *,
        task_id: str,
        agent: str,
        outcome: str | None = None,
        cited_nodes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run ``task_complete`` and decode the JSON body."""
        return self._result_json_dict(
            await self.task_complete(
                task_id=task_id,
                agent=agent,
                outcome=outcome,
                cited_nodes=cited_nodes,
            ),
            operation="task_complete",
        )

    async def task_list(
        self,
        *,
        tags: list[str] | None = None,
        status: str | None = None,
        agent: str | None = None,
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_task_list`` (inbox intake, ``docs/plans/inbox.md`` §4.1).

        ``limit`` is enforced by the inbox tick (max-items-per-tick) rather
        than the server — ``lithos_task_list`` has no ``limit`` parameter.
        """
        args: dict[str, Any] = {}
        if tags is not None:
            args["tags"] = tags
        if status is not None:
            args["status"] = status
        if agent is not None:
            args["agent"] = agent
        return await self._call_lcma_tool("lithos_task_list", args)

    async def task_list_body(
        self,
        *,
        tags: list[str] | None = None,
        status: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Run ``task_list`` and decode the JSON body (``{"tasks": [...]}``)."""
        return self._result_json_dict(
            await self.task_list(tags=tags, status=status, agent=agent),
            operation="task_list",
        )

    async def task_claim(
        self,
        *,
        task_id: str,
        agent: str,
        aspect: str,
        ttl_minutes: int = 60,
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_task_claim`` (inbox intake, ``docs/plans/inbox.md`` §4.1)."""
        return await self._call_lcma_tool(
            "lithos_task_claim",
            {
                "task_id": task_id,
                "aspect": aspect,
                "agent": agent,
                "ttl_minutes": ttl_minutes,
            },
        )

    async def task_claim_body(
        self,
        *,
        task_id: str,
        agent: str,
        aspect: str,
        ttl_minutes: int = 60,
    ) -> dict[str, Any]:
        """Run ``task_claim`` and decode the body (``{"success", "expires_at"}``)."""
        return self._result_json_dict(
            await self.task_claim(
                task_id=task_id, agent=agent, aspect=aspect, ttl_minutes=ttl_minutes
            ),
            operation="task_claim",
        )

    async def task_update(
        self,
        *,
        task_id: str,
        agent: str,
        metadata: dict[str, Any],
    ) -> mcp_types.CallToolResult:
        """Call ``lithos_task_update`` (inbox intake, ``docs/plans/inbox.md`` §7.3).

        Attaches the structured ``inbox_result`` payload before completion.
        ``metadata`` is applied as an additive per-key merge by the server.
        """
        return await self._call_lcma_tool(
            "lithos_task_update",
            {"task_id": task_id, "agent": agent, "metadata": metadata},
        )

    async def task_update_body(
        self,
        *,
        task_id: str,
        agent: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Run ``task_update`` and decode the JSON body (``{"success", "message"}``)."""
        return self._result_json_dict(
            await self.task_update(task_id=task_id, agent=agent, metadata=metadata),
            operation="task_update",
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mcp_types.CallToolResult:
        """Call a Lithos MCP tool, lazily connecting on first use."""
        session = await self._ensure_connected()
        return await session.call_tool(name, arguments)

    async def list_tools(self) -> list[str]:
        """List the names of every tool the connected Lithos exposes.

        Used by the probe loop to assert the LCMA tool surface is
        present at deployment time (issue #69) — replacing the legacy
        per-call ``LCMAError("unknown_tool")`` latch with a probe-time
        gate.  Returns the tool names as an ordered list (MCP's
        ``tools/list`` ordering preserved).
        """
        session = await self._ensure_connected()
        result = await session.list_tools()
        return [tool.name for tool in result.tools]

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
