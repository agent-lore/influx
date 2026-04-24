"""Unit tests for tag-merging and confidence helpers (US-005, FR-NOTE-5/6/7/8)."""

from __future__ import annotations

from influx.notes import merge_tags, recompute_confidence

# ── FR-NOTE-5: Influx-owned tags fully replaced ─────────────────────


class TestInfluxOwnedReplacement:
    """Influx-owned prefix/exact tags are fully replaced by new_tags."""

    def test_source_prefix_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["source:rss"],
            new_tags=["source:arxiv"],
        )
        assert "source:arxiv" in result
        assert "source:rss" not in result

    def test_arxiv_id_prefix_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["arxiv-id:old.1234"],
            new_tags=["arxiv-id:new.5678"],
        )
        assert "arxiv-id:new.5678" in result
        assert "arxiv-id:old.1234" not in result

    def test_cat_prefix_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["cat:cs.AI"],
            new_tags=["cat:cs.RO", "cat:cs.CL"],
        )
        assert "cat:cs.RO" in result
        assert "cat:cs.CL" in result
        assert "cat:cs.AI" not in result

    def test_text_prefix_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["text:abstract-only"],
            new_tags=["text:full"],
        )
        assert "text:full" in result
        assert "text:abstract-only" not in result

    def test_ingested_by_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["ingested-by:old"],
            new_tags=["ingested-by:influx"],
        )
        assert "ingested-by:influx" in result
        assert "ingested-by:old" not in result

    def test_schema_prefix_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["schema:0"],
            new_tags=["schema:1"],
        )
        assert "schema:1" in result
        assert "schema:0" not in result

    def test_full_text_exact_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["full-text"],
            new_tags=[],
        )
        assert "full-text" not in result

    def test_full_text_added_when_new(self) -> None:
        result = merge_tags(
            existing_tags=[],
            new_tags=["full-text"],
        )
        assert "full-text" in result

    def test_influx_repair_needed_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["influx:repair-needed"],
            new_tags=[],
        )
        assert "influx:repair-needed" not in result

    def test_influx_archive_missing_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["influx:archive-missing"],
            new_tags=["influx:archive-missing"],
        )
        assert result.count("influx:archive-missing") == 1

    def test_influx_deep_extracted_replaced(self) -> None:
        result = merge_tags(
            existing_tags=[],
            new_tags=["influx:deep-extracted"],
        )
        assert "influx:deep-extracted" in result

    def test_influx_text_terminal_replaced(self) -> None:
        result = merge_tags(
            existing_tags=["influx:text-terminal"],
            new_tags=[],
        )
        assert "influx:text-terminal" not in result

    def test_multiple_influx_owned_replaced_together(self) -> None:
        result = merge_tags(
            existing_tags=[
                "source:rss",
                "arxiv-id:old.1234",
                "schema:0",
                "full-text",
                "influx:repair-needed",
            ],
            new_tags=[
                "source:arxiv",
                "arxiv-id:new.5678",
                "schema:1",
                "ingested-by:influx",
            ],
        )
        assert "source:arxiv" in result
        assert "arxiv-id:new.5678" in result
        assert "schema:1" in result
        assert "ingested-by:influx" in result
        # Old ones removed
        assert "source:rss" not in result
        assert "arxiv-id:old.1234" not in result
        assert "schema:0" not in result
        assert "full-text" not in result
        assert "influx:repair-needed" not in result


# ── FR-NOTE-6: profile:* union merge with rejection guard ───────────


class TestProfileUnionMerge:
    """profile:* tags are merged as union, guarded by rejection tags."""

    def test_profile_union_existing_and_new(self) -> None:
        result = merge_tags(
            existing_tags=["profile:research"],
            new_tags=["profile:engineering"],
        )
        assert "profile:research" in result
        assert "profile:engineering" in result

    def test_profile_dedup_in_union(self) -> None:
        result = merge_tags(
            existing_tags=["profile:research"],
            new_tags=["profile:research"],
        )
        assert result.count("profile:research") == 1

    def test_rejected_profile_not_readded(self) -> None:
        """FR-NOTE-6: influx:rejected:<profile> blocks profile:<profile>."""
        result = merge_tags(
            existing_tags=[
                "profile:research",
                "influx:rejected:research",
            ],
            new_tags=["profile:research"],
        )
        assert "profile:research" not in result
        assert "influx:rejected:research" in result

    def test_rejected_profile_from_new_tags(self) -> None:
        """Rejection from new_tags also blocks the profile."""
        result = merge_tags(
            existing_tags=["profile:research"],
            new_tags=["influx:rejected:research"],
        )
        assert "profile:research" not in result
        assert "influx:rejected:research" in result

    def test_unrejected_profile_survives(self) -> None:
        """Profiles not rejected survive the merge."""
        result = merge_tags(
            existing_tags=[
                "profile:research",
                "profile:engineering",
                "influx:rejected:research",
            ],
            new_tags=["profile:engineering"],
        )
        assert "profile:engineering" in result
        assert "profile:research" not in result


