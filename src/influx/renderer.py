"""Canonical Lithos note renderer (CONTEXT.md Renderer module).

The Renderer produces a CanonicalNote from one Acquired item plus its
EnrichedSections plus the score/reason. It owns the canonical Markdown
shape from spec §9 (frontmatter, section ordering, omitted-section
rules) and the byte-exact ``## User Notes`` preservation rule (R-5).

Public surface:

- :func:`render` — high-level facade for source builders. Wraps a
  single-profile :class:`ProfileRelevanceEntry` and delegates to
  :func:`render_note`.
- :func:`render_note` — full renderer used directly by repair / rewrite
  paths that need to control profile-relevance composition.
- :func:`render_archive_section`, :func:`validate_archive_tag_invariant`
  — Archive-section helpers (FR-NOTE-9).
- :func:`build_profile_relevance_for_rewrite`,
  :func:`merge_profile_relevance_union` — profile-relevance merge
  helpers used during rewrites (FR-NOTE-6).
"""

from __future__ import annotations

from influx.canonical_note import (
    ARCHIVE,
    BUILDS_ON,
    CLAIMS,
    DATASETS,
    FULL_TEXT,
    OPEN_QUESTIONS,
    PROFILE_RELEVANCE,
    SUMMARY,
    CanonicalNote,
    ProfileRelevanceEntry,
    Section,
    render_profile_relevance_body,
    serialize,
)
from influx.errors import InfluxError
from influx.schemas import Tier1Enrichment, Tier3Extraction

__all__ = [
    "ArchiveInvariantError",
    "MissingIngestedByTagError",
    "ProfileRelevanceEntry",
    "build_profile_relevance_for_rewrite",
    "merge_profile_relevance_union",
    "render",
    "render_archive_section",
    "render_note",
    "validate_archive_tag_invariant",
]

# ``ProfileRelevanceEntry`` and the profile-relevance body renderer now live
# in :mod:`influx.canonical_note` (the CanonicalNote shape owner).  Re-export
# the value type and keep the private-name alias so existing callers that
# import ``_render_profile_relevance_body`` from the renderer are undisturbed.
_render_profile_relevance_body = render_profile_relevance_body


# ── Exceptions ───────────────────────────────────────────────────────


class ArchiveInvariantError(InfluxError):
    """Raised when a path: line and influx:archive-missing co-exist."""


class MissingIngestedByTagError(InfluxError):
    """Raised when an Influx-authored note lacks ``ingested-by:influx`` (FR-RES-6)."""


# ── Archive section render / invariant (FR-NOTE-9) ──────────────────


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
    body = _archive_body(archive_path)
    return f"## {ARCHIVE}\n{body}\n" if body else f"## {ARCHIVE}\n"


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


# ── Section-body builders (compose the CanonicalNote model) ─────────


def _archive_body(archive_path: str | None) -> str:
    """Body of the ``## Archive`` section — the ``path:`` line, or empty."""
    return f"path: {archive_path}" if archive_path is not None else ""


def _summary_body(
    tier1_enrichment: Tier1Enrichment | None,
    summary: str,
    keywords: list[str],
) -> str | None:
    """Body of the ``## Summary`` section, or ``None`` to omit it (FR-ENR-6).

    Structured Tier-1 enrichment wins (``### Contributions`` / ``### Method``
    / ``### Result`` / ``### Relevance``); else the plain *summary* (with a
    ``Keywords:`` line when present); else the section is omitted.
    """
    if tier1_enrichment is not None:
        parts = ["### Contributions"]
        parts.extend(f"- {contrib}" for contrib in tier1_enrichment.contributions)
        parts.append(f"\n### Method\n{tier1_enrichment.method}")
        parts.append(f"\n### Result\n{tier1_enrichment.result}")
        parts.append(f"\n### Relevance\n{tier1_enrichment.relevance}")
        return "\n".join(parts)
    if summary:
        body = summary
        if keywords:
            body += f"\n\nKeywords: {', '.join(keywords)}"
        return body
    return None


def _tier3_sections(tier3: Tier3Extraction) -> list[Section]:
    """The four Tier 3 sections as ``Section`` values (US-012)."""

    def _bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    return [
        Section(CLAIMS, _bullets(tier3.claims)),
        Section(DATASETS, _bullets(tier3.datasets)),
        Section(BUILDS_ON, _bullets(tier3.builds_on)),
        Section(OPEN_QUESTIONS, _bullets(tier3.open_questions)),
    ]


