"""LCMA helpers for Lithos integration (PRD 08).

Provides deterministic query composition, arXiv-ID extraction,
and the post-write retrieve + edge wiring used by the LCMA flow.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from influx.telemetry import current_run_id, get_tracer

if TYPE_CHECKING:
    from influx.lithos_client import LithosClient

__all__ = [
    "after_write",
    "compose_retrieve_query",
    "extract_arxiv_ref",
    "resolve_builds_on",
]

logger = logging.getLogger(__name__)

_MAX_QUERY_LEN = 500
_MAX_CONTRIBUTIONS = 3
_WHITESPACE_RE = re.compile(r"\s+")
_ARXIV_RE = re.compile(r"arXiv:(\d{4}\.\d{4,5}(?:v\d+)?)")


def _escape_phrase_inner(s: str) -> str:
    """Backslash-escape ``\\`` and ``"`` for inclusion inside a phrase query."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _phrase_quote(s: str) -> str:
    """Wrap *s* as a Tantivy phrase query, backslash-escaping ``\\`` and ``"``.

    Phrase quoting neutralises Tantivy reserved characters inside the
    payload (notably ``:`` from ``Topic: Subtitle`` titles, plus ``(``,
    ``)``, ``'``), so the parser cannot mistake content for field
    qualifiers or syntax (#80).
    """
    return f'"{_escape_phrase_inner(s)}"'


def _fit_phrase(content: str, budget: int) -> str:
    """Return a phrase-quoted form of *content* with total length ``<= budget``.

    Truncates from the tail of the *escaped* form so the result is
    always syntactically balanced — both surrounding quotes survive,
    and a trailing odd-count run of backslashes (which would cut a
    ``\\\\`` or ``\\"`` escape in half) is shaved off.

    Returns ``'""'`` (empty phrase) when *budget* < the escaped length
    of the content but the budget still leaves room for the two
    surrounding quotes.
    """
    if budget < 2:
        return ""
    escaped = _escape_phrase_inner(content)
    inner_budget = budget - 2
    if len(escaped) <= inner_budget:
        return f'"{escaped}"'

    truncated = escaped[:inner_budget]
    # Avoid leaving a dangling escape: every backslash in the escaped
    # form is the leading half of either ``\\`` or ``\"``. An odd
    # trailing run means we truncated mid-escape — drop one to restore
    # a valid escaped string.
    trailing = 0
    i = len(truncated) - 1
    while i >= 0 and truncated[i] == "\\":
        trailing += 1
        i -= 1
    if trailing % 2 == 1:
        truncated = truncated[:-1]
    return f'"{truncated}"'


def compose_retrieve_query(
    title: str,
    contributions: list[str] | None = None,
) -> str:
    """Compose a deterministic ``lithos_retrieve`` query string (FR-LCMA-2).

    1. Start with *title*.
    2. Take the first up to 3 *contributions* in original list order,
       trim each, and skip any that are empty after trimming. Empty
       elements within those first three are dropped, NOT replaced by
       later non-empty entries (FR-LCMA-2 step 2).
    3. Collapse internal whitespace runs to a single space within each
       disjunct.
    4. Wrap each disjunct as a Tantivy phrase (double-quoted, with
       ``\\`` and ``"`` escaped) so reserved characters in titles like
       ``Topic: Subtitle`` cannot be parsed as field qualifiers (#80).
    5. Join the wrapped parts with ``" | "``.
    6. Bound the final string at 500 characters while preserving
       Tantivy syntactic validity: the title phrase is truncated as a
       phrase if needed, and any contribution whose wrapped form would
       overflow the remaining budget is dropped (no mid-phrase slicing
       — see #80 review).
    """
    parts: list[str] = [_WHITESPACE_RE.sub(" ", title).strip()]

    if contributions is not None:
        for c in contributions[:_MAX_CONTRIBUTIONS]:
            stripped = _WHITESPACE_RE.sub(" ", c).strip()
            if not stripped:
                continue
            parts.append(stripped)

    title_phrase = _fit_phrase(parts[0], _MAX_QUERY_LEN)
    pieces = [title_phrase]
    used = len(title_phrase)
    separator = " | "
    for content in parts[1:]:
        wrapped = _phrase_quote(content)
        if used + len(separator) + len(wrapped) > _MAX_QUERY_LEN:
            break
        pieces.append(wrapped)
        used += len(separator) + len(wrapped)
    return separator.join(pieces)


def extract_arxiv_ref(item: str) -> tuple[str, str] | None:
    """Extract ``(prior_title, arxiv_id)`` from a Tier 3 ``builds_on`` item.

    Recognises the ``arXiv:<id>`` form embedded in parentheses or freestanding.

    Returns ``None`` when no recognisable arXiv ID is present.

    AC-08-C: ``"FooNet (arXiv:2412.12345)"`` → ``("FooNet", "2412.12345")``.
    AC-08-D: ``"arXiv:2412.12345"`` → ``("2412.12345", "2412.12345")``.
    """
    m = _ARXIV_RE.search(item)
    if m is None:
        return None

    arxiv_id = m.group(1)

    # Extract the prior_title as the text before the arXiv reference,
    # stripping any trailing parentheses wrapper and whitespace.
    prefix = item[: m.start()].rstrip()
    # Remove trailing opening paren if present (e.g. "FooNet (" → "FooNet")
    if prefix.endswith("("):
        prefix = prefix[:-1].rstrip()

    prior_title = prefix if prefix else arxiv_id
    return (prior_title, arxiv_id)


# ── Post-write LCMA hook (FR-LCMA-2, FR-LCMA-3) ─────────────────


