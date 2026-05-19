"""Tests for the invalid-source-metadata audit + cleanup helpers (#162).

The module is the post-incident operator path for notes that have
already been persisted with empty/garbled ``source:*`` metadata.  These
tests pin:

* Classification: recoverable (URL / path / id hints present) vs
  unrecoverable (genuine metadata loss).
* Tag rewrites: ``reconstruct_tags`` backfills the source tag,
  drops the in-band terminal flags, and re-arms ``influx:repair-needed``;
  ``tombstone_tags`` layers ``influx:tombstone`` on top of the existing
  terminal state.
* Report formatting: per-note action label, hint preview, and the
  recoverable / unrecoverable totals.
"""

from __future__ import annotations

from influx.audit_invalid_source import (
    INVALID_SOURCE_TAG,
    REPAIR_NEEDED_TAG,
    TEXT_TERMINAL_TAG,
    TOMBSTONE_TAG,
    AuditFinding,
    audit_notes,
    audit_one_note,
    format_audit_report,
    reconstruct_tags,
    tombstone_tags,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_invalid_note(
    *,
    note_id: str = "27d6b6f5-b3ce-4d0a-97c9-4acecc0cb3b8",
    title: str = "Orphan note",
    path: str = "",
    source_url: str = "",
    extra_tags: list[str] | None = None,
) -> dict[str, object]:
    """Build a staging-shaped invalid-source note dict."""
    return {
        "id": note_id,
        "title": title,
        "path": path,
        "source_url": source_url,
        "tags": [
            "profile:retro-computing",
            "text:abstract-only",
            TEXT_TERMINAL_TAG,
            INVALID_SOURCE_TAG,
            *(extra_tags or []),
        ],
    }


# ── Classification ────────────────────────────────────────────────────


class TestAuditOneNote:
    """Classification covers the recoverable / unrecoverable split."""

    def test_unrecoverable_when_no_metadata_signals(self) -> None:
        # Mirrors the staging evidence verbatim: empty source, empty
        # path, no source_url, no recognisable id prefix.
        note = _make_invalid_note(path="", source_url="")
        finding = audit_one_note(note)
        assert finding.recoverable is False
        assert finding.recommended_action == "tombstone"
        assert finding.inferred_source is None
        assert finding.existing_source_tag == ""

    def test_recoverable_when_source_url_points_to_arxiv(self) -> None:
        note = _make_invalid_note(source_url="https://arxiv.org/abs/2601.12345")
        finding = audit_one_note(note)
        assert finding.recoverable is True
        assert finding.recommended_action == "reconstruct"
        assert finding.inferred_source == "arxiv"

    def test_recoverable_when_note_path_implies_source(self) -> None:
        note = _make_invalid_note(path="papers/arxiv/2026/04")
        finding = audit_one_note(note)
        assert finding.recoverable is True
        assert finding.inferred_source == "arxiv"

    def test_recoverable_when_note_id_prefix_is_recognisable(self) -> None:
        # arXiv notes have id prefix ``arxiv-``.  Path/url-empty notes
        # that still carry the id prefix recover cleanly.
        note = _make_invalid_note(note_id="arxiv-2601.12345")
        finding = audit_one_note(note)
        assert finding.recoverable is True
        assert finding.inferred_source == "arxiv"

    def test_existing_non_empty_source_tag_is_preserved(self) -> None:
        # If the existing source tag has a value (even one we don't
        # currently dispatch — e.g. "hackernews") the audit honours it.
        # The text-extraction path handles unsupported but well-formed
        # sources separately; that's not this workflow's job.
        note = _make_invalid_note(
            extra_tags=["source:hackernews"],
        )
        finding = audit_one_note(note)
        assert finding.existing_source_tag == "hackernews"
        assert finding.inferred_source == "hackernews"
        assert finding.recoverable is True


class TestAuditNotes:
    """``audit_notes`` runs over a list preserving order."""

    def test_returns_one_finding_per_note(self) -> None:
        notes = [
            _make_invalid_note(note_id="a"),
            _make_invalid_note(note_id="b", path="papers/arxiv/2026/04"),
        ]
        findings = audit_notes(notes)
        assert len(findings) == 2
        assert [f.note_id for f in findings] == ["a", "b"]
        # a is unrecoverable, b is recoverable.
        assert findings[0].recoverable is False
        assert findings[1].recoverable is True


# ── Tag rewrites ──────────────────────────────────────────────────────


class TestReconstructTags:
    """``reconstruct_tags`` re-arms the note for the next sweep."""

    def test_backfills_source_tag_and_clears_terminal_flags(self) -> None:
        note = _make_invalid_note()
        result = reconstruct_tags(note, inferred_source="arxiv")

        assert "source:arxiv" in result
        assert INVALID_SOURCE_TAG not in result
        assert TEXT_TERMINAL_TAG not in result
        assert REPAIR_NEEDED_TAG in result

    def test_drops_existing_empty_or_garbled_source_tag(self) -> None:
        # A note may carry an empty ``source:`` tag from the bad-state
        # write — the reconstruct must replace it, not duplicate it.
        note = _make_invalid_note(extra_tags=["source:"])
        result = reconstruct_tags(note, inferred_source="arxiv")
        # Exactly one source:* tag in the rebuilt list.
        source_tags = [t for t in result if t.startswith("source:")]
        assert source_tags == ["source:arxiv"]

    def test_drops_existing_text_tag(self) -> None:
        # The note carries ``text:abstract-only`` from the in-band
        # terminal flip.  Reconstruct must drop it so the next sweep
        # re-derives the text provenance from a clean cascade.
        note = _make_invalid_note()
        result = reconstruct_tags(note, inferred_source="arxiv")
        assert not any(t.startswith("text:") for t in result)

    def test_preserves_unrelated_tags(self) -> None:
        note = _make_invalid_note(
            extra_tags=["profile:other", "schema:1", "ingested-by:influx"],
        )
        result = reconstruct_tags(note, inferred_source="arxiv")
        for tag in ("profile:other", "schema:1", "ingested-by:influx"):
            assert tag in result

    def test_does_not_mutate_input_note(self) -> None:
        note = _make_invalid_note()
        original_tags = list(note["tags"])  # type: ignore[arg-type]
        reconstruct_tags(note, inferred_source="arxiv")
        assert note["tags"] == original_tags

    def test_drops_existing_tombstone_tag(self) -> None:
        # PR #170 Copilot review: a previously-tombstoned note that
        # becomes recoverable (e.g. because inference paths expanded)
        # must shed the tombstone tag during reconstruct — otherwise
        # the note would still be filtered out by "operator-cleaned"
        # dashboards / sweep selectors despite being re-armed for
        # processing.
        note = _make_invalid_note(extra_tags=[TOMBSTONE_TAG])
        result = reconstruct_tags(note, inferred_source="arxiv")
        assert TOMBSTONE_TAG not in result
        # And the reconstruct still re-arms the note.
        assert REPAIR_NEEDED_TAG in result


class TestTombstoneTags:
    """``tombstone_tags`` adds the tombstone marker and drops repair-needed."""

    def test_adds_tombstone_tag(self) -> None:
        note = _make_invalid_note()
        result = tombstone_tags(note)
        assert TOMBSTONE_TAG in result

    def test_preserves_in_band_terminal_state(self) -> None:
        # The in-band terminal flags document the original state; the
        # tombstone tag layers on top.  Operators reading the note's
        # tags later see the full lifecycle: invalid → text-terminal →
        # tombstone.
        note = _make_invalid_note()
        result = tombstone_tags(note)
        assert INVALID_SOURCE_TAG in result
        assert TEXT_TERMINAL_TAG in result

    def test_drops_repair_needed_if_present(self) -> None:
        note = _make_invalid_note(extra_tags=[REPAIR_NEEDED_TAG])
        result = tombstone_tags(note)
        assert REPAIR_NEEDED_TAG not in result

    def test_idempotent(self) -> None:
        # A second call must not double-tag.
        note = _make_invalid_note()
        once = tombstone_tags(note)
        # Replace the note's tag list with the first-pass output and
        # re-run.  Exactly one tombstone tag in the result.
        note_after = dict(note)
        note_after["tags"] = once
        twice = tombstone_tags(note_after)
        assert twice.count(TOMBSTONE_TAG) == 1

    def test_does_not_mutate_input_note(self) -> None:
        note = _make_invalid_note()
        original_tags = list(note["tags"])  # type: ignore[arg-type]
        tombstone_tags(note)
        assert note["tags"] == original_tags


# ── Report formatting ─────────────────────────────────────────────────


class TestFormatAuditReport:
    """``format_audit_report`` produces an operator-friendly summary."""

    def test_empty_input_returns_no_op_line(self) -> None:
        report = format_audit_report([])
        assert "No notes carry" in report
        assert INVALID_SOURCE_TAG in report

    def test_includes_per_note_action_label(self) -> None:
        findings = [
            AuditFinding(
                note_id="recoverable-id",
                title="Recoverable Note",
                path="papers/arxiv/2026/04",
                source_url="https://arxiv.org/abs/2601.12345",
                existing_source_tag="",
                inferred_source="arxiv",
                tags=(INVALID_SOURCE_TAG,),
            ),
            AuditFinding(
                note_id="lost-id",
                title="Lost Note",
                path="",
                source_url="",
                existing_source_tag="",
                inferred_source=None,
                tags=(INVALID_SOURCE_TAG,),
            ),
        ]
        report = format_audit_report(findings)

        assert "[RECONSTRUCT] recoverable-id" in report
        assert "[TOMBSTONE] lost-id" in report
        assert "1 recoverable, 1 unrecoverable" in report
        # Hint preview surfaces the inferred source for the recoverable
        # note and the "no fallback" note for the unrecoverable one.
        assert "source:arxiv" in report
        assert "no fallback" in report.lower() or "(none" in report

    def test_concise_when_metadata_missing(self) -> None:
        # An unrecoverable note with empty path/source_url should not
        # produce stray ``None`` or ``''`` in the report.
        findings = [
            AuditFinding(
                note_id="x",
                title="",
                path="",
                source_url="",
                existing_source_tag="",
                inferred_source=None,
                tags=(INVALID_SOURCE_TAG,),
            ),
        ]
        report = format_audit_report(findings)
        # Render uses ``(empty)`` for missing path so the column lines
        # up; ``source_url`` is omitted entirely when empty.
        assert "(empty)" in report
        assert "source_url" not in report


# ── Audit + apply round-trip ──────────────────────────────────────────


class TestAuditAndApplyRoundTrip:
    """Audit findings drive the right action without operator override.

    Pins the operator workflow contract: the audit classifies, the
    apply step uses the classification's recommended action.  A
    re-audit of the post-apply state shows the cleanup landed.
    """

    def test_recoverable_note_becomes_dispatchable_after_reconstruct(self) -> None:
        note = _make_invalid_note(source_url="https://arxiv.org/abs/2601.12345")
        finding = audit_one_note(note)
        assert finding.recommended_action == "reconstruct"
        assert finding.inferred_source == "arxiv"

        new_tags = reconstruct_tags(note, inferred_source=finding.inferred_source)

        # Simulate the rewrite by swapping in the new tag list.
        note_after = dict(note)
        note_after["tags"] = new_tags

        # Re-audit: the note is no longer invalid + no longer terminal.
        finding_after = audit_one_note(note_after)
        assert INVALID_SOURCE_TAG not in finding_after.tags
        assert TEXT_TERMINAL_TAG not in finding_after.tags
        assert REPAIR_NEEDED_TAG in finding_after.tags
        assert finding_after.existing_source_tag == "arxiv"

    def test_unrecoverable_note_carries_tombstone_after_apply(self) -> None:
        note = _make_invalid_note()
        finding = audit_one_note(note)
        assert finding.recommended_action == "tombstone"

        new_tags = tombstone_tags(note)
        note_after = dict(note)
        note_after["tags"] = new_tags

        # Tombstone tag present, repair-needed gone, in-band terminal
        # state preserved for audit history.
        assert TOMBSTONE_TAG in new_tags
        assert REPAIR_NEEDED_TAG not in new_tags
        assert INVALID_SOURCE_TAG in new_tags
        assert TEXT_TERMINAL_TAG in new_tags
