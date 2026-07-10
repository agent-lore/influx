"""Shared back-half helpers for the Source note builders.

Once :func:`~influx.sources.arxiv.build_arxiv_note_item`,
:func:`~influx.sources.rss.build_rss_note_item`, and
:func:`~influx.sources.inbox.build_inbox_note_item` have each produced
their source-specific :class:`~influx.cascade.Acquired` bundle,
provenance / archive tags, and enriched ``sections``, the remainder of
the pipeline is identical across all three: the cascade-outcome tag
tail, the Tier-1-aware note render, and the ``ProfileItem`` dict
assembly.  Hoisting those three pieces here keeps the shared shape in
one place so a change lands once instead of drifting across three
copies.  Each adapter keeps only what genuinely differs — fetch /
download, the id + archive-path scheme, the provenance / policy tags,
and arXiv's ``text:*`` provenance tag + Tier-2 extractor.

Finding 2, scope 2.2 (finish the Source seam).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from influx.renderer import render

if TYPE_CHECKING:
    from collections.abc import Iterable

    from influx.cascade import EnrichedSections


def append_cascade_outcome_tags(tags: list[str], sections: EnrichedSections) -> None:
    """Append the cascade-driven outcome tags every builder emits.

    ``influx:deep-extracted`` (when Tier 3 produced content), then the
    cascade's ``repair_flags`` and ``terminal_flags`` — each appended
    only if not already present in ``tags``.  This is the byte-identical
    tail all three builders share after their provenance / archive tags.

    The three source builders never pre-seed ``influx:deep-extracted``
    (this helper is its sole emitter), so guarding it is inert for the
    current callers; the guard keeps the helper's contract uniform —
    "append each outcome tag at most once" — for any future caller.

    ``full-text`` and ``influx:repair-needed`` deliberately stay with
    each caller: their placement relative to the source-specific archive
    tags differs (arXiv / RSS interleave ``influx:repair-needed`` between
    ``full-text`` and this tail; inbox emits it earlier), so hoisting
    them would either reorder tags or need a placement flag.

    Mutates ``tags`` in place, matching the callers' existing style.
    """
    if sections.tier3 is not None and "influx:deep-extracted" not in tags:
        tags.append("influx:deep-extracted")
    for flag in sections.repair_flags:
        if flag not in tags:
            tags.append(flag)
    for flag in sections.terminal_flags:
        if flag not in tags:
            tags.append(flag)


def render_note_content(
    *,
    title: str,
    tags: list[str],
    confidence: float,
    archive_path: str | None,
    summary: str,
    profile_name: str,
    score: int,
    reason: str,
    sections: EnrichedSections,
) -> str:
    """Render the canonical note, applying the shared Tier-1 summary rule.

    When Tier 1 was attempted but failed (``tier1_attempted`` is true and
    ``tier1`` is ``None``), the plain-text summary is suppressed so
    ``## Summary`` is omitted entirely (AC-07-A / FR-ENR-6).  All three
    builders apply this identically before calling
    :func:`influx.renderer.render`; only the *summary* source differs
    (arXiv abstract / RSS extraction-fallback / inbox summary), which the
    caller passes in.
    """
    summary_for_note = (
        "" if sections.tier1_attempted and sections.tier1 is None else summary
    )
    return render(
        title=title,
        tags=tags,
        confidence=confidence,
        archive_path=archive_path,
        summary=summary_for_note,
        profile_name=profile_name,
        score=score,
        reason=reason,
        tier1_enrichment=sections.tier1,
        full_text=sections.full_text,
        tier3_extraction=sections.tier3,
    )


def profile_item_dict(
    *,
    item_id: str,
    title: str,
    source: str,
    source_url: str,
    content: str,
    tags: list[str],
    filter_tags: Iterable[str] | None,
    score: int,
    confidence: float,
    reason: str,
    path: str,
    abstract_or_summary: str,
    sections: EnrichedSections,
) -> dict[str, Any]:
    """Assemble the 14-key ``ProfileItem`` dict the scheduler consumes.

    The dict shape is identical across all three builders; only the id /
    source / path / summary values differ (passed in).  ``filter_tags``
    is normalised to a list (``[]`` when ``None``); ``contributions`` and
    ``builds_on`` are derived from the cascade ``sections`` the same way
    everywhere.
    """
    return {
        "id": item_id,
        "title": title,
        "source": source,
        "source_url": source_url,
        "content": content,
        "tags": tags,
        "filter_tags": list(filter_tags) if filter_tags is not None else [],
        "score": score,
        "confidence": confidence,
        "reason": reason,
        "path": path,
        "abstract_or_summary": abstract_or_summary,
        "contributions": sections.tier1.contributions if sections.tier1 else None,
        "builds_on": list(sections.tier3.builds_on) if sections.tier3 else None,
    }
