"""Unit tests for the multi-profile note merge (FR-NOTE-6, lithos task c1196e30).

:func:`influx.note_merge.merge_into_existing` decides what a note looks
like when a second Profile ingests an item Lithos already holds: profile
tags and Profile Relevance entries are unioned, and the richer body wins,
so a later summary-only write never replaces an earlier full-text body.
"""

from __future__ import annotations

from influx.note_merge import MergedNote, merge_into_existing
from influx.notes import parse_note, parse_profile_relevance
from influx.renderer import ProfileRelevanceEntry, render_note
from influx.schemas import Tier3Extraction

TITLE = "Shared Paper"
BASE_TAGS = ["source:arxiv", "arxiv-id:2601.00001", "ingested-by:influx", "schema:1"]


def _note(
    profile: str,
    score: int,
    *,
    full_text: str | None = None,
    tier3: bool = False,
    reason: str | None = None,
    archive_path: str | None = "arxiv/2026/01/2601.00001.pdf",
) -> str:
    return render_note(
        title=TITLE,
        tags=[f"profile:{profile}", "ingested-by:influx"],
        confidence=score / 10,
        archive_path=archive_path,
        summary=f"Summary written for {profile}.",
        keywords=[],
        profile_entries=[
            ProfileRelevanceEntry(
                profile_name=profile,
                score=score,
                reason=reason or f"Relevant to {profile}.",
            )
        ],
        full_text=full_text,
        tier3_extraction=(
            Tier3Extraction(
                claims=[f"Claim found by {profile}"],
                datasets=[],
                builds_on=[],
                open_questions=[],
                potential_connections=[],
            )
            if tier3
            else None
        ),
    )


def _tags(profile: str, *extra: str) -> list[str]:
    return [*BASE_TAGS, f"profile:{profile}", *extra]


def _entries(content: str) -> dict[str, ProfileRelevanceEntry]:
    return {e.profile_name: e for e in parse_profile_relevance(parse_note(content))}


def _merge(
    *,
    existing_content: str,
    existing_tags: list[str],
    incoming_content: str,
    incoming_tags: list[str],
    existing_confidence: float = 0.7,
    incoming_confidence: float = 0.8,
    keep_existing_body: bool = False,
) -> MergedNote | None:
    return merge_into_existing(
        title=TITLE,
        existing_content=existing_content,
        existing_tags=existing_tags,
        existing_confidence=existing_confidence,
        incoming_content=incoming_content,
        incoming_tags=incoming_tags,
        incoming_confidence=incoming_confidence,
        keep_existing_body=keep_existing_body,
    )


class TestNewProfileJoinsNote:
    def test_second_profile_tag_and_entry_are_added(self) -> None:
        merged = _merge(
            existing_content=_note("ai-agents", 8),
            existing_tags=_tags("ai-agents"),
            incoming_content=_note("knowledge-systems", 7),
            incoming_tags=_tags("knowledge-systems"),
        )

        assert merged is not None
        assert "profile:ai-agents" in merged.tags
        assert "profile:knowledge-systems" in merged.tags
        assert merged.added_profiles == ("knowledge-systems",)
        entries = _entries(merged.content)
        assert entries["ai-agents"].score == 8
        assert entries["knowledge-systems"].score == 7

    def test_confidence_is_the_higher_of_the_two(self) -> None:
        merged = _merge(
            existing_content=_note("ai-agents", 9),
            existing_tags=_tags("ai-agents"),
            incoming_content=_note("knowledge-systems", 7),
            incoming_tags=_tags("knowledge-systems"),
            existing_confidence=0.9,
            incoming_confidence=0.7,
        )

        assert merged is not None
        assert merged.confidence == 0.9

    def test_external_tags_survive(self) -> None:
        merged = _merge(
            existing_content=_note("ai-agents", 8),
            existing_tags=_tags("ai-agents", "reading-list"),
            incoming_content=_note("knowledge-systems", 7),
            incoming_tags=_tags("knowledge-systems"),
        )

        assert merged is not None
        assert "reading-list" in merged.tags


