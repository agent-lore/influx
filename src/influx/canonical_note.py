"""CanonicalNote — the single owner of the Influx note shape.

The **CanonicalNote** is Influx's central artifact: typed frontmatter (owned
by Lithos), a fixed section order, and a ``## User Notes`` region preserved
byte-exactly across every rewrite (CONTEXT.md CanonicalNote, spec §9).

Historically the note shape was spread across four modules — the renderer
emitted the section order imperatively, ``notes.py`` parsed it, the Lithos
client and the repair sweep each hand-spliced sections with their own
heading matchers, and the ``## User Notes`` invariant was implemented five
times with three different matching semantics.  This module is the single
seam that owns:

- the canonical **section order** as data (:data:`SECTION_ORDER`);
- the one **User Notes matcher** (:func:`find_user_notes_start`,
  :func:`split_user_notes`) — line-anchored so a mid-line ``## User Notes``
  literal cannot be mistaken for the heading;
- the structured **parse ↔ serialize** round-trip
  (:func:`parse`, :func:`parse_lenient`, :func:`serialize`,
  :class:`CanonicalNote`);
- the **string-level section operations** the rewrite paths use to edit a
  persisted note without a full re-render (insert / drop / upsert / graft).

The string-level ops are deliberately byte-conservative: they touch only the
spliced region and preserve the historical ``rstrip() + "\\n\\n"`` join
semantics, so migrating a caller onto them does not change the bytes written
for any canonical note.  What tightens is the *locator* — every op finds a
heading as a whole line (``^## <heading>`` at the start of a line, CRLF- and
trailing-space-tolerant) rather than as an unanchored substring.

This module imports only Foundation (:mod:`influx.errors`,
:mod:`influx.schemas`); ``notes``, ``renderer``, ``lithos_client`` and the
repair modules import *it*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Final

from influx.errors import InfluxError
from influx.schemas import Tier3Extraction

__all__ = [
    "ARCHIVE",
    "BUILDS_ON",
    "CLAIMS",
    "DATASETS",
    "FULL_TEXT",
    "OPEN_QUESTIONS",
    "PROFILE_RELEVANCE",
    "REPAIR",
    "SECTION_ORDER",
    "SUMMARY",
    "TIER2_SECTIONS",
    "TIER3_SECTIONS",
    "USER_NOTES",
    "CanonicalNote",
    "NoteParseError",
    "ProfileRelevanceEntry",
    "Section",
    "drop_tier2",
    "drop_tier2_and_tier3",
    "extract_section_body",
    "find_user_notes_start",
    "graft_user_notes",
    "insert_full_text_section",
    "insert_tier3_sections",
    "insertion_point",
    "parse",
    "parse_lenient",
    "render_profile_relevance_body",
    "render_tier3_sections",
    "replace_profile_relevance_section",
    "serialize",
    "split_user_notes",
    "upsert_archive_path",
    "upsert_section_text",
]


# ── Section order (spec §9) ─────────────────────────────────────────

ARCHIVE = "Archive"
SUMMARY = "Summary"
FULL_TEXT = "Full Text"
CLAIMS = "Claims"
DATASETS = "Datasets & Benchmarks"
BUILDS_ON = "Builds On"
OPEN_QUESTIONS = "Open Questions"
REPAIR = "Repair"
PROFILE_RELEVANCE = "Profile Relevance"
USER_NOTES = "User Notes"

#: The canonical ``## <heading>`` order.  ``## Repair`` sits between
#: ``## Open Questions`` and ``## Profile Relevance``; ``## User Notes`` is
#: always last and preserved byte-exactly.
SECTION_ORDER: Final[tuple[str, ...]] = (
    ARCHIVE,
    SUMMARY,
    FULL_TEXT,
    CLAIMS,
    DATASETS,
    BUILDS_ON,
    OPEN_QUESTIONS,
    REPAIR,
    PROFILE_RELEVANCE,
    USER_NOTES,
)

#: Sections produced by Tier 2 enrichment (full-text extraction).
TIER2_SECTIONS: Final[tuple[str, ...]] = (FULL_TEXT,)

#: Sections produced by Tier 3 enrichment (deep extraction).
TIER3_SECTIONS: Final[tuple[str, ...]] = (CLAIMS, DATASETS, BUILDS_ON, OPEN_QUESTIONS)

_SECTION_RANK: Final[dict[str, int]] = {h: i for i, h in enumerate(SECTION_ORDER)}


# ── Exceptions ───────────────────────────────────────────────────────


class NoteParseError(InfluxError):
    """Raised when a note cannot be parsed into a :class:`CanonicalNote`."""


# ── Regexes / matchers ───────────────────────────────────────────────

_FRONTMATTER_FENCE = "---"

# H2 heading capture stops at CR or LF so CRLF notes don't capture a
# trailing ``\r``.  A lookahead (not ``$``) terminates the match so CRLF
# endings are tolerated; re.MULTILINE's ``$`` only matches before ``\n``.
_H2_RE = re.compile(r"^## ([^\r\n]+)(?=\r?\n|$)", re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^## ", re.MULTILINE)
_TITLE_RE = re.compile(r"^# ([^\r\n]+)", re.MULTILINE)
_CLOSING_FENCE_RE = re.compile(r"\r?\n---(?:[ \t]*)(?=\r?\n|$)")
# A ``path:`` line anywhere in the ``## Archive`` body — used to keep
# :func:`upsert_archive_path` idempotent regardless of where the line sits.
_PATH_LINE_RE = re.compile(r"^path:", re.MULTILINE)

# The canonical ``## User Notes`` matcher: the heading as a whole line,
# CRLF- and trailing-space-tolerant.  This is the single definition of
# where the byte-exact User Notes region begins.  It deliberately does NOT
# match a mid-line ``## User Notes`` literal, a ``### User Notes`` H3, or a
# ``## User Notes: extra`` variant.
_USER_NOTES_RE = re.compile(r"^## User Notes[ \t]*(?=\r?\n|$)", re.MULTILINE)


def _heading_line_re(heading: str) -> re.Pattern[str]:
    """Return a line-anchored, exact-heading matcher for ``## {heading}``.

    Matches the heading only as a complete line: ``^## {heading}`` followed
    by optional trailing spaces/tabs and a line ending (or EOF).  Tolerant
    of CRLF.  Does not match a longer heading (``## Full Texts``), a deeper
    level (``### {heading}``), or a mid-line occurrence.
    """
    return re.compile(rf"^## {re.escape(heading)}[ \t]*(?=\r?\n|$)", re.MULTILINE)


# ── Profile relevance value type ────────────────────────────────────


@dataclass(frozen=True)
class ProfileRelevanceEntry:
    """One profile's relevance data for the ``## Profile Relevance`` section."""

    profile_name: str
    score: int
    reason: str


def render_profile_relevance_body(entries: list[ProfileRelevanceEntry]) -> str:
    """Render the body of the ``## Profile Relevance`` section.

    Each entry becomes a ``### {profile}`` / ``Score: {score}/10`` /
    ``{reason}`` block; blocks are separated by a blank line.  Returns the
    empty string for an empty entry list.
    """
    parts = [
        f"### {entry.profile_name}\nScore: {entry.score}/10\n{entry.reason}"
        for entry in entries
    ]
    return "\n\n".join(parts)


# ── Structured model ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Section:
    """One ``## <heading>`` section from the Influx-owned body."""

    heading: str
    body: str


@dataclass(frozen=True)
class CanonicalNote:
    """A parsed canonical Lithos note.

    Attributes
    ----------
    frontmatter_raw:
        The raw YAML between ``---`` fences (legacy notes only; empty for
        the post-#178 body-only shape).
    title:
        The ``# <Title>`` text (without the ``# `` prefix).
    sections:
        Influx-owned ``## <heading>`` sections above ``## User Notes``, in
        document order.  Does NOT include ``## User Notes``.  Section bodies
        are trailing-stripped.
    user_notes:
        The byte-exact ``## User Notes`` region (heading line to EOF,
        inclusive), or ``None`` when the heading is absent.
    """

    frontmatter_raw: str
    title: str
    sections: tuple[Section, ...] = field(default_factory=tuple)
    user_notes: str | None = None

    def get_section(self, heading: str) -> Section | None:
        """Return the section with *heading*, or ``None``."""
        for section in self.sections:
            if section.heading == heading:
                return section
        return None

    def drop_sections(self, *headings: str) -> CanonicalNote:
        """Return a copy with the named sections removed."""
        drop = frozenset(headings)
        kept = tuple(s for s in self.sections if s.heading not in drop)
        return replace(self, sections=kept)

    def upsert_section(self, heading: str, body: str) -> CanonicalNote:
        """Return a copy with *heading* set to *body*.

        Replaces the section in place when present; otherwise inserts it at
        its :data:`SECTION_ORDER` position.
        """
        new_section = Section(heading=heading, body=body)
        if any(s.heading == heading for s in self.sections):
            updated = tuple(
                new_section if s.heading == heading else s for s in self.sections
            )
            return replace(self, sections=updated)

        target = _SECTION_RANK.get(heading, len(SECTION_ORDER))
        result: list[Section] = []
        inserted = False
        for section in self.sections:
            rank = _SECTION_RANK.get(section.heading, len(SECTION_ORDER))
            if not inserted and rank > target:
                result.append(new_section)
                inserted = True
            result.append(section)
        if not inserted:
            result.append(new_section)
        return replace(self, sections=tuple(result))


# ── User Notes matcher (the single byte-exact locator) ──────────────


def find_user_notes_start(content: str) -> int | None:
    """Return the byte offset of the ``## User Notes`` heading, or ``None``.

    Uses the canonical line-anchored matcher: the heading must begin a line
    and be the whole heading text.  A mid-line literal or a ``### User
    Notes`` H3 is not matched.
    """
    match = _USER_NOTES_RE.search(content)
    return match.start() if match is not None else None


def split_user_notes(content: str) -> tuple[str, str | None]:
    """Split *content* at ``## User Notes``.

    Returns ``(influx_body, user_notes_region)`` where *user_notes_region*
    is the byte-exact text from the heading to EOF, or ``None`` when the
    heading is absent (in which case *influx_body* is all of *content*).
    """
    idx = find_user_notes_start(content)
    if idx is None:
        return content, None
    return content[:idx], content[idx:]


# ── Parse ────────────────────────────────────────────────────────────


def parse(text: str) -> CanonicalNote:
    """Parse a canonical Lithos note into a :class:`CanonicalNote`.

    Accepts the post-#178 body-only shape (starts with ``# {title}``;
    ``frontmatter_raw`` is empty) and the legacy frontmatter-prefixed shape
    (starts with a ``---``-fenced YAML block).

    Raises
    ------
    NoteParseError
        When the note lacks a ``# ...`` title heading, or a legacy-shape
        input has an unclosed frontmatter fence.
    """
    if text.startswith(_FRONTMATTER_FENCE):
        frontmatter_raw, after_frontmatter = _split_frontmatter(text)
    else:
        frontmatter_raw = ""
        after_frontmatter = text
    title, body = _split_title(after_frontmatter)
    sections, user_notes = _split_sections(body)
    return CanonicalNote(
        frontmatter_raw=frontmatter_raw,
        title=title,
        sections=tuple(sections),
        user_notes=user_notes,
    )


def parse_lenient(text: str, *, fallback_title: str = "") -> CanonicalNote:
    """Parse *text*, tolerating a missing ``# {title}`` heading.

    ``read_note`` serves note ``content`` with the doc-level ``# {title}``
    heading stripped, but the archive-path / profile-relevance data the
    repair sweep needs lives in the body below the title.  When *text* has
    no title heading and *fallback_title* is given, the title is reattached
    for parsing only (the persisted body is never modified here).

    With no title heading and no *fallback_title*, delegates to
    :func:`parse`, which raises :class:`NoteParseError`.
    """
    if _has_title_heading(text):
        return parse(text)
    if fallback_title:
        return parse(f"# {fallback_title}\n\n{text}")
    return parse(text)


def _has_title_heading(text: str) -> bool:
    """Return True when *text* contains a ``# ...`` (non-``##``) title line.

    Mirrors :func:`_split_title` detection exactly — the line is stripped
    and ``## `` headings are excluded — so callers agree on what counts as
    a title.
    """
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return True
    return False


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_raw, rest)`` by splitting on ``---`` fences.

    Tolerates LF and CRLF without normalising them; *rest* is sliced from
    the original text so byte-exact preservation downstream is retained.
    Raises :class:`NoteParseError` when fences are missing.
    """
    if not text.startswith(_FRONTMATTER_FENCE):
        raise NoteParseError("Note does not start with frontmatter fence '---'")

    nl_idx = text.find("\n")
    if nl_idx == -1:
        raise NoteParseError("No closing frontmatter fence '---' found")
    after_open = nl_idx + 1

    close_match = _CLOSING_FENCE_RE.search(text, after_open - 1)
    if close_match is None:
        raise NoteParseError("No closing frontmatter fence '---' found")

    frontmatter_raw = text[after_open : close_match.start()]

    rest_start = close_match.end()
    if rest_start < len(text) and text[rest_start] == "\r":
        rest_start += 1
    if rest_start < len(text) and text[rest_start] == "\n":
        rest_start += 1
    return frontmatter_raw, text[rest_start:]


def _split_title(text: str) -> tuple[str, str]:
    """Return ``(title, body)`` from the text after any frontmatter.

    The title is the first ``# <text>`` line; the body is everything after
    it.  Raises :class:`NoteParseError` when no title heading is found.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:]
            body = "\n".join(lines[i + 1 :])
            return title, body
    raise NoteParseError("No title heading '# ...' found in note")


