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

import asyncio
import contextlib
import copy
import enum
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from influx import metrics
from influx.canonical_note import (
    drop_tier2,
    drop_tier2_and_tier3,
    graft_user_notes,
    parse_lenient,
    upsert_archive_path,
)
from influx.errors import ExtractionError, LCMAError, LithosError
from influx.notes import merge_tags
from influx.repair_counters import (
    CountedStage,
    classify_failure,
    record_counted_failure,
)
from influx.telemetry import current_run_id, get_tracer

if TYPE_CHECKING:
    from influx.config import AppConfig
    from influx.lithos_client import LithosClient

__all__ = [
    "ArchiveDownloadHook",
    "ClearingDecision",
    "ContentTooLargeSkipped",
    "ExtractionOutcome",
    "ReExtractionResult",
    "ReExtractArchiveHook",
    "StageSelection",
    "SweepHooks",
    "SweepWriteError",
    "TextExtractionHook",
    "Tier2EnrichHook",
    "Tier3ExtractHook",
    "apply_abstract_only_reextraction",
    "compute_clearing",
    "select_stages",
    "sweep",
]

logger = logging.getLogger(__name__)


# ── Sweep-specific exceptions ────────────────────────────────────────


class SweepWriteError(LithosError):
    """Raised when a sweep rewrite fails terminally (§5.4 failure mode 1).

    A terminal write failure means either an unresolved
    ``version_conflict`` after the FR-MCP-7 re-read + re-merge + retry,
    or a generic write transport failure that exhausts the retry budget.
    The sweep aborts the run on this error: no later candidate is
    rewritten, ``updated_at`` does not advance, and readiness becomes
    degraded.
    """


class ContentTooLargeSkipped(Exception):
    """Raised when a sweep rewrite hits chronic ``content_too_large``.

    §5.4 failure mode 2: the existing stored note remains untouched,
    ``updated_at`` does NOT advance, the sweep continues to the next
    candidate, and the event is logged + counted.  This is the sole
    exemption from the retry-order advancement invariant.
    """

    def __init__(self, note_id: str) -> None:
        self.note_id = note_id
        super().__init__(f"chronic content_too_large on repair path for note {note_id}")


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


class ArchiveDownloadHook(Protocol):
    """Callable protocol for archive download retry (PRD 04).

    Called by the sweep when ``influx:archive-missing`` is present.
    The implementation downloads the archive and returns the relative
    POSIX path for the ``path:`` line in ``## Archive``.

    Parameters
    ----------
    note:
        The current note state (as returned by ``lithos_read``).

    Returns
    -------
    str
        The relative POSIX path to the downloaded archive.

    Raises
    ------
    ExtractionError
        On download failure — treated as "stage failed this pass"
        by the sweep.
    LithosError
        On Lithos API failure — propagated to the sweep's error
        handling.
    """

    def __call__(self, note: dict[str, object]) -> str: ...


class TextExtractionHook(Protocol):
    """Callable protocol for text-extraction retry (FR-REP-1 stage 2).

    Called by the sweep when a note carries no ``text:*`` tag at all
    — typically a legacy note from before ``text:*`` was always set,
    or one whose tag was hand-stripped.  Distinct from
    ``re_extract_archive``, which upgrades ``text:abstract-only``
    against an existing archive.

    Implementations run the source-specific extraction cascade and
    return the new ``text:*`` tag (e.g. ``"text:html"``,
    ``"text:pdf"``, or ``"text:abstract-only"`` when the cascade
    falls all the way through).

    Parameters
    ----------
    note:
        The current note state (as returned by ``lithos_read``).

    Returns
    -------
    str
        The replacement ``text:*`` tag to add to the note.

    Raises
    ------
    ExtractionError
        On extraction failure — the sweep treats this as "stage
        failed this pass" and keeps ``influx:repair-needed``.
    LithosError
        On Lithos API failure — propagated to the sweep's error
        handling.
    """

    def __call__(self, note: dict[str, object]) -> str: ...


