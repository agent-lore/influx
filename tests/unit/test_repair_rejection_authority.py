"""Tests for sweep tag-merge rejection authority (US-021, AC-M3-6).

Verifies that the sweep's tag-merge step honours the rejection
authority defined in FR-NOTE-6: ``influx:rejected:<profile>`` blocks
``profile:<profile>`` from being (re-)added, and the ``## Profile
Relevance`` section is not created or refreshed for rejected profiles.
"""

from __future__ import annotations

from influx.notes import (
    ProfileRelevanceEntry,
    build_profile_relevance_for_rewrite,
    merge_tags,
)

# ── Shared fixtures ──────────────────────────────────────────────────

PROFILE_A = "profile-a"
PROFILE_B = "profile-b"


def _sweep_tags(
    *,
    existing_tags: list[str],
    new_tags: list[str],
) -> list[str]:
    """Wrapper mimicking the sweep's merge_tags call."""
    return merge_tags(existing_tags=existing_tags, new_tags=new_tags)


# ── AC 2: Sweep on profile A, note carries rejected profile B ────────


class TestSweepHonoursRejection:
    """AC-M3-6 positive: sweep on profile A against a note carrying
    ``influx:rejected:<profile_B>`` + ``profile:A`` +
    ``influx:repair-needed``.
    """

    def test_rejected_profile_tag_preserved(self) -> None:
        """(i) ``influx:rejected:<profile_B>`` is preserved."""
        existing = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
            "influx:repair-needed",
            "text:html",
            "source:arxiv",
        ]
        # Sweep modified tags: cleared repair-needed
        new = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
            "text:html",
            "source:arxiv",
        ]
        result = _sweep_tags(existing_tags=existing, new_tags=new)
        assert f"influx:rejected:{PROFILE_B}" in result

    def test_rejected_profile_not_readded(self) -> None:
        """(ii) ``profile:<profile_B>`` is NOT (re-)added."""
        existing = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
            "influx:repair-needed",
            "text:html",
            "source:arxiv",
        ]
        # Simulate a scenario where new_tags includes profile:B
        # (e.g. from a concurrent write or source re-match)
        new = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",
            f"influx:rejected:{PROFILE_B}",
            "text:html",
            "source:arxiv",
        ]
        result = _sweep_tags(existing_tags=existing, new_tags=new)
        assert f"profile:{PROFILE_B}" not in result
        assert f"profile:{PROFILE_A}" in result

    def test_sweep_scenario_full_tag_set(self) -> None:
        """Full sweep scenario: profile A sweep, note has rejected B.

        Verifies all three aspects:
        (i) ``influx:rejected:<profile_B>`` preserved,
        (ii) ``profile:<profile_B>`` NOT re-added,
        (iii) ``profile:<profile_A>`` survives.
        """
        existing = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
            "influx:repair-needed",
            "influx:archive-missing",
            "text:abstract-only",
            "source:arxiv",
            "cat:cs.AI",
        ]
        # After sweep stages: archive cleared, text upgraded,
        # repair-needed cleared
        new = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
            "text:html",
            "source:arxiv",
            "cat:cs.AI",
        ]
        result = _sweep_tags(existing_tags=existing, new_tags=new)

        # (i) rejection preserved
        assert f"influx:rejected:{PROFILE_B}" in result
        # (ii) rejected profile NOT re-added
        assert f"profile:{PROFILE_B}" not in result
        # profile A survives
        assert f"profile:{PROFILE_A}" in result
        # Influx-owned tags from new_tags
        assert "text:html" in result
        assert "source:arxiv" in result
        # Cleared tags not present
        assert "influx:repair-needed" not in result
        assert "influx:archive-missing" not in result
        # External tags preserved
        assert "cat:cs.AI" in result

    def test_profile_relevance_not_refreshed_for_rejected(self) -> None:
        """(iii) ``## Profile Relevance`` NOT created or refreshed
        for ``profile_B`` during the repair pass.

        The sweep does not compute new profile relevance entries; it
        passes empty ``new_entries`` for profiles it doesn't re-evaluate.
        ``build_profile_relevance_for_rewrite`` must keep old entries for
        rejected profiles and not create new ones.
        """
        old_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=8,
                reason="Relevant to profile A.",
            ),
            ProfileRelevanceEntry(
                profile_name=PROFILE_B,
                score=6,
                reason="Old profile B relevance (frozen).",
            ),
        ]
        # Sweep does not compute new entries — empty list
        new_entries: list[ProfileRelevanceEntry] = []
        tags = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
            "text:html",
        ]

        resolved = build_profile_relevance_for_rewrite(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )

        by_name = {e.profile_name: e for e in resolved}
        # profile_B is rejected — old entry frozen, not refreshed
        assert PROFILE_B in by_name
        assert by_name[PROFILE_B].score == 6
        assert by_name[PROFILE_B].reason == "Old profile B relevance (frozen)."
        # profile_A is NOT rejected but has no new entry — not included
        # (sweep didn't re-evaluate it)
        assert PROFILE_A not in by_name

    def test_profile_relevance_not_created_for_rejected(self) -> None:
        """Rejected profile with no old entry: no entry is created."""
        old_entries: list[ProfileRelevanceEntry] = []
        new_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_B,
                score=9,
                reason="Fresh profile B entry.",
            ),
        ]
        tags = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
        ]

        resolved = build_profile_relevance_for_rewrite(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )

        by_name = {e.profile_name: e for e in resolved}
        # profile_B is rejected — new entry blocked
        assert PROFILE_B not in by_name