def _split_sections(body: str) -> tuple[list[Section], str | None]:
    """Split the body into Influx-owned sections and the User Notes region.

    Returns ``(sections, user_notes)`` — *user_notes* is ``None`` when
    absent and otherwise the byte-exact region from the ``## User Notes``
    heading to EOF.
    """
    influx_body, user_notes = split_user_notes(body)

    sections: list[Section] = []
    matches = list(_H2_RE.finditer(influx_body))
    for idx, match in enumerate(matches):
        heading = match.group(1)
        body_start = match.end()
        if idx + 1 < len(matches):
            body_end = matches[idx + 1].start()
        else:
            body_end = len(influx_body)
        section_body = influx_body[body_start:body_end]
        # Strip a single leading newline (LF or CRLF) after the heading.
        if section_body.startswith("\r\n"):
            section_body = section_body[2:]
        elif section_body.startswith("\n"):
            section_body = section_body[1:]
        section_body = section_body.rstrip("\r\n")
        sections.append(Section(heading=heading, body=section_body))

    return sections, user_notes


# ── Serialize (inverse of parse for canonical inputs) ───────────────


def serialize(note: CanonicalNote) -> str:
    """Serialise a :class:`CanonicalNote` back to canonical note text.

    The inverse of :func:`parse` for notes already in **canonical LF form**
    — i.e. renderer-produced output (single blank line between sections,
    trailing-stripped bodies, LF line endings): ``serialize(parse(text)) ==
    text`` holds there.  It does **not** promise byte-identity for CRLF or
    legacy hand-authored notes: fences are emitted with ``---\\n`` and
    sections are joined with LF, so a CRLF note round-trips with its
    separators normalised to LF (the ``## User Notes`` region alone is
    preserved byte-exactly by :func:`parse`).  Sections are emitted in
    their stored order; the ``## User Notes`` region is appended
    byte-exactly, or an empty ``## User Notes`` heading when absent.
    """
    pieces = [f"# {note.title}"]
    for section in note.sections:
        if section.body:
            pieces.append(f"## {section.heading}\n{section.body}")
        else:
            pieces.append(f"## {section.heading}")
    pieces.append(note.user_notes if note.user_notes is not None else "## User Notes\n")

    prefix = f"---\n{note.frontmatter_raw}\n---\n" if note.frontmatter_raw else ""
    return prefix + "\n\n".join(pieces)


