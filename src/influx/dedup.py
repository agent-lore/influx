"""Dedup helpers: lookup-query composition and same-source identity.

Composes the source-agnostic ``query`` string for
``lithos_cache_lookup`` from *title* and an optional *abstract* or
*summary*.  The first-sentence extraction is shared across arXiv and
RSS sources so dedup behaviour is identical (AC-05-B).

Also decides whether an existing Lithos document is the *same source*
as an incoming URL (:func:`same_source_reason`); slug-collision
recovery uses it to classify squatters (#31, #148).
:func:`verify_cache_hit` keeps only the ``lithos_cache_lookup`` hits
Lithos found in its URL index (Lithos task e4300784).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from influx.urls import safe_normalise_url

__all__ = [
    "arxiv_id_from_url",
    "cache_hit_document",
    "compose_dedup_query",
    "first_sentence",
    "same_source_reason",
    "verify_cache_hit",
]

_ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/([^\s?#]+)")


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


def arxiv_id_from_url(source_url: str) -> str | None:
    """Return the arxiv id from a URL like ``https://arxiv.org/abs/2604.28197``."""
    m = _ARXIV_ID_RE.search(source_url)
    return m.group(1) if m else None


def same_source_reason(
    *,
    doc_tags: Sequence[str],
    doc_source_url: str | None,
    incoming_source_url: str,
) -> str | None:
    """Say why an existing doc is the same source as *incoming_source_url*.

    Returns a short reason, or ``None`` when the doc is a different
    source.  Stable-identity matches (#148):

    * the doc carries the incoming URL's ``arxiv-id:<id>`` tag;
    * the doc's ``source_url`` names the same arxiv id (catches docs
      whose tagset was truncated by an earlier merge);
    * exact ``source_url`` equality;
    * canonical-URL equality via :func:`influx.urls.normalise_url`
      (scheme/host case, default ports, tracking params, trailing
      slash).
    """
    incoming_arxiv_id = arxiv_id_from_url(incoming_source_url)
    if incoming_arxiv_id:
        if f"arxiv-id:{incoming_arxiv_id}" in doc_tags:
            return f"carries arxiv-id:{incoming_arxiv_id}"
        if doc_source_url and arxiv_id_from_url(doc_source_url) == incoming_arxiv_id:
            return f"source_url names arxiv-id:{incoming_arxiv_id}"

    if not doc_source_url:
        return None
    if doc_source_url == incoming_source_url:
        return f"source_url matches incoming ({doc_source_url})"
    doc_canonical = safe_normalise_url(doc_source_url)
    incoming_canonical = safe_normalise_url(incoming_source_url)
    if doc_canonical and incoming_canonical and doc_canonical == incoming_canonical:
        return (
            f"source_url is canonical match for incoming "
            f"({doc_source_url} ≡ {incoming_source_url})"
        )
    return None


def verify_cache_hit(body: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a ``lithos_cache_lookup`` hit only if Lithos matched it by URL.

    Influx always passes ``source_url``, which Lithos answers from its
    URL index alone and reports as ``match: "source_url"`` (lithos-core
    d0392561).  Any other hit is not proof the URL is stored: a Lithos
    older than that sends no ``match`` and fell back to a threshold-0.0
    semantic search that reported the nearest unrelated note as a hit
    (e4300784).  Such a hit comes back as a miss (``hit: False``,
    ``document: None``) with the document's id, ``source_url`` and title
    and the ``match`` under ``ignored_neighbour`` so callers can log it.
    Misses and URL hits are returned as copies, unchanged.
    """
    if not body.get("hit") or body.get("match") == "source_url":
        return dict(body)
    raw_doc = body.get("document")
    doc: Mapping[str, Any] = raw_doc if isinstance(raw_doc, Mapping) else {}
    return {
        **body,
        "hit": False,
        "document": None,
        "ignored_neighbour": {
            "id": doc.get("id"),
            "source_url": doc.get("source_url"),
            "title": doc.get("title"),
            "match": body.get("match"),
        },
    }


def cache_hit_document(body: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the hit's ``document`` (``id``, ``source_url``, ``tags``, ...).

    ``None`` on a miss.  Lithos nests the note fields under ``document``;
    nothing is read from the top level of the body, where Lithos never
    puts them.
    """
    if not body.get("hit"):
        return None
    doc = body.get("document")
    return dict(doc) if isinstance(doc, Mapping) else None
