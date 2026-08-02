"""Per-stage repair counters parsed from the note's ``## Repair`` section.

The ``RepairCounters`` module owns the read / advance / cap-check
contract for the per-stage Layer-2 attempt counters Influx persists in
each note (CONTEXT.md ``RepairCounters``):

- :func:`parse_repair_section` reads existing
  ``tier{N}_attempts``/``tier{N}_last_stage``/``tier{N}_last_error``
  bullets from a note body.
- :func:`render_repair_section` / :func:`upsert_repair_section`
  serialise the counters back into the note before its rewrite.
- :func:`classify_failure` partitions sweep-stage failures into
  ``"transient"`` vs ``"counted"``.  Transient failures (HTTP, transport,
  resolve, archive_read, etc.) do **not** advance the counter — that
  partition stays in callers via the ``classify_failure`` check.
- :func:`record_counted_failure` performs the canonical
  parse → bump → upsert → cap-check sequence and emits the
  ``influx:<stage>-terminal`` tag when the cap is reached.

The cap (:data:`REPAIR_COUNTED_CAP`) is hardcoded to 3 to mirror the
abstract-only re-extraction TERMINAL outcome cadence; tunability is a
follow-up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from influx.canonical_note import REPAIR, extract_section_body, upsert_section_text
from influx.errors import ExtractionError, LCMAError

__all__ = [
    "REPAIR_COUNTED_CAP",
    "CountedFailureResult",
    "CountedStage",
    "RepairCounters",
    "classify_failure",
    "parse_repair_section",
    "record_counted_failure",
    "render_repair_section",
    "terminal_tag_for",
    "upsert_repair_section",
]

# Per-stage cap on counted failures before flipping
# influx:tier{2,3}-terminal / influx:archive-terminal.  Tunable via
# influx.toml in a follow-up; hardcoded for the initial roll-out (plan:
# 3 mirrors the abstract-only re-extraction TERMINAL outcome cadence).
REPAIR_COUNTED_CAP = 3


CountedStage = Literal["tier2", "tier3", "archive"]


# ── Counters dataclass ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RepairCounters:
    """Per-stage attempt counters tracked in the note's ``## Repair`` section.

    ``{archive,tier2,tier3}_attempts`` count *counted-toward-cap*
    failures only — transient HTTP / network failures are not bumped
    (see :func:`classify_failure`).  When ``<stage>_attempts`` reaches
    the configured cap, the sweep adds the ``influx:<stage>-terminal``
    tag and stops re-running that stage on subsequent passes.
    """

    tier2_attempts: int = 0
    tier2_last_stage: str = ""
    tier2_last_error: str = ""
    tier3_attempts: int = 0
    tier3_last_stage: str = ""
    tier3_last_error: str = ""
    archive_attempts: int = 0
    archive_last_kind: str = ""
    archive_last_error: str = ""

    def bump_tier2(self, *, stage: str, error: str) -> RepairCounters:
        """Return a new counters value with Tier 2 attempts incremented."""
        return replace(
            self,
            tier2_attempts=self.tier2_attempts + 1,
            tier2_last_stage=stage,
            tier2_last_error=_collapse_to_single_line(error),
        )

    def bump_tier3(self, *, stage: str, error: str) -> RepairCounters:
        """Return a new counters value with Tier 3 attempts incremented."""
        return replace(
            self,
            tier3_attempts=self.tier3_attempts + 1,
            tier3_last_stage=stage,
            tier3_last_error=_collapse_to_single_line(error),
        )

    def bump_archive(self, *, kind: str, error: str) -> RepairCounters:
        """Return a new counters value with archive attempts incremented.

        *kind* is the underlying failure classifier — typically the
        ``stage`` from ``ExtractionError`` or the ``kind`` from
        ``NetworkError`` (e.g. ``"oversize"``).  Stored verbatim so an
        operator inspecting the note can tell at a glance whether the
        archive is being terminated for size, content-type mismatch,
        or some other persistent reason.
        """
        return replace(
            self,
            archive_attempts=self.archive_attempts + 1,
            archive_last_kind=kind,
            archive_last_error=_collapse_to_single_line(error),
        )

    def attempts_for(self, stage: CountedStage) -> int:
        """Return the per-stage counter for *stage*."""
        if stage == "tier2":
            return self.tier2_attempts
        if stage == "tier3":
            return self.tier3_attempts
        return self.archive_attempts


# ── Section parser / serializer ─────────────────────────────────────


_REPAIR_BULLET_RE = re.compile(r"^[ \t]*-\s*(\w+)\s*:\s*(.*)$")
_QUOTED_RE = re.compile(r'^"(.*)"$')


def _collapse_to_single_line(value: str) -> str:
    """Flatten *value* to a single line (caps at 300 chars).

    The ``## Repair`` section uses one bullet per key, so multi-line
    values would corrupt round-tripping.  Truncating long error strings
    also keeps notes from ballooning when the same provider message
    repeats every sweep.
    """
    return " ".join(value.replace("\r", " ").split())[:300]


def parse_repair_section(content: str) -> RepairCounters:
    """Parse the ``## Repair`` section of *content* into :class:`RepairCounters`.

    The section is located by :func:`canonical_note.extract_section_body`
    (CRLF-tolerant); only the bullet grammar is owned here.  Returns
    zero-defaults when the section is absent.  Unknown bullets are ignored;
    malformed integer counts default to zero so a hand-edit cannot break the
    sweep.
    """
    body = extract_section_body(content, REPAIR)
    if not body:
        return RepairCounters()

    fields: dict[str, Any] = {}
    int_keys = {"tier2_attempts", "tier3_attempts", "archive_attempts"}
    str_keys = {
        "tier2_last_stage",
        "tier2_last_error",
        "tier3_last_stage",
        "tier3_last_error",
        "archive_last_kind",
        "archive_last_error",
    }
    for line in body.splitlines():
        bm = _REPAIR_BULLET_RE.match(line)
        if not bm:
            continue
        key = bm.group(1)
        raw = bm.group(2).strip()
        qm = _QUOTED_RE.match(raw)
        value: Any = qm.group(1) if qm else raw
        if key in int_keys:
            try:
                fields[key] = int(value)
            except (TypeError, ValueError):
                fields[key] = 0
        elif key in str_keys:
            fields[key] = value
    return RepairCounters(**fields)


def render_repair_section(counters: RepairCounters) -> str:
    """Serialise *counters* as a ``## Repair`` section (trailing newline)."""
    lines = [
        "## Repair",
        f"- tier2_attempts: {counters.tier2_attempts}",
        f'- tier2_last_stage: "{counters.tier2_last_stage}"',
        f'- tier2_last_error: "{_collapse_to_single_line(counters.tier2_last_error)}"',
        f"- tier3_attempts: {counters.tier3_attempts}",
        f'- tier3_last_stage: "{counters.tier3_last_stage}"',
        f'- tier3_last_error: "{_collapse_to_single_line(counters.tier3_last_error)}"',
        f"- archive_attempts: {counters.archive_attempts}",
        f'- archive_last_kind: "{counters.archive_last_kind}"',
        (
            "- archive_last_error: "
            f'"{_collapse_to_single_line(counters.archive_last_error)}"'
        ),
    ]
    return "\n".join(lines) + "\n"