# ── String-level section operations (the migration surface) ─────────


def insertion_point(content: str) -> int:
    """Return the offset to insert a new section at its canonical position.

    New Tier 2 / Tier 3 / Repair sections are inserted before
    ``## Profile Relevance``; falling back to before ``## User Notes``, then
    to end-of-content.
    """
    match = _heading_line_re(PROFILE_RELEVANCE).search(content)
    if match is not None:
        return match.start()
    match = _USER_NOTES_RE.search(content)
    if match is not None:
        return match.start()
    return len(content)


def insert_full_text_section(content: str, full_text: str) -> str:
    """Insert a ``## Full Text`` section at the canonical position."""
    pos = insertion_point(content)
    section = f"\n## Full Text\n{full_text}\n"
    return content[:pos] + section + "\n" + content[pos:]


def render_tier3_sections(tier3: Tier3Extraction) -> str:
    """Render the four Tier 3 sections as markdown (trailing newline).

    The single definition of the Tier 3 section block, shared by the
    renderer and the repair sweep.  The leading blank-line separator is the
    caller's responsibility (see :func:`insert_tier3_sections`).
    """
    parts: list[str] = ["## Claims"]
    parts.extend(f"- {claim}" for claim in tier3.claims)
    parts.append("\n## Datasets & Benchmarks")
    parts.extend(f"- {ds}" for ds in tier3.datasets)
    parts.append("\n## Builds On")
    parts.extend(f"- {item}" for item in tier3.builds_on)
    parts.append("\n## Open Questions")
    parts.extend(f"- {q}" for q in tier3.open_questions)
    return "\n".join(parts) + "\n"


