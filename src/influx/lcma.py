"""LCMA helpers for Lithos integration (PRD 08).

Provides deterministic query composition and arXiv-ID extraction
helpers used by the LCMA post-write flow.
"""

from __future__ import annotations

import re

__all__ = ["compose_retrieve_query", "extract_arxiv_ref"]

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