# ── Full canonical renderer (FR-NOTE-1..8, US-007) ──────────────────


def render_note(
    *,
    title: str,
    tags: list[str],
    confidence: float,
    archive_path: str | None,
    summary: str,
    keywords: list[str],
    profile_entries: list[ProfileRelevanceEntry],
    tier1_enrichment: Tier1Enrichment | None = None,
    full_text: str | None = None,
    tier3_extraction: Tier3Extraction | None = None,
    user_notes: str | None = None,
) -> str:
    """Render a full canonical Lithos note (FR-NOTE-1..8).

    Parameters
    ----------
    title:
        The ``# <Title>`` text (without the ``# `` prefix).
    tags:
        The final merged tag list (from ``influx.notes.merge_tags``).
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
    tier1_enrichment:
        Tier-1 structured enrichment result. When provided, emits ``## Summary``
        containing ``### Contributions``, ``### Method``, ``### Result``,
        ``### Relevance`` sub-blocks. When ``None`` falls back to the plain
        *summary* string; when both are absent the section is omitted (FR-ENR-6).
    full_text:
        Tier-2 extracted plain text for the ``## Full Text`` section.
        When ``None`` or empty the section is omitted entirely (FR-ENR-6).
    tier3_extraction:
        Tier-3 deep extraction result. When provided, emits ``## Claims``,
        ``## Datasets & Benchmarks``, ``## Builds On``, ``## Open Questions``
        sections. ``potential_connections`` is NOT rendered (consumed by
        PRD 08 LCMA only). When ``None`` all four sections are omitted
        (FR-ENR-6).
    user_notes:
        Byte-exact ``## User Notes`` region from a previous parse,
        or ``None`` to append an empty ``## User Notes`` heading.

    Returns
    -------
    str
        The complete canonical note text.  The note is composed as a
        :class:`~influx.canonical_note.CanonicalNote` and emitted via
        :func:`~influx.canonical_note.serialize`, so the output is always in
        canonical form — single blank-line separators, with section bodies
        stripped of trailing line endings (``\\r``/``\\n``).  Trailing spaces
        and tabs are *not* stripped (matching the parser's canonical form, so
        such bodies still round-trip).  This is byte-identical to the
        historical imperative rendering for bodies that do not end in a
        newline; a body that *does* end in one or more newlines (e.g. a
        summary / full-text / reason with a trailing ``\\n``) has them
        collapsed rather than emitted as a double blank line.  The
        ``## User Notes`` region is still appended byte-exactly.

    Raises
    ------
    ArchiveInvariantError
        When *archive_path* is not ``None`` and *tags* contains
        ``influx:archive-missing``.
    MissingIngestedByTagError
        When *tags* does not contain ``ingested-by:influx`` (FR-RES-6).
    """
    if "ingested-by:influx" not in tags:
        raise MissingIngestedByTagError(
            "Influx-authored notes must carry the 'ingested-by:influx' tag (FR-RES-6)"
        )
    validate_archive_tag_invariant(archive_path=archive_path, tags=tags)

    # Compose the note as a CanonicalNote and serialise it — body-only output
    # (#178).  Lithos owns the outer YAML frontmatter and persists tags /
    # source_url / confidence / note_type / namespace as first-class
    # ``lithos_write`` API parameters (spec §5.1); the renderer never emits a
    # ``---``-fenced block of its own (LithosClient.write_note enforces this).
    # The ``# {title}`` H1 is retained as the body's canonical title.
    sections: list[Section] = [Section(ARCHIVE, _archive_body(archive_path))]

    summary_body = _summary_body(tier1_enrichment, summary, keywords)
    if summary_body is not None:
        sections.append(Section(SUMMARY, summary_body))

    # Full Text (Tier 2) — omitted when absent/empty (FR-ENR-6, US-011).
    if full_text:
        sections.append(Section(FULL_TEXT, full_text))

    # Tier 3 sections (US-012) — omitted entirely when absent (FR-ENR-6).
    if tier3_extraction is not None:
        sections.extend(_tier3_sections(tier3_extraction))

    # Profile Relevance — always emitted to keep the shape stable (US-007);
    # the body is empty when no entries are given.
    sections.append(
        Section(PROFILE_RELEVANCE, render_profile_relevance_body(profile_entries))
    )

    # Strip trailing line endings (\r/\n) from every body — matching the
    # parser's canonical form — so serialize() emits single blank-line
    # separators (see the Returns note on normalisation).  Trailing spaces are
    # preserved; the ## User Notes region is appended byte-exactly.
    note = CanonicalNote(
        frontmatter_raw="",
        title=title,
        sections=tuple(Section(s.heading, s.body.rstrip("\r\n")) for s in sections),
        user_notes=user_notes,
    )
    return serialize(note)