def insert_tier3_sections(content: str, tier3: Tier3Extraction) -> str:
    """Insert the Tier 3 sections at the canonical position."""
    pos = insertion_point(content)
    section_text = "\n" + render_tier3_sections(tier3)
    return content[:pos] + section_text + "\n" + content[pos:]


def _drop_section(content: str, heading: str) -> str:
    """Remove one section (heading to the next ``## `` heading).

    Trailing-strips the preceding content, then rejoins to the following
    section with a single blank line.  The tail — including any trailing
    ``## User Notes`` region — is preserved **byte-exactly**: this module
    owns that invariant, so the drop ops deliberately do *not* apply the
    legacy whole-document ``rstrip()`` that trimmed user-note trailing
    whitespace (an intended fix that reaches production when PR 3 migrates
    the oversize-trim path onto these helpers).  A trailing section (no
    following ``## ``) drops to end-of-content.
    """
    match = _heading_line_re(heading).search(content)
    if match is None:
        return content
    before = content[: match.start()].rstrip()
    rest = content[match.end() :]
    next_heading = _NEXT_H2_RE.search(rest)
    if next_heading is None:
        return before
    after = rest[next_heading.start() :]
    return before + "\n\n" + after


def drop_tier2(content: str) -> str:
    """Remove the ``## Full Text`` (Tier 2) section, keeping Tier 1/Tier 3."""
    return _drop_section(content, FULL_TEXT)


