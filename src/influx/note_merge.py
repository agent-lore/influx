"""Merge a Profile's incoming note into the note Lithos already holds (FR-NOTE-6).

When a second Profile ingests an item that is already in Lithos, the
create-path ``lithos_write`` comes back ``duplicate`` and names the
existing note (lithos task c1196e30).  :func:`merge_into_existing` decides
what the merged note looks like:

- ``profile:*`` tags are union-merged, honouring the ``influx:rejected:*``
  guard of :func:`influx.notes.merge_tags`, and the ``## Profile Relevance``
  entries are unioned, the incoming Profile's entry replacing its old one.
- The richer body wins.  A body ranks by whether it carries Tier 2
  (``## Full Text``) and Tier 3 content.  The incoming body replaces the
  existing one only when it ranks strictly higher and keeps the existing
  note's archived copy (an ``## Archive`` path), and then takes the
  existing ``## User Notes`` and ``## Repair`` with it.  Otherwise the
  existing body stays, together with the Influx-owned tags that describe it
  (``full-text``, ``text:*``, the terminal markers), so a later,
  lower-scoring Profile's summary-only write never replaces an earlier
  Profile's full text.
- Confidence is the higher of the two (FR-NOTE-8).

It returns ``None`` when the merge would add nothing: no new Profile and no
richer body.  That is the usual case of a Profile re-seeing its own note,
and the caller then writes nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from influx.canonical_note import (
    FULL_TEXT,
    REPAIR,
    SECTION_ORDER,
    TIER3_SECTIONS,
    CanonicalNote,
    NoteParseError,
    carry_forward_section,
    graft_user_notes,
    parse_lenient,
    replace_profile_relevance_section,
)
from influx.notes import merge_tags, parse_archive_path, parse_profile_relevance
from influx.renderer import merge_profile_relevance_union

__all__ = ["MergedNote", "merge_into_existing"]

_PROFILE_PREFIX = "profile:"


@dataclass(frozen=True)
class MergedNote:
    """The note to write back over the existing one."""

    content: str
    tags: list[str]
    confidence: float
    #: True when the existing body was kept and only tags / Profile
    #: Relevance changed; False when the richer incoming body replaced it.
    kept_existing_body: bool
    #: Profiles whose tag the merge adds to the note, sorted.
    added_profiles: tuple[str, ...]


def merge_into_existing(
    *,
    title: str,
    existing_content: str,
    existing_tags: Sequence[str],
    existing_confidence: float,
    incoming_content: str,
    incoming_tags: Sequence[str],
    incoming_confidence: float,
    keep_existing_body: bool = False,
) -> MergedNote | None:
    """Merge *incoming* into *existing*, or return ``None`` if nothing changes.

    *title* is the existing note's title; ``lithos_read`` may serve the
    content without its ``# {title}`` heading, so it is reattached for
    parsing only.  *keep_existing_body* forces the existing body to stay
    whatever the ranks (the caller's fallback when the richer body is too
    large for Lithos).
    """
    existing = _parse_canonical(existing_content, title)
    incoming = _parse_canonical(incoming_content, title)
    take_incoming = not keep_existing_body and _incoming_is_richer(existing, incoming)

    if take_incoming:
        tags = merge_tags(
            existing_tags=list(existing_tags), new_tags=list(incoming_tags)
        )
        content = graft_user_notes(existing_content, incoming_content)
        content = carry_forward_section(existing_content, content, REPAIR)
    else:
        # Re-offering the existing tags as the "new" set keeps the existing
        # Influx-owned tags; only the incoming profile tags are added.
        incoming_profiles = [t for t in incoming_tags if t.startswith(_PROFILE_PREFIX)]
        tags = merge_tags(
            existing_tags=list(existing_tags),
            new_tags=[*existing_tags, *incoming_profiles],
        )
        content = existing_content

    added = tuple(sorted(_profiles(tags) - _profiles(existing_tags)))
    if not take_incoming and not added:
        return None

    return MergedNote(
        content=_union_profile_relevance(content, existing, incoming, tags),
        tags=tags,
        confidence=max(existing_confidence, incoming_confidence),
        kept_existing_body=not take_incoming,
        added_profiles=added,
    )


def _parse_canonical(content: str, title: str) -> CanonicalNote | None:
    """Parse *content* as an Influx note, or ``None`` if it isn't one.

    A body with none of the canonical ``## `` sections is someone else's
    text (or a hand rewrite): the merge must neither rank it nor replace it.
    """
    try:
        note = parse_lenient(content, fallback_title=title)
    except NoteParseError:
        return None
    if not any(s.heading in SECTION_ORDER for s in note.sections):
        return None
    return note


def _incoming_is_richer(
    existing: CanonicalNote | None, incoming: CanonicalNote | None
) -> bool:
    """True when both bodies are readable and *incoming* ranks higher.

    A body that would drop the existing note's archived copy (its
    ``## Archive`` path) never wins, however rich.
    """
    if existing is None or incoming is None:
        return False
    if _has_archive(existing) and not _has_archive(incoming):
        return False
    return _body_rank(incoming) > _body_rank(existing)


def _has_archive(note: CanonicalNote) -> bool:
    try:
        return parse_archive_path(note) is not None
    except NoteParseError:
        return True  # an unreadable ## Archive body: assume it holds a path


def _union_profile_relevance(
    content: str,
    existing: CanonicalNote | None,
    incoming: CanonicalNote | None,
    tags: list[str],
) -> str:
    """Rewrite *content*'s ``## Profile Relevance`` with both notes' entries."""
    if existing is None or incoming is None:
        return content
    entries = merge_profile_relevance_union(
        old_entries=parse_profile_relevance(existing),
        new_entries=parse_profile_relevance(incoming),
        tags=tags,
    )
    return replace_profile_relevance_section(content, entries)


def _body_rank(note: CanonicalNote) -> int:
    """0 = summary only, +1 for full text, +1 for Tier 3 content."""
    bodies = {s.heading: s.body.strip() for s in note.sections}
    has_full_text = bool(bodies.get(FULL_TEXT))
    has_tier3 = any(bodies.get(h) for h in TIER3_SECTIONS)
    return int(has_full_text) + int(has_tier3)


def _profiles(tags: Sequence[str]) -> set[str]:
    return {t[len(_PROFILE_PREFIX) :] for t in tags if t.startswith(_PROFILE_PREFIX)}
