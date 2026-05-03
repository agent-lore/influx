"""Tests for the unified ``Source`` protocol (issue #57).

Verifies that the arXiv and RSS adapter classes conform to the
:class:`influx.source.Source` protocol and that their
:meth:`fetch_candidates` / :meth:`acquire` stages compose into the
``Source.fetch_candidates → Filter.score → Source.acquire`` sequence
called out in CONTEXT.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from influx.config import (
    AppConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    RssSourceEntry,
    ScheduleConfig,
)
from influx.coordinator import RunKind
from influx.source import Candidate, ScoredCandidate, Source
from influx.sources.arxiv import ArxivItem, ArxivSource
from influx.sources.rss import RssFeedItem, RssSource


def _make_config(profile: ProfileConfig | None = None) -> AppConfig:
    return AppConfig(
        schedule=ScheduleConfig(cron="0 6 * * *", misfire_grace_seconds=3600),
        profiles=[profile or ProfileConfig(name="alpha")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="t"),
            tier1_enrich=PromptEntryConfig(text="t"),
            tier3_extract=PromptEntryConfig(text="t"),
        ),
    )


# ── Protocol conformance ───────────────────────────────────────────


def test_arxiv_source_conforms_to_source_protocol() -> None:
    """ArxivSource conforms to the runtime-checkable Source protocol."""
    config = _make_config()
    source = ArxivSource(config)
    assert isinstance(source, Source)
    assert source.name == "arxiv"


def test_rss_source_conforms_to_source_protocol() -> None:
    """RssSource conforms to the runtime-checkable Source protocol."""
    config = _make_config()
    source = RssSource(config)
    assert isinstance(source, Source)
    assert source.name == "rss"


# ── ArxivSource: fetch_candidates → acquire ───────────────────────


async def test_arxiv_source_fetch_candidates_returns_typed_candidates() -> None:
    """fetch_candidates wraps each ArxivItem in a Candidate."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    items = [
        ArxivItem(
            arxiv_id="2401.00001",
            title="Sample paper",
            abstract="Body",
            published=datetime(2024, 1, 1, tzinfo=UTC),
            categories=["cs.AI"],
        )
    ]

    with patch(
        "influx.sources.arxiv.fetch_arxiv",
        new_callable=MagicMock,
        return_value=items,
    ):
        source = ArxivSource(config)
        candidates = await source.fetch_candidates(
            profile_cfg=profile_cfg,
            kind=RunKind.SCHEDULED,
            run_range=None,
        )

    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.item_id == "2401.00001"
    assert cand.title == "Sample paper"
    assert cand.abstract == "Body"
    assert cand.source_url == "https://arxiv.org/abs/2401.00001"
    assert cand.payload is items[0]


async def test_arxiv_source_fetch_candidates_returns_empty_when_disabled() -> None:
    """A profile with arxiv.enabled=False yields zero candidates."""
    profile_cfg = ProfileConfig(name="alpha")
    profile_cfg.sources.arxiv.enabled = False
    config = _make_config(profile_cfg)

    source = ArxivSource(config)
    candidates = await source.fetch_candidates(
        profile_cfg=profile_cfg,
        kind=RunKind.SCHEDULED,
        run_range=None,
    )

    assert candidates == []


