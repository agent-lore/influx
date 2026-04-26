"""Unit tests for multi-profile tag and Profile Relevance merging (US-005).

Verifies that ``merge_tags()`` union-merges ``profile:*`` tags,
``merge_profile_relevance_union()`` preserves entries from non-current
profiles, rejection authority is honoured, and the content-level merge
helper correctly replaces the ``## Profile Relevance`` section.
"""

from __future__ import annotations

from influx.lithos_client import (
    _merge_profile_relevance_in_content,
    _replace_profile_relevance_section,
)
from influx.notes import (
    ProfileRelevanceEntry,
    merge_profile_relevance_union,
    merge_tags,
    parse_note,
    parse_profile_relevance,
    render_note,
)

# ── Shared constants ─────────────────────────────────────────────────

PROFILE_A = "ai-robotics"
PROFILE_B = "web-tech"


def _make_note_content(
    *,
    title: str = "Shared Paper",
    source_url: str = "https://arxiv.org/abs/2601.00001",
    tags: list[str],
    profile_entries: list[ProfileRelevanceEntry],
) -> str:
    """Render a minimal canonical note for testing."""
    return render_note(
        title=title,
        source_url=source_url,
        tags=tags,
        confidence=0.8,
        archive_path=None,
        summary="A shared paper abstract.",
        keywords=[],
        profile_entries=profile_entries,
    )


# ── AC-M3-2: profile:* tag union merge ──────────────────────────────


class TestTagUnionMerge:
    """merge_tags() union-merges profile:* tags (FR-NOTE-6)."""

    def test_union_merge_two_profiles(self) -> None:
        """Tags from profile A and B are both present after merge."""
        existing = [
            f"profile:{PROFILE_A}",
            "source:arxiv",
            "ingested-by:influx",
        ]
        new = [
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
        ]
        result = merge_tags(existing_tags=existing, new_tags=new)
        assert f"profile:{PROFILE_A}" in result
        assert f"profile:{PROFILE_B}" in result

    def test_union_merge_idempotent(self) -> None:
        """Re-merging the same profile tag does not duplicate it."""
        existing = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
        ]
        new = [
            f"profile:{PROFILE_A}",
            "source:arxiv",
            "ingested-by:influx",
        ]
        result = merge_tags(existing_tags=existing, new_tags=new)
        profile_tags = [t for t in result if t.startswith("profile:")]
        assert len(profile_tags) == 2
        assert f"profile:{PROFILE_A}" in profile_tags
        assert f"profile:{PROFILE_B}" in profile_tags

    def test_disjoint_single_profile(self) -> None:
        """A note matching only one profile gets only that profile tag (AC-M3-3)."""
        existing: list[str] = []
        new = [
            f"profile:{PROFILE_A}",
            "source:arxiv",
            "ingested-by:influx",
        ]
        result = merge_tags(existing_tags=existing, new_tags=new)
        profile_tags = [t for t in result if t.startswith("profile:")]
        assert profile_tags == [f"profile:{PROFILE_A}"]

    def test_rejected_profile_not_readded(self) -> None:
        """Union merge respects rejection guard (FR-NOTE-6)."""
        existing = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
        ]
        new = [
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
        ]
        result = merge_tags(existing_tags=existing, new_tags=new)
        assert f"profile:{PROFILE_A}" in result
        assert f"profile:{PROFILE_B}" not in result
        assert f"influx:rejected:{PROFILE_B}" in result


# ── Profile Relevance union merge ────────────────────────────────────


