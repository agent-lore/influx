"""Tests for ``filter.batch_size`` wiring in the arXiv item provider (AC-X-1).

The arXiv provider must batch its calls to the LLM filter scorer
according to ``config.filter.batch_size`` so the configured tunable
actually shapes runtime behaviour rather than being read from config and
discarded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from influx.config import (
    AppConfig,
    FilterTuningConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.coordinator import RunKind
from influx.source import ScoredCandidate
from influx.sources.arxiv import (
    ArxivItem,
    make_arxiv_item_provider,
)


def _make_config(batch_size: int) -> AppConfig:
    return AppConfig(
        schedule=ScheduleConfig(
            cron="0 6 * * *",
            timezone="UTC",
            misfire_grace_seconds=3600,
        ),
        profiles=[ProfileConfig(name="alpha")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="t"),
            tier1_enrich=PromptEntryConfig(text="t"),
            tier3_extract=PromptEntryConfig(text="t"),
        ),
        filter=FilterTuningConfig(batch_size=batch_size),
    )


def _make_items(n: int) -> list[ArxivItem]:
    return [
        ArxivItem(
            arxiv_id=f"2401.{i:05d}",
            title=f"Paper {i}",
            abstract="Abstract",
            published=datetime(2024, 1, 1, tzinfo=UTC),
            categories=["cs.AI"],
        )
        for i in range(n)
    ]


async def test_filter_scorer_invoked_in_chunks_of_batch_size() -> None:
    """With 10 items and ``batch_size=3``, the filter scorer is called
    4 times (3, 3, 3, 1) — proving ``filter.batch_size`` shapes runtime
    behaviour (AC-X-1)."""
    config = _make_config(batch_size=3)
    items = _make_items(10)
    chunk_lengths: list[int] = []

    async def fake_filter_scorer(
        chunk: list[Any],
        profile: str,
        filter_prompt: str,
    ) -> dict[str, ScoredCandidate]:
        chunk_lengths.append(len(chunk))
        return {
            c.item_id: ScoredCandidate(
                candidate=c, score=8, confidence=1.0, reason="ok", filter_tags=()
            )
            for c in chunk
        }

    provider = make_arxiv_item_provider(config, scorer=fake_filter_scorer)

    with (
        patch(
            "influx.sources.arxiv.fetch_arxiv",
            new_callable=MagicMock,
            return_value=items,
        ),
        patch(
            "influx.sources.arxiv.build_arxiv_note_item",
            new_callable=MagicMock,
            return_value={
                "title": "x",
                "source_url": "http://e.com",
                "content": "c",
                "tags": [],
                "confidence": 0.0,
                "score": 0,
            },
        ),
    ):
        result = await provider("alpha", RunKind.SCHEDULED, None, "p")
        list(result)

    assert chunk_lengths == [3, 3, 3, 1]


async def test_filter_scorer_single_call_when_batch_exceeds_total() -> None:
    """With 4 items and ``batch_size=10``, the filter scorer is called
    exactly once with all 4 items."""
    config = _make_config(batch_size=10)
    items = _make_items(4)
    chunk_lengths: list[int] = []

    async def fake_filter_scorer(
        chunk: list[Any],
        profile: str,
        filter_prompt: str,
    ) -> dict[str, ScoredCandidate]:
        chunk_lengths.append(len(chunk))
        return {
            c.item_id: ScoredCandidate(
                candidate=c, score=8, confidence=1.0, reason="ok", filter_tags=()
            )
            for c in chunk
        }

    provider = make_arxiv_item_provider(config, scorer=fake_filter_scorer)

    with (
        patch(
            "influx.sources.arxiv.fetch_arxiv",
            new_callable=MagicMock,
            return_value=items,
        ),
        patch(
            "influx.sources.arxiv.build_arxiv_note_item",
            new_callable=MagicMock,
            return_value={
                "title": "x",
                "source_url": "http://e.com",
                "content": "c",
                "tags": [],
                "confidence": 0.0,
                "score": 0,
            },
        ),
    ):
        result = await provider("alpha", RunKind.SCHEDULED, None, "p")
        list(result)

    assert chunk_lengths == [4]


def _config_filter_default_batch_size() -> int:
    """Sanity-check helper: ``FilterTuningConfig`` exposes a
    ``batch_size`` field with the documented default (25)."""
    return FilterTuningConfig().batch_size


def test_filter_tuning_config_has_batch_size() -> None:
    """Sanity check: ``filter.batch_size`` is a real config field."""
    assert _config_filter_default_batch_size() == 25