class TestRicherBodyWins:
    def test_poorer_incoming_keeps_existing_full_text_and_tags(self) -> None:
        """The fan-out concern from the PR #300 review: a later Tier 1 write
        must not replace an earlier Profile's full text or Tier 3."""
        existing = _note("ai-agents", 9, full_text="The whole paper.", tier3=True)
        existing_tags = _tags(
            "ai-agents", "full-text", "text:html", "influx:deep-extracted"
        )

        merged = _merge(
            existing_content=existing,
            existing_tags=existing_tags,
            incoming_content=_note("knowledge-systems", 7),
            incoming_tags=_tags("knowledge-systems", "text:abstract-only"),
        )

        assert merged is not None
        assert merged.kept_existing_body is True
        assert "## Full Text\nThe whole paper." in merged.content
        assert "Claim found by ai-agents" in merged.content
        assert "Summary written for ai-agents." in merged.content
        assert "Summary written for knowledge-systems." not in merged.content
        assert {"full-text", "text:html", "influx:deep-extracted"} <= set(merged.tags)
        assert "text:abstract-only" not in merged.tags
        assert set(_entries(merged.content)) == {"ai-agents", "knowledge-systems"}

    def test_richer_incoming_replaces_body_and_influx_owned_tags(self) -> None:
        existing_tags = _tags(
            "ai-agents", "text:abstract-only", "influx:tier2-terminal"
        )

        merged = _merge(
            existing_content=_note("ai-agents", 7),
            existing_tags=existing_tags,
            incoming_content=_note(
                "knowledge-systems", 9, full_text="The whole paper.", tier3=True
            ),
            incoming_tags=_tags(
                "knowledge-systems", "full-text", "text:pdf", "influx:deep-extracted"
            ),
        )

        assert merged is not None
        assert merged.kept_existing_body is False
        assert "## Full Text\nThe whole paper." in merged.content
        assert "Summary written for knowledge-systems." in merged.content
        assert {"full-text", "text:pdf", "influx:deep-extracted"} <= set(merged.tags)
        assert "text:abstract-only" not in merged.tags
        assert "influx:tier2-terminal" not in merged.tags
        assert {"profile:ai-agents", "profile:knowledge-systems"} <= set(merged.tags)
        assert set(_entries(merged.content)) == {"ai-agents", "knowledge-systems"}

    def test_equal_rank_keeps_existing_body(self) -> None:
        merged = _merge(
            existing_content=_note("ai-agents", 8, full_text="Existing text."),
            existing_tags=_tags("ai-agents", "full-text"),
            incoming_content=_note("knowledge-systems", 9, full_text="Incoming text."),
            incoming_tags=_tags("knowledge-systems", "full-text"),
        )

        assert merged is not None
        assert merged.kept_existing_body is True
        assert "Existing text." in merged.content
        assert "Incoming text." not in merged.content

    def test_tier3_outranks_full_text_alone(self) -> None:
        merged = _merge(
            existing_content=_note("ai-agents", 8, full_text="Existing text."),
            existing_tags=_tags("ai-agents", "full-text"),
            incoming_content=_note(
                "knowledge-systems", 9, full_text="Incoming text.", tier3=True
            ),
            incoming_tags=_tags(
                "knowledge-systems", "full-text", "influx:deep-extracted"
            ),
        )

        assert merged is not None
        assert merged.kept_existing_body is False

    def test_richer_incoming_that_would_drop_the_archive_keeps_existing(
        self,
    ) -> None:
        """A full-text body whose archive download failed must not replace
        a body that points at an archived copy."""
        merged = _merge(
            existing_content=_note("ai-agents", 7),
            existing_tags=_tags("ai-agents"),
            incoming_content=_note(
                "knowledge-systems", 9, full_text="Body.", archive_path=None
            ),
            incoming_tags=_tags(
                "knowledge-systems", "full-text", "influx:archive-missing"
            ),
        )

        assert merged is not None
        assert merged.kept_existing_body is True
        assert "path: arxiv/2026/01/2601.00001.pdf" in merged.content
        assert "influx:archive-missing" not in merged.tags

    def test_keep_existing_body_flag_overrides_rank(self) -> None:
        """Used as the fallback when the richer body is too large for Lithos."""
        merged = _merge(
            existing_content=_note("ai-agents", 7),
            existing_tags=_tags("ai-agents"),
            incoming_content=_note("knowledge-systems", 9, full_text="Huge."),
            incoming_tags=_tags("knowledge-systems", "full-text"),
            keep_existing_body=True,
        )

        assert merged is not None
        assert merged.kept_existing_body is True
        assert "Huge." not in merged.content
        assert "full-text" not in merged.tags
        assert "profile:knowledge-systems" in merged.tags

    def test_richer_incoming_carries_user_notes_and_repair(self) -> None:
        repair = (
            "## Repair\n"
            "- tier2_attempts: 3\n- tier3_attempts: 0\n- archive_attempts: 0\n"
        )
        existing = _note("ai-agents", 7).replace(
            "## Profile Relevance", f"{repair}\n## Profile Relevance"
        )
        existing = existing.rstrip() + "\nHand-written annotation.\n"

        merged = _merge(
            existing_content=existing,
            existing_tags=_tags("ai-agents"),
            incoming_content=_note("knowledge-systems", 9, full_text="Body."),
            incoming_tags=_tags("knowledge-systems", "full-text"),
        )

        assert merged is not None
        assert merged.kept_existing_body is False
        assert "Hand-written annotation." in merged.content
        assert "tier2_attempts: 3" in merged.content


