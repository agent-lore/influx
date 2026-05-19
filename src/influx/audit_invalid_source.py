"""Audit and cleanup of notes with invalid source metadata (issue #162).

Companion to the in-band repair fix in #150 (which terminalised
mid-flight ``invalid_source_metadata`` failures so the sweep stopped
looping).  This module is the *after-incident* operator path:
already-persisted notes that carry the ``influx:source-invalid`` tag
need a separate audit + cleanup workflow because the in-band path
only stops the retry loop — it does NOT remove or repair the
historical noise.

Concepts
--------
* **Finding** — one note's audit classification.  ``recoverable`` when
  :func:`influx.repair_hooks.infer_note_source` can derive a
  dispatchable source from URL / path / id hints; ``unrecoverable``
  otherwise.
* **Recommended action** — derived from the finding:

  * ``reconstruct`` for recoverable notes — backfill the ``source:*``
    tag, drop ``influx:source-invalid`` + ``influx:text-terminal``,
    re-arm ``influx:repair-needed`` so the next sweep picks the note
    up and re-extracts cleanly.
  * ``tombstone`` for unrecoverable notes — leave the existing
    terminal state in place and add an explicit
    ``influx:tombstone`` tag so the note is discoverable as
    "operator-cleaned, do not re-process".

Both actions are tag-level (no destructive deletes).  Deletion lives
in :mod:`influx-diagnose squatters` for the rare cases where the note
must be removed entirely.

Pure module — no Lithos client dependency, no async, no IO.  The
operator entrypoint in ``scripts/influx-diagnose.py`` wraps it with
the MCP fetch / write plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from influx.repair_hooks import _note_source_tag, infer_note_source

__all__ = [
    "INVALID_SOURCE_TAG",
    "TEXT_TERMINAL_TAG",
    "TOMBSTONE_TAG",
    "AuditFinding",
    "audit_one_note",
    "audit_notes",
    "format_audit_report",
    "reconstruct_tags",
    "tombstone_tags",
]


# Tag literals — single source of truth for the audit/cleanup contract.
INVALID_SOURCE_TAG = "influx:source-invalid"
TEXT_TERMINAL_TAG = "influx:text-terminal"
REPAIR_NEEDED_TAG = "influx:repair-needed"
# Issue #162: applied by the cleanup workflow on unrecoverable notes
# (no URL / path / id hint to infer a source from).  Distinct from
# ``influx:source-invalid`` (which the in-band repair sets) because
# the tombstone signals "operator has reviewed this and decided no
# recovery is possible" — a permanent, post-cleanup state.  Future
# sweeps and dashboards can filter on this tag to exclude
# operator-cleaned notes from "what's still broken?" queries.
TOMBSTONE_TAG = "influx:tombstone"


AuditAction = Literal["reconstruct", "tombstone"]


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """Audit classification for one note.

    ``recoverable`` mirrors :func:`infer_note_source`'s return value:
    when inference yields a dispatchable ``source:*`` suffix the note
    can be reconstructed in-place; otherwise it is tombstoned.
    """

    note_id: str
    title: str
    path: str
    source_url: str
    existing_source_tag: str
    inferred_source: str | None
    tags: tuple[str, ...]

    @property
    def recoverable(self) -> bool:
        """Whether the note has enough metadata to reconstruct."""
        return self.inferred_source is not None

    @property
    def recommended_action(self) -> AuditAction:
        """Default action implied by :attr:`recoverable`."""
        return "reconstruct" if self.recoverable else "tombstone"


def audit_one_note(note: dict[str, Any]) -> AuditFinding:
    """Inspect one note (as returned by ``lithos_read``) and classify it.

    Returns an :class:`AuditFinding` regardless of whether the note
    carries the ``influx:source-invalid`` tag; the caller is expected
    to filter the candidate set upstream (the CLI does this via
    ``lithos_list``).  Accepting any note dict here keeps the helper
    test-friendly.
    """
    tags = tuple(str(t) for t in note.get("tags", []) if isinstance(t, str))
    return AuditFinding(
        note_id=str(note.get("id", "")),
        title=str(note.get("title", "")),
        path=str(note.get("path", "")),
        source_url=str(note.get("source_url", "")),
        existing_source_tag=_note_source_tag(note),
        inferred_source=infer_note_source(note),
        tags=tags,
    )


def audit_notes(notes: list[dict[str, Any]]) -> list[AuditFinding]:
    """Vectorised :func:`audit_one_note` over a list of notes.

    Convenience wrapper — the CLI scans pages of notes returned by
    ``lithos_list`` + ``lithos_read``.  Order-preserving.
    """
    return [audit_one_note(note) for note in notes]


def reconstruct_tags(note: dict[str, Any], *, inferred_source: str) -> list[str]:
    """Compute the tag set for a *reconstruct* action.

    Operations applied to the existing tag list, in order:

    1. Drop any existing ``source:*`` tag.
    2. Append ``source:<inferred_source>`` (the backfilled tag).
    3. Drop ``influx:source-invalid`` and ``influx:text-terminal`` so
       the next sweep re-runs text extraction with a valid source.
    4. Add ``influx:repair-needed`` so the next sweep actually picks
       the note up (the in-band terminal flip cleared it; we re-arm
       it here because the metadata is now repaired).
    5. Drop any pre-existing ``text:*`` tag (e.g.
       ``text:abstract-only``) so the next sweep re-derives the text
       provenance from the cascade rather than carrying over the
       degraded marker.

    Returns the new tag list (caller passes it to ``lithos_write`` /
    ``rewrite``).  Does NOT mutate the input note.
    """
    tags = [str(t) for t in note.get("tags", []) if isinstance(t, str)]
    rebuilt = [
        t
        for t in tags
        if not t.startswith("source:")
        and not t.startswith("text:")
        and t not in {INVALID_SOURCE_TAG, TEXT_TERMINAL_TAG}
    ]
    rebuilt.append(f"source:{inferred_source}")
    if REPAIR_NEEDED_TAG not in rebuilt:
        rebuilt.append(REPAIR_NEEDED_TAG)
    return rebuilt


def tombstone_tags(note: dict[str, Any]) -> list[str]:
    """Compute the tag set for a *tombstone* action.

    Adds ``influx:tombstone`` to the existing tags and removes
    ``influx:repair-needed`` (if present) so subsequent sweeps
    immediately skip the note.  The in-band terminal tags
    (``influx:text-terminal``, ``influx:source-invalid``) are
    *preserved* — they document the original state, and the
    tombstone tag layers on top to signal "operator-cleaned".

    Returns the new tag list.  Does NOT mutate the input note.
    """
    tags = [str(t) for t in note.get("tags", []) if isinstance(t, str)]
    rebuilt = [t for t in tags if t != REPAIR_NEEDED_TAG]
    if TOMBSTONE_TAG not in rebuilt:
        rebuilt.append(TOMBSTONE_TAG)
    return rebuilt


def format_audit_report(findings: list[AuditFinding]) -> str:
    """Return a concise human-readable report of *findings*.

    Layout:

    * Header summary line: counts of recoverable / unrecoverable.
    * One block per finding: id, title, path, action, hints.

    Designed for terminal consumption (``influx-diagnose`` print
    stream).  Stable, line-oriented so operators can ``grep`` or pipe
    to ``less``.
    """
    if not findings:
        return "No notes carry influx:source-invalid — nothing to clean up."

    reconstructable = sum(1 for f in findings if f.recoverable)
    tombstoneable = len(findings) - reconstructable

    lines: list[str] = []
    lines.append(
        f"Found {len(findings)} note(s) tagged {INVALID_SOURCE_TAG}: "
        f"{reconstructable} recoverable, {tombstoneable} unrecoverable.\n"
    )
    for f in findings:
        action = f.recommended_action.upper()
        lines.append(f"  [{action}] {f.note_id}")
        lines.append(f"      title       : {f.title!r}")
        lines.append(f"      path        : {f.path or '(empty)'}")
        if f.source_url:
            lines.append(f"      source_url  : {f.source_url}")
        existing = f.existing_source_tag or "(empty)"
        lines.append(f"      existing tag: source:{existing}")
        if f.recoverable:
            lines.append(
                f"      inferred    : source:{f.inferred_source}  "
                f"(reconstruct will backfill this tag)"
            )
        else:
            lines.append(
                "      inferred    : (none — URL / path / id provide no fallback)"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