def test_arxiv_source_acquire_delegates_to_build_arxiv_note_item() -> None:
    """acquire wires the cascade + renderer via build_arxiv_note_item."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    item = ArxivItem(
        arxiv_id="2401.00001",
        title="Sample",
        abstract="Body",
        published=datetime(2024, 1, 1, tzinfo=UTC),
        categories=["cs.AI"],
    )
    candidate = Candidate(
        item_id=item.arxiv_id,
        title=item.title,
        abstract=item.abstract,
        source_url=f"https://arxiv.org/abs/{item.arxiv_id}",
        payload=item,
    )
    scored = ScoredCandidate(
        candidate=candidate,
        score=8,
        confidence=1.0,
        reason="relevant",
        filter_tags=("ai-safety",),
    )

    sentinel = {"title": "Sample", "tags": ["x"]}
    with patch(
        "influx.sources.arxiv.build_arxiv_note_item",
        new_callable=MagicMock,
        return_value=sentinel,
    ) as build_mock:
        source = ArxivSource(config)
        result = source.acquire(scored, profile_cfg=profile_cfg, config=config)

    assert result is sentinel
    build_mock.assert_called_once()
    kwargs = build_mock.call_args.kwargs
    assert kwargs["item"] is item
    assert kwargs["score"] == 8
    assert kwargs["confidence"] == 1.0
    assert kwargs["reason"] == "relevant"
    assert kwargs["profile_name"] == "alpha"
    assert kwargs["filter_tags"] == ("ai-safety",)


def test_arxiv_source_acquire_rejects_wrong_payload_type() -> None:
    """acquire raises TypeError when the payload isn't an ArxivItem."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    candidate = Candidate(
        item_id="x",
        title="t",
        abstract="a",
        source_url="https://example.com",
        payload="not-an-arxiv-item",
    )
    scored = ScoredCandidate(candidate=candidate, score=8, confidence=1.0, reason="")

    source = ArxivSource(config)
    with pytest.raises(TypeError, match="ArxivItem"):
        source.acquire(scored, profile_cfg=profile_cfg, config=config)


# ── RssSource: fetch_candidates → acquire ─────────────────────────


async def test_rss_source_fetch_candidates_flattens_feeds() -> None:
    """fetch_candidates concatenates items across configured feeds."""
    profile_cfg = ProfileConfig(name="alpha")
    profile_cfg.sources.rss = [
        RssSourceEntry(
            name="Feed One",
            url="https://a.example/feed",
            source_tag="blog",
        ),
    ]
    config = _make_config(profile_cfg)

    items = [
        RssFeedItem(
            title="Post 1",
            url="https://a.example/post-1",
            published=datetime(2024, 1, 1, tzinfo=UTC),
            summary="Summary 1",
            source_tag="blog",
            feed_name="Feed One",
        )
    ]

    async def fake_fetch(*args, **kwargs):  # type: ignore[no-untyped-def]
        return items

    with patch("influx.sources.rss._fetch_rss_feed", side_effect=fake_fetch):
        source = RssSource(config)
        candidates = await source.fetch_candidates(
            profile_cfg=profile_cfg,
            kind=RunKind.SCHEDULED,
            run_range=None,
        )

    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.title == "Post 1"
    assert cand.abstract == "Summary 1"
    assert cand.source_url == "https://a.example/post-1"
    assert cand.payload is items[0]


def test_rss_source_acquire_delegates_to_build_rss_note_item() -> None:
    """acquire wires the RSS cascade + renderer via build_rss_note_item."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    item = RssFeedItem(
        title="Post",
        url="https://a.example/post",
        published=datetime(2024, 1, 1, tzinfo=UTC),
        summary="Summary",
        source_tag="blog",
        feed_name="Feed One",
    )
    candidate = Candidate(
        item_id="hash-1",
        title=item.title,
        abstract=item.summary,
        source_url=item.url,
        payload=item,
    )
    scored = ScoredCandidate(
        candidate=candidate,
        score=8,
        confidence=1.0,
        reason="relevant",
        filter_tags=("blog-ai",),
    )

    sentinel = {"title": "Post", "tags": ["x"]}
    with patch(
        "influx.sources.rss.build_rss_note_item",
        new_callable=MagicMock,
        return_value=sentinel,
    ) as build_mock:
        source = RssSource(config)
        result = source.acquire(scored, profile_cfg=profile_cfg, config=config)

    assert result is sentinel
    kwargs = build_mock.call_args.kwargs
    assert kwargs["item"] is item
    assert kwargs["profile_name"] == "alpha"
    assert kwargs["filter_tags"] == ("blog-ai",)


def test_rss_source_acquire_rejects_wrong_payload_type() -> None:
    """acquire raises TypeError when the payload isn't an RssFeedItem."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    candidate = Candidate(
        item_id="x", title="t", abstract="a", source_url="u", payload=42
    )
    scored = ScoredCandidate(candidate=candidate, score=8, confidence=1.0, reason="")

    source = RssSource(config)
    with pytest.raises(TypeError, match="RssFeedItem"):
        source.acquire(scored, profile_cfg=profile_cfg, config=config)
