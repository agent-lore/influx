"""Dedup-query composition helpers (FR-MCP-3, AC-05-B).

Composes the source-agnostic ``query`` string for
``lithos_cache_lookup`` from *title* and an optional *abstract* or
*summary*.  The first-sentence extraction is shared across arXiv and
RSS sources so dedup behaviour is identical (AC-05-B).
"""

from __future__ import annotations

import re

__all__ = ["compose_dedup_query", "first_sentence"]


# Sentence-ending punctuation preceded by at least two lowercase ASCII
# letters (avoids treating abbreviations like "Mr." or "e.g." as
# sentence terminators) and followed by whitespace or end-of-string.
_SENTENCE_END_RE = re.compile(r"(?<=[a-z]{2})[.!?](?:\s|$)")

_MAX_FIRST_SENTENCE_LEN = 200


def first_sentence(text: str) -> str:
    """Extract the first sentence from *text*.

    "First sentence" is the substring up to (but not including) the
    first ``.``, ``!``, or ``?`` that acts as a sentence terminator
    (preceded by at least two lowercase ASCII letters to skip
    abbreviations like ``Mr.`` or ``e.g.``) and is followed by
    whitespace or end-of-string (FR-MCP-3).

    When no sentence terminator is found, the entire *text* is
    returned, trimmed and capped at 200 characters.
    """
    m = _SENTENCE_END_RE.search(text)
    result = text[: m.start()] if m is not None else text
    return result.strip()[:_MAX_FIRST_SENTENCE_LEN]


def compose_dedup_query(
    title: str,
    abstract_or_summary: str | None = None,
) -> str:
    """Compose the ``query`` argument for ``lithos_cache_lookup``.

    Parameters
    ----------
    title:
        The item title.  Must be non-empty.
    abstract_or_summary:
        Optional abstract (arXiv) or summary (RSS).

    Returns
    -------
    str
        ``title`` alone when *abstract_or_summary* is absent or empty;
        ``title + " " + first_sentence(abstract_or_summary)`` otherwise
        (FR-MCP-3).

    Raises
    ------
    ValueError
        When *title* is empty after stripping whitespace.
    """
    title = title.strip()
    if not title:
        raise ValueError("title must be non-empty for dedup query composition")

    if abstract_or_summary and abstract_or_summary.strip():
        return f"{title} {first_sentence(abstract_or_summary)}"
    return title
