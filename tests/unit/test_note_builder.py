"""Unit tests for the shared Source note-builder helpers (finding 2.2).

Covers the three pieces hoisted out of ``build_arxiv_note_item`` /
``build_rss_note_item`` / ``build_inbox_note_item``:
``append_cascade_outcome_tags``, ``render_note_content`` (the shared
Tier-1 summary-suppression rule), and ``profile_item_dict``.
"""

from __future__ import annotations

from typing import Any

import pytest

from influx.cascade import EnrichedSections
from influx.schemas import Tier1Enrichment, Tier3Extraction
from influx.sources.note_builder import (
    append_cascade_outcome_tags,
    profile_item_dict,
    render_note_content,
)


def _tier1(contributions: list[str] | None = None) -> Tier1Enrichment:
    return Tier1Enrichment(
        contributions=contributions or ["c1"],
        method="m",
        result="r",
        relevance="rel",
    )


def _tier3(builds_on: list[str] | None = None) -> Tier3Extraction:
    return Tier3Extraction(claims=["claim1"], builds_on=builds_on or ["b1"])


# ── append_cascade_outcome_tags ───────────────────────────────────


class TestAppendCascadeOutcomeTags:
    def test_deep_extracted_added_when_tier3_present(self) -> None:
        tags: list[str] = ["source:arxiv"]
        append_cascade_outcome_tags(tags, EnrichedSections(tier3=_tier3()))
        assert "influx:deep-extracted" in tags

    def test_no_deep_extracted_without_tier3(self) -> None:
        tags: list[str] = ["source:arxiv"]
        append_cascade_outcome_tags(tags, EnrichedSections())
        assert "influx:deep-extracted" not in tags

    def test_deep_extracted_deduplicated_against_existing_tags(self) -> None:
        # A pre-seeded influx:deep-extracted is not duplicated — the
        # helper appends each outcome tag at most once.
        tags: list[str] = ["influx:deep-extracted"]
        append_cascade_outcome_tags(tags, EnrichedSections(tier3=_tier3()))
        assert tags.count("influx:deep-extracted") == 1

    def test_repair_and_terminal_flags_appended(self) -> None:
        tags: list[str] = []
        sections = EnrichedSections(
            repair_flags=("influx:repair-needed",),
            terminal_flags=("influx:tier3-terminal",),
        )
        append_cascade_outcome_tags(tags, sections)
        assert tags == ["influx:repair-needed", "influx:tier3-terminal"]

    def test_flags_deduplicated_against_existing_tags(self) -> None:
        tags: list[str] = ["influx:repair-needed"]
        sections = EnrichedSections(
            repair_flags=("influx:repair-needed",),
            terminal_flags=("influx:repair-needed",),
        )
        append_cascade_outcome_tags(tags, sections)
        # The already-present tag is not duplicated.
        assert tags == ["influx:repair-needed"]

    def test_order_is_deep_extracted_then_repair_then_terminal(self) -> None:
        tags: list[str] = []
        sections = EnrichedSections(
            tier3=_tier3(),
            repair_flags=("r1",),
            terminal_flags=("t1",),
        )
        append_cascade_outcome_tags(tags, sections)
        assert tags == ["influx:deep-extracted", "r1", "t1"]


# ── render_note_content ───────────────────────────────────────────


class TestRenderNoteContent:
    def _spy_render(
        self, monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
    ) -> None:
        def _spy(**kwargs: Any) -> str:
            captured.update(kwargs)
            return "RENDERED"

        monkeypatch.setattr("influx.sources.note_builder.render", _spy)

    def test_summary_suppressed_when_tier1_attempted_and_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        self._spy_render(monkeypatch, captured)
        out = render_note_content(
            title="T",
            tags=["x"],
            confidence=1.0,
            archive_path=None,
            summary="the abstract",
            profile_name="p",
            score=8,
            reason="r",
            sections=EnrichedSections(tier1_attempted=True, tier1=None),
        )
        assert out == "RENDERED"
        assert captured["summary"] == ""

    def test_summary_passed_through_when_tier1_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        self._spy_render(monkeypatch, captured)
        render_note_content(
            title="T",
            tags=[],
            confidence=1.0,
            archive_path=None,
            summary="body",
            profile_name="p",
            score=8,
            reason="r",
            sections=EnrichedSections(tier1_attempted=True, tier1=_tier1()),
        )
        assert captured["summary"] == "body"

    def test_summary_kept_when_tier1_not_attempted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        self._spy_render(monkeypatch, captured)
        render_note_content(
            title="T",
            tags=[],
            confidence=0.0,
            archive_path="p/a.pdf",
            summary="fallback body",
            profile_name="p",
            score=0,
            reason="",
            sections=EnrichedSections(tier1_attempted=False, tier1=None),
        )
        assert captured["summary"] == "fallback body"
        # Section-derived render args are forwarded verbatim.
        assert captured["tier1_enrichment"] is None
        assert captured["full_text"] is None
        assert captured["tier3_extraction"] is None


# ── profile_item_dict ─────────────────────────────────────────────


class TestProfileItemDict:
    def test_all_14_keys_present_and_derived_fields(self) -> None:
        d = profile_item_dict(
            item_id="arxiv-1",
            title="T",
            source="arxiv",
            source_url="https://x",
            content="C",
            tags=["a"],
            filter_tags=["ft"],
            score=8,
            confidence=1.0,
            reason="r",
            path="papers/arxiv/2026/01",
            abstract_or_summary="abs",
            sections=EnrichedSections(tier1=_tier1(["c1", "c2"]), tier3=_tier3(["b1"])),
        )
        assert set(d) == {
            "id",
            "title",
            "source",
            "source_url",
            "content",
            "tags",
            "filter_tags",
            "score",
            "confidence",
            "reason",
            "path",
            "abstract_or_summary",
            "contributions",
            "builds_on",
        }
        assert d["id"] == "arxiv-1"
        assert d["contributions"] == ["c1", "c2"]
        assert d["builds_on"] == ["b1"]

    def test_filter_tags_none_becomes_empty_list(self) -> None:
        d = profile_item_dict(
            item_id="x",
            title="t",
            source="s",
            source_url="u",
            content="c",
            tags=[],
            filter_tags=None,
            score=0,
            confidence=0.0,
            reason="",
            path="p",
            abstract_or_summary="",
            sections=EnrichedSections(),
        )
        assert d["filter_tags"] == []

    def test_filter_tags_tuple_normalised_to_list(self) -> None:
        d = profile_item_dict(
            item_id="x",
            title="t",
            source="s",
            source_url="u",
            content="c",
            tags=[],
            filter_tags=("a", "b"),
            score=0,
            confidence=0.0,
            reason="",
            path="p",
            abstract_or_summary="",
            sections=EnrichedSections(),
        )
        assert d["filter_tags"] == ["a", "b"]

    def test_contributions_and_builds_on_none_without_tiers(self) -> None:
        d = profile_item_dict(
            item_id="x",
            title="t",
            source="s",
            source_url="u",
            content="c",
            tags=[],
            filter_tags=[],
            score=0,
            confidence=0.0,
            reason="",
            path="p",
            abstract_or_summary="",
            sections=EnrichedSections(),
        )
        assert d["contributions"] is None
        assert d["builds_on"] is None