@dataclass(frozen=True, slots=True)
class SweepHooks:
    """Optional hook callables for stage execution within the sweep.

    When a hook is ``None``, the corresponding stage is skipped even if
    stage selection would otherwise select it.  PRD 07 wires the real
    implementations; tests inject fakes.
    """

    archive_download: ArchiveDownloadHook | None = None
    re_extract_archive: ReExtractArchiveHook | None = None
    tier2_enrich: Tier2EnrichHook | None = None
    tier3_extract: Tier3ExtractHook | None = None
    text_extraction: TextExtractionHook | None = None


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
    is_tier2_terminal = "influx:tier2-terminal" in tag_set
    is_tier3_terminal = "influx:tier3-terminal" in tag_set
    is_archive_terminal = "influx:archive-terminal" in tag_set

    # 1. Archive retry: influx:archive-missing present (AC-06-A) AND
    #    the per-stage archive-terminal cap has not been reached.
    archive_retry = "influx:archive-missing" in tag_set and not is_archive_terminal

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
    #    neither the global text-terminal nor the per-stage tier2-terminal
    #    marker is set (the latter caps repeated parse/validate failures).
    tier2_retry = (
        "full-text" not in tag_set
        and max_profile_score >= full_text_threshold
        and not is_text_terminal
        and not is_tier2_terminal
    )

    # 5. Tier 3 retry: influx:deep-extracted missing AND score >=
    #    threshold AND neither text-terminal nor the per-stage
    #    tier3-terminal marker is set.
    tier3_retry = (
        "influx:deep-extracted" not in tag_set
        and max_profile_score >= deep_extract_threshold
        and not is_text_terminal
        and not is_tier3_terminal
    )

    return StageSelection(
        archive_retry=archive_retry,
        text_extraction_retry=text_extraction_retry,
        abstract_only_reextraction=abstract_only_reextraction,
        tier2_retry=tier2_retry,
        tier3_retry=tier3_retry,
    )


# ── Hook-call rollback helpers (finding #1) ──────────────────────────


