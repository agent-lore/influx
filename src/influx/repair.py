"""Repair sweep — durable retry via Lithos tags (PRD 06 §5).

Drives per-profile repair by fetching ``influx:repair-needed`` notes
oldest-``updated_at``-first, independently selecting retry stages
(archive download, text extraction, abstract-only re-extraction,
Tier 2, Tier 3) based on the current tag set, and rewriting every
visited note so ``updated_at`` advances (retry-order advancement).

Worker hooks (``re_extract_archive``, ``tier2_enrich``,
``tier3_extract``) are test-injectable callables whose real
implementations ship with PRD 07.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from influx.config import AppConfig
    from influx.lithos_client import LithosClient

__all__ = [
    "ExtractionOutcome",
    "ReExtractionResult",
    "ReExtractArchiveHook",
    "Tier2EnrichHook",
    "Tier3ExtractHook",
    "sweep",
]

logger = logging.getLogger(__name__)


# ── Abstract-only re-extraction outcome discriminator ────────────────


class ExtractionOutcome(enum.Enum):
    """Discriminator for the three abstract-only re-extraction outcomes.

    Used by the ``re_extract_archive`` hook to communicate what happened
    when re-extracting text from an already-archived document whose
    current text quality is ``text:abstract-only``.

    Values
    ------
    UPGRADE
        Extraction yielded ``text:html`` or ``text:pdf`` — strictly
        better than abstract-only.  The sweep replaces
        ``text:abstract-only`` with the upgraded tag.
        ``influx:text-terminal`` is NOT added.
    TERMINAL
        Extraction completed successfully but still yielded
        abstract-quality text.  The sweep keeps ``text:abstract-only``
        and adds ``influx:text-terminal``.
    TRANSIENT
        Extraction failed after its retry budget this pass (network
        error, transient service failure, etc.).  The sweep keeps
        ``text:abstract-only`` and ``influx:repair-needed`` WITHOUT
        adding ``influx:text-terminal``.  The note re-enters the
        sweep on a later run.
    """

    UPGRADE = "upgrade"
    TERMINAL = "terminal"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class ReExtractionResult:
    """Return value of the ``re_extract_archive`` hook.

    Parameters
    ----------
    outcome:
        One of the three :class:`ExtractionOutcome` variants.
    upgraded_text_tag:
        The replacement ``text:*`` tag when *outcome* is
        :attr:`ExtractionOutcome.UPGRADE` (e.g. ``"text:html"`` or
        ``"text:pdf"``).  Must be non-empty for UPGRADE; ignored for
        TERMINAL and TRANSIENT.
    """

    outcome: ExtractionOutcome
    upgraded_text_tag: str = ""


# ── Hook protocols ───────────────────────────────────────────────────


class ReExtractArchiveHook(Protocol):
    """Callable protocol for abstract-only re-extraction (PRD 06 §4).

    Called by the sweep when a ``text:abstract-only`` note (without
    ``influx:text-terminal``) has an available archive path.  The
    implementation attempts to re-extract text from the archived
    document and returns a :class:`ReExtractionResult` discriminating
    the three outcomes.

    Parameters
    ----------
    note:
        The current note state (as returned by ``lithos_read``).
    archive_path:
        The relative POSIX path to the archived document.

    Returns
    -------
    ReExtractionResult
        The outcome of the re-extraction attempt.

    Raises
    ------
    ExtractionError
        On extraction failure — treated as a Transient outcome by
        the sweep.
    LithosError
        On Lithos API failure — propagated to the sweep's error
        handling.
    """

    def __call__(
        self,
        note: dict[str, object],
        archive_path: str,
    ) -> ReExtractionResult: ...


class Tier2EnrichHook(Protocol):
    """Callable protocol for Tier 2 enrichment retry (PRD 06 §4).

    Called by the sweep when a note is missing ``full-text``, the
    current max profile score meets the threshold, and
    ``influx:text-terminal`` is absent.

    The implementation performs full-text enrichment and updates the
    note accordingly.  On success, the note should carry ``full-text``
    after the sweep's rewrite.

    Parameters
    ----------
    note:
        The current note state (as returned by ``lithos_read``).

    Raises
    ------
    ExtractionError
        On enrichment failure — the sweep treats this as "stage
        failed this pass" and keeps ``influx:repair-needed``.
    LithosError
        On Lithos API failure — propagated to the sweep's error
        handling.
    """

    def __call__(self, note: dict[str, object]) -> None: ...


class Tier3ExtractHook(Protocol):
    """Callable protocol for Tier 3 deep extraction retry (PRD 06 §4).

    Called by the sweep when a note is missing
    ``influx:deep-extracted``, the current max profile score meets the
    threshold, and ``influx:text-terminal`` is absent.

    The implementation performs deep extraction and updates the note
    accordingly.  On success, the note should carry
    ``influx:deep-extracted`` after the sweep's rewrite.

    Parameters
    ----------
    note:
        The current note state (as returned by ``lithos_read``).

    Raises
    ------
    ExtractionError
        On extraction failure — the sweep treats this as "stage
        failed this pass" and keeps ``influx:repair-needed``.
    LithosError
        On Lithos API failure — propagated to the sweep's error
        handling.
    """

    def __call__(self, note: dict[str, object]) -> None: ...


# ── Sweep entry point ──────────────────────────────────────────────


async def sweep(
    profile: str,
    *,
    client: LithosClient,
    config: AppConfig,
) -> list[dict[str, Any]]:
    """Run the repair sweep for *profile* (PRD 06 §5.1 FR-REP-1).

    Fetches up to ``repair.max_items_per_run`` notes tagged
    ``influx:repair-needed`` + ``profile:<profile>``, ordered by
    ``updated_at`` ascending (oldest first).  Each candidate is
    re-read via ``lithos_read`` before further processing.

    Returns the list of re-read note dicts so downstream stages
    (US-005+) can hang per-note logic off this loop.
    """
    limit = config.repair.max_items_per_run
    list_result = await client.list_notes(
        tags=["influx:repair-needed", f"profile:{profile}"],
        limit=limit,
        order_by="updated_at",
        order="asc",
    )

    text = list_result.content[0].text  # type: ignore[union-attr]
    body = json.loads(text)
    items: list[dict[str, Any]] = body.get("items", [])

    if not items:
        logger.debug("repair sweep for %r: no candidates found", profile)
        return []

    logger.info(
        "repair sweep for %r: visiting %d candidate(s)",
        profile,
        len(items),
    )

    visited: list[dict[str, Any]] = []
    for item in items:
        note_id = item.get("id", "")
        if not note_id:
            continue
        note = await client.read_note(note_id=note_id)
        visited.append(note)

    return visited
