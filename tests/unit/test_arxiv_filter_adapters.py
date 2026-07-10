"""Unit tests for the arXiv → Filter BatchScorer adapters (finding 2.3).

``make_arxiv_item_provider`` drives scoring through
:class:`influx.filter.Filter`; the per-item ``ArxivScorer`` (test seam)
and batched ``ArxivFilterScorer`` (production) are adapted onto the
source-agnostic ``BatchScorer`` by these two helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from influx.coordinator import RunKind
from influx.filter import FilterScorerError
from influx.source import Candidate
from influx.sources.arxiv import (
    ArxivItem,
    ArxivScoreResult,
    _batch_scorer_from_arxiv_filter_scorer,
    _batch_scorer_from_item_scorer,
    make_arxiv_item_provider,
)


def _config_without_filter() -> Any:
    """Minimal AppConfig with an arXiv profile and no ``[models.filter]``."""
    from influx.config import (
        AppConfig,
        ProfileConfig,
        PromptEntryConfig,
        PromptsConfig,
        ScheduleConfig,
    )

    return AppConfig(
        schedule=ScheduleConfig(
            cron="0 6 * * *", timezone="UTC", misfire_grace_seconds=3600
        ),
        profiles=[ProfileConfig(name="ai-robotics")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="t"),
            tier1_enrich=PromptEntryConfig(text="t"),
            tier3_extract=PromptEntryConfig(text="t"),
        ),
    )


def _item(arxiv_id: str) -> ArxivItem:
    return ArxivItem(
        arxiv_id=arxiv_id,
        title=f"Title {arxiv_id}",
        abstract="abstract",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        categories=["cs.AI"],
    )


def _cand(arxiv_id: str) -> Candidate:
    it = _item(arxiv_id)
    return Candidate(
        item_id=it.arxiv_id,
        title=it.title,
        abstract=it.abstract,
        source_url=f"https://arxiv.org/abs/{arxiv_id}",
        payload=it,
    )


class TestItemScorerAdapter:
    async def test_keeps_scored_and_drops_none(self) -> None:
        def scorer(item: ArxivItem, profile: str) -> ArxivScoreResult | None:
            if item.arxiv_id == "drop":
                return None
            return ArxivScoreResult(
                score=8, confidence=0.9, reason="ok", filter_tags=("t",)
            )

        batch = _batch_scorer_from_item_scorer(scorer)
        out = await batch([_cand("keep"), _cand("drop")], "p", "prompt")

        assert set(out) == {"keep"}
        sc = out["keep"]
        assert sc.candidate.item_id == "keep"
        assert (sc.score, sc.confidence, sc.reason) == (8, 0.9, "ok")
        assert sc.filter_tags == ("t",)

    async def test_empty_when_no_candidates(self) -> None:
        batch = _batch_scorer_from_item_scorer(lambda item, profile: None)
        assert await batch([], "p", "prompt") == {}


class TestArxivFilterScorerAdapter:
    async def test_reassociates_results_with_candidates(self) -> None:
        async def filter_scorer(
            items: list[ArxivItem], profile: str, prompt: str
        ) -> dict[str, ArxivScoreResult]:
            return {
                it.arxiv_id: ArxivScoreResult(score=7, confidence=1.0, reason="r")
                for it in items
            }

        batch = _batch_scorer_from_arxiv_filter_scorer(filter_scorer)
        out = await batch([_cand("a"), _cand("b")], "p", "prompt")

        assert set(out) == {"a", "b"}
        assert out["a"].candidate.payload.arxiv_id == "a"
        assert out["a"].score == 7

    async def test_ignores_ids_not_in_chunk(self) -> None:
        async def filter_scorer(
            items: list[Any], profile: str, prompt: str
        ) -> dict[str, ArxivScoreResult]:
            return {
                "a": ArxivScoreResult(score=7, confidence=1.0, reason="r"),
                "ghost": ArxivScoreResult(score=9, confidence=1.0, reason="x"),
            }

        batch = _batch_scorer_from_arxiv_filter_scorer(filter_scorer)
        out = await batch([_cand("a")], "p", "prompt")

        assert set(out) == {"a"}

    async def test_propagates_filter_scorer_error(self) -> None:
        async def filter_scorer(
            items: list[Any], profile: str, prompt: str
        ) -> dict[str, ArxivScoreResult]:
            raise FilterScorerError("boom")

        batch = _batch_scorer_from_arxiv_filter_scorer(filter_scorer)
        with pytest.raises(FilterScorerError):
            await batch([_cand("a")], "p", "prompt")


class TestProviderNoScorer:
    async def test_no_scorer_yields_zero_items(self) -> None:
        """With neither scorer wired, the provider scores nothing and
        yields zero bound candidates — it never fabricates a score."""
        config = _config_without_filter()
        provider = make_arxiv_item_provider(config)  # no scorer, no filter_scorer

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            new_callable=MagicMock,
            return_value=[_item("2401.00001")],
        ):
            bounds = list(
                await provider("ai-robotics", RunKind.SCHEDULED, None, "prompt")
            )

        assert bounds == []
