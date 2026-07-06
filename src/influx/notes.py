"""Rewrite-merge helpers and semantic parsers for canonical Lithos notes.

The canonical note *shape* — section order, the parse ↔ serialize
round-trip, the byte-exact ``## User Notes`` region, and the string-level
section operations — is owned by :mod:`influx.canonical_note`.  This module
layers the rewrite-time semantics on top of it:

- :func:`merge_tags` / :func:`recompute_confidence` — the FR-NOTE-5/6/7/8
  tag-merge and confidence rules applied on every rewrite.
- :func:`parse_archive_path` / :func:`parse_profile_relevance` — semantic
  parsers over the already-parsed :class:`CanonicalNote` sections.

:func:`parse_note`, :class:`ParsedNote`, and :class:`ParsedSection` are
kept as thin re-exports of the :mod:`influx.canonical_note` primitives so
existing callers are undisturbed.  Rendering lives in :mod:`influx.renderer`.
"""

from __future__ import annotations

import re

from influx.canonical_note import (
    CanonicalNote,
    NoteParseError,
    ProfileRelevanceEntry,
    Section,
    parse,
)

# Thin compatibility aliases — the canonical primitives now live in
# :mod:`influx.canonical_note`.  Kept so the many ``from influx.notes
# import ...`` call sites are undisturbed.
ParsedNote = CanonicalNote
ParsedSection = Section
parse_note = parse

__all__ = [
    "ArchiveParseError",
    "NoteParseError",
    "ParsedNote",
    "ParsedSection",
    "ProfileRelevanceEntry",
    "merge_tags",
    "parse_archive_path",
    "parse_note",
    "parse_profile_relevance",
    "recompute_confidence",
]


# ── Exceptions ───────────────────────────────────────────────────────


class ArchiveParseError(NoteParseError):
    """Raised when the ``## Archive`` section body is malformed."""


# ── Tag-merging (FR-NOTE-5/6/7/8) ──────────────────────────────────

# Prefixes whose existing tags are fully replaced by new Influx tags.
_INFLUX_OWNED_PREFIXES: tuple[str, ...] = (
    "source:",
    "arxiv-id:",
    "cat:",
    "text:",
    "ingested-by:",
    "schema:",
)

# Exact tag values that are fully replaced on rewrite.
_INFLUX_OWNED_EXACT: frozenset[str] = frozenset(
    {
        "full-text",
        "influx:repair-needed",
        "influx:archive-missing",
        "influx:deep-extracted",
        "influx:text-terminal",
        # Per-stage terminal markers — set after the cap of counted-
        # toward-cap failures is reached so the sweep stops re-running
        # the same broken extraction (mirrors influx:text-terminal).
        "influx:tier2-terminal",
        "influx:tier3-terminal",
        # Set after repeated oversize (or other counted-class) archive
        # download failures — caps the archive_retry stage in select_stages
        # the same way tier{2,3}-terminal cap their respective stages.
        "influx:archive-terminal",
        # Set by the repair sweep when a note's source metadata is
        # unrecoverable (#150) — pins the bad-state notes terminal AND
        # makes them discoverable via Lithos tag search for operator
        # cleanup.  Mirrors the influx:text-terminal lifecycle.
        "influx:source-invalid",
    }
)


def _is_influx_owned(tag: str) -> bool:
    """Return True if *tag* is Influx-owned (replaced on rewrite)."""
    for prefix in _INFLUX_OWNED_PREFIXES:
        if tag.startswith(prefix):
            return True
    return tag in _INFLUX_OWNED_EXACT


def merge_tags(
    *,
    existing_tags: list[str],
    new_tags: list[str],
) -> list[str]:
    """Compute the final tag set for a note rewrite (FR-NOTE-5/6/7/8).

    Parameters
    ----------
    existing_tags:
        Tags currently on the note (from parsed frontmatter).
    new_tags:
        Newly-computed Influx-owned tags for this rewrite cycle.

    Returns
    -------
    list[str]
        The merged tag list: Influx-owned tags fully replaced by
        *new_tags*, ``profile:*`` tags union-merged (with rejection
        guard), and external tags preserved verbatim.
    """
    # Collect influx:rejected:<profile> guards from both sets
    rejected_profiles: set[str] = set()
    for tag in (*existing_tags, *new_tags):
        if tag.startswith("influx:rejected:"):
            rejected_profiles.add(tag[len("influx:rejected:") :])

    # 1. External tags: not Influx-owned and not profile:*
    external = [
        t
        for t in existing_tags
        if not _is_influx_owned(t)
        and not t.startswith("profile:")
        and not t.startswith("influx:rejected:")
    ]

    # 2. Influx-owned tags: fully replaced by new_tags
    influx_owned = [t for t in new_tags if _is_influx_owned(t)]

    # 3. profile:* union merge with rejection guard (FR-NOTE-6)
    existing_profiles = {t for t in existing_tags if t.startswith("profile:")}
    new_profiles = {t for t in new_tags if t.startswith("profile:")}
    union_profiles = existing_profiles | new_profiles
    # Remove profiles that have been rejected
    guarded_profiles = sorted(
        t for t in union_profiles if t[len("profile:") :] not in rejected_profiles
    )

    # 4. Rejection tags: preserve from both sets
    rejection_tags = sorted(
        {t for t in (*existing_tags, *new_tags) if t.startswith("influx:rejected:")}
    )

    return influx_owned + guarded_profiles + rejection_tags + external


