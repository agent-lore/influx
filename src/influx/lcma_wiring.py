"""Post-write LCMA wiring (CONTEXT.md ``LcmaWiring``).

The LcmaWiring module owns the post-``lithos_write`` graph-edge dispatch
the scheduler used to inline:

- Calling ``lithos_retrieve`` with the title-plus-contributions query
  (FR-LCMA-2) and scoring results against ``thresholds.lcma_edge_score``
  to upsert ``related_to`` edges (FR-LCMA-3, AC-M2-5/6).
- Resolving Tier 3 ``builds_on`` entries via ``lithos_cache_lookup`` and
  upserting ``builds_on`` edges only on exact ``source_url`` match
  (FR-LCMA-4, AC-M2-7/8).

LCMA tool-availability checking has moved out of the per-call latch
path (issue #69).  The probe loop now asserts the LCMA tool surface
once per probe interval and gates the run with
``reason="lcma_tools_unavailable"``; mid-run ``LCMAError("unknown_tool")``
propagates as a normal failure here, and the next probe cycle
re-evaluates the latch.

The actual retrieve / cache-lookup / edge-upsert primitives still live
in :mod:`influx.lcma`.  This module is the seam the scheduler (and,
later, the ``Run`` module) calls into after each successful write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from influx.lcma import after_write, resolve_builds_on

if TYPE_CHECKING:
    from influx.lithos_client import LithosClient

__all__ = [
    "CascadeOutput",
    "LcmaWiringDeps",
    "wire",
]

logger = logging.getLogger(__name__)


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
        edge-score threshold).

    Returns
    -------
    list[dict[str, Any]]
        The high-scoring ``lithos_retrieve`` results for the webhook
        digest's ``related_in_lithos`` field — see
        :func:`influx.lcma.after_write` for the dict shape.

    Raises
    ------
    LCMAError
        Propagated verbatim.  ``unknown_tool`` failures are no longer
        latched here — the probe loop's tool-availability check
        (issue #69) drives the latch and the next probe cycle
        re-evaluates.
    """
    related = await after_write(
        client=deps.client,
        title=cascade.title,
        contributions=cascade.contributions,
        run_task_id=deps.run_task_id,
        profile=deps.profile,
        lcma_edge_score=deps.lcma_edge_score,
        source_note_id=written_note_id,
    )
    await resolve_builds_on(
        client=deps.client,
        builds_on=cascade.builds_on,
        source_note_id=written_note_id,
    )
    return related