# ── High-level facade for source builders ──────────────────────────


def render(
    *,
    title: str,
    tags: list[str],
    confidence: float,
    archive_path: str | None,
    summary: str,
    profile_name: str,
    score: int,
    reason: str,
    keywords: list[str] | None = None,
    tier1_enrichment: Tier1Enrichment | None = None,
    full_text: str | None = None,
    tier3_extraction: Tier3Extraction | None = None,
    user_notes: str | None = None,
) -> str:
    """Render a CanonicalNote for one Acquired item from a single Source.

    The Source-builder facade that wraps a single-profile relevance
    entry and delegates to :func:`render_note`. Source builders that
    have applied their own summary-suppression rule (e.g. AC-07-A:
    suppress fallback summary when Tier 1 was attempted but failed)
    pass an empty *summary* string here.

    Parameters
    ----------
    title, tags, confidence, archive_path, summary, keywords,
    tier1_enrichment, full_text, tier3_extraction, user_notes:
        Forwarded to :func:`render_note`.
    profile_name, score, reason:
        Used to construct a single-entry profile relevance list.

    Returns
    -------
    str
        The complete canonical note text.
    """
    profile_entries = [
        ProfileRelevanceEntry(
            profile_name=profile_name,
            score=score,
            reason=reason,
        ),
    ]
    return render_note(
        title=title,
        tags=tags,
        confidence=confidence,
        archive_path=archive_path,
        summary=summary,
        keywords=keywords if keywords is not None else [],
        profile_entries=profile_entries,
        tier1_enrichment=tier1_enrichment,
        full_text=full_text,
        tier3_extraction=tier3_extraction,
        user_notes=user_notes,
    )


# ── Profile relevance merge helpers (FR-NOTE-6) ─────────────────────


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
        t[len("influx:rejected:") :] for t in tags if t.startswith("influx:rejected:")
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


def merge_profile_relevance_union(
    *,
    old_entries: list[ProfileRelevanceEntry],
    new_entries: list[ProfileRelevanceEntry],
    tags: list[str],
) -> list[ProfileRelevanceEntry]:
    """Union-merge profile relevance entries for multi-profile merging (FR-NOTE-6).

    Unlike :func:`build_profile_relevance_for_rewrite` — which drops
    old entries for non-rejected, non-new profiles (correct for
    single-profile rewrites and repair sweeps) — this function
    preserves old entries for ALL profiles not superseded by new
    entries, enabling multi-profile tag merging on shared notes.

    Rejected profiles (``influx:rejected:<profile>`` in *tags*) always
    keep their old entries; new entries for rejected profiles are
    dropped.

    Parameters
    ----------
    old_entries:
        Entries from the previously existing note.
    new_entries:
        Entries from the incoming write (typically one profile).
    tags:
        The final merged tag list (used to detect rejection guards).

    Returns
    -------
    list[ProfileRelevanceEntry]
        The merged entries to render.
    """
    rejected = {
        t[len("influx:rejected:") :] for t in tags if t.startswith("influx:rejected:")
    }

    result: list[ProfileRelevanceEntry] = []
    seen: set[str] = set()

    # Non-rejected profiles use new entries
    for entry in new_entries:
        if entry.profile_name not in rejected:
            result.append(entry)
            seen.add(entry.profile_name)

    # ALL old entries for profiles not yet in result (union semantics):
    # - Rejected profiles: keep old entry (rejection authority)
    # - Non-current profiles: keep old entry (multi-profile preservation)
    for entry in old_entries:
        if entry.profile_name not in seen:
            result.append(entry)
            seen.add(entry.profile_name)

    return result
