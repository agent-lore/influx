"""LCMA helpers for Lithos integration (PRD 08).

Provides deterministic query composition, arXiv-ID extraction,
and the post-write retrieve + edge wiring used by the LCMA flow.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

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
    3. Join the surviving parts with ``" | "``.
    4. Collapse internal whitespace runs to a single space.
    5. Truncate to 500 characters (simple slice, no word re-wrap).
    """
    parts: list[str] = [title]

    if contributions is not None:
        for c in contributions[:_MAX_CONTRIBUTIONS]:
            stripped = c.strip()
            if not stripped:
                continue
            parts.append(stripped)

    composed = " | ".join(parts)
    composed = _WHITESPACE_RE.sub(" ", composed)
    return composed[:_MAX_QUERY_LEN]


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
) -> list[dict[str, Any]]:
    """Retrieve related Lithos memory and upsert ``related_to`` edges.

    Called after every successful canonical note write (FR-LCMA-2,
    FR-LCMA-3, AC-M2-5, AC-M2-6).

    Returns a list of ``{"title": str, "score": float}`` dicts for
    high-scoring results so the webhook digest can populate
    ``related_in_lithos`` (FR-NOT-6, AC-08-F).
    """
    query = compose_retrieve_query(title, contributions)
    result = await client.retrieve(
        query=query,
        limit=5,
        agent_id="influx",
        task_id=run_task_id,
        tags=[f"profile:{profile}"],
    )

    body = json.loads(result.content[0].text)  # type: ignore[union-attr]
    results: list[dict[str, Any]] = body.get("results", [])

    related: list[dict[str, Any]] = []
    for r in results:
        score = float(r.get("score", 0.0))
        if score < lcma_edge_score:
            continue

        receipt_id = r.get("receipt_id", "")
        await client.edge_upsert(
            type="related_to",
            evidence={
                "kind": "lithos_retrieve",
                "score": score,
                "receipt_id": receipt_id,
            },
        )
        related.append({"title": r.get("title", ""), "score": score})

    return related


# ── Tier 3 builds_on resolver (FR-LCMA-4, AC-M2-7/8) ────────────


async def resolve_builds_on(
    *,
    client: LithosClient,
    builds_on: list[str] | None = None,
) -> None:
    """Resolve Tier 3 ``builds_on`` items via ``lithos_cache_lookup``.

    For each item with a recognisable arXiv ID, calls
    ``lithos_cache_lookup(query=prior_title, source_url=…)`` and upserts
    a ``builds_on`` edge only on an exact ``source_url`` match
    (FR-LCMA-4, AC-M2-7).  Items without an arXiv ID or without a
    matching cache entry are silently skipped (AC-M2-8).
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
            continue

        # Exact source_url match required — no fuzzy matching (AC-M2-8).
        if body.get("source_url") != source_url:
            continue

        await client.edge_upsert(
            type="builds_on",
            evidence={"kind": "tier3_builds_on_extraction"},
        )
