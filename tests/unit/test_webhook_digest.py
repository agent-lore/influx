"""Unit tests for webhook digest builder (§11, FR-NOT-2..3, AC-05-I)."""

from __future__ import annotations

from influx.notifications import (
    HighlightItem,
    ProfileRunResult,
    RunStats,
    build_digest,
)


def _make_item(
    *,
    id: str = "2603.12939",
    title: str = "RoboStream: Real-Time Robot Memory",
    score: int = 10,
    tags: list[str] | None = None,
    reason: str = "Highly relevant to embodied AI",
    url: str = "https://arxiv.org/abs/2603.12939",
) -> HighlightItem:
    """Build a sample item for digest tests."""
    return HighlightItem(
        id=id,
        title=title,
        score=score,
        tags=tags if tags is not None else ["robot-memory", "embodied-ai"],
        reason=reason,
        url=url,
        related_in_lithos=[],
    )


class TestNonZeroIngestDigest:
    """§11.1 non-zero-ingest digest body shape (FR-NOT-2)."""

    def test_full_digest_shape(self) -> None:
        """FR-NOT-2: digest contains all required keys."""
        result = ProfileRunResult(
            run_date="2026-03-16",
            profile="ai-robotics",
            stats=RunStats(sources_checked=157, ingested=12),
            items=[
                _make_item(score=10),
                _make_item(
                    id="2603.11111",
                    title="Low Score Paper",
                    score=5,
                    url="https://arxiv.org/abs/2603.11111",
                ),
            ],
        )
        digest = build_digest(result, notify_immediate_threshold=8)

        assert digest["type"] == "influx_digest"
        assert digest["run_date"] == "2026-03-16"
        assert digest["profile"] == "ai-robotics"
        assert digest["stats"]["sources_checked"] == 157
        assert digest["stats"]["ingested"] == 12
        assert digest["stats"]["high_relevance"] == 1
        assert len(digest["highlights"]) == 1
        assert digest["highlights"][0]["id"] == "2603.12939"
        assert digest["highlights"][0]["score"] == 10
        assert len(digest["all_ingested"]) == 2

    def test_highlights_selected_by_threshold(self) -> None:
        """AC-05-I: highlights use score >= notify_immediate from config."""
        items = [
            _make_item(id="a", score=9, title="High A", url="https://a"),
            _make_item(id="b", score=8, title="At Threshold", url="https://b"),
            _make_item(id="c", score=7, title="Below", url="https://c"),
            _make_item(id="d", score=10, title="Max", url="https://d"),
        ]
        result = ProfileRunResult(
            run_date="2026-04-01",
            profile="ml-safety",
            stats=RunStats(sources_checked=50, ingested=4),
            items=items,
        )
        digest = build_digest(result, notify_immediate_threshold=8)

        highlight_ids = {h["id"] for h in digest["highlights"]}
        assert highlight_ids == {"a", "b", "d"}
        assert digest["stats"]["high_relevance"] == 3

    def test_threshold_not_hardcoded(self) -> None:
        """AC-X-1: threshold comes from config, not a hardcoded constant."""
        items = [_make_item(score=6)]
        result = ProfileRunResult(
            run_date="2026-04-01",
            profile="test",
            stats=RunStats(sources_checked=10, ingested=1),
            items=items,
        )
        # With threshold=6, the item IS a highlight
        digest_low = build_digest(result, notify_immediate_threshold=6)
        assert len(digest_low["highlights"]) == 1

        # With threshold=7, the item is NOT a highlight
        digest_high = build_digest(result, notify_immediate_threshold=7)
        assert len(digest_high["highlights"]) == 0

    def test_highlight_entry_shape(self) -> None:
        """§11.1: highlight has all required keys."""
        result = ProfileRunResult(
            run_date="2026-03-16",
            profile="ai-robotics",
            stats=RunStats(sources_checked=10, ingested=1),
            items=[_make_item()],
        )
        digest = build_digest(result, notify_immediate_threshold=8)
        h = digest["highlights"][0]

        assert h["id"] == "2603.12939"
        assert h["title"] == "RoboStream: Real-Time Robot Memory"
        assert h["score"] == 10
        assert h["tags"] == ["robot-memory", "embodied-ai"]
        assert h["reason"] == "Highly relevant to embodied AI"
        assert h["url"] == "https://arxiv.org/abs/2603.12939"
        assert h["related_in_lithos"] == []

    def test_related_in_lithos_always_empty(self) -> None:
        """FR-NOT-6: related_in_lithos is [] until PRD 08."""
        result = ProfileRunResult(
            run_date="2026-03-16",
            profile="ai-robotics",
            stats=RunStats(sources_checked=10, ingested=1),
            items=[_make_item()],
        )
        digest = build_digest(result, notify_immediate_threshold=1)
        for h in digest["highlights"]:
            assert h["related_in_lithos"] == []

    def test_all_ingested_includes_all_items(self) -> None:
        """all_ingested includes every item regardless of score."""
        items = [
            _make_item(id="high", score=10, url="https://high"),
            _make_item(id="low", score=3, url="https://low"),
        ]
        result = ProfileRunResult(
            run_date="2026-04-01",
            profile="test",
            stats=RunStats(sources_checked=20, ingested=2),
            items=items,
        )
        digest = build_digest(result, notify_immediate_threshold=8)
        ingested_ids = {i["id"] for i in digest["all_ingested"]}
        assert ingested_ids == {"high", "low"}

    def test_no_highlights_but_ingested(self) -> None:
        """Non-zero ingest with all scores below threshold → empty highlights."""
        result = ProfileRunResult(
            run_date="2026-04-01",
            profile="test",
            stats=RunStats(sources_checked=20, ingested=3),
            items=[
                _make_item(id="a", score=5, url="https://a"),
                _make_item(id="b", score=6, url="https://b"),
                _make_item(id="c", score=7, url="https://c"),
            ],
        )
        digest = build_digest(result, notify_immediate_threshold=8)
        assert digest["highlights"] == []
        assert digest["stats"]["high_relevance"] == 0
        assert len(digest["all_ingested"]) == 3