def drop_tier2_and_tier3(content: str) -> str:
    """Remove ``## Full Text`` and all Tier 3 sections, keeping Tier 1."""
    result = drop_tier2(content)
    for heading in TIER3_SECTIONS:
        result = _drop_section(result, heading)
    return result


def extract_section_body(content: str, heading: str) -> str:
    """Return the trailing-stripped body of *heading*, or ``""`` if absent.

    CRLF-tolerant: skips a ``\\r\\n`` or ``\\n`` line ending after the
    heading before reading the body.
    """
    match = _heading_line_re(heading).search(content)
    if match is None:
        return ""
    body_start = match.end()
    if content[body_start : body_start + 2] == "\r\n":
        body_start += 2
    elif body_start < len(content) and content[body_start] == "\n":
        body_start += 1
    next_match = _NEXT_H2_RE.search(content, body_start)
    if next_match is not None:
        return content[body_start : next_match.start()].rstrip()
    return content[body_start:].rstrip()


def upsert_archive_path(content: str, archive_path: str) -> str:
    """Set the ``## Archive`` ``path:`` line, idempotently.

    Inserts ``path: {archive_path}`` immediately below the ``## Archive``
    heading when the section has no ``path:`` line yet; a no-op when the
    section already contains a ``path:`` line *anywhere* in its body (not
    just as the first line, so a hand-edited section carrying other
    metadata is never given a duplicate ``path:``) or when the section is
    absent.
    """
    match = _heading_line_re(ARCHIVE).search(content)
    if match is None:
        return content
    next_h2 = _NEXT_H2_RE.search(content, match.end())
    section_end = next_h2.start() if next_h2 is not None else len(content)
    if _PATH_LINE_RE.search(content, match.end(), section_end):
        return content
    insert_pos = match.end()
    if content[insert_pos : insert_pos + 2] == "\r\n":
        insert_pos += 2
    elif insert_pos < len(content) and content[insert_pos] == "\n":
        insert_pos += 1
    return content[:insert_pos] + f"path: {archive_path}\n" + content[insert_pos:]