# ── AC 3: Negative test — no rejection ───────────────────────────────


class TestNoRejectionNormalBehaviour:
    """Without ``influx:rejected:<profile_B>``, normal behaviour applies.

    ``profile:<profile_B>`` may be (re-)added and the corresponding
    ``## Profile Relevance`` entry may be (re-)created/refreshed.
    """

    def test_profile_readded_without_rejection(self) -> None:
        """Without rejection, profile:B may be (re-)added."""
        existing = [
            f"profile:{PROFILE_A}",
            "influx:repair-needed",
            "text:html",
            "source:arxiv",
        ]
        # new_tags includes profile:B (source matches B again)
        new = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",
            "text:html",
            "source:arxiv",
        ]
        result = _sweep_tags(existing_tags=existing, new_tags=new)
        assert f"profile:{PROFILE_B}" in result
        assert f"profile:{PROFILE_A}" in result

    def test_profile_relevance_refreshed_without_rejection(self) -> None:
        """Without rejection, profile B relevance is refreshed."""
        old_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_B,
                score=6,
                reason="Old profile B relevance.",
            ),
        ]
        new_entries = [
            ProfileRelevanceEntry(
                profile_name=PROFILE_B,
                score=9,
                reason="Updated profile B relevance.",
            ),
        ]
        # No influx:rejected:profile-b tag
        tags = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",
            "text:html",
        ]

        resolved = build_profile_relevance_for_rewrite(
            old_entries=old_entries,
            new_entries=new_entries,
            tags=tags,
        )

        by_name = {e.profile_name: e for e in resolved}
        # profile_B is NOT rejected — new entry used
        assert PROFILE_B in by_name
        assert by_name[PROFILE_B].score == 9
        assert by_name[PROFILE_B].reason == "Updated profile B relevance."

    def test_existing_profile_preserved_without_rejection(self) -> None:
        """Without rejection, existing profile tags survive the merge."""
        existing = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",
            "influx:repair-needed",
            "text:html",
            "source:arxiv",
        ]
        new = [
            f"profile:{PROFILE_A}",
            "text:html",
            "source:arxiv",
        ]
        result = _sweep_tags(existing_tags=existing, new_tags=new)
        # profile:B was in existing, union-merged
        assert f"profile:{PROFILE_B}" in result
        assert f"profile:{PROFILE_A}" in result


# ── Edge cases ────────────────────────────────────────────────────────


class TestRejectionEdgeCases:
    """Edge cases for the rejection guard in sweep context."""

    def test_multiple_rejections(self) -> None:
        """Multiple rejected profiles all honoured."""
        existing = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
            "influx:rejected:profile-c",
            "influx:repair-needed",
            "text:html",
        ]
        new = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",
            "profile:profile-c",
            f"influx:rejected:{PROFILE_B}",
            "influx:rejected:profile-c",
            "text:html",
        ]
        result = _sweep_tags(existing_tags=existing, new_tags=new)
        assert f"profile:{PROFILE_B}" not in result
        assert "profile:profile-c" not in result
        assert f"profile:{PROFILE_A}" in result
        assert f"influx:rejected:{PROFILE_B}" in result
        assert "influx:rejected:profile-c" in result

    def test_rejection_guard_applied_on_version_conflict_merge(
        self,
    ) -> None:
        """Simulates the version_conflict re-merge path.

        When a version_conflict occurs, the sweep re-reads the note
        and merges tags.  The merge must still apply the rejection
        guard.
        """
        # refreshed_tags (from re-read after version_conflict)
        refreshed = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",  # concurrent write added this
            f"influx:rejected:{PROFILE_B}",
            "influx:repair-needed",
            "text:html",
            "source:arxiv",
        ]
        # sweep's intended tags
        sweep_tags = [
            f"profile:{PROFILE_A}",
            f"influx:rejected:{PROFILE_B}",
            "text:html",
            "source:arxiv",
        ]
        result = merge_tags(
            existing_tags=refreshed,
            new_tags=sweep_tags,
        )
        assert f"profile:{PROFILE_B}" not in result
        assert f"influx:rejected:{PROFILE_B}" in result
        assert f"profile:{PROFILE_A}" in result