def _snapshot_note(note: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy mutable note state for hook-call rollback.

    Hooks (``archive_download``, ``re_extract_archive``, ``tier2_enrich``,
    ``tier3_extract``) receive the live mutable note dict.  When a hook
    raises ``ExtractionError`` / ``LithosError`` the sweep treats that
    as "stage failed this pass" (per US-003 / US-013) and must NOT
    persist any partial in-place mutations the hook applied before
    raising — otherwise a hook that appends e.g. ``full-text`` and
    then raises could spuriously satisfy the clearing rules.
    """
    return copy.deepcopy(note)


def _restore_note(note: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Restore *note* to *snapshot* state in place."""
    note.clear()
    note.update(snapshot)


# ── ## Repair section / counter helpers (re-exported) ───────────────
#
# ``RepairCounters``, ``parse_repair_section``, ``render_repair_section``,
# ``upsert_repair_section``, ``classify_failure``, and
# :data:`REPAIR_COUNTED_CAP` now live in :mod:`influx.repair_counters`
# (issue #53) and are imported above so the sweep code below can use
# them unchanged.


def _log_stage_failure(
    stage: str,
    *,
    note: dict[str, Any],
    profile: str,
    exc: BaseException,
) -> None:
    """Emit a structured WARNING for a sweep-stage hook failure.

    The pre-existing logging at these call sites used ``logger.info`` and
    discarded the exception object entirely, leaving operators with a
    bare "stage failed for <id>" line and no way to root-cause the
    incident from logs (staging incident 2026-04-30).  We now lift the
    structured ``model``/``stage``/``detail`` fields off ``LCMAError``
    and ``ExtractionError`` into the JSON record alongside ``exc_info``.
    """
    logger.warning(
        "sweep: %s failed for %s",
        stage,
        note.get("id", "?"),
        extra={
            "sweep_stage": stage,
            "note_id": note.get("id"),
            "profile": profile,
            "run_id": current_run_id.get() or "",
            "exc_type": type(exc).__name__,
            "model": getattr(exc, "model", None),
            "stage": getattr(exc, "stage", None),
            "detail": getattr(exc, "detail", None),
            "url": getattr(exc, "url", None),
        },
        exc_info=exc,
    )


# ── Shared per-stage execution envelope ───────────────────────────


@contextlib.contextmanager
def _stage_attempt(
    note: dict[str, Any],
    *,
    profile: str,
    kind: str,
    span_name: str,
    sync_tags: list[str] | None = None,
) -> Iterator[None]:
    """Common per-stage hook-call envelope.

    Increments the per-kind candidate metric, opens an OTEL span tagged
    with note id / profile / run id, and snapshots the live note dict so
    a hook that mutates it and then raises does NOT leak partial state
    into the rewrite (finding #1).  When *sync_tags* is supplied,
    ``note["tags"]`` is set to a copy of it before the snapshot so the
    snapshot reflects the latest stage-output tag state callers expect
    rolled back on failure.

    On any exception the snapshot is restored before re-raising so the
    caller can compute counted-failure advancement / source-specific
    recovery against the rolled-back note state.
    """
    if sync_tags is not None:
        note["tags"] = list(sync_tags)
    metrics.repair_candidates().add(1, {"profile": profile, "kind": kind})
    snapshot = _snapshot_note(note)
    tracer = get_tracer()
    try:
        with tracer.span(
            span_name,
            attributes={
                "influx.note_id": note.get("id", ""),
                "influx.profile": profile,
                "influx.run_id": current_run_id.get() or "",
            },
        ):
            yield
    except BaseException:
        _restore_note(note, snapshot)
        raise


def _apply_counted_failure(
    *,
    note: dict[str, Any],
    current_tags: list[str],
    exc: BaseException,
    profile: str,
    stage: CountedStage,
    failure_stage: str,
) -> list[str]:
    """Advance the per-stage counter and emit terminal-flip logging.

    No-op for transient failures (returns *current_tags* unchanged).
    For counted failures, mutates ``note["content"]`` with the bumped
    ``## Repair`` section and, when the cap is just crossed, also writes
    ``note["tags"]`` and emits a structured WARNING so operators see the
    stage flip in logs.  The ``<stage>_attempts`` log field is named per
    *stage* so existing log-filter rules keep matching.
    """
    if classify_failure(exc) != "counted":
        return current_tags
    advance = record_counted_failure(
        content=str(note.get("content", "")),
        tags=current_tags,
        stage=stage,
        failure_stage=failure_stage,
        failure_error=str(exc),
    )
    note["content"] = advance.new_content
    if advance.terminal_tag_added:
        note["tags"] = list(advance.new_tags)
        logger.warning(
            "sweep: %s marked terminal after %d counted failures for %s",
            stage,
            advance.attempts,
            note.get("id", "?"),
            extra={
                "sweep_stage": f"{stage}_terminal_flip",
                "note_id": note.get("id"),
                "profile": profile,
                "run_id": current_run_id.get() or "",
                f"{stage}_attempts": advance.attempts,
                "exc_type": type(exc).__name__,
                "stage": getattr(exc, "stage", None),
                "kind": getattr(exc, "kind", None) or failure_stage,
                "detail": getattr(exc, "detail", None),
            },
        )
    return advance.new_tags


# ── Abstract-only re-extraction stage (§5.2) ──────────────────────


def apply_abstract_only_reextraction(
    *,
    tags: list[str],
    note: dict[str, object],
    archive_path: str,
    hook: ReExtractArchiveHook,
) -> list[str]:
    """Execute the abstract-only re-extraction stage (PRD 06 §5.2).

    Calls the ``re_extract_archive`` hook and applies tag mutations
    based on the three-outcome discriminator:

    * **Upgrade** — replace ``text:abstract-only`` with the upgraded
      tag (e.g. ``text:html``); ``influx:text-terminal`` is NOT added.
    * **Terminal** — keep ``text:abstract-only``; ADD
      ``influx:text-terminal``.
    * **Transient** (or ``ExtractionError``) — keep
      ``text:abstract-only`` and ``influx:repair-needed``;
      ``influx:text-terminal`` is NOT added.

    ``influx:text-terminal`` is NEVER set on the initial write — only
    through this stage's Terminal outcome (AC-M2-3).

    Parameters
    ----------
    tags:
        The note's current tag list (will not be mutated).
    note:
        The current note state dict (from ``lithos_read``).
    archive_path:
        The archive path for re-extraction.
    hook:
        The ``re_extract_archive`` callable (PRD 06 §4).

    Returns
    -------
    list[str]
        A new tag list with the appropriate mutations applied.
    """
    # Snapshot the mutable note dict so a hook that mutates it and then
    # raises does not leak partial state into the rewrite (finding #1).
    snapshot = _snapshot_note(note)
    try:
        result = hook(note, archive_path)
    except (ExtractionError, LithosError):
        # Transient failure — keep tags unchanged AND roll back any
        # in-place note mutations the hook applied before raising.
        # ``LithosError`` raised by the hook is treated as a per-stage
        # failure (not a fatal sweep abort) per US-003 / US-013.
        _restore_note(note, snapshot)
        logger.info(
            "abstract-only re-extraction hook raised "
            "ExtractionError/LithosError (transient); tags unchanged"
        )
        return list(tags)

    if result.outcome is ExtractionOutcome.UPGRADE:
        # Replace text:abstract-only with the upgraded tag.
        new_tags = [t for t in tags if t != "text:abstract-only"]
        if result.upgraded_text_tag:
            new_tags.append(result.upgraded_text_tag)
        return new_tags

    if result.outcome is ExtractionOutcome.TERMINAL:
        # Keep text:abstract-only, ADD influx:text-terminal.
        new_tags = list(tags)
        if "influx:text-terminal" not in new_tags:
            new_tags.append("influx:text-terminal")
        return new_tags

    # TRANSIENT — keep tags unchanged AND roll back any in-place note
    # mutations the hook applied before returning. A returned TRANSIENT
    # is "failed this pass" with the same semantics as a raised
    # ExtractionError/LithosError, so persisted note state must not
    # carry the hook's partial work into the rewrite (finding #1).
    _restore_note(note, snapshot)
    logger.info("abstract-only re-extraction returned TRANSIENT; tags unchanged")
    return list(tags)


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


# ── Sweep rewrite helper (§5.4, AC-06-F) ─────────────────────────


async def _sweep_call_write(
    client: LithosClient,
    args: dict[str, Any],
    note_id: str,
) -> str:
    """Single ``lithos_write`` attempt — wraps transport errors.

    Returns the response ``status`` string.  Wraps any
    :class:`LithosError` raised by the transport as
    :class:`SweepWriteError` so the sweep aborts cleanly per
    §5.4 failure mode 1.
    """
    try:
        result = await client.call_tool("lithos_write", args)
    except LithosError as exc:
        raise SweepWriteError(
            f"sweep rewrite transport failure for note {note_id}",
            operation="lithos_write",
            detail=str(exc),
        ) from exc
    text = result.content[0].text  # type: ignore[union-attr]
    body = json.loads(text)
    return body.get("status", "")


async def _sweep_resolve_version_conflict(
    client: LithosClient,
    args: dict[str, Any],
    pending_tags: list[str],
    note_id: str,
) -> str:
    """FR-MCP-7: re-read + re-merge tags + retry once on version_conflict.

    Preserves the sweep's pending content edits (e.g. a freshly written
    archive ``path:`` line) and merges only the ``## User Notes`` region
    from the refreshed note — parallels
    :meth:`LithosClient._retry_version_conflict`.

    Returns the retry's response ``status``.  Raises
    :class:`SweepWriteError` on unresolved conflict or transport failure.
    """
    logger.info(
        "sweep rewrite version_conflict for note %s; re-reading and retrying once",
        note_id,
    )
    try:
        refreshed = await client.read_note(note_id=note_id)
    except LithosError as exc:
        raise SweepWriteError(
            f"sweep re-read failed for note {note_id}",
            operation="lithos_read",
            detail=str(exc),
        ) from exc

    refreshed_tags: list[str] = refreshed.get("tags", [])
    merged_tags = merge_tags(
        existing_tags=refreshed_tags,
        new_tags=list(pending_tags),
    )

    refreshed_content: str = refreshed.get("content", "")
    # Use the sweep's pending content (with any sweep edits like a new
    # archive ``path:`` line) and only graft the user-notes region from
    # the refreshed note — never overwrite pending edits with refreshed
    # body content.
    pending_content: str = args.get("content", "")
    merged_content = graft_user_notes(refreshed_content, pending_content)

    retry_args = {
        **args,
        "tags": merged_tags,
        "content": merged_content,
    }
    refresh_version = refreshed.get("version")
    if refresh_version is not None:
        retry_args["expected_version"] = refresh_version

    status = await _sweep_call_write(client, retry_args, note_id)
    if status == "version_conflict":
        raise SweepWriteError(
            f"sweep rewrite unresolved version_conflict "
            f"for note {note_id} after FR-MCP-7 retry",
            operation="lithos_write",
            detail="version_conflict_unresolved",
        )
    return status


async def _attempt_sweep_write(
    client: LithosClient,
    args: dict[str, Any],
    *,
    pending_tags: list[str],
    note_id: str,
) -> str:
    """Single rewrite attempt with FR-MCP-7 version-conflict retry.

    Returns the final response status (after one re-read + retry on
    ``version_conflict``).  Raises :class:`SweepWriteError` on
    transport failure or unresolved conflict.
    """
    status = await _sweep_call_write(client, args, note_id)
    if status == "version_conflict":
        status = await _sweep_resolve_version_conflict(
            client, args, pending_tags, note_id
        )
    return status


async def _rewrite_sweep_note(
    client: LithosClient,
    note: dict[str, Any],
    tags: list[str],
) -> None:
    """Rewrite a single sweep-visited note via ``lithos_write`` (§5.4).

    Implements the full PRD 05/06 envelope contract:

    * On ``version_conflict``: one re-read + tag re-merge + retry per
      FR-MCP-7 (AC-06-F first half).  Pending content is preserved and
      only ``## User Notes`` is grafted from the refreshed note —
      never overwriting sweep edits like a freshly inserted archive
      ``path:`` line.
    * On ``content_too_large``: drop ``## Full Text`` (Tier 2) and
      retry; if still oversize, drop Tier 2 + Tier 3 and ensure
      ``influx:repair-needed``, retry; only after that final attempt
      also returns ``content_too_large`` is the note treated as
      chronic-oversize (master PRD §9.7, US-012, finding #2).
    * On a second ``version_conflict`` or generic transport failure:
      raises :class:`SweepWriteError` to abort the run (AC-06-F
      second half).

    Parameters
    ----------
    client:
        The Lithos MCP client.
    note:
        The note dict from ``lithos_read``.
    tags:
        The updated tag list to write (post-stage, post-clearing).

    Raises
    ------
    SweepWriteError
        On unresolved ``version_conflict`` (after FR-MCP-7 retry) or
        generic transport failure — the sweep must abort.
    ContentTooLargeSkipped
        When all trim attempts (original → Tier 2 dropped → Tier 1
        only) returned ``content_too_large`` — the chronic-oversize
        repair-path exemption (§5.4 failure mode 2, AC-X-8).
    """
    note_id: str = note.get("id", "")
    base_args: dict[str, Any] = {
        "id": note_id,
        "title": note.get("title", ""),
        "content": note.get("content", ""),
        "agent": "influx",
        "path": note.get("path", ""),
        "source_url": note.get("source_url", ""),
        "tags": list(tags),
        "confidence": note.get("confidence", 0.0),
        "note_type": note.get("note_type", "summary"),
        "namespace": note.get("namespace", "influx"),
    }
    version = note.get("version")
    if version is not None:
        base_args["expected_version"] = version

    # Attempt 1: original content + tags.
    status = await _attempt_sweep_write(
        client, base_args, pending_tags=list(tags), note_id=note_id
    )
    if status != "content_too_large":
        return

    # Attempt 2: drop ## Full Text (Tier 2) and retry.
    tier2_args = {**base_args, "content": drop_tier2(base_args["content"])}
    status = await _attempt_sweep_write(
        client, tier2_args, pending_tags=list(tags), note_id=note_id
    )
    if status != "content_too_large":
        return

    # Attempt 3: drop Tier 2 + Tier 3 and ensure influx:repair-needed
    # so the note re-enters the sweep on a later run.
    repair_tags = list(tags)
    if "influx:repair-needed" not in repair_tags:
        repair_tags.append("influx:repair-needed")
    tier1_args = {
        **base_args,
        "content": drop_tier2_and_tier3(base_args["content"]),
        "tags": repair_tags,
    }
    status = await _attempt_sweep_write(
        client, tier1_args, pending_tags=repair_tags, note_id=note_id
    )
    if status != "content_too_large":
        return

    # All trim attempts exhausted — chronic-oversize on repair path.
    raise ContentTooLargeSkipped(note_id)


# ── Per-note stage execution ──────────────────────────────────────


def _get_profile_thresholds(
    config: AppConfig,
    profile: str,
) -> tuple[int, int]:
    """Return ``(full_text_threshold, deep_extract_threshold)``."""
    for p in config.profiles:
        if p.name == profile:
            return p.thresholds.full_text, p.thresholds.deep_extract
    # Fallback defaults from ProfileThresholds.
    return 8, 9


def _run_archive_retry(
    note: dict[str, Any],
    *,
    profile: str,
    current_tags: list[str],
    archive_path: str | None,
    hook: ArchiveDownloadHook,
) -> tuple[list[str], str | None, bool]:
    """Run the archive-download retry stage (FR-REP-1).

    Returns ``(tags, archive_path, succeeded)``.  On failure the tag
    list reflects any counted-failure terminal flip and the archive
    path is left at its prior value; callers feed the returned path
    back into :func:`compute_clearing` and the abstract-only
    re-extraction eligibility re-check.
    """
    try:
        with _stage_attempt(
            note,
            profile=profile,
            kind="archive",
            span_name="influx.repair.archive",
        ):
            downloaded_path = hook(note)
        # Patch ## Archive with the freshly downloaded path. Idempotent,
        # CRLF-tolerant, and scans the whole section for an existing
        # ``path:`` line — canonical_note owns the splice (a no-op when the
        # section is absent or already carries a path).
        note["content"] = upsert_archive_path(
            str(note.get("content", "")), downloaded_path
        )
        return current_tags, downloaded_path, True
    except (ExtractionError, LithosError) as exc:
        failure_stage = (
            getattr(exc, "stage", "") or getattr(exc, "kind", "") or "archive_failed"
        )
        new_tags = _apply_counted_failure(
            note=note,
            current_tags=current_tags,
            exc=exc,
            profile=profile,
            stage="archive",
            failure_stage=failure_stage,
        )
        _log_stage_failure(
            "archive_download",
            note=note,
            profile=profile,
            exc=exc,
        )
        return new_tags, archive_path, False


def _terminate_unsupported_text_source(note: dict[str, Any]) -> list[str]:
    """Mark text extraction terminal when the note's source is unsupported.

    Without a per-source resolver the text-extraction hook raises
    ``ExtractionError(stage="unsupported_source")`` every pass and the
    note loops forever.  We pin Tier 1 quality to ``text:abstract-only``,
    add ``influx:text-terminal`` so later sweeps skip the stage, and
    drop ``influx:repair-needed`` (when no other repair condition holds)
    so the note exits the sweep entirely.
    """
    restored_tags = list(note.get("tags", []))
    if not any(tag.startswith("text:") for tag in restored_tags):
        restored_tags.append("text:abstract-only")
    if "influx:text-terminal" not in restored_tags:
        restored_tags.append("influx:text-terminal")
    if "influx:archive-missing" not in restored_tags:
        restored_tags = [tag for tag in restored_tags if tag != "influx:repair-needed"]
    note["tags"] = list(restored_tags)
    return restored_tags


def _terminate_invalid_source_metadata(note: dict[str, Any]) -> list[str]:
    """Mark a note terminal because its source metadata is unrecoverable (#150).

    Distinct from :func:`_terminate_unsupported_text_source` — that
    helper covers legitimate future sources without a resolver yet
    (``source:hackernews``).  This helper covers notes whose source
    tag is empty/garbled AND whose URL/path/id provide no inference
    fallback, so the previous behaviour was an infinite loop of
    ``text_extraction retry: source '' not supported`` warnings every
    sweep.

    The note is pinned at ``text:abstract-only`` with
    ``influx:text-terminal`` so the text-extraction stage is skipped,
    AND tagged with ``influx:source-invalid`` so the bad-state notes
    are independently discoverable (Lithos tag search) for later
    operator cleanup.  ``influx:repair-needed`` is dropped when no
    other repair condition remains pending, mirroring the unsupported-
    source path so the note exits the sweep entirely.
    """
    restored_tags = list(note.get("tags", []))
    if not any(tag.startswith("text:") for tag in restored_tags):
        restored_tags.append("text:abstract-only")
    if "influx:text-terminal" not in restored_tags:
        restored_tags.append("influx:text-terminal")
    if "influx:source-invalid" not in restored_tags:
        restored_tags.append("influx:source-invalid")
    if "influx:archive-missing" not in restored_tags:
        restored_tags = [tag for tag in restored_tags if tag != "influx:repair-needed"]
    note["tags"] = list(restored_tags)
    return restored_tags


def _run_text_extraction_retry(
    note: dict[str, Any],
    *,
    profile: str,
    current_tags: list[str],
    hook: TextExtractionHook,
) -> list[str]:
    """Run the text-extraction retry stage (FR-REP-1 stage 2).

    Distinct from the abstract-only re-extraction stage below, which
    upgrades ``text:abstract-only`` against an existing archive.  No
    new terminal tag is introduced for normal failures (out of scope
    for issue #24); ``unsupported_source`` failures are flipped to
    terminal so the note exits the sweep instead of looping.

    ``invalid_source_metadata`` failures (#150) are also flipped to
    terminal — distinct from ``unsupported_source`` so logs and tags
    cleanly separate the metadata-integrity case from the future-
    source case.  The terminal flip also adds the
    ``influx:source-invalid`` tag so the bad-state notes are
    independently discoverable for operator cleanup.
    """
    try:
        with _stage_attempt(
            note,
            profile=profile,
            kind="text_extraction",
            span_name="influx.repair.text_extraction",
            sync_tags=current_tags,
        ):
            new_text_tag = hook(note)
        new_tags = list(current_tags)
        # The hook may have backfilled the ``source:*`` tag on the
        # note dict during inference (#150); reflect that mutation in
        # the working tag set so the rewrite persists it even on the
        # success path.
        hook_tags = list(note.get("tags", new_tags))
        if hook_tags != new_tags:
            new_tags = merge_tags(existing_tags=new_tags, new_tags=hook_tags)
        if new_text_tag and not any(t.startswith("text:") for t in new_tags):
            new_tags.append(new_text_tag)
        note["tags"] = list(new_tags)
        return new_tags
    except (ExtractionError, LCMAError, LithosError) as exc:
        new_tags = current_tags
        if isinstance(exc, ExtractionError):
            stage = getattr(exc, "stage", "")
            if stage == "unsupported_source":
                new_tags = _terminate_unsupported_text_source(note)
            elif stage == "invalid_source_metadata":
                new_tags = _terminate_invalid_source_metadata(note)
        _log_stage_failure(
            "text_extraction",
            note=note,
            profile=profile,
            exc=exc,
        )
        return new_tags


def _run_tier_retry(
    note: dict[str, Any],
    *,
    profile: str,
    current_tags: list[str],
    hook: Tier2EnrichHook | Tier3ExtractHook,
    stage: CountedStage,
    log_subject: str,
) -> list[str]:
    """Run a Tier 2 / Tier 3 retry stage.

    Both tiers share the same shape — counted-failure cap, terminal-tag
    flip, hook-mutation rollback — so they delegate to a single helper
    parameterised on *stage* (``"tier2"`` / ``"tier3"``) and the log
    subject used for the per-stage failure WARN line.
    """
    try:
        with _stage_attempt(
            note,
            profile=profile,
            kind=stage,
            span_name=f"influx.repair.{stage}",
            sync_tags=current_tags,
        ):
            hook(note)
        # Sync any tag/content mutations the hook applied to the note
        # dict back into the working tag set.
        return list(note.get("tags", current_tags))
    except (ExtractionError, LCMAError, LithosError) as exc:
        new_tags = _apply_counted_failure(
            note=note,
            current_tags=current_tags,
            exc=exc,
            profile=profile,
            stage=stage,
            failure_stage=getattr(exc, "stage", "") or "",
        )
        _log_stage_failure(
            log_subject,
            note=note,
            profile=profile,
            exc=exc,
        )
        return new_tags


def _doc_title(note: dict[str, Any]) -> str:
    """Return the note's doc-level title (top-level, then ``metadata``)."""
    direct = note.get("title")
    if isinstance(direct, str) and direct:
        return direct
    meta = note.get("metadata")
    if isinstance(meta, dict):
        nested = meta.get("title")
        if isinstance(nested, str) and nested:
            return nested
    return ""


async def _process_sweep_note(
    note: dict[str, Any],
    *,
    profile: str,
    client: LithosClient,
    config: AppConfig,
    hooks: SweepHooks,
) -> None:
    """Select stages, execute, clear tags, rewrite one note (§5.2-5.4).

    Raises :class:`SweepWriteError` on terminal write failure.
    """
    from influx.notes import parse_archive_path, parse_profile_relevance

    tags: list[str] = list(note.get("tags", []))

    # Parse note structure for stage selection inputs. ``read_note`` strips
    # the doc-level title from ``content``; reattach it for parsing only so
    # archive-path / profile-relevance extraction works (#220).
    archive_path: str | None = None
    max_profile_score: int = 0
    try:
        parsed = parse_lenient(
            str(note.get("content") or ""), fallback_title=_doc_title(note)
        )
        archive_path = parse_archive_path(parsed)
        entries = parse_profile_relevance(parsed)
        if entries:
            max_profile_score = max(e.score for e in entries)
    except Exception as exc:
        logger.warning(
            "sweep: could not parse note %s; will still rewrite",
            note.get("id", "?"),
            extra={
                "sweep_stage": "parse_note",
                "note_id": note.get("id"),
                "profile": profile,
                "run_id": current_run_id.get() or "",
                "exc_type": type(exc).__name__,
            },
            exc_info=exc,
        )

    ft_thresh, de_thresh = _get_profile_thresholds(config, profile)

    stages = select_stages(
        tags=tags,
        archive_path=archive_path,
        archive_succeeded_this_pass=False,
        max_profile_score=max_profile_score,
        full_text_threshold=ft_thresh,
        deep_extract_threshold=de_thresh,
    )

    # ── Execute selected stages ─────────────────────────────────
    current_tags = list(tags)
    archive_succeeded = False

    # Issue #124: each per-stage helper invokes a sync hook that does
    # blocking work — archive download (sync HTTP), text extraction
    # (sync HTML/PDF), and tier 2/3 model calls (sync POST). Running
    # them on the event loop starves the admin API exactly the way the
    # acquire/filter path used to. Offload each helper to a worker
    # thread; the helpers themselves stay sync (preserving the
    # _stage_attempt context manager, telemetry, and tag-mutation
    # rollback semantics inside them).
    if stages.archive_retry and hooks.archive_download:
        current_tags, archive_path, archive_succeeded = await asyncio.to_thread(
            _run_archive_retry,
            note,
            profile=profile,
            current_tags=current_tags,
            archive_path=archive_path,
            hook=hooks.archive_download,
        )

    if stages.text_extraction_retry and hooks.text_extraction:
        current_tags = await asyncio.to_thread(
            _run_text_extraction_retry,
            note,
            profile=profile,
            current_tags=current_tags,
            hook=hooks.text_extraction,
        )

    # Re-evaluate abstract-only re-extraction eligibility now that the
    # archive download outcome is known (initial selection above used
    # archive_succeeded_this_pass=False).
    run_abstract_reextraction = stages.abstract_only_reextraction or (
        archive_succeeded
        and "text:abstract-only" in set(tags)
        and "influx:text-terminal" not in set(tags)
    )
    if (
        run_abstract_reextraction
        and hooks.re_extract_archive
        and archive_path is not None
    ):
        current_tags = await asyncio.to_thread(
            apply_abstract_only_reextraction,
            tags=current_tags,
            note=note,
            archive_path=archive_path,
            hook=hooks.re_extract_archive,
        )

    if stages.tier2_retry and hooks.tier2_enrich:
        current_tags = await asyncio.to_thread(
            _run_tier_retry,
            note,
            profile=profile,
            current_tags=current_tags,
            hook=hooks.tier2_enrich,
            stage="tier2",
            log_subject="tier2_enrichment",
        )

    if stages.tier3_retry and hooks.tier3_extract:
        current_tags = await asyncio.to_thread(
            _run_tier_retry,
            note,
            profile=profile,
            current_tags=current_tags,
            hook=hooks.tier3_extract,
            stage="tier3",
            log_subject="tier3_extraction",
        )

    # ── Compute and apply clearing ──────────────────────────────
    clearing = compute_clearing(
        tags=current_tags,
        archive_path=archive_path,
        max_profile_score=max_profile_score,
        full_text_threshold=ft_thresh,
        deep_extract_threshold=de_thresh,
    )
    if clearing.clear_archive_missing:
        current_tags = [t for t in current_tags if t != "influx:archive-missing"]
    if clearing.clear_repair_needed:
        current_tags = [t for t in current_tags if t != "influx:repair-needed"]

    # Apply rejection guard (FR-NOTE-6, AC-M3-6): preserve any
    # influx:rejected:<profile> tags and do NOT re-add profile:<name>
    # for rejected profiles.
    current_tags = merge_tags(existing_tags=tags, new_tags=current_tags)

    # ── Rewrite (§5.4 retry-order advancement) ──────────────────
    await _rewrite_sweep_note(client, note, current_tags)


# ── Sweep entry point ──────────────────────────────────────────────


async def sweep(
    profile: str,
    *,
    client: LithosClient,
    config: AppConfig,
    hooks: SweepHooks | None = None,
) -> list[dict[str, Any]]:
    """Run the repair sweep for *profile* (PRD 06 §5.1 FR-REP-1).

    Fetches up to ``repair.max_items_per_run`` notes tagged
    ``influx:repair-needed`` + ``profile:<profile>``, ordered by
    ``updated_at`` ascending (oldest first).  Each candidate is
    re-read via ``lithos_read``, then stages are selected + executed,
    clearing is applied, and the note is rewritten via ``lithos_write``
    to advance ``updated_at`` (retry-order advancement, §5.4).

    On terminal write failure (unresolved ``version_conflict`` after
    FR-MCP-7 retry, or generic transport failure), the sweep aborts
    and raises :class:`SweepWriteError`.  No later candidate is
    rewritten in that run (AC-X-8 failure mode 1).

    Parameters
    ----------
    hooks:
        Optional hook callables for stage execution.  When ``None``,
        a default empty :class:`SweepHooks` is used (stages that
        require hooks are skipped, but notes are still rewritten).

    Returns
    -------
    list[dict[str, Any]]
        The list of re-read note dicts that were visited (may be
        shorter than the candidate list if the sweep aborted).

    Raises
    ------
    SweepWriteError
        On terminal write failure — the caller (service.py) treats
        this as a run abort per FR-RES-3.
    """
    if hooks is not None:
        effective_hooks = hooks
    else:
        from influx.repair_hooks import make_default_sweep_hooks

        effective_hooks = make_default_sweep_hooks(config).to_sweep_hooks()

    limit = config.repair.max_items_per_run
    body = await client.list_notes_body(
        tags=["influx:repair-needed", f"profile:{profile}"],
        limit=limit,
        order_by="updated_at",
        order="asc",
    )
    items: list[dict[str, Any]] = body.get("items", [])
    items.sort(
        key=lambda item: str(item.get("updated_at") or item.get("updated") or "")
    )

    if not items:
        logger.debug("repair sweep for %r: no candidates found", profile)
        return []

    logger.info(
        "repair sweep for %r: visiting %d candidate(s)",
        profile,
        len(items),
    )

    visited: list[dict[str, Any]] = []
    content_too_large_skipped = 0
    for item in items:
        note_id = item.get("id", "")
        if not note_id:
            continue
        note = await client.read_note(note_id=note_id)
        visited.append(note)

        try:
            # Process and rewrite — raises SweepWriteError on terminal
            # write failure, aborting the loop (§5.4 failure mode 1).
            await _process_sweep_note(
                note,
                profile=profile,
                client=client,
                config=config,
                hooks=effective_hooks,
            )
        except ContentTooLargeSkipped:
            # §5.4 failure mode 2: chronic content_too_large on repair
            # path.  The existing stored note remains untouched,
            # updated_at does NOT advance, and the sweep continues.
            content_too_large_skipped += 1
            logger.warning(
                "sweep: chronic content_too_large for note %s "
                "— skipping (existing note untouched), "
                "content_too_large_skipped=%d",
                note_id,
                content_too_large_skipped,
            )

    if content_too_large_skipped:
        logger.info(
            "repair sweep for %r: %d note(s) skipped due to chronic content_too_large",
            profile,
            content_too_large_skipped,
        )

    return visited