def replace_profile_relevance_section(
    content: str,
    entries: list[ProfileRelevanceEntry],
) -> str:
    """Replace the ``## Profile Relevance`` section body with *entries*.

    A no-op when the section is absent.  Preserves the blank line before a
    following section.
    """
    match = _heading_line_re(PROFILE_RELEVANCE).search(content)
    if match is None:
        return content
    pr_idx = match.start()
    next_h2 = content.find("\n## ", match.end())

    pr_body = render_profile_relevance_body(entries)
    marker = f"## {PROFILE_RELEVANCE}"
    replacement = f"{marker}\n{pr_body}\n" if pr_body else f"{marker}\n"
    if next_h2 != -1:
        return content[:pr_idx] + replacement + "\n" + content[next_h2 + 1 :]
    return content[:pr_idx] + replacement


def upsert_section_text(content: str, heading: str, rendered_section: str) -> str:
    """Insert or replace a full section with *rendered_section*.

    *rendered_section* is the complete ``## {heading}\\n...`` text with its
    trailing newline.  When the section already exists it is replaced in
    place (trimming leading blank lines from the following content so blank
    lines do not accumulate across re-renders); otherwise it is inserted at
    :func:`insertion_point`.
    """
    span = _section_span(content, heading)
    if span is not None:
        start, end = span
        tail = content[end:].lstrip("\n")
        if tail:
            tail = "\n" + tail
        head = content[:start].rstrip("\n")
        separator = "\n\n" if head.strip() else ""
        return head + separator + rendered_section + tail

    pos = insertion_point(content)
    if pos < len(content):
        return content[:pos] + rendered_section + "\n" + content[pos:]
    if content and not content.endswith("\n"):
        content += "\n"
    return content + ("\n" if content else "") + rendered_section


def _section_span(content: str, heading: str) -> tuple[int, int] | None:
    """Return ``(start, end)`` of the *heading* section, or ``None``.

    *start* is the heading offset; *end* is the next ``## `` heading offset
    (or end-of-content for a trailing section).
    """
    match = _heading_line_re(heading).search(content)
    if match is None:
        return None
    next_h = _NEXT_H2_RE.search(content, match.end())
    end = next_h.start() if next_h is not None else len(content)
    return match.start(), end


def graft_user_notes(existing_content: str, new_content: str) -> str:
    """Graft the existing note's ``## User Notes`` region onto *new_content*.

    The byte-exact ``## User Notes`` region from *existing_content* replaces
    any User Notes region in *new_content* (AC-05-E).  When *existing_content*
    has no User Notes region, *new_content* is returned unchanged.  Preserves
    the historical ``rstrip() + "\\n\\n"`` join.
    """
    _, existing_region = split_user_notes(existing_content)
    if existing_region is None:
        return new_content
    new_body, _ = split_user_notes(new_content)
    return new_body.rstrip() + "\n\n" + existing_region
