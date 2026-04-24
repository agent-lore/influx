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
    "ArchiveInvariantError",
    "ArchiveParseError",
    "NoteParseError",
    "ParsedNote",
    "ParsedSection",
    "ProfileRelevanceEntry",
    "build_profile_relevance_for_rewrite",
    "merge_tags",
    "parse_archive_path",
    "parse_note",
    "parse_profile_relevance",
    "recompute_confidence",
    "render_archive_section",
    "render_note",
    "validate_archive_tag_invariant",
]

# ── Exceptions ───────────────────────────────────────────────────────


class NoteParseError(InfluxError):
    """Raised when a note cannot be parsed."""


class ArchiveParseError(NoteParseError):
    """Raised when the ``## Archive`` section body is malformed."""


class ArchiveInvariantError(InfluxError):
    """Raised when a path: line and influx:archive-missing co-exist."""


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
    influx_owned = [
        t for t in new_tags if _is_influx_owned(t)
    ]

    # 3. profile:* union merge with rejection guard (FR-NOTE-6)
    existing_profiles = {
        t for t in existing_tags if t.startswith("profile:")
    }
    new_profiles = {
        t for t in new_tags if t.startswith("profile:")
    }
    union_profiles = existing_profiles | new_profiles
    # Remove profiles that have been rejected
    guarded_profiles = sorted(
        t
        for t in union_profiles
        if t[len("profile:") :] not in rejected_profiles
    )

    # 4. Rejection tags: preserve from both sets
    rejection_tags = sorted(
        {
            t
            for t in (*existing_tags, *new_tags)
            if t.startswith("influx:rejected:")
        }
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


# ── Archive section render / parse (FR-NOTE-9) ──────────────────────

_ARCHIVE_PATH_RE = re.compile(r"^path:\s*(.+)$")


def render_archive_section(archive_path: str | None) -> str:
    """Render the ``## Archive`` section body.

    Parameters
    ----------
    archive_path:
        A POSIX-separator relative path for the ``path:`` line, or
        ``None`` for the empty-body (failure-path) form.

    Returns
    -------
    str
        The rendered section text starting with ``## Archive\\n``.
    """
    if archive_path is not None:
        return f"## Archive\npath: {archive_path}\n"
    return "## Archive\n"


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


def validate_archive_tag_invariant(
    *,
    archive_path: str | None,
    tags: list[str],
) -> None:
    """Enforce: never write both a path: line AND influx:archive-missing.

    Parameters
    ----------
    archive_path:
        The archive path that will be rendered (or ``None``).
    tags:
        The tag list that will be written to frontmatter.

    Raises
    ------
    ArchiveInvariantError
        When *archive_path* is not ``None`` and *tags* contains
        ``influx:archive-missing``.
    """
    if archive_path is not None and "influx:archive-missing" in tags:
        raise ArchiveInvariantError(
            "Cannot write both a path: line and influx:archive-missing "
            "tag on the same note"
        )


# ── Full canonical renderer (FR-NOTE-1..8, US-007) ──────────────────


@dataclass(frozen=True)
class ProfileRelevanceEntry:
    """One profile's relevance data for the ``## Profile Relevance`` section."""

    profile_name: str
    score: int
    reason: str


def _format_confidence(confidence: float) -> str:
    """Format confidence as a clean decimal string for frontmatter."""
    val = round(confidence, 4)
    if val == int(val):
        return f"{int(val)}.0"
    return str(val)


def _render_frontmatter(
    *,
    source_url: str,
    tags: list[str],
    confidence: float,
) -> str:
    """Render the YAML frontmatter content (between ``---`` fences)."""
    lines = [
        "note_type: summary",
        "namespace: influx",
        f"source_url: {source_url}",
    ]
    if tags:
        lines.append("tags:")
        for tag in tags:
            lines.append(f"  - {tag}")
    else:
        lines.append("tags: []")
    lines.append(f"confidence: {_format_confidence(confidence)}")
    return "\n".join(lines)


def _render_profile_relevance_body(
    entries: list[ProfileRelevanceEntry],
) -> str:
    """Render the body of the ``## Profile Relevance`` section."""
    parts: list[str] = []
    for entry in entries:
        parts.append(
            f"### {entry.profile_name}\n"
            f"Score: {entry.score}/10\n"
            f"{entry.reason}"
        )
    return "\n\n".join(parts)


def render_note(
    *,
    title: str,
    source_url: str,
    tags: list[str],
    confidence: float,
    archive_path: str | None,
    summary: str,
    keywords: list[str],
    profile_entries: list[ProfileRelevanceEntry],
    user_notes: str | None = None,
) -> str:
    """Render a full canonical Lithos note (FR-NOTE-1..8).

    Parameters
    ----------
    title:
        The ``# <Title>`` text (without the ``# `` prefix).
    source_url:
        Normalised canonical URL for frontmatter.
    tags:
        The final merged tag list (from :func:`merge_tags`).
    confidence:
        The confidence value for frontmatter.
    archive_path:
        POSIX-separator relative path or ``None`` for empty Archive.
    summary:
        The Tier-1 summary text for the ``## Summary`` section.
    keywords:
        Keywords from Tier-1 enrichment (may be empty).
    profile_entries:
        Profile relevance entries to render. For rewrites, use
        :func:`build_profile_relevance_for_rewrite` to resolve
        entries that honour the rejection guard.
    user_notes:
        Byte-exact ``## User Notes`` region from a previous parse,
        or ``None`` to append an empty ``## User Notes`` heading.

    Returns
    -------
    str
        The complete canonical note text.

    Raises
    ------
    ArchiveInvariantError
        When *archive_path* is not ``None`` and *tags* contains
        ``influx:archive-missing``.
    """
    validate_archive_tag_invariant(archive_path=archive_path, tags=tags)

    frontmatter = _render_frontmatter(
        source_url=source_url,
        tags=tags,
        confidence=confidence,
    )
    archive_section = render_archive_section(archive_path)

    # Summary body
    summary_body = summary
    if keywords:
        summary_body += f"\n\nKeywords: {', '.join(keywords)}"

    # Compose note
    output = f"---\n{frontmatter}\n---\n"
    output += f"# {title}\n\n"
    output += archive_section + "\n"
    output += f"## Summary\n{summary_body}\n"

    # Profile Relevance section
    if profile_entries:
        output += "\n"
        pr_body = _render_profile_relevance_body(profile_entries)
        output += f"## Profile Relevance\n{pr_body}\n"

    # User Notes — preserved byte-exactly or empty heading appended
    output += "\n"
    if user_notes is not None:
        output += user_notes
    else:
        output += "## User Notes\n"

    return output


# ── Profile relevance parse / rewrite helpers ────────────────────────

_H3_RE = re.compile(r"^### (.+)$", re.MULTILINE)
_SCORE_RE = re.compile(r"^Score:\s*(\d+)/10$", re.MULTILINE)


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

        # Reason is everything after the Score: line
        reason_lines: list[str] = []
        past_score = False
        for line in entry_body.split("\n"):
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


def build_profile_relevance_for_rewrite(
    *,
    old_entries: list[ProfileRelevanceEntry],
    new_entries: list[ProfileRelevanceEntry],
    tags: list[str],
) -> list[ProfileRelevanceEntry]:
    """Resolve profile relevance entries for a rewrite (FR-NOTE-6).

    Rejected profiles (``influx:rejected:<profile>`` in *tags*) keep
    their old entries unchanged.  Non-rejected profiles use new entries.

    Parameters
    ----------
    old_entries:
        Entries from the previously parsed note.
    new_entries:
        Freshly computed entries for the current rewrite cycle.
    tags:
        The final merged tag list (used to detect rejection guards).

    Returns
    -------
    list[ProfileRelevanceEntry]
        The resolved entries to pass to :func:`render_note`.
    """
    rejected = {
        t[len("influx:rejected:") :]
        for t in tags
        if t.startswith("influx:rejected:")
    }

    result: list[ProfileRelevanceEntry] = []
    seen: set[str] = set()

    # Non-rejected profiles use new entries
    for entry in new_entries:
        if entry.profile_name not in rejected:
            result.append(entry)
            seen.add(entry.profile_name)

    # Rejected profiles keep old entries
    for entry in old_entries:
        if entry.profile_name in rejected and entry.profile_name not in seen:
            result.append(entry)
            seen.add(entry.profile_name)

    return result