def upsert_repair_section(content: str, counters: RepairCounters) -> str:
    """Return *content* with the ``## Repair`` section set to *counters*.

    Placement is delegated to :func:`canonical_note.upsert_section_text`:
    an existing section is replaced in place (trimming accumulated blank
    lines); otherwise the section is inserted at
    :func:`canonical_note.insertion_point` — before ``## Profile Relevance``,
    else before ``## User Notes``, else at end of content.
    """
    return upsert_section_text(content, REPAIR, render_repair_section(counters))


# ── Failure classification ──────────────────────────────────────────


# Stage / kind discriminators that mean "rerunning will almost certainly
# fail the same way" — bump the attempt counter toward the cap.  Includes
# both LCMA-side schema failures (parse, validate) and archive-side
# size violations (oversize) which won't shrink on retry.
_COUNTED_STAGES: frozenset[str] = frozenset({"parse", "validate", "oversize"})

# Discriminators that are permanent for *one* stage but not globally.
#
# ``unsupported_source``: the note's ``source:*`` tag has no registered
# reacquirer, so ``repair_hooks._reacquirer_for_source`` returns ``None``
# and the archive hook raises before any network call.  Nothing about a
# retry changes that — it is fixed only by shipping a resolver — so for
# the archive stage it advances the cap rather than retrying forever.
#
# The text-extraction path deliberately keeps classifying it transient:
# it has its own terminal handling in
# ``repair._terminate_unsupported_text_source``, which flips
# ``influx:text-terminal`` directly instead of going through a counter.
_STAGE_SCOPED_COUNTED_STAGES: dict[CountedStage, frozenset[str]] = {
    "archive": frozenset({"unsupported_source"}),
}


