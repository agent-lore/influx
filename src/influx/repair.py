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

from influx.errors import ExtractionError, LithosError

if TYPE_CHECKING:
    from influx.config import AppConfig
    from influx.lithos_client import LithosClient

__all__ = [
    "ClearingDecision",
    "ExtractionOutcome",
    "ReExtractionResult",
    "ReExtractArchiveHook",
    "StageSelection",
    "SweepHooks",
    "SweepWriteError",
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


@dataclass(frozen=True, slots=True)
class SweepHooks:
    """Optional hook callables for stage execution within the sweep.

    When a hook is ``None``, the corresponding stage is skipped even if
    stage selection would otherwise select it.  PRD 07 wires the real
    implementations; tests inject fakes.
    """

    re_extract_archive: ReExtractArchiveHook | None = None
    tier2_enrich: Tier2EnrichHook | None = None
    tier3_extract: Tier3ExtractHook | None = None


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
    try:
        result = hook(note, archive_path)
    except ExtractionError:
        # Transient failure — keep tags unchanged.
        logger.info(
            "abstract-only re-extraction raised ExtractionError "
            "(transient); tags unchanged"
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

    # TRANSIENT — keep tags unchanged.
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


async def _rewrite_sweep_note(
    client: LithosClient,
    note: dict[str, Any],
    tags: list[str],
) -> None:
    """Rewrite a single sweep-visited note via ``lithos_write`` (§5.4).

    Builds the write args from the existing note dict with the updated
    *tags* and calls ``lithos_write``.  On ``version_conflict``, performs
    one re-read + tag re-merge + retry per FR-MCP-7 (AC-06-F first half).
    On a second ``version_conflict`` or transport failure, raises
    :class:`SweepWriteError` to abort the run (AC-06-F second half).

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
    """
    note_id: str = note.get("id", "")
    args: dict[str, Any] = {
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
        args["expected_version"] = version

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
    status = body.get("status", "")

    if status != "version_conflict":
        return

    # FR-MCP-7: re-read + re-merge tags + retry once.
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
    # Re-merge: union the existing tags with the sweep's intended tags
    # while preserving the sweep's Influx-owned tag changes.
    merged_tag_set: dict[str, None] = {}
    for t in tags:
        merged_tag_set[t] = None
    for t in refreshed_tags:
        if t not in merged_tag_set:
            merged_tag_set[t] = None

    retry_args = {
        **args,
        "tags": list(merged_tag_set),
        "content": refreshed.get("content", args["content"]),
    }
    refresh_version = refreshed.get("version")
    if refresh_version is not None:
        retry_args["expected_version"] = refresh_version

    try:
        retry_result = await client.call_tool("lithos_write", retry_args)
    except LithosError as exc:
        raise SweepWriteError(
            f"sweep rewrite retry transport failure for note {note_id}",
            operation="lithos_write",
            detail=str(exc),
        ) from exc

    retry_text = retry_result.content[0].text  # type: ignore[union-attr]
    retry_body = json.loads(retry_text)
    if retry_body.get("status") == "version_conflict":
        raise SweepWriteError(
            f"sweep rewrite unresolved version_conflict "
            f"for note {note_id} after FR-MCP-7 retry",
            operation="lithos_write",
            detail="version_conflict_unresolved",
        )


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
    from influx.notes import parse_archive_path, parse_note, parse_profile_relevance

    tags: list[str] = list(note.get("tags", []))
    content: str = note.get("content", "")

    # Parse note structure for stage selection inputs.
    archive_path: str | None = None
    max_profile_score: int = 0
    try:
        parsed = parse_note(content)
        archive_path = parse_archive_path(parsed)
        entries = parse_profile_relevance(parsed)
        if entries:
            max_profile_score = max(e.score for e in entries)
    except Exception:
        logger.warning(
            "sweep: could not parse note %s; will still rewrite",
            note.get("id", "?"),
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

    # Archive retry (PRD 04 hook — not yet wired).
    # Placeholder: archive download hook would go here.
    archive_succeeded = False

    # Text extraction retry (PRD 07 hook — not yet wired).
    # Placeholder: text extraction hook would go here.

    # Abstract-only re-extraction.
    if (
        stages.abstract_only_reextraction
        and hooks.re_extract_archive
        and archive_path is not None
    ):
        current_tags = apply_abstract_only_reextraction(
            tags=current_tags,
            note=note,
            archive_path=archive_path,
            hook=hooks.re_extract_archive,
        )

    # Tier 2 retry.
    if stages.tier2_retry and hooks.tier2_enrich:
        try:
            hooks.tier2_enrich(note)
        except ExtractionError:
            logger.info("sweep: tier2 enrichment failed for %s", note.get("id"))

    # Tier 3 retry.
    if stages.tier3_retry and hooks.tier3_extract:
        try:
            hooks.tier3_extract(note)
        except ExtractionError:
            logger.info("sweep: tier3 extraction failed for %s", note.get("id"))

    # ── Compute and apply clearing ──────────────────────────────
    # Re-parse archive_path from post-stage state if archive
    # succeeded this pass (currently always False until wired).
    post_archive_path = archive_path
    if archive_succeeded:
        post_archive_path = archive_path  # would be updated

    clearing = compute_clearing(
        tags=current_tags,
        archive_path=post_archive_path,
        max_profile_score=max_profile_score,
        full_text_threshold=ft_thresh,
        deep_extract_threshold=de_thresh,
    )

    if clearing.clear_archive_missing:
        current_tags = [t for t in current_tags if t != "influx:archive-missing"]
    if clearing.clear_repair_needed:
        current_tags = [t for t in current_tags if t != "influx:repair-needed"]

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
    effective_hooks = hooks or SweepHooks()

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

        # Process and rewrite — raises SweepWriteError on terminal
        # write failure, aborting the loop (§5.4 failure mode 1).
        await _process_sweep_note(
            note,
            profile=profile,
            client=client,
            config=config,
            hooks=effective_hooks,
        )

    return visited
