"""LCMA helpers for Lithos integration (PRD 08).

Provides deterministic query composition and arXiv-ID extraction
helpers used by the LCMA post-write flow.
"""

from __future__ import annotations

import re

__all__ = ["compose_retrieve_query"]

_MAX_QUERY_LEN = 500
_MAX_CONTRIBUTIONS = 3
_WHITESPACE_RE = re.compile(r"\s+")


def compose_retrieve_query(
    title: str,
    contributions: list[str] | None = None,
) -> str:
    """Compose a deterministic ``lithos_retrieve`` query string (FR-LCMA-2).

    1. Start with *title*.
    2. Append up to 3 non-empty (after ``.strip()``) *contributions*
       in original list order, joined with ``" | "``.
    3. Collapse internal whitespace runs to a single space.
    4. Truncate to 500 characters (simple slice, no word re-wrap).
    """
    parts: list[str] = [title]

    if contributions is not None:
        count = 0
        for c in contributions:
            stripped = c.strip()
            if not stripped:
                continue
            parts.append(stripped)
            count += 1
            if count >= _MAX_CONTRIBUTIONS:
                break

    composed = " | ".join(parts)
    composed = _WHITESPACE_RE.sub(" ", composed)
    return composed[:_MAX_QUERY_LEN]