class TestProfileRelevanceUnionMerge:
    """Union-merge preserves old entries for non-current profiles."""

    def test_union_preserves_old_profile_entry(self) -> None:
        """Profile A's entry is preserved when Profile B adds a new entry."""
        old_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=8,
                reason="Relevant to AI robotics.",
            ),
        ]
        new_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_B,
                score=7,
                reason="Relevant to web tech.",
            ),
        ]
        tags = [f"profile:{PROFILE_A}", f"profile:{PROFILE_B}"]

        result = merge_profile_relevance_union(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )

        by_name = {e.profile_name: e for e in result}
        assert PROFILE_A in by_name
        assert by_name[PROFILE_A].score == 8
        assert PROFILE_B in by_name
        assert by_name[PROFILE_B].score == 7

    def test_new_entry_supersedes_old_for_same_profile(self) -> None:
        """When new_entries has an entry for the same profile, it takes precedence."""
        old_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=6,
                reason="Old.",
            ),
        ]
        new_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=9,
                reason="Updated.",
            ),
        ]
        tags = [f"profile:{PROFILE_A}"]

        result = merge_profile_relevance_union(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )

        assert len(result) == 1
        assert result[0].score == 9
        assert result[0].reason == "Updated."

    def test_rejected_profile_keeps_old_entry(self) -> None:
        """Rejected profile's old entry is preserved, new entry is dropped (AC-09-K)."""
        old_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=8,
                reason="Relevant to AI robotics.",
            ),
        ]
        new_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=9,
                reason="Updated entry.",
            ),
            ProfileRelevanceEntry(
                profile_name=PROFILE_B,
                score=7,
                reason="Web tech match.",
            ),
        ]
        tags = [
            f"profile:{PROFILE_B}",
            f"influx:rejected:{PROFILE_A}",
        ]

        result = merge_profile_relevance_union(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )

        by_name = {e.profile_name: e for e in result}
        # Profile B accepted: new entry used
        assert PROFILE_B in by_name
        assert by_name[PROFILE_B].score == 7
        # Profile A rejected: old entry preserved
        assert PROFILE_A in by_name
        assert by_name[PROFILE_A].score == 8

    def test_idempotent_remerge(self) -> None:
        """Re-ingesting a note for a profile already present keeps existing data."""
        old_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=8,
                reason="AI robotics.",
            ),
            ProfileRelevanceEntry(
                profile_name=PROFILE_B,
                score=7,
                reason="Web tech.",
            ),
        ]
        # Re-ingest from profile A only
        new_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=8,
                reason="AI robotics.",
            ),
        ]
        tags = [f"profile:{PROFILE_A}", f"profile:{PROFILE_B}"]

        result = merge_profile_relevance_union(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )

        by_name = {e.profile_name: e for e in result}
        assert len(by_name) == 2
        assert PROFILE_A in by_name
        assert PROFILE_B in by_name
        # Profile B preserved from old entries
        assert by_name[PROFILE_B].score == 7

    def test_empty_old_entries(self) -> None:
        """First profile write — no old entries to merge."""
        old_entries: list[ProfileRelevanceEntry] = []
        new_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=8,
                reason="AI robotics.",
            ),
        ]
        tags = [f"profile:{PROFILE_A}"]

        result = merge_profile_relevance_union(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )

        assert len(result) == 1
        assert result[0].profile_name == PROFILE_A


# ── Content-level Profile Relevance merge ───────���────────────────────


class TestContentLevelProfileRelevanceMerge:
    """_merge_profile_relevance_in_content() correctly merges sections."""

    def test_merge_adds_old_profile_entry_to_new_content(self) -> None:
        """Profile A's entry from existing note is added to Profile B's new note."""
        existing_tags = [
            f"profile:{PROFILE_A}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        existing_content = _make_note_content(
            tags=existing_tags,
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name=PROFILE_A,
                    score=8,
                    reason="Relevant to AI robotics.",
                ),
            ],
        )

        new_tags = [
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        new_content = _make_note_content(
            tags=new_tags,
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name=PROFILE_B,
                    score=7,
                    reason="Relevant to web tech.",
                ),
            ],
        )

        merged_tags = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        merged_content = _merge_profile_relevance_in_content(
            existing_content, new_content, merged_tags
        )

        # Parse and verify both entries are present
        parsed = parse_note(merged_content)
        entries = parse_profile_relevance(parsed)
        by_name = {e.profile_name: e for e in entries}

        assert PROFILE_A in by_name
        assert by_name[PROFILE_A].score == 8
        assert PROFILE_B in by_name
        assert by_name[PROFILE_B].score == 7

    def test_merge_preserves_user_notes_section(self) -> None:
        """Content-level merge does not damage ## User Notes."""
        tags = [
            f"profile:{PROFILE_A}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        existing_content = _make_note_content(
            tags=tags,
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name=PROFILE_A,
                    score=8,
                    reason="AI.",
                ),
            ],
        )

        new_tags = [
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        new_content = _make_note_content(
            tags=new_tags,
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name=PROFILE_B,
                    score=7,
                    reason="Web.",
                ),
            ],
        )

        merged_content = _merge_profile_relevance_in_content(
            existing_content, new_content, tags + [f"profile:{PROFILE_B}"]
        )

        assert "## User Notes" in merged_content

    def test_merge_with_no_old_entries_returns_new(self) -> None:
        """When existing note has no Profile Relevance entries, return new content."""
        new_tags = [
            f"profile:{PROFILE_A}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        new_content = _make_note_content(
            tags=new_tags,
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name=PROFILE_A,
                    score=8,
                    reason="AI.",
                ),
            ],
        )
        # Existing content with empty profile relevance
        existing_content = _make_note_content(
            tags=new_tags,
            profile_entries=[],
        )

        merged = _merge_profile_relevance_in_content(
            existing_content, new_content, new_tags
        )
        assert merged == new_content