class TestNothingToMerge:
    def test_same_profile_and_no_richer_body_is_a_no_op(self) -> None:
        assert (
            _merge(
                existing_content=_note("ai-agents", 8, full_text="Text."),
                existing_tags=_tags("ai-agents", "full-text"),
                incoming_content=_note("ai-agents", 9),
                incoming_tags=_tags("ai-agents"),
            )
            is None
        )

    def test_rejected_profile_is_not_re_added(self) -> None:
        assert (
            _merge(
                existing_content=_note("ai-agents", 8),
                existing_tags=_tags("ai-agents", "influx:rejected:knowledge-systems"),
                incoming_content=_note("knowledge-systems", 7),
                incoming_tags=_tags("knowledge-systems"),
            )
            is None
        )

    def test_same_profile_richer_body_upgrades_the_note(self) -> None:
        merged = _merge(
            existing_content=_note("ai-agents", 8, reason="First read."),
            existing_tags=_tags("ai-agents"),
            incoming_content=_note("ai-agents", 9, full_text="Body.", reason="Second."),
            incoming_tags=_tags("ai-agents", "full-text"),
        )

        assert merged is not None
        assert merged.added_profiles == ()
        assert merged.kept_existing_body is False
        assert _entries(merged.content)["ai-agents"].reason == "Second."


class TestReadShapes:
    def test_existing_content_without_title_heading_still_merges(self) -> None:
        """``lithos_read`` can serve content with the ``# Title`` heading
        stripped; the old merge parsed it strictly and silently dropped the
        Profile Relevance union."""
        existing = _note("ai-agents", 8).split("\n", 2)[2]
        assert not existing.startswith("# ")

        merged = _merge(
            existing_content=existing,
            existing_tags=_tags("ai-agents"),
            incoming_content=_note("knowledge-systems", 7),
            incoming_tags=_tags("knowledge-systems"),
        )

        assert merged is not None
        assert merged.content.startswith("## Archive")
        assert "### ai-agents" in merged.content
        assert "### knowledge-systems" in merged.content

    def test_unparseable_existing_body_is_kept_and_tags_still_merge(self) -> None:
        existing = "Free-form text someone wrote, no canonical sections."

        merged = _merge(
            existing_content=existing,
            existing_tags=_tags("ai-agents"),
            incoming_content=_note("knowledge-systems", 9, full_text="Body."),
            incoming_tags=_tags("knowledge-systems", "full-text"),
        )

        assert merged is not None
        assert merged.kept_existing_body is True
        assert merged.content == existing
        assert "profile:knowledge-systems" in merged.tags