def recompute_confidence(
    *,
    existing_confidence: float,
    current_max_score: int,
) -> float:
    """Compute the rewrite confidence value (FR-NOTE-8).

    Returns ``max(existing_confidence, current_max_score / 10.0)``.
    """
    return max(existing_confidence, current_max_score / 10.0)


# ── Archive section parser (FR-NOTE-9) ──────────────────────────────

_ARCHIVE_PATH_RE = re.compile(r"^path:\s*(.+)$")


def parse_archive_path(note: ParsedNote) -> str | None:
    """Extract the archive path from a parsed note (FR-NOTE-9).

    Parameters
    ----------
    note:
        A ``ParsedNote`` returned by :func:`parse_note`.

    Returns
    -------
    str | None
        The relative POSIX path from the ``path:`` line, or ``None``
        when the ``## Archive`` section is absent or has an empty body.

    Raises
    ------
    ArchiveParseError
        When the ``## Archive`` section contains stray text that is
        neither empty nor a single ``path:`` line (AC-04-B).
    """
    archive_section: ParsedSection | None = None
    for section in note.sections:
        if section.heading == "Archive":
            archive_section = section
            break

    if archive_section is None:
        return None

    body = archive_section.body.strip()
    if not body:
        return None

    m = _ARCHIVE_PATH_RE.match(body)
    if m is None:
        raise ArchiveParseError(
            f"Malformed ## Archive body: expected 'path: <rel-path>' "
            f"or empty, got: {body!r}"
        )

    # Ensure the body is exactly one path: line (no extra lines)
    lines = [ln for ln in body.split("\n") if ln.strip()]
    if len(lines) != 1:
        raise ArchiveParseError(
            "## Archive body must contain exactly one 'path:' line, "
            f"found {len(lines)} non-empty lines"
        )

    return m.group(1).strip()


# ── Profile relevance parser (FR-NOTE-6) ────────────────────────────

# Heading and score regexes are CRLF-tolerant: H3 captures stop before
# CR/LF (lookahead, not $) and score matches accept ``\r?\n`` after the
# trailing digit so CRLF notes parse identically to LF notes.
_H3_RE = re.compile(r"^### ([^\r\n]+)(?=\r?\n|$)", re.MULTILINE)
_SCORE_RE = re.compile(r"^Score:\s*(\d+)/10[ \t]*(?=\r?\n|$)", re.MULTILINE)
_LINE_SPLIT_RE = re.compile(r"\r?\n")


def parse_profile_relevance(
    note: ParsedNote,
) -> list[ProfileRelevanceEntry]:
    """Extract per-profile entries from ``## Profile Relevance``.

    Parameters
    ----------
    note:
        A ``ParsedNote`` from :func:`parse_note`.

    Returns
    -------
    list[ProfileRelevanceEntry]
        Entries in document order.  Empty when the section is absent.
    """
    section: ParsedSection | None = None
    for s in note.sections:
        if s.heading == "Profile Relevance":
            section = s
            break
    if section is None:
        return []

    body = section.body
    matches = list(_H3_RE.finditer(body))
    entries: list[ProfileRelevanceEntry] = []

    for idx, match in enumerate(matches):
        profile_name = match.group(1)
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        entry_body = body[start:end].strip()

        # Extract score
        score = 0
        score_match = _SCORE_RE.search(entry_body)
        if score_match:
            score = int(score_match.group(1))

        # Reason is everything after the Score: line. Split on either
        # ``\n`` or ``\r\n`` so CRLF entry bodies don't leave a trailing
        # ``\r`` on each line.
        reason_lines: list[str] = []
        past_score = False
        for line in _LINE_SPLIT_RE.split(entry_body):
            if _SCORE_RE.match(line):
                past_score = True
                continue
            if past_score:
                reason_lines.append(line)
        reason = "\n".join(reason_lines).strip()

        entries.append(
            ProfileRelevanceEntry(
                profile_name=profile_name,
                score=score,
                reason=reason,
            )
        )

    return entries
