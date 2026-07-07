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

    def test_has_title_heading_recognises_indented_h1(self) -> None:
        # _split_title strips the line, so an indented "# Title" IS a title;
        # the lenient detector must agree and not reattach a second one.
        assert cn._has_title_heading("   # Indented Title\n\n## Archive\n")
        body = "   # Indented Title\n\n## Archive\npath: p\n"
        note = cn.parse_lenient(body, fallback_title="Doc Title")
        assert note.title == "Indented Title"

    def test_has_title_heading_excludes_h2(self) -> None:
        # A "## " heading is not a title — the fallback must still fire.
        assert not cn._has_title_heading("## Archive\n## Summary\n")
        note = cn.parse_lenient("## Archive\n## Summary\n", fallback_title="Doc")
        assert note.title == "Doc"


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

    def test_idempotent_when_path_is_indented(self) -> None:
        # parse_archive_path strips the body, so an indented lone path: is a
        # valid archive path — upsert must treat it as present, not duplicate.
        content = "# T\n\n## Archive\n  path: existing.pdf\n\n## User Notes\n"
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
    """Exact-byte insert/replace behaviour the repair_counters ## Repair
    placement (PR 5) delegates to. Pins the insertion-point fallback chain
    (before Profile Relevance → before User Notes → EOF) and in-place
    replacement without blank-line accumulation.
    """

    _RENDERED = "## Repair\n- tier2_attempts: 1\n"

    def test_insert_before_profile_relevance(self) -> None:
        content = (
            "# T\n\n## Summary\ns\n\n"
            "## Profile Relevance\n### p\nScore: 5/10\nr\n\n## User Notes\n"
        )
        assert cn.upsert_section_text(content, REPAIR, self._RENDERED) == (
            "# T\n\n## Summary\ns\n\n## Repair\n- tier2_attempts: 1\n\n"
            "## Profile Relevance\n### p\nScore: 5/10\nr\n\n## User Notes\n"
        )

    def test_insert_fallback_before_user_notes(self) -> None:
        content = "# T\n\n## Summary\ns\n\n## User Notes\nMINE\n"
        assert cn.upsert_section_text(content, REPAIR, self._RENDERED) == (
            "# T\n\n## Summary\ns\n\n## Repair\n- tier2_attempts: 1\n\n"
            "## User Notes\nMINE\n"
        )

    def test_insert_fallback_at_eof(self) -> None:
        content = "# T\n\n## Summary\ns\n"
        assert cn.upsert_section_text(content, REPAIR, self._RENDERED) == (
            "# T\n\n## Summary\ns\n\n## Repair\n- tier2_attempts: 1\n"
        )

    def test_replace_in_place_no_blank_line_accumulation(self) -> None:
        content = "# T\n\n## Repair\n- tier2_attempts: 1\n\n## User Notes\nMINE\n"
        result = cn.upsert_section_text(
            content, REPAIR, "## Repair\n- tier2_attempts: 2\n"
        )
        assert result == (
            "# T\n\n## Repair\n- tier2_attempts: 2\n\n## User Notes\nMINE\n"
        )
        assert "\n\n\n" not in result

    def test_crlf_note_section_and_separators_written_lf(self) -> None:
        # Influx-owned sections are written LF (as upsert_archive_path does for
        # the path: line). On a CRLF note the replaced ## Repair block AND its
        # adjacent separators come out LF — note the extra LF where the
        # preceding CRLF blank line meets the new "\n\n" separator — while the
        # trailing ## User Notes region keeps its CRLF bytes verbatim.
        content = (
            "# T\n\n## Repair\n- tier2_attempts: 1\n\n## User Notes\nMINE\n"
        ).replace("\n", "\r\n")
        assert cn.upsert_section_text(
            content, REPAIR, "## Repair\n- tier2_attempts: 2\n"
        ) == (
            "# T\r\n\r\n\n## Repair\n- tier2_attempts: 2\n\n## User Notes\r\nMINE\r\n"
        )


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


# ── render_tier3_sections / section inserts ─────────────────────────


def test_render_tier3_sections_shape() -> None:
    rendered = cn.render_tier3_sections(TIER3)
    assert rendered.startswith("## Claims\n- c1\n- c2\n")
    assert "## Datasets & Benchmarks\n- d1\n" in rendered
    assert "## Builds On\n- b1\n" in rendered
    assert rendered.endswith("## Open Questions\n- q1\n")


def test_render_tier3_sections_emits_all_four_headings_when_lists_empty() -> None:
    rendered = cn.render_tier3_sections(Tier3Extraction(claims=["c1"]))
    for heading in (
        "## Claims",
        "## Datasets & Benchmarks",
        "## Builds On",
        "## Open Questions",
    ):
        assert heading in rendered