# ── _replace_profile_relevance_section ───────────────────────────────


class TestReplaceProfileRelevanceSection:
    """_replace_profile_relevance_section() correctly replaces the section body."""

    def test_replaces_single_entry_with_two(self) -> None:
        """Replace one entry with two merged entries."""
        tags = [
            f"profile:{PROFILE_A}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        original = _make_note_content(
            tags=tags,
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name=PROFILE_A,
                    score=8,
                    reason="AI.",
                ),
            ],
        )

        merged_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=8,
                reason="AI.",
            ),
            ProfileRelevanceEntry(
                profile_name=PROFILE_B,
                score=7,
                reason="Web.",
            ),
        ]

        result = _replace_profile_relevance_section(original, merged_entries)

        parsed = parse_note(result)
        entries = parse_profile_relevance(parsed)
        assert len(entries) == 2
        by_name = {e.profile_name: e for e in entries}
        assert PROFILE_A in by_name
        assert PROFILE_B in by_name


# ── Negative: running one profile alone preserves other profiles ─────


class TestSingleProfileRunPreservesOthers:
    """Running one profile alone MUST NOT remove existing profile data.

    This exercises the negative test case from AC-M3-2: running web-tech
    alone preserves an existing profile:ai-robotics tag and its
    ## Profile Relevance entry.
    """

    def test_single_profile_run_does_not_remove_other_profile(self) -> None:
        """Merge from a single-profile run preserves other profiles."""
        # Existing note has both profiles
        existing_tags = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        existing_content = _make_note_content(
            tags=existing_tags,
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name=PROFILE_A,
                    score=8,
                    reason="AI robotics.",
                ),
                ProfileRelevanceEntry(
                    profile_name=PROFILE_B,
                    score=7,
                    reason="Web tech.",
                ),
            ],
        )

        # Profile B re-runs alone, only provides its own entry
        new_tags = [
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        new_content = _make_note_content(
            tags=new_tags,
            profile_entries=[
                ProfileRelevanceEntry(
                    profile_name=PROFILE_B,
                    score=7,
                    reason="Web tech.",
                ),
            ],
        )

        # Tags are union-merged
        merged_tags = merge_tags(existing_tags=existing_tags, new_tags=new_tags)
        assert f"profile:{PROFILE_A}" in merged_tags
        assert f"profile:{PROFILE_B}" in merged_tags

        # Profile Relevance is union-merged
        merged_content = _merge_profile_relevance_in_content(
            existing_content, new_content, merged_tags
        )

        parsed = parse_note(merged_content)
        entries = parse_profile_relevance(parsed)
        by_name = {e.profile_name: e for e in entries}
        assert PROFILE_A in by_name, "profile:ai-robotics entry must be preserved"
        assert by_name[PROFILE_A].score == 8
        assert PROFILE_B in by_name
        assert by_name[PROFILE_B].score == 7
