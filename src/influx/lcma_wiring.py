"""Post-write LCMA wiring (CONTEXT.md ``LcmaWiring``).

The LcmaWiring module owns the post-``lithos_write`` graph-edge dispatch
the scheduler used to inline:

- Calling ``lithos_retrieve`` with the title-plus-contributions query
  (FR-LCMA-2) and scoring results against ``thresholds.lcma_edge_score``
  to upsert ``related_to`` edges (FR-LCMA-3, AC-M2-5/6).
- Resolving Tier 3 ``builds_on`` entries via ``lithos_cache_lookup`` and
  upserting ``builds_on`` edges only on exact ``source_url`` match
  (FR-LCMA-4, AC-M2-7/8).
- Latching the ``lcma_unknown_tool_failure`` probe flag (FR-LCMA-6) when
  Lithos returns ``unknown_tool`` for any of those calls.

The actual retrieve / cache-lookup / edge-upsert primitives still live
in :mod:`influx.lcma`.  This module is the seam the scheduler (and,
later, the ``Run`` module) calls into after each successful write.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from influx.errors import LCMAError
from influx.lcma import after_write, resolve_builds_on

if TYPE_CHECKING:
    from influx.lithos_client import LithosClient

__all__ = [
    "CascadeOutput",
    "LcmaUnknownToolLatch",
    "LcmaWiringDeps",
    "wire",
]

logger = logging.getLogger(__name__)


# Latch callback shape — typically ``ProbeLoop.mark_lcma_unknown_tool_failure``
# (kwargs: ``profile``, ``detail``).  Any callable accepting those kwargs
# is acceptable so unit tests can pass a plain capturing fake.
LcmaUnknownToolLatch = Callable[..., None]


@dataclass(frozen=True, slots=True)
class CascadeOutput:
    """Subset of one item's enrichment output the LCMA wiring consumes.

    The fields mirror the cascade keys ``build_*_note_item`` populate on
    the ``ProfileItem`` dict today.  When the Cascade module lands the
    real ``EnrichedSections`` value (CONTEXT.md), its public reading
    helpers will return this same shape so callers do not need to
    re-thread fields by hand.
    """

    title: str
    contributions: list[str] | None = None
    builds_on: list[str] | None = None


@dataclass(frozen=True, slots=True)
class LcmaWiringDeps:
    """Per-run dependencies the LCMA wiring needs to dispatch.

    Bundled into one value so the scheduler can build the deps once per
    profile run and pass them to :func:`wire` for every written note.
    """

    client: LithosClient
    profile: str
    run_task_id: str
    lcma_edge_score: float
    on_unknown_tool: LcmaUnknownToolLatch | None = field(default=None)


# ── Entry point ─────────────────────────────────────────────────────


async def wire(
    *,
    written_note_id: str,
    cascade: CascadeOutput,
    deps: LcmaWiringDeps,
) -> list[dict[str, Any]]:
    """Run the post-write LCMA wiring for one just-written note.

    Parameters
    ----------
    written_note_id:
        The ``note_id`` returned by the successful ``lithos_write`` —
        becomes the ``source_note_id`` on every edge_upsert.
    cascade:
        Title plus the Tier 1 ``contributions`` and Tier 3 ``builds_on``
        from the cascade output for this item.
    deps:
        Per-run wiring dependencies (Lithos client, profile, run task,
        edge-score threshold, optional unknown-tool latch).

    Returns
    -------
    list[dict[str, Any]]
        The high-scoring ``lithos_retrieve`` results for the webhook
        digest's ``related_in_lithos`` field — see
        :func:`influx.lcma.after_write` for the dict shape.

    Raises
    ------
    LCMAError
        Propagated verbatim after latching ``lcma_unknown_tool_failure``
        on ``unknown_tool``; other errors propagate unchanged.
    """
    related: list[dict[str, Any]] = []
    try:
        related = await after_write(
            client=deps.client,
            title=cascade.title,
            contributions=cascade.contributions,
            run_task_id=deps.run_task_id,
            profile=deps.profile,
            lcma_edge_score=deps.lcma_edge_score,
            source_note_id=written_note_id,
        )
    except LCMAError as exc:
        _maybe_latch_unknown_tool(
            exc=exc,
            profile=deps.profile,
            fallback_tool="lithos_retrieve",
            on_unknown_tool=deps.on_unknown_tool,
        )
        raise

    try:
        await resolve_builds_on(
            client=deps.client,
            builds_on=cascade.builds_on,
            source_note_id=written_note_id,
        )
    except LCMAError as exc:
        _maybe_latch_unknown_tool(
            exc=exc,
            profile=deps.profile,
            fallback_tool="lithos_cache_lookup",
            on_unknown_tool=deps.on_unknown_tool,
        )
        raise

    return related


# ── Unknown-tool latch helper ──────────────────────────────────────


def _maybe_latch_unknown_tool(
    *,
    exc: LCMAError,
    profile: str,
    fallback_tool: str,
    on_unknown_tool: LcmaUnknownToolLatch | None,
) -> None:
    """Log + latch readiness when *exc* is an LCMA ``unknown_tool`` failure.

    Mirrors the original ``_handle_lcma_unknown_tool`` behaviour from the
    scheduler: an ERROR log line names the offending tool, and the
    optional latch callback flips the probe flag so ``/ready`` reports
    degraded.  Other LCMAError values are no-ops here so the caller can
    re-raise unchanged.
    """
    if str(exc) != "unknown_tool":
        return

    tool = getattr(exc, "stage", "") or fallback_tool
    logger.error(
        "LCMA deployment error: unknown_tool for %s during profile %r run "
        "— aborting run. Check that the connected Lithos deployment "
        "supports the required LCMA tools.",
        tool,
        profile,
    )
    if on_unknown_tool is not None:
        on_unknown_tool(profile=profile, detail=f"tool={tool!r}")
