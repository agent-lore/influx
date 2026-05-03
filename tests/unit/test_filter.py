"""Tests for the source-agnostic ``Filter`` (issue #57).

Covers the three behaviours called out in the AC:

- threshold gating (drop items below ``thresholds.relevance``)
- missing-from-response handling (drop items the scorer omits)
- negative-feedback wiring (the rendered ``filter_prompt`` actually
  reaches the scorer untouched, so feedback examples flow through)

Also exercises :data:`FilterScorerError` skip-the-batch behaviour
(FR-FLT-6 / spec §7.1) and the no-scorer fallback.
"""

from __future__ import annotations

from influx.config import (
    AppConfig,
    FilterTuningConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.filter import BatchScorer, Filter, FilterScorerError
from influx.source import Candidate, ScoredCandidate


def _make_config(batch_size: int = 25) -> AppConfig:
    return AppConfig(
        schedule=ScheduleConfig(
            cron="0 6 * * *",
            timezone="UTC",
            misfire_grace_seconds=3600,
        ),
        profiles=[
            ProfileConfig(
                name="alpha",
                thresholds=ProfileThresholds(relevance=7),
            )
        ],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="t"),
            tier1_enrich=PromptEntryConfig(text="t"),
            tier3_extract=PromptEntryConfig(text="t"),
        ),
        filter=FilterTuningConfig(batch_size=batch_size),
    )


def _candidate(item_id: str) -> Candidate:
    return Candidate(
        item_id=item_id,
        title=f"Title {item_id}",
        abstract=f"Abstract {item_id}",
        source_url=f"https://example.com/{item_id}",
    )


def _make_scorer(scores: dict[str, int]) -> BatchScorer:
    """Return a deterministic batch scorer keyed by ``Candidate.item_id``."""

    async def _scorer(
        chunk: list[Candidate],
        profile: str,
        filter_prompt: str,
    ) -> dict[str, ScoredCandidate]:
        return {
            c.item_id: ScoredCandidate(
                candidate=c,
                score=scores[c.item_id],
                confidence=1.0,
                reason="ok",
                filter_tags=("ai-safety",),
            )
            for c in chunk
            if c.item_id in scores
        }

    return _scorer


# ── Threshold gating ────────────────────────────────────────────────


async def test_score_drops_items_below_threshold() -> None:
    """Filter drops items whose score < ``thresholds.relevance``."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    candidates = [_candidate("a"), _candidate("b"), _candidate("c")]
    scorer = _make_scorer({"a": 9, "b": 6, "c": 7})  # b drops, threshold=7

    f = Filter(config=config, profile_cfg=profile_cfg, scorer=scorer)
    result = await f.score(candidates, filter_prompt="prompt", source="arxiv")

    assert [s.candidate.item_id for s in result] == ["a", "c"]
    assert all(s.score >= 7 for s in result)


async def test_score_keeps_item_at_exact_threshold() -> None:
    """A score exactly equal to the threshold passes (>= comparison)."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    candidates = [_candidate("a")]
    scorer = _make_scorer({"a": 7})

    f = Filter(config=config, profile_cfg=profile_cfg, scorer=scorer)
    result = await f.score(candidates, filter_prompt="prompt", source="arxiv")

    assert [s.candidate.item_id for s in result] == ["a"]


# ── Missing-from-response handling ─────────────────────────────────


async def test_score_drops_items_missing_from_response() -> None:
    """Items the scorer omits are dropped — not re-scored or fabricated."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    candidates = [_candidate("a"), _candidate("b"), _candidate("c")]
    # Scorer only returns "a"; b and c are absent → both dropped.
    scorer = _make_scorer({"a": 9})

    f = Filter(config=config, profile_cfg=profile_cfg, scorer=scorer)
    result = await f.score(candidates, filter_prompt="prompt", source="arxiv")

    assert [s.candidate.item_id for s in result] == ["a"]


# ── Failed-batch handling (FR-FLT-6 / spec §7.1) ───────────────────


async def test_score_skips_entire_batch_on_filter_scorer_error() -> None:
    """A scorer that raises FilterScorerError causes the run to ingest zero."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    candidates = [_candidate("a"), _candidate("b")]

    async def failing_scorer(
        chunk: list[Candidate],
        profile: str,
        filter_prompt: str,
    ) -> dict[str, ScoredCandidate]:
        raise FilterScorerError("upstream LLM down")

    f = Filter(config=config, profile_cfg=profile_cfg, scorer=failing_scorer)
    result = await f.score(candidates, filter_prompt="prompt", source="arxiv")

    # Failed batches must NOT be ingested with a default score.
    assert result == []