class TestSectionInsertBytes:
    """Exact-byte splice behaviour for the section-insert ops.

    Pins the precise inserted bytes and blank-line shape (byte-identical to
    the legacy repair_hooks helpers PR 2 replaced), the insertion-point
    fallback chain (before Profile Relevance → before User Notes → EOF), and
    byte-exact preservation of the surrounding note incl. ``## User Notes``.
    """

    def test_insert_full_text_before_profile_relevance(self) -> None:
        content = (
            "# T\n\n## Summary\ns\n\n## Profile Relevance\npr\n\n## User Notes\nMINE\n"
        )
        assert cn.insert_full_text_section(content, "FT") == (
            "# T\n\n## Summary\ns\n\n\n## Full Text\nFT\n\n"
            "## Profile Relevance\npr\n\n## User Notes\nMINE\n"
        )

    def test_insert_full_text_fallback_before_user_notes(self) -> None:
        content = "# T\n\n## Summary\ns\n\n## User Notes\nMINE\n"
        result = cn.insert_full_text_section(content, "FT")
        assert result == (
            "# T\n\n## Summary\ns\n\n\n## Full Text\nFT\n\n## User Notes\nMINE\n"
        )
        # User Notes region preserved byte-exactly.
        idx = cn.find_user_notes_start(result)
        assert result[idx:] == "## User Notes\nMINE\n"

    def test_insert_full_text_fallback_eof(self) -> None:
        content = "# T\n\n## Summary\ns\n"
        assert cn.insert_full_text_section(content, "FT") == (
            "# T\n\n## Summary\ns\n\n## Full Text\nFT\n\n"
        )

    def test_insert_tier3_before_profile_relevance(self) -> None:
        content = (
            "# T\n\n## Full Text\ntext\n\n"
            "## Profile Relevance\npr\n\n## User Notes\nMINE\n"
        )
        tier3 = Tier3Extraction(
            claims=["C1"], datasets=["D1"], builds_on=["B1"], open_questions=["Q1"]
        )
        assert cn.insert_tier3_sections(content, tier3) == (
            "# T\n\n## Full Text\ntext\n\n\n"
            "## Claims\n- C1\n\n## Datasets & Benchmarks\n- D1\n\n"
            "## Builds On\n- B1\n\n## Open Questions\n- Q1\n\n"
            "## Profile Relevance\npr\n\n## User Notes\nMINE\n"
        )

    def test_insert_tier3_preserves_user_notes_region(self) -> None:
        content = (
            "# T\n\n## Full Text\ntext\n\n## Profile Relevance\npr\n\n"
            "## User Notes\nMINE  \n\n\n"
        )
        result = cn.insert_tier3_sections(content, TIER3)
        idx = cn.find_user_notes_start(result)
        assert result[idx:] == "## User Notes\nMINE  \n\n\n"


# ── ProfileRelevanceEntry identity across modules ───────────────────


def test_profile_relevance_entry_shared_type() -> None:
    from influx.notes import ProfileRelevanceEntry as NotesPRE
    from influx.renderer import ProfileRelevanceEntry as RendererPRE

    assert NotesPRE is ProfileRelevanceEntry
    assert RendererPRE is ProfileRelevanceEntry


def test_render_profile_relevance_body_empty() -> None:
    assert cn.render_profile_relevance_body([]) == ""


class TestReplaceProfileRelevanceSection:
    """Exact-byte replacement — the section end-boundary was converged onto
    the shared _section_span helper (PR 6 / #253 review); these pin that the
    output is byte-identical to the prior find("\\n## ") logic.
    """

    _ENTRIES = [
        ProfileRelevanceEntry("a", 8, "r1"),
        ProfileRelevanceEntry("b", 4, "r2"),
    ]

    def test_replace_mid_note_before_user_notes(self) -> None:
        content = (
            "# T\n\n## Summary\ns\n\n"
            "## Profile Relevance\n### old\nScore: 1/10\nx\n\n## User Notes\nMINE\n"
        )
        assert cn.replace_profile_relevance_section(content, self._ENTRIES) == (
            "# T\n\n## Summary\ns\n\n## Profile Relevance\n"
            "### a\nScore: 8/10\nr1\n\n### b\nScore: 4/10\nr2\n\n## User Notes\nMINE\n"
        )

    def test_replace_followed_by_another_section(self) -> None:
        content = (
            "# T\n\n## Profile Relevance\n### old\nScore: 1/10\nx\n\n"
            "## Repair\n- a: 1\n"
        )
        assert cn.replace_profile_relevance_section(content, self._ENTRIES) == (
            "# T\n\n## Profile Relevance\n"
            "### a\nScore: 8/10\nr1\n\n### b\nScore: 4/10\nr2\n\n## Repair\n- a: 1\n"
        )

    def test_replace_last_section_at_eof(self) -> None:
        content = (
            "# T\n\n## Summary\ns\n\n## Profile Relevance\n### old\nScore: 1/10\nx\n"
        )
        assert cn.replace_profile_relevance_section(content, self._ENTRIES) == (
            "# T\n\n## Summary\ns\n\n## Profile Relevance\n"
            "### a\nScore: 8/10\nr1\n\n### b\nScore: 4/10\nr2\n"
        )

    def test_replace_with_empty_entries_keeps_bare_heading(self) -> None:
        content = (
            "# T\n\n## Profile Relevance\n### old\nScore: 1/10\nx\n\n"
            "## User Notes\nMINE\n"
        )
        assert cn.replace_profile_relevance_section(content, []) == (
            "# T\n\n## Profile Relevance\n\n## User Notes\nMINE\n"
        )

    def test_noop_when_absent(self) -> None:
        content = "# T\n\n## Summary\ns\n\n## User Notes\n"
        assert cn.replace_profile_relevance_section(content, self._ENTRIES) == content


def test_constants_are_section_headings() -> None:
    assert ARCHIVE in SECTION_ORDER
    assert FULL_TEXT in SECTION_ORDER
    assert PROFILE_RELEVANCE in SECTION_ORDER
    assert SECTION_ORDER[-1] == USER_NOTES


# NOTE: the transitional legacy-parity suite (repair_hooks / lithos_client /
# repair_counters helpers vs the canonical ops) was retired across PRs 2-5 as
# each legacy helper was deleted in favour of the shared canonical_note op. The
# per-op byte-exactness those cases guarded is now pinned directly by the
# canonical-op tests above and each migrated module's own suite.