# ── FR-NOTE-7: External tags preserved verbatim ─────────────────────


class TestExternalTagsPreserved:
    """Tags not matching Influx-owned prefixes/values are preserved."""

    def test_favourite_preserved(self) -> None:
        result = merge_tags(
            existing_tags=["favourite", "source:rss"],
            new_tags=["source:arxiv"],
        )
        assert "favourite" in result

    def test_reading_queue_preserved(self) -> None:
        result = merge_tags(
            existing_tags=["reading-queue", "to-review"],
            new_tags=["source:arxiv", "ingested-by:influx"],
        )
        assert "reading-queue" in result
        assert "to-review" in result

    def test_custom_prefix_tag_preserved(self) -> None:
        result = merge_tags(
            existing_tags=["custom:my-tag"],
            new_tags=["source:arxiv"],
        )
        assert "custom:my-tag" in result

    def test_external_tags_not_duplicated_by_new(self) -> None:
        """External tags come only from existing, not from new."""
        result = merge_tags(
            existing_tags=["favourite"],
            new_tags=["source:arxiv"],
        )
        assert result.count("favourite") == 1


# ── FR-NOTE-8: Confidence recompute ─────────────────────────────────


class TestRecomputeConfidence:
    """Confidence = max(existing, current_max_score / 10.0)."""

    def test_existing_higher(self) -> None:
        assert recompute_confidence(
            existing_confidence=0.9,
            current_max_score=7,
        ) == 0.9

    def test_new_higher(self) -> None:
        assert recompute_confidence(
            existing_confidence=0.5,
            current_max_score=8,
        ) == 0.8

    def test_equal(self) -> None:
        assert recompute_confidence(
            existing_confidence=0.7,
            current_max_score=7,
        ) == 0.7

    def test_zero_existing(self) -> None:
        assert recompute_confidence(
            existing_confidence=0.0,
            current_max_score=9,
        ) == 0.9

    def test_perfect_score(self) -> None:
        assert recompute_confidence(
            existing_confidence=0.8,
            current_max_score=10,
        ) == 1.0


# ── Full merge scenario ─────────────────────────────────────────────


class TestFullMergeScenario:
    """End-to-end merge combining all rules."""

    def test_complete_rewrite_merge(self) -> None:
        result = merge_tags(
            existing_tags=[
                "source:rss",
                "arxiv-id:2601.00001",
                "cat:cs.AI",
                "text:abstract-only",
                "ingested-by:influx",
                "schema:1",
                "full-text",
                "influx:repair-needed",
                "profile:research",
                "profile:engineering",
                "influx:rejected:engineering",
                "favourite",
                "reading-queue",
            ],
            new_tags=[
                "source:arxiv",
                "arxiv-id:2601.00001",
                "cat:cs.RO",
                "text:full",
                "ingested-by:influx",
                "schema:1",
                "influx:deep-extracted",
                "profile:research",
                "profile:data-science",
            ],
        )
        # Influx-owned replaced
        assert "source:arxiv" in result
        assert "source:rss" not in result
        assert "cat:cs.RO" in result
        assert "cat:cs.AI" not in result
        assert "text:full" in result
        assert "text:abstract-only" not in result
        assert "full-text" not in result
        assert "influx:repair-needed" not in result
        assert "influx:deep-extracted" in result

        # profile union, guarded
        assert "profile:research" in result
        assert "profile:data-science" in result
        assert "profile:engineering" not in result  # rejected
        assert "influx:rejected:engineering" in result

        # external preserved
        assert "favourite" in result
        assert "reading-queue" in result