class TestZeroIngestDigest:
    """§11.2 zero-ingest quiet digest (FR-NOT-3)."""

    def test_quiet_digest_shape(self) -> None:
        """§11.2: zero-ingest → quiet shape with message, no highlights."""
        result = ProfileRunResult(
            run_date="2026-03-16",
            profile="ai-robotics",
            stats=RunStats(sources_checked=0, ingested=0),
        )
        digest = build_digest(result, notify_immediate_threshold=8)

        assert digest["type"] == "influx_digest"
        assert digest["run_date"] == "2026-03-16"
        assert digest["profile"] == "ai-robotics"
        assert digest["stats"]["sources_checked"] == 0
        assert digest["stats"]["ingested"] == 0
        assert digest["message"] == "No new relevant content found today."
        assert "highlights" not in digest
        assert "all_ingested" not in digest

    def test_quiet_digest_with_sources_checked(self) -> None:
        """§11.2: sources checked but zero ingested → still quiet."""
        result = ProfileRunResult(
            run_date="2026-03-16",
            profile="ai-robotics",
            stats=RunStats(sources_checked=157, ingested=0),
        )
        digest = build_digest(result, notify_immediate_threshold=8)

        assert digest["stats"]["sources_checked"] == 157
        assert digest["stats"]["ingested"] == 0
        assert digest["message"] == "No new relevant content found today."
        assert "highlights" not in digest
        assert "all_ingested" not in digest

    def test_quiet_digest_no_high_relevance_key(self) -> None:
        """§11.2: quiet digest has no high_relevance in stats."""
        result = ProfileRunResult(
            run_date="2026-04-01",
            profile="test",
            stats=RunStats(sources_checked=50, ingested=0),
        )
        digest = build_digest(result, notify_immediate_threshold=8)
        assert "high_relevance" not in digest["stats"]