async def after_write(
    *,
    client: LithosClient,
    title: str,
    contributions: list[str] | None = None,
    run_task_id: str,
    profile: str,
    lcma_edge_score: float,
    source_note_id: str = "",
    source_url: str = "",
) -> list[dict[str, Any]]:
    """Retrieve related Lithos memory and upsert ``related_to`` edges.

    Called after every successful canonical note write (FR-LCMA-2,
    FR-LCMA-3, AC-M2-5, AC-M2-6).

    *source_note_id* is the id of the note that was just written; it is
    passed verbatim as ``source_note_id`` to each ``edge_upsert`` so a
    real graph edge can be created (per finding 1).  Each retrieve
    result contributes its own ``note_id`` as ``target_note_id``.

    Returns a list of ``{"title": str, "score": float}`` dicts for
    high-scoring results so the webhook digest can populate
    ``related_in_lithos`` (FR-NOT-6, AC-08-F).
    """
    query = compose_retrieve_query(title, contributions)
    tracer = get_tracer()
    with tracer.span(
        "influx.lithos.retrieve",
        attributes={
            "influx.profile": profile,
            "influx.run_id": current_run_id.get() or "",
        },
    ):
        result = await client.retrieve(
            query=query,
            limit=5,
            agent_id="influx",
            task_id=run_task_id,
            tags=[f"profile:{profile}"],
        )

    body = json.loads(result.content[0].text)  # type: ignore[union-attr]
    results: list[dict[str, Any]] = body.get("results", [])
    logger.info(
        "LCMA retrieve completed profile=%s run_id=%s source_url=%s "
        "note_id=%s tool=%s results=%d",
        profile,
        current_run_id.get() or "",
        source_url,
        source_note_id,
        "lithos_retrieve",
        len(results),
    )

    related: list[dict[str, Any]] = []
    for r in results:
        score = float(r.get("score", 0.0))
        if score < lcma_edge_score:
            continue

        receipt_id = r.get("receipt_id", "")
        target_note_id = r.get("note_id", "")
        await client.edge_upsert(
            type="related_to",
            evidence={
                "kind": "lithos_retrieve",
                "score": score,
                "receipt_id": receipt_id,
            },
            source_note_id=source_note_id,
            target_note_id=target_note_id,
        )
        logger.info(
            "LCMA related_to edge upserted profile=%s run_id=%s "
            "source_url=%s note_id=%s tool=%s target_note_id=%s score=%.3f",
            profile,
            current_run_id.get() or "",
            source_url,
            source_note_id,
            "lithos_edge_upsert",
            target_note_id,
            score,
        )
        related.append({"title": r.get("title", ""), "score": score})

    if not related:
        logger.info(
            "LCMA retrieve produced no related matches above threshold "
            "profile=%s run_id=%s source_url=%s note_id=%s threshold=%.3f",
            profile,
            current_run_id.get() or "",
            source_url,
            source_note_id,
            lcma_edge_score,
        )

    return related


# ── Tier 3 builds_on resolver (FR-LCMA-4, AC-M2-7/8) ────────────


async def resolve_builds_on(
    *,
    client: LithosClient,
    builds_on: list[str] | None = None,
    source_note_id: str = "",
    profile: str = "",
    written_source_url: str = "",
) -> None:
    """Resolve Tier 3 ``builds_on`` items via ``lithos_cache_lookup``.

    For each item with a recognisable arXiv ID, calls
    ``lithos_cache_lookup(query=prior_title, source_url=…)`` and upserts
    a ``builds_on`` edge only on an exact ``source_url`` match
    (FR-LCMA-4, AC-M2-7).  Items without an arXiv ID or without a
    matching cache entry are silently skipped (AC-M2-8).

    *source_note_id* is the id of the note that was just written; the
    cache-lookup hit supplies the prior note's id as
    ``target_note_id``.  Both are forwarded to ``edge_upsert`` so a real
    graph edge can be created (per finding 1).
    """
    if not builds_on:
        return

    for item in builds_on:
        ref = extract_arxiv_ref(item)
        if ref is None:
            continue

        prior_title, arxiv_id = ref
        source_url = f"https://arxiv.org/abs/{arxiv_id}"

        result = await client.cache_lookup(
            query=prior_title,
            source_url=source_url,
        )

        body = json.loads(result.content[0].text)  # type: ignore[union-attr]
        if not body.get("hit"):
            logger.info(
                "LCMA builds_on lookup miss profile=%s run_id=%s "
                "source_url=%s note_id=%s tool=%s target_source_url=%s",
                profile,
                current_run_id.get() or "",
                written_source_url,
                source_note_id,
                "lithos_cache_lookup",
                source_url,
            )
            continue

        # Exact source_url match required — no fuzzy matching (AC-M2-8).
        if body.get("source_url") != source_url:
            logger.info(
                "LCMA builds_on lookup source_url mismatch profile=%s run_id=%s "
                "source_url=%s note_id=%s tool=%s expected_source_url=%s "
                "actual_source_url=%s",
                profile,
                current_run_id.get() or "",
                written_source_url,
                source_note_id,
                "lithos_cache_lookup",
                source_url,
                body.get("source_url", ""),
            )
            continue

        await client.edge_upsert(
            type="builds_on",
            evidence={"kind": "tier3_builds_on_extraction"},
            source_note_id=source_note_id,
            target_note_id=body.get("note_id", ""),
        )
        logger.info(
            "LCMA builds_on edge upserted profile=%s run_id=%s "
            "source_url=%s note_id=%s tool=%s target_note_id=%s",
            profile,
            current_run_id.get() or "",
            written_source_url,
            source_note_id,
            "lithos_edge_upsert",
            body.get("note_id", ""),
        )
