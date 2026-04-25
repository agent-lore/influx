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
    "ClearingDecision",
    "ExtractionOutcome",
    "ReExtractionResult",
    "ReExtractArchiveHook",
    "StageSelection",
    "Tier2EnrichHook",
    "Tier3ExtractHook",
    "compute_clearing",
    "select_stages",
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


# ── Per-note stage selection (§5.2) ────────────────────────────────


@dataclass(frozen=True, slots=True)
class StageSelection:
    """Per-note stage selection result (PRD 06 §5.2).

    Each boolean indicates whether the corresponding retry stage
    should be exercised for this note in the current sweep pass.
    """

    archive_retry: bool = False
    text_extraction_retry: bool = False
    abstract_only_reextraction: bool = False
    tier2_retry: bool = False
    tier3_retry: bool = False


def select_stages(
    *,
    tags: list[str],
    archive_path: str | None,
    archive_succeeded_this_pass: bool = False,
    max_profile_score: int,
    full_text_threshold: int,
    deep_extract_threshold: int,
) -> StageSelection:
    """Select retry stages for a single note (PRD 06 §5.2).

    Each stage is independently selected based on the note's
    current tag set and profile score thresholds.

    Parameters
    ----------
    tags:
        Current tags on the note (from ``lithos_read``).
    archive_path:
        The archive path from the note's ``## Archive`` section,
        or ``None`` if no ``path:`` line is stored.
    archive_succeeded_this_pass:
        Whether the archive download stage succeeded during this
        sweep pass.  Used for abstract-only re-extraction
        eligibility.
    max_profile_score:
        The maximum profile score across profile entries on this
        note.
    full_text_threshold:
        ``thresholds.full_text`` from the profile config.
    deep_extract_threshold:
        ``thresholds.deep_extract`` from the profile config.
    """
    tag_set = set(tags)
    is_text_terminal = "influx:text-terminal" in tag_set

    # 1. Archive retry: influx:archive-missing present (AC-06-A).
    archive_retry = "influx:archive-missing" in tag_set

    # 2. Text-extraction retry: no text:* tag present.
    has_text_tag = any(t.startswith("text:") for t in tag_set)
    text_extraction_retry = not has_text_tag

    # 3. Abstract-only re-extraction: text:abstract-only AND NOT
    #    influx:text-terminal AND (archive succeeded this pass OR
    #    archive path already stored).
    abstract_only_reextraction = (
        "text:abstract-only" in tag_set
        and not is_text_terminal
        and (archive_succeeded_this_pass or archive_path is not None)
    )

    # 4. Tier 2 retry: full-text missing AND score >= threshold AND
    #    NOT terminal.
    tier2_retry = (
        "full-text" not in tag_set
        and max_profile_score >= full_text_threshold
        and not is_text_terminal
    )

    # 5. Tier 3 retry: influx:deep-extracted missing AND score >=
    #    threshold AND NOT terminal.
    tier3_retry = (
        "influx:deep-extracted" not in tag_set
        and max_profile_score >= deep_extract_threshold
        and not is_text_terminal
    )

    return StageSelection(
        archive_retry=archive_retry,
        text_extraction_retry=text_extraction_retry,
        abstract_only_reextraction=abstract_only_reextraction,
        tier2_retry=tier2_retry,
        tier3_retry=tier3_retry,
    )


# ── Post-stage tag clearing (§5.3) ─────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClearingDecision:
    """Post-stage tag-clearing decision (PRD 06 §5.3).

    Each boolean indicates whether the corresponding tag should be
    removed from the note's tag set during the rewrite step.
    """

    clear_archive_missing: bool = False
    clear_repair_needed: bool = False


def compute_clearing(
    *,
    tags: list[str],
    archive_path: str | None,
    max_profile_score: int,
    full_text_threshold: int,
    deep_extract_threshold: int,
) -> ClearingDecision:
    """Decide which tags to clear after stage execution (PRD 06 §5.3).

    Called after all selected stages have run for a single note.
    The *tags* parameter represents the note's tag set after stage
    execution has potentially modified it (e.g. upgraded
    ``text:abstract-only`` → ``text:html``).

    Parameters
    ----------
    tags:
        The note's tag set after all stages have run this pass.
    archive_path:
        The archive path after potential archive retry, or ``None``
        when no ``path:`` line is stored in ``## Archive``.
    max_profile_score:
        Maximum profile score across profile entries on the note.
    full_text_threshold:
        ``thresholds.full_text`` from the profile config.
    deep_extract_threshold:
        ``thresholds.deep_extract`` from the profile config.

    Returns
    -------
    ClearingDecision
        Which tags to remove.  The caller should strip the indicated
        tags from the Influx-owned tag set before the ``lithos_write``
        rewrite.
    """
    tag_set = set(tags)
    is_text_terminal = "influx:text-terminal" in tag_set

    # FR-NOTE-9: clear influx:archive-missing iff archive path stored.
    clear_archive_missing = archive_path is not None

    # §5.3: clear influx:repair-needed iff ALL four conditions hold.

    # (a) Non-empty path: line in ## Archive.
    archive_ok = archive_path is not None

    # (b) Text quality: text:html or text:pdf, OR (text:abstract-only
    #     accompanied by influx:text-terminal).
    #     AC-06-C: text:abstract-only WITHOUT influx:text-terminal
    #     → NEVER clear influx:repair-needed.
    text_ok = (
        "text:html" in tag_set
        or "text:pdf" in tag_set
        or ("text:abstract-only" in tag_set and is_text_terminal)
    )

    # (c) Tier 2 satisfied: only required when score ≥ full_text
    #     threshold AND influx:text-terminal absent.
    #     AC-X-7: terminal exemption waives Tier 2.
    if is_text_terminal or max_profile_score < full_text_threshold:
        tier2_ok = True
    else:
        tier2_ok = "full-text" in tag_set

    # (d) Tier 3 satisfied: only required when score ≥ deep_extract
    #     threshold AND influx:text-terminal absent.
    #     AC-X-7: terminal exemption waives Tier 3.
    if is_text_terminal or max_profile_score < deep_extract_threshold:
        tier3_ok = True
    else:
        tier3_ok = "influx:deep-extracted" in tag_set

    clear_repair_needed = archive_ok and text_ok and tier2_ok and tier3_ok

    return ClearingDecision(
        clear_archive_missing=clear_archive_missing,
        clear_repair_needed=clear_repair_needed,
    )


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
