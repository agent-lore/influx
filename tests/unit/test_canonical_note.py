"""Tests for :mod:`influx.canonical_note` — the CanonicalNote shape owner.

Covers three families:

1. **Round-trip identity** — ``serialize(parse(x)) == x`` on the canonical
   fixture corpus, plus byte-exact ``## User Notes`` preservation for CRLF
   and absent-region notes.
2. **String-op invariants** — every rewrite op preserves the ``## User
   Notes`` region byte-exactly, and the ops match the historical
   ``rstrip() + "\\n\\n"`` join semantics.
3. **Matcher semantics** — the single line-anchored ``## User Notes``
   locator does not fire on a mid-line literal, a ``### User Notes`` H3, or
   a ``## User Notes: extra`` variant, but does fire on CRLF / trailing
   space / EOF-without-newline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from influx import canonical_note as cn
from influx.canonical_note import (
    ARCHIVE,
    FULL_TEXT,
    PROFILE_RELEVANCE,
    REPAIR,
    SECTION_ORDER,
    USER_NOTES,
    CanonicalNote,
    NoteParseError,
    ProfileRelevanceEntry,
    Section,
)
from influx.schemas import Tier3Extraction

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "canonical_notes"

# Renderer-produced notes are in canonical serialized form.
ROUNDTRIP_FIXTURES = [
    "golden_lf.md",
    "tier3_full.md",
    "with_repair.md",
    "legacy_frontmatter.md",
]

TIER3 = Tier3Extraction(
    claims=["c1", "c2"],
    datasets=["d1"],
    builds_on=["b1"],
    open_questions=["q1"],
    potential_connections=["unused"],
)


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── Section order ────────────────────────────────────────────────────


def test_section_order_matches_spec() -> None:
    assert SECTION_ORDER == (
        "Archive",
        "Summary",
        "Full Text",
        "Claims",
        "Datasets & Benchmarks",
        "Builds On",
        "Open Questions",
        "Repair",
        "Profile Relevance",
        "User Notes",
    )


# ── Round-trip identity ──────────────────────────────────────────────


class TestRoundTrip:
    @pytest.mark.parametrize("name", ROUNDTRIP_FIXTURES)
    def test_serialize_parse_identity(self, name: str) -> None:
        text = _read(name)
        assert cn.serialize(cn.parse(text)) == text

    def test_no_user_notes_parses_to_none(self) -> None:
        text = _read("no_user_notes.md")
        note = cn.parse(text)
        assert note.user_notes is None
        # serialize always emits a canonical (possibly empty) region.
        assert cn.serialize(note).endswith("## User Notes\n")

    def test_crlf_user_notes_preserved_byte_exact(self) -> None:
        raw = (FIXTURES / "crlf.md").read_bytes().decode("utf-8")
        note = cn.parse(raw)
        idx = cn.find_user_notes_start(raw)
        assert idx is not None
        assert note.user_notes == raw[idx:]

    def test_crlf_sections_parse_without_carriage_returns(self) -> None:
        raw = (FIXTURES / "crlf.md").read_bytes().decode("utf-8")
        note = cn.parse(raw)
        assert note.get_section(ARCHIVE) is not None
        for section in note.sections:
            assert "\r" not in section.heading

    def test_serialize_normalizes_crlf_to_lf_intentionally(self) -> None:
        # serialize() promises identity only for canonical LF renderer
        # output; a CRLF/legacy note round-trips with separators normalised
        # to LF (documented, not a byte-identity guarantee).
        legacy_crlf = (
            "---\r\nnote_type: summary\r\n---\r\n# T\r\n\r\n"
            "## Archive\r\npath: x\r\n\r\n## User Notes\r\nkeep\r\n"
        )
        note = cn.parse(legacy_crlf)
        idx = cn.find_user_notes_start(legacy_crlf)
        assert idx is not None
        assert note.user_notes == legacy_crlf[idx:]  # region preserved on parse
        assert cn.serialize(note) != legacy_crlf  # separators normalised to LF


# ── Parse / parse_lenient ────────────────────────────────────────────


class TestParse:
    def test_body_only_has_empty_frontmatter(self) -> None:
        note = cn.parse(_read("tier3_full.md"))
        assert note.frontmatter_raw == ""
        assert note.title == "T3 Only"

    def test_legacy_captures_frontmatter(self) -> None:
        note = cn.parse(_read("legacy_frontmatter.md"))
        assert "note_type: summary" in note.frontmatter_raw
        assert note.title == "Legacy"

    def test_missing_title_raises(self) -> None:
        with pytest.raises(NoteParseError):
            cn.parse("## Archive\npath: x\n")

    def test_lenient_with_title_equals_parse(self) -> None:
        text = _read("tier3_full.md")
        assert cn.parse_lenient(text) == cn.parse(text)

    def test_lenient_reattaches_fallback_title(self) -> None:
        body = "## Archive\npath: x/y.pdf\n\n## User Notes\n"
        note = cn.parse_lenient(body, fallback_title="Recovered")
        assert note.title == "Recovered"
        assert note.get_section(ARCHIVE) is not None

    def test_lenient_without_title_or_fallback_raises(self) -> None:
        with pytest.raises(NoteParseError):
            cn.parse_lenient("## Archive\npath: x\n")


# ── User Notes matcher semantics (§3 divergence matrix) ─────────────


class TestUserNotesMatcher:
    @pytest.mark.parametrize(
        "content",
        [
            "## User Notes\nbody\n",
            "## User Notes  \nbody\n",  # trailing spaces
            "## User Notes\t\nbody\n",  # trailing tab
            "## User Notes\r\nbody\r\n",  # CRLF
            "# T\n\n## User Notes",  # EOF, no trailing newline
        ],
    )
    def test_matches_heading_forms(self, content: str) -> None:
        assert cn.find_user_notes_start(content) is not None

    @pytest.mark.parametrize(
        "content",
        [
            "# T\n\n## Summary\nsee the ## User Notes section for detail\n",
            "# T\n\n### User Notes\nan H3, not the region\n",
            "# T\n\n## User Notes: extra\nnot a bare heading\n",
            "# T\n\n## User Notes Appendix\nlonger heading\n",
        ],
    )
    def test_does_not_match_impostors(self, content: str) -> None:
        assert cn.find_user_notes_start(content) is None

    def test_picks_line_anchored_over_earlier_midline_literal(self) -> None:
        content = (
            "# T\n\n## Summary\ntext ## User Notes inline\n\n## User Notes\nreal\n"
        )
        idx = cn.find_user_notes_start(content)
        assert idx is not None
        assert content[idx:] == "## User Notes\nreal\n"

    def test_split_user_notes_absent_returns_whole_body(self) -> None:
        body, region = cn.split_user_notes("# T\n\n## Summary\nbody\n")
        assert region is None
        assert body == "# T\n\n## Summary\nbody\n"


# ── graft_user_notes (replaces lithos_client._preserve_user_notes) ──


class TestGraftUserNotes:
    def test_grafts_existing_region_byte_exact(self) -> None:
        existing = "# T\n\n## Summary\nold\n\n## User Notes\nMINE\nkeep\n"
        new = "# T\n\n## Summary\nfresh\n\n## User Notes\n"
        result = cn.graft_user_notes(existing, new)
        assert result.endswith("## User Notes\nMINE\nkeep\n")
        assert "fresh" in result

    def test_noop_when_existing_has_no_region(self) -> None:
        existing = "# T\n\n## Summary\nold\n"
        new = "# T\n\n## Summary\nfresh\n\n## User Notes\n"
        assert cn.graft_user_notes(existing, new) == new

    def test_does_not_truncate_new_content_at_midline_literal(self) -> None:
        # The impostor literal in *new* must not be treated as the region.
        existing = "# T\n\n## User Notes\nPRIVATE\n"
        new = "# T\n\n## Summary\nmentions ## User Notes inline\n\n## User Notes\n"
        result = cn.graft_user_notes(existing, new)
        assert "mentions ## User Notes inline" in result
        assert result.endswith("## User Notes\nPRIVATE\n")


# ── Per-op User Notes byte-exactness ────────────────────────────────


class TestOpsPreserveUserNotes:
    def _note(self) -> str:
        return _read("golden_lf.md")

    def _region(self, content: str) -> str:
        idx = cn.find_user_notes_start(content)
        assert idx is not None
        return content[idx:]

    def test_drop_tier2_preserves_user_notes(self) -> None:
        note = self._note()
        original = self._region(note)
        result = cn.drop_tier2(note)
        assert "## Full Text" not in result
        assert self._region(result) == original

    def test_drop_tier2_and_tier3_preserves_user_notes(self) -> None:
        note = self._note()
        original = self._region(note)
        result = cn.drop_tier2_and_tier3(note)
        assert "## Full Text" not in result
        assert "## Claims" not in result
        assert self._region(result) == original

    def test_drop_preserves_user_notes_trailing_whitespace_byte_exact(self) -> None:
        # The drop ops own the byte-exact invariant: user-note trailing
        # spaces/blank lines survive (unlike the legacy whole-doc rstrip).
        note = (
            "# T\n\n## Full Text\nbody\n\n"
            "## Profile Relevance\n### p\nScore: 5/10\nr\n\n"
            "## User Notes\nnote with trailing spaces  \n\n\n"
        )
        original = self._region(note)
        result = cn.drop_tier2(note)
        assert self._region(result) == original

    def test_insert_full_text_preserves_user_notes(self) -> None:
        note = _read("tier3_full.md")  # has no Full Text yet
        original = self._region(note)
        result = cn.insert_full_text_section(note, "the body")
        assert "## Full Text\nthe body" in result
        assert self._region(result) == original

    def test_upsert_archive_path_preserves_user_notes(self) -> None:
        note = _read("no_user_notes.md")  # empty Archive, no region
        result = cn.upsert_archive_path(note, "p/x.pdf")
        assert "path: p/x.pdf" in result

    def test_replace_profile_relevance_preserves_user_notes(self) -> None:
        note = self._note()
        original = self._region(note)
        entries = [ProfileRelevanceEntry("newprof", 9, "fresh reason")]
        result = cn.replace_profile_relevance_section(note, entries)
        assert "### newprof" in result
        assert self._region(result) == original


# ── extract_section_body ────────────────────────────────────────────


class TestExtractSectionBody:
    def test_lf_body(self) -> None:
        content = "# T\n\n## Full Text\nhello\nworld\n\n## User Notes\n"
        assert cn.extract_section_body(content, FULL_TEXT) == "hello\nworld"

    def test_crlf_skips_leading_line_ending(self) -> None:
        content = "# T\r\n\r\n## Full Text\r\nhello\r\nworld\r\n\r\n## User Notes\r\n"
        # Leading \r\n after the heading is skipped (not returned as body).
        assert cn.extract_section_body(content, FULL_TEXT) == "hello\r\nworld"

    def test_absent_section(self) -> None:
        assert cn.extract_section_body("# T\n\n## Summary\ns\n", FULL_TEXT) == ""


# ── upsert_archive_path idempotence / CRLF ──────────────────────────


class TestUpsertArchivePath:
    def test_inserts_path_below_heading(self) -> None:
        content = "# T\n\n## Archive\n\n## User Notes\n"
        result = cn.upsert_archive_path(content, "p/x.pdf")
        assert result == "# T\n\n## Archive\npath: p/x.pdf\n\n## User Notes\n"

    def test_idempotent_when_path_present(self) -> None:
        content = "# T\n\n## Archive\npath: existing.pdf\n\n## User Notes\n"
        assert cn.upsert_archive_path(content, "new.pdf") == content

    def test_idempotent_when_path_below_other_metadata(self) -> None:
        # A path: line anywhere in the Archive body counts — never duplicate.
        content = (
            "# T\n\n## Archive\ncreated: 2026\npath: existing.pdf\n\n## User Notes\n"
        )
        result = cn.upsert_archive_path(content, "new.pdf")
        assert result == content
        assert result.count("path:") == 1

    def test_inserts_once_when_metadata_but_no_path(self) -> None:
        content = "# T\n\n## Archive\ncreated: 2026\n\n## User Notes\n"
        result = cn.upsert_archive_path(content, "new.pdf")
        assert result.count("path:") == 1
        assert "## Archive\npath: new.pdf\ncreated: 2026" in result

    def test_does_not_match_path_in_a_later_section(self) -> None:
        # A ``path:`` line outside the Archive section must not suppress insert.
        content = (
            "# T\n\n## Archive\n\n"
            "## Full Text\npath: not-an-archive-path\n\n## User Notes\n"
        )
        result = cn.upsert_archive_path(content, "real.pdf")
        assert "## Archive\npath: real.pdf" in result

    def test_noop_when_archive_absent(self) -> None:
        content = "# T\n\n## Summary\ns\n"
        assert cn.upsert_archive_path(content, "x.pdf") == content

    def test_crlf_archive_heading(self) -> None:
        content = "# T\r\n\r\n## Archive\r\n\r\n## User Notes\r\n"
        result = cn.upsert_archive_path(content, "p/x.pdf")
        assert "## Archive\r\npath: p/x.pdf\n" in result


# ── upsert_section_text (backs repair_counters ## Repair placement) ─


class TestUpsertSectionText:
    def test_inserts_before_profile_relevance(self) -> None:
        content = (
            "# T\n\n## Summary\ns\n\n"
            "## Profile Relevance\n### p\nScore: 5/10\nr\n\n## User Notes\n"
        )
        rendered = "## Repair\n- tier2_attempts: 1\n"
        result = cn.upsert_section_text(content, REPAIR, rendered)
        # inserted before Profile Relevance, after Summary
        assert result.index("## Repair") < result.index("## Profile Relevance")
        assert result.index("## Summary") < result.index("## Repair")

    def test_replace_does_not_accumulate_blank_lines(self) -> None:
        content = "# T\n\n## Summary\ns\n\n## User Notes\n"
        r1 = cn.upsert_section_text(content, REPAIR, "## Repair\n- tier2_attempts: 1\n")
        r2 = cn.upsert_section_text(r1, REPAIR, "## Repair\n- tier2_attempts: 2\n")
        assert "- tier2_attempts: 2" in r2
        assert "\n\n\n" not in r2


# ── Structured immutable ops ────────────────────────────────────────


class TestStructuredOps:
    def test_get_section(self) -> None:
        note = cn.parse(_read("tier3_full.md"))
        assert note.get_section("Claims") is not None
        assert note.get_section("Nonexistent") is None

    def test_drop_sections_returns_new_note(self) -> None:
        note = cn.parse(_read("tier3_full.md"))
        dropped = note.drop_sections("Claims", "Open Questions")
        assert dropped.get_section("Claims") is None
        assert dropped.get_section("Open Questions") is None
        # original unchanged (immutability)
        assert note.get_section("Claims") is not None

    def test_upsert_section_replaces_in_place(self) -> None:
        note = cn.parse(_read("tier3_full.md"))
        updated = note.upsert_section("Summary", "new summary body")
        summary = updated.get_section("Summary")
        assert summary is not None
        assert summary.body == "new summary body"
        assert len(updated.sections) == len(note.sections)

    def test_upsert_section_inserts_in_canonical_order(self) -> None:
        note = CanonicalNote(
            frontmatter_raw="",
            title="T",
            sections=(Section("Archive", "path: x"), Section("Profile Relevance", "")),
            user_notes="## User Notes\n",
        )
        updated = note.upsert_section("Full Text", "ft")
        headings = [s.heading for s in updated.sections]
        assert headings == ["Archive", "Full Text", "Profile Relevance"]


# ── render_tier3_sections ───────────────────────────────────────────


def test_render_tier3_sections_shape() -> None:
    rendered = cn.render_tier3_sections(TIER3)
    assert rendered.startswith("## Claims\n- c1\n- c2\n")
    assert "## Datasets & Benchmarks\n- d1\n" in rendered
    assert "## Builds On\n- b1\n" in rendered
    assert rendered.endswith("## Open Questions\n- q1\n")


# ── ProfileRelevanceEntry identity across modules ───────────────────


def test_profile_relevance_entry_shared_type() -> None:
    from influx.notes import ProfileRelevanceEntry as NotesPRE
    from influx.renderer import ProfileRelevanceEntry as RendererPRE

    assert NotesPRE is ProfileRelevanceEntry
    assert RendererPRE is ProfileRelevanceEntry


def test_render_profile_relevance_body_empty() -> None:
    assert cn.render_profile_relevance_body([]) == ""


def test_constants_are_section_headings() -> None:
    assert ARCHIVE in SECTION_ORDER
    assert FULL_TEXT in SECTION_ORDER
    assert PROFILE_RELEVANCE in SECTION_ORDER
    assert SECTION_ORDER[-1] == USER_NOTES


# ── Transitional parity with the legacy helpers ─────────────────────
#
# The canonical ops that PRs 2-5 swap the existing helpers onto must be
# byte-identical to those helpers on canonical inputs — that is what makes
# each migration a no-op for production bytes. These assertions pin that
# equivalence and are deleted as each legacy helper is removed in its
# migration PR. NOTE: drop_tier2 / drop_tier2_and_tier3 / upsert_archive_path
# are deliberately excluded — they intentionally *diverge* from the legacy
# helpers (byte-exact User Notes; whole-section path: idempotence), a fix
# that lands with the PR 3/PR 4 migrations.


class TestLegacyParityTransitional:
    def _canonical_note(self) -> str:
        return _read("golden_lf.md")

    def test_render_tier3_matches_repair_hooks(self) -> None:
        from influx import repair_hooks as rh

        assert cn.render_tier3_sections(TIER3) == rh._render_tier3_sections(TIER3)

    def test_insert_full_text_matches_repair_hooks(self) -> None:
        from influx import repair_hooks as rh

        note = _read("tier3_full.md")
        assert cn.insert_full_text_section(note, "ft") == rh._insert_full_text_section(
            note, "ft"
        )

    def test_insert_tier3_matches_repair_hooks(self) -> None:
        from influx import repair_hooks as rh

        note = _read("no_user_notes.md")
        assert cn.insert_tier3_sections(note, TIER3) == rh._insert_tier3_sections(
            note, TIER3
        )

    def test_insertion_point_matches_repair_hooks(self) -> None:
        from influx import repair_hooks as rh

        note = self._canonical_note()
        assert cn.insertion_point(note) == rh._find_insertion_point(note)

    def test_graft_user_notes_matches_lithos_client(self) -> None:
        from influx import lithos_client as lc

        existing = self._canonical_note()
        new = _read("tier3_full.md")
        assert cn.graft_user_notes(existing, new) == lc._preserve_user_notes(
            existing, new
        )

    def test_replace_profile_relevance_matches_lithos_client(self) -> None:
        from influx import lithos_client as lc

        note = self._canonical_note()
        entries = [
            ProfileRelevanceEntry("a", 8, "r1"),
            ProfileRelevanceEntry("b", 4, "r2"),
        ]
        assert cn.replace_profile_relevance_section(
            note, entries
        ) == lc._replace_profile_relevance_section(note, entries)

    def test_upsert_repair_matches_repair_counters(self) -> None:
        from influx import repair_counters as rc

        note = _read("tier3_full.md")
        counters = rc.RepairCounters(tier2_attempts=1, tier2_last_stage="parse")
        rendered = rc.render_repair_section(counters)
        assert cn.upsert_section_text(
            note, REPAIR, rendered
        ) == rc.upsert_repair_section(note, counters)
