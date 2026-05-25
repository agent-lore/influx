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

from dataclasses import dataclass

from influx.errors import InfluxError
from influx.schemas import Tier1Enrichment, Tier3Extraction
from influx.urls import normalise_url

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


# ── Exceptions ───────────────────────────────────────────────────────


class ArchiveInvariantError(InfluxError):
    """Raised when a path: line and influx:archive-missing co-exist."""


class MissingIngestedByTagError(InfluxError):
    """Raised when an Influx-authored note lacks ``ingested-by:influx`` (FR-RES-6)."""


# ── Profile relevance entry ─────────────────────────────────────────


@dataclass(frozen=True)
class ProfileRelevanceEntry:
    """One profile's relevance data for the ``## Profile Relevance`` section."""

    profile_name: str
    score: int
    reason: str


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
    if archive_path is not None:
        return f"## Archive\npath: {archive_path}\n"
    return "## Archive\n"


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


# ── Frontmatter / profile-relevance body helpers ────────────────────


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
    """Render the YAML frontmatter content (between ``---`` fences).

    ``source_url`` is normalised via :func:`influx.urls.normalise_url`
    so that frontmatter always carries the canonical form (FR-MCP-4).
    """
    lines = [
        "note_type: summary",
        "namespace: influx",
        f"source_url: {normalise_url(source_url)}",
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
            f"### {entry.profile_name}\nScore: {entry.score}/10\n{entry.reason}"
        )
    return "\n\n".join(parts)


# ── Full canonical renderer (FR-NOTE-1..8, US-007) ──────────────────


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
    source_url:
        Normalised canonical URL for frontmatter.
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
        The complete canonical note text.

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

    frontmatter = _render_frontmatter(
        source_url=source_url,
        tags=tags,
        confidence=confidence,
    )
    archive_section = render_archive_section(archive_path)

    # Compose note
    output = f"---\n{frontmatter}\n---\n"
    output += f"# {title}\n\n"
    output += archive_section + "\n"

    # Summary section: structured Tier1Enrichment → plain summary → omit
    if tier1_enrichment is not None:
        output += "## Summary\n"
        output += "### Contributions\n"
        for contrib in tier1_enrichment.contributions:
            output += f"- {contrib}\n"
        output += f"\n### Method\n{tier1_enrichment.method}\n"
        output += f"\n### Result\n{tier1_enrichment.result}\n"
        output += f"\n### Relevance\n{tier1_enrichment.relevance}\n"
    elif summary:
        summary_body = summary
        if keywords:
            summary_body += f"\n\nKeywords: {', '.join(keywords)}"
        output += f"## Summary\n{summary_body}\n"

    # Full Text section (Tier 2) — omitted when absent/empty (FR-ENR-6, US-011)
    if full_text:
        output += f"\n## Full Text\n{full_text}\n"

    # Tier 3 sections (US-012) — omitted entirely when absent (FR-ENR-6)
    if tier3_extraction is not None:
        output += "\n## Claims\n"
        for claim in tier3_extraction.claims:
            output += f"- {claim}\n"
        output += "\n## Datasets & Benchmarks\n"
        for ds in tier3_extraction.datasets:
            output += f"- {ds}\n"
        output += "\n## Builds On\n"
        for item in tier3_extraction.builds_on:
            output += f"- {item}\n"
        output += "\n## Open Questions\n"
        for q in tier3_extraction.open_questions:
            output += f"- {q}\n"

    # Profile Relevance section — always emitted to keep the canonical
    # note shape stable (US-007); body is empty when no entries are given.
    output += "\n"
    pr_body = _render_profile_relevance_body(profile_entries)
    if pr_body:
        output += f"## Profile Relevance\n{pr_body}\n"
    else:
        output += "## Profile Relevance\n"

    # User Notes — preserved byte-exactly or empty heading appended
    output += "\n"
    if user_notes is not None:
        output += user_notes
    else:
        output += "## User Notes\n"

    return output


# ── High-level facade for source builders ──────────────────────────


def render(
    *,
    title: str,
    source_url: str,
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
    title, source_url, tags, confidence, archive_path, summary,
    keywords, tier1_enrichment, full_text, tier3_extraction, user_notes:
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
        source_url=source_url,
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
