"""Canonical Lithos note parser and rewrite-merge helpers (FR-NOTE-1..9).

Parses the canonical note format used by Influx:

    ---
    <YAML frontmatter>
    ---
    # <Title>

    ## Archive
    ...
    ## Summary
    ...
    ## User Notes
    <user content preserved byte-identically>

The ``## User Notes`` region is everything from the ``## User Notes``
heading to end-of-file, preserved byte-exactly across parse/rewrite
cycles (FR-NOTE-4, R-5).

Rendering of canonical notes lives in :mod:`influx.renderer`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from influx.errors import InfluxError
from influx.renderer import ProfileRelevanceEntry

__all__ = [
    "ArchiveParseError",
    "NoteParseError",
    "ParsedNote",
    "ParsedSection",
    "merge_tags",
    "parse_archive_path",
    "parse_note",
    "parse_profile_relevance",
    "recompute_confidence",
]

# ── Exceptions ───────────────────────────────────────────────────────


class NoteParseError(InfluxError):
    """Raised when a note cannot be parsed."""


class ArchiveParseError(NoteParseError):
    """Raised when the ``## Archive`` section body is malformed."""


# ── Data structures ──────────────────────────────────────────────────

_FRONTMATTER_FENCE = "---"
_USER_NOTES_HEADING = "## User Notes"
# Heading captures stop at CR or LF so CRLF notes don't capture a trailing \r.
# A lookahead (not $) terminates the match so CRLF endings are tolerated;
# re.MULTILINE's $ only matches before \n, not before \r.
_H2_RE = re.compile(r"^## ([^\r\n]+)(?=\r?\n|$)", re.MULTILINE)


@dataclass(frozen=True)
class ParsedSection:
    """One ``## <heading>`` section from the Influx-owned body."""

    heading: str
    body: str


@dataclass(frozen=True)
class ParsedNote:
    """Result of parsing a canonical Lithos note.

    Attributes
    ----------
    frontmatter_raw:
        The raw YAML text between the ``---`` fences (excluding the
        fences themselves).  Includes tags, confidence, namespace, etc.
    title:
        The ``# <Title>`` text (without the ``# `` prefix).
    sections:
        Influx-owned ``## <heading>`` sections found above ``## User
        Notes``, in document order.  Does NOT include ``## User Notes``.
    user_notes:
        The byte-exact content of the ``## User Notes`` region
        (everything from the ``## User Notes`` line to EOF, inclusive).
        ``None`` when the heading is absent.
    """

    frontmatter_raw: str
    title: str
    sections: tuple[ParsedSection, ...] = field(default_factory=tuple)
    user_notes: str | None = None


# ── Parser ───────────────────────────────────────────────────────────


def parse_note(text: str) -> ParsedNote:
    """Parse a canonical Lithos note into its constituent parts.

    Parameters
    ----------
    text:
        The full note text including frontmatter fences.

    Returns
    -------
    ParsedNote
        Structured representation with frontmatter, title, Influx-owned
        sections, and the ``## User Notes`` region.

    Raises
    ------
    NoteParseError
        When the note lacks valid frontmatter fences or a title.
    """
    frontmatter_raw, after_frontmatter = _split_frontmatter(text)
    title, body = _split_title(after_frontmatter)
    sections, user_notes = _split_sections(body)

    return ParsedNote(
        frontmatter_raw=frontmatter_raw,
        title=title,
        sections=tuple(sections),
        user_notes=user_notes,
    )


# ── Internal helpers ─────────────────────────────────────────────────


_CLOSING_FENCE_RE = re.compile(r"\r?\n---(?:[ \t]*)(?=\r?\n|$)")


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_raw, rest)`` by splitting on ``---`` fences.

    Tolerates both LF and CRLF line endings without normalising them; the
    returned *rest* is sliced from the original text so downstream
    byte-exact preservation of the ``## User Notes`` region is retained
    regardless of newline style.

    Raises ``NoteParseError`` if fences are missing.
    """
    if not text.startswith(_FRONTMATTER_FENCE):
        raise NoteParseError("Note does not start with frontmatter fence '---'")

    # Find end of opening fence line (either \n or \r\n).
    nl_idx = text.find("\n")
    if nl_idx == -1:
        raise NoteParseError("No closing frontmatter fence '---' found")
    after_open = nl_idx + 1

    close_match = _CLOSING_FENCE_RE.search(text, after_open - 1)
    if close_match is None:
        raise NoteParseError("No closing frontmatter fence '---' found")

    # frontmatter_raw is the YAML between the fences. Exclude the leading
    # CR if present so callers see clean YAML content.
    fm_end = close_match.start()
    frontmatter_raw = text[after_open:fm_end]

    # Skip past the closing fence line, including any trailing newline.
    rest_start = close_match.end()
    if rest_start < len(text) and text[rest_start] == "\r":
        rest_start += 1
    if rest_start < len(text) and text[rest_start] == "\n":
        rest_start += 1
    rest = text[rest_start:]
    return frontmatter_raw, rest


def _split_title(text: str) -> tuple[str, str]:
    """Return ``(title, body)`` from the text after frontmatter.

    The title is the first ``# <text>`` line.  Everything after the
    title line (with leading blank lines consumed) is the body.

    Raises ``NoteParseError`` if no title heading is found.
    """
    for i, line in enumerate(text.split("\n")):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:]
            # Body is everything after the title line
            remaining_lines = text.split("\n")[i + 1 :]
            body = "\n".join(remaining_lines)
            return title, body

    raise NoteParseError("No title heading '# ...' found in note")


def _split_sections(body: str) -> tuple[list[ParsedSection], str | None]:
    """Split the body into Influx-owned sections and the User Notes region.

    Returns ``(sections, user_notes)`` where *user_notes* is ``None``
    when ``## User Notes`` is absent.

    The User Notes region is captured byte-exactly: everything from the
    ``## User Notes`` line (inclusive) to EOF.
    """
    # Find ## User Notes position in the original body
    user_notes: str | None = None
    influx_body = body

    # Search for ## User Notes — we need byte-exact preservation.
    # Tolerate trailing CR (CRLF line endings) without consuming it.
    un_pattern = re.compile(r"^## User Notes[ \t]*(?=\r?\n|$)", re.MULTILINE)
    un_match = un_pattern.search(body)
    if un_match is not None:
        user_notes = body[un_match.start() :]
        influx_body = body[: un_match.start()]

    # Parse ## sections from the Influx-owned body
    sections: list[ParsedSection] = []
    matches = list(_H2_RE.finditer(influx_body))

    for idx, match in enumerate(matches):
        heading = match.group(1)
        body_start = match.end()
        if idx + 1 < len(matches):
            body_end = matches[idx + 1].start()
        else:
            body_end = len(influx_body)
        section_body = influx_body[body_start:body_end]
        # Strip single leading newline (LF or CRLF) after heading.
        if section_body.startswith("\r\n"):
            section_body = section_body[2:]
        elif section_body.startswith("\n"):
            section_body = section_body[1:]
        # Strip trailing whitespace/newlines between sections
        section_body = section_body.rstrip("\r\n")
        sections.append(ParsedSection(heading=heading, body=section_body))

    return sections, user_notes


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
