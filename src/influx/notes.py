"""Canonical Lithos note parser and renderer (FR-NOTE-1..9).

Parses and renders the canonical note format used by Influx:

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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from influx.errors import InfluxError

__all__ = [
    "NoteParseError",
    "ParsedNote",
    "ParsedSection",
    "parse_note",
]

# ── Exceptions ───────────────────────────────────────────────────────


class NoteParseError(InfluxError):
    """Raised when a note cannot be parsed."""


# ── Data structures ──────────────────────────────────────────────────

_FRONTMATTER_FENCE = "---"
_USER_NOTES_HEADING = "## User Notes"
_H2_RE = re.compile(r"^## (.+)$", re.MULTILINE)


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


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_raw, rest)`` by splitting on ``---`` fences.

    Raises ``NoteParseError`` if fences are missing.
    """
    if not text.startswith(_FRONTMATTER_FENCE):
        raise NoteParseError("Note does not start with frontmatter fence '---'")

    # Find closing fence (skip the opening one)
    after_open = text.index("\n") + 1
    close_idx = text.find(f"\n{_FRONTMATTER_FENCE}", after_open)
    if close_idx == -1:
        raise NoteParseError("No closing frontmatter fence '---' found")

    frontmatter_raw = text[after_open:close_idx]
    # Skip past the closing fence line
    rest_start = close_idx + 1 + len(_FRONTMATTER_FENCE)
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

    # Search for ## User Notes — we need byte-exact preservation
    un_pattern = re.compile(r"^## User Notes[ \t]*$", re.MULTILINE)
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
        # Strip single leading newline after heading, preserve rest
        if section_body.startswith("\n"):
            section_body = section_body[1:]
        # Strip trailing whitespace between sections
        section_body = section_body.rstrip("\n")
        sections.append(ParsedSection(heading=heading, body=section_body))

    return sections, user_notes