def classify_failure(
    exc: BaseException,
    *,
    stage: CountedStage | None = None,
) -> Literal["transient", "counted"]:
    """Partition a sweep stage failure into transient vs counted.

    *Counted* failures advance the per-stage attempt counter and
    eventually flip the stage to terminal — these are exceptions whose
    ``stage`` indicates the model output itself was malformed (``parse``,
    ``validate``).  Re-running the same prompt on the same input will
    almost certainly keep failing.

    *Transient* failures (HTTP, transport, resolve, archive_read, or
    anything we don't specifically recognise) leave the counter alone.
    The next sweep retries them indefinitely.

    Parameters
    ----------
    exc:
        The stage failure to classify.
    stage:
        The sweep stage that raised, when known.  Some discriminators
        are permanent for one stage and transient for another — see
        :data:`_STAGE_SCOPED_COUNTED_STAGES`.  Omitting it yields the
        global classification.
    """
    if isinstance(exc, (LCMAError, ExtractionError)):
        exc_stage = getattr(exc, "stage", "") or ""
        counted = _COUNTED_STAGES
        if stage is not None:
            counted = counted | _STAGE_SCOPED_COUNTED_STAGES.get(stage, frozenset())
        if exc_stage in counted:
            return "counted"
    return "transient"


# ── Combined record-counted-failure operation ──────────────────────


def terminal_tag_for(stage: CountedStage) -> str:
    """Return the ``influx:<stage>-terminal`` tag for *stage*."""
    return f"influx:{stage}-terminal"


@dataclass(frozen=True, slots=True)
class CountedFailureResult:
    """Outcome of recording one counted failure for a stage.

    Attributes
    ----------
    counters:
        The post-bump :class:`RepairCounters` value (already serialised
        into ``new_content``).
    new_content:
        The note body with the updated ``## Repair`` section upserted.
    new_tags:
        The tag list with ``influx:<stage>-terminal`` appended when the
        cap was just crossed.  Existing tags are preserved verbatim.
    attempts:
        The post-bump per-stage counter.
    cap_reached:
        ``True`` when ``attempts >= REPAIR_COUNTED_CAP``.
    terminal_tag:
        The ``influx:<stage>-terminal`` literal for *stage*.
    terminal_tag_added:
        ``True`` when the terminal tag was *newly* appended on this
        record (i.e. cap reached AND tag was not already present).
        Callers use this as the trigger for the "stage marked terminal"
        warning so the log fires exactly once per stage flip.
    """

    counters: RepairCounters
    new_content: str
    new_tags: list[str]
    attempts: int
    cap_reached: bool
    terminal_tag: str
    terminal_tag_added: bool


def record_counted_failure(
    *,
    content: str,
    tags: list[str],
    stage: CountedStage,
    failure_stage: str,
    failure_error: str,
) -> CountedFailureResult:
    """Advance the per-stage counter, upsert ``## Repair``, check cap.

    Performs the canonical four-step pattern that the repair sweep
    repeats for every counted-class failure on tier 2, tier 3, and
    archive download:

    1. Parse the existing ``## Repair`` section from *content*.
    2. Bump the per-stage counter (``bump_tier2`` /
       ``bump_tier3`` / ``bump_archive``) with *failure_stage* and
       *failure_error* attribution.
    3. Upsert the new counters into the note body.
    4. Compare against :data:`REPAIR_COUNTED_CAP`; when reached, append
       ``influx:<stage>-terminal`` to *tags* (idempotent).

    Parameters
    ----------
    content:
        The note's current Markdown body.
    tags:
        The current tag list. Not mutated; the returned ``new_tags`` is
        a fresh list with the terminal tag appended on cap.
    stage:
        Which counter to bump (``"tier2"``, ``"tier3"``, ``"archive"``).
    failure_stage:
        Stage attribution for the bump — typically
        ``LCMAError.stage`` or ``ExtractionError.stage``.  For archive
        failures pass the ``kind`` (e.g. ``"oversize"``).
    failure_error:
        Stringified exception for the ``last_error`` field.

    Returns
    -------
    CountedFailureResult
        Post-bump counters, updated content, updated tags, plus
        cap-check signals callers use to drive structured logging.
    """
    counters = parse_repair_section(content)
    if stage == "tier2":
        counters = counters.bump_tier2(stage=failure_stage, error=failure_error)
    elif stage == "tier3":
        counters = counters.bump_tier3(stage=failure_stage, error=failure_error)
    else:  # "archive"
        counters = counters.bump_archive(kind=failure_stage, error=failure_error)

    new_content = upsert_repair_section(content, counters)
    attempts = counters.attempts_for(stage)
    cap_reached = attempts >= REPAIR_COUNTED_CAP
    terminal_tag = terminal_tag_for(stage)

    new_tags = list(tags)
    terminal_tag_added = False
    if cap_reached and terminal_tag not in new_tags:
        new_tags.append(terminal_tag)
        terminal_tag_added = True

    return CountedFailureResult(
        counters=counters,
        new_content=new_content,
        new_tags=new_tags,
        attempts=attempts,
        cap_reached=cap_reached,
        terminal_tag=terminal_tag,
        terminal_tag_added=terminal_tag_added,
    )