# ── No-scorer fallback ─────────────────────────────────────────────


async def test_score_returns_empty_when_no_scorer_configured() -> None:
    """A misconfigured deployment (scorer=None) yields zero items, not crashes."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    candidates = [_candidate("a")]

    f = Filter(config=config, profile_cfg=profile_cfg, scorer=None)
    result = await f.score(candidates, filter_prompt="prompt", source="arxiv")

    assert result == []
    assert f.has_scorer is False


async def test_score_returns_empty_for_empty_candidates() -> None:
    """No candidates → no scorer call, empty result."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    called = False

    async def scorer(
        chunk: list[Candidate], profile: str, filter_prompt: str
    ) -> dict[str, ScoredCandidate]:
        nonlocal called
        called = True
        return {}

    f = Filter(config=config, profile_cfg=profile_cfg, scorer=scorer)
    result = await f.score([], filter_prompt="prompt", source="arxiv")

    assert result == []
    assert called is False


# ── Negative-feedback wiring ───────────────────────────────────────


async def test_score_passes_filter_prompt_through_to_scorer() -> None:
    """The rendered filter_prompt (with negative-feedback block) reaches
    the scorer unmodified — proving feedback examples flow through."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    candidates = [_candidate("a"), _candidate("b")]

    rendered_prompt = (
        "Score these candidates...\n\n"
        "## NEGATIVE EXAMPLES\n"
        "- Boring paper about widgets\n"
        "- Another irrelevant title\n"
    )
    captured: list[str] = []

    async def scorer(
        chunk: list[Candidate],
        profile: str,
        filter_prompt: str,
    ) -> dict[str, ScoredCandidate]:
        captured.append(filter_prompt)
        return {
            c.item_id: ScoredCandidate(
                candidate=c, score=8, confidence=1.0, reason="ok"
            )
            for c in chunk
        }

    f = Filter(config=config, profile_cfg=profile_cfg, scorer=scorer)
    await f.score(candidates, filter_prompt=rendered_prompt, source="arxiv")

    assert captured == [rendered_prompt]
    assert "## NEGATIVE EXAMPLES" in captured[0]


# ── Batching ───────────────────────────────────────────────────────


async def test_score_chunks_by_filter_batch_size() -> None:
    """Filter splits candidates into ``filter.batch_size`` chunks (AC-X-1)."""
    config = _make_config(batch_size=3)
    profile_cfg = config.profiles[0]
    candidates = [_candidate(f"id-{i}") for i in range(10)]
    chunk_lens: list[int] = []

    async def scorer(
        chunk: list[Candidate], profile: str, filter_prompt: str
    ) -> dict[str, ScoredCandidate]:
        chunk_lens.append(len(chunk))
        return {
            c.item_id: ScoredCandidate(
                candidate=c, score=8, confidence=1.0, reason="ok"
            )
            for c in chunk
        }

    f = Filter(config=config, profile_cfg=profile_cfg, scorer=scorer)
    result = await f.score(candidates, filter_prompt="p", source="arxiv")

    assert chunk_lens == [3, 3, 3, 1]
    assert len(result) == 10


async def test_score_propagates_filter_tags_and_reason() -> None:
    """The scored candidate carries through filter_tags + reason."""
    config = _make_config()
    profile_cfg = config.profiles[0]
    cand = _candidate("a")
    scorer = _make_scorer({"a": 9})

    f = Filter(config=config, profile_cfg=profile_cfg, scorer=scorer)
    result = await f.score([cand], filter_prompt="p", source="arxiv")

    assert len(result) == 1
    assert result[0].filter_tags == ("ai-safety",)
    assert result[0].reason == "ok"
    assert result[0].confidence == 1.0
