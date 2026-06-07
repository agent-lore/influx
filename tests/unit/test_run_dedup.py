"""Unit tests for the Run-stage pre-acquire dedup helper (#125).

Validates the partitioning logic of :func:`influx.run_dedup.dedup_scored_candidates`:

- Cache miss → ``to_acquire`` with ``cache_hit=False``.
- Cache hit + ``skip_cache_hits=True`` → ``hits_to_skip`` (backfill drops
  duplicates *before* full ``Source.acquire``).
- Cache hit + ``skip_cache_hits=False`` → ``to_acquire`` with
  ``cache_hit=True`` (multi-profile merge path preserved).

The helper composes the dedup query via
:meth:`LithosClient.cache_lookup_for_item_body`, so we mock that call
directly rather than re-implementing query composition in the tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from influx.errors import LithosError
from influx.run_dedup import (
    DedupOutcome,
    dedup_scored_candidates,
)
from influx.source import BoundScoredCandidate, Candidate, ScoredCandidate
from influx.telemetry import current_dedup_lookup_errors


def _make_bound(
    *,
    item_id: str = "id-1",
    title: str = "A relevant paper",
    abstract: str = "We propose a new method for X.",
    source_url: str = "https://example.org/paper-1",
    score: int = 8,
    source_label: str = "arxiv",
) -> BoundScoredCandidate:
    """Build a BoundScoredCandidate with a stub acquire closure."""

    async def _acquire() -> dict[str, Any] | None:
        return {"title": title, "source_url": source_url}

    return BoundScoredCandidate(
        scored=ScoredCandidate(
            candidate=Candidate(
                item_id=item_id,
                title=title,
                abstract=abstract,
                source_url=source_url,
            ),
            score=score,
            confidence=1.0,
            reason="test",
            filter_tags=("test",),
        ),
        acquire=_acquire,
        source_label=source_label,
    )


def _client_with_responses(*responses: dict[str, Any]) -> Any:
    """Build a MagicMock LithosClient whose cache_lookup_for_item_body
    returns *responses* in order.
    """
    client = MagicMock()
    client.cache_lookup_for_item_body = AsyncMock(side_effect=list(responses))
    return client


@pytest.mark.asyncio
async def test_empty_input_no_client_calls() -> None:
    """Zero scored candidates → zero lookups, empty outcome."""
    client = _client_with_responses()
    outcome = await dedup_scored_candidates(
        [],
        client=client,
        profile="p1",
        skip_cache_hits=False,
    )
    assert outcome == DedupOutcome(to_acquire=(), hits_to_skip=())
    client.cache_lookup_for_item_body.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_called_with_candidate_metadata() -> None:
    """Helper passes title + source_url + abstract straight through to the client."""
    bound = _make_bound(
        title="Scaling Laws",
        abstract="We study scaling.",
        source_url="https://example.org/scaling",
    )
    client = _client_with_responses({"hit": False})

    await dedup_scored_candidates(
        [bound],
        client=client,
        profile="p1",
        skip_cache_hits=False,
    )

    client.cache_lookup_for_item_body.assert_awaited_once_with(
        title="Scaling Laws",
        source_url="https://example.org/scaling",
        abstract_or_summary="We study scaling.",
    )


@pytest.mark.asyncio
async def test_empty_abstract_passed_as_none() -> None:
    """Empty-string abstract is normalised to None for the lookup."""
    bound = _make_bound(abstract="")
    client = _client_with_responses({"hit": False})
    await dedup_scored_candidates(
        [bound],
        client=client,
        profile="p1",
        skip_cache_hits=False,
    )
    _, kwargs = client.cache_lookup_for_item_body.call_args
    assert kwargs["abstract_or_summary"] is None


@pytest.mark.asyncio
async def test_cache_miss_lands_in_to_acquire() -> None:
    """Miss → to_acquire with cache_hit=False, cache_body recorded."""
    bound = _make_bound()
    body = {"hit": False, "stale_exists": False}
    client = _client_with_responses(body)

    outcome = await dedup_scored_candidates(
        [bound],
        client=client,
        profile="p1",
        skip_cache_hits=False,
    )

    assert len(outcome.to_acquire) == 1
    assert len(outcome.hits_to_skip) == 0
    decision = outcome.to_acquire[0]
    assert decision.bound is bound
    assert decision.cache_hit is False
    assert decision.cache_hit_reason is None
    assert decision.cache_body == body


@pytest.mark.asyncio
async def test_cache_hit_with_skip_drops_to_hits_to_skip() -> None:
    """Backfill (skip_cache_hits=True) drops cache hits pre-acquire."""
    bound = _make_bound()
    body = {"hit": True}
    client = _client_with_responses(body)

    outcome = await dedup_scored_candidates(
        [bound],
        client=client,
        profile="p1",
        skip_cache_hits=True,
    )

    assert len(outcome.to_acquire) == 0
    assert len(outcome.hits_to_skip) == 1
    decision = outcome.hits_to_skip[0]
    assert decision.bound is bound
    assert decision.cache_hit is True
    assert decision.cache_hit_reason == "primary"
    assert decision.cache_body == body


@pytest.mark.asyncio
async def test_cache_hit_without_skip_lands_in_to_acquire_for_merge() -> None:
    """Normal run preserves multi-profile merge: hit still goes to acquire."""
    bound = _make_bound()
    body = {"hit": True, "id": "note-123"}
    client = _client_with_responses(body)

    outcome = await dedup_scored_candidates(
        [bound],
        client=client,
        profile="p1",
        skip_cache_hits=False,
    )

    assert len(outcome.to_acquire) == 1
    assert len(outcome.hits_to_skip) == 0
    decision = outcome.to_acquire[0]
    assert decision.cache_hit is True
    assert decision.cache_hit_reason == "primary"


@pytest.mark.asyncio
async def test_mixed_batch_partitions_correctly() -> None:
    """Three-way mix: miss → acquire, hit+skip → drop, hit+merge → acquire.

    Order within each partition is preserved.
    """
    miss_bound = _make_bound(item_id="miss", title="Miss")
    skip_bound = _make_bound(item_id="skip", title="Skip")
    merge_bound = _make_bound(item_id="merge", title="Merge")

    # In skip_cache_hits=True the merge_bound also gets dropped — to test
    # the three-way partition we run two passes.
    client = _client_with_responses(
        {"hit": False},  # miss
        {"hit": True},  # skip (with skip_cache_hits=True)
    )
    outcome_backfill = await dedup_scored_candidates(
        [miss_bound, skip_bound],
        client=client,
        profile="p1",
        skip_cache_hits=True,
    )
    assert [d.bound.scored.candidate.item_id for d in outcome_backfill.to_acquire] == [
        "miss"
    ]
    assert [
        d.bound.scored.candidate.item_id for d in outcome_backfill.hits_to_skip
    ] == ["skip"]

    # Normal run: same hit goes to to_acquire (merge path).
    client2 = _client_with_responses({"hit": True}, {"hit": False})
    outcome_normal = await dedup_scored_candidates(
        [merge_bound, miss_bound],
        client=client2,
        profile="p1",
        skip_cache_hits=False,
    )
    assert [d.bound.scored.candidate.item_id for d in outcome_normal.to_acquire] == [
        "merge",
        "miss",
    ]
    assert outcome_normal.hits_to_skip == ()
    assert outcome_normal.to_acquire[0].cache_hit is True
    assert outcome_normal.to_acquire[1].cache_hit is False


@pytest.mark.asyncio
async def test_non_lithos_error_propagates() -> None:
    """Non-Lithos lookup errors propagate — only ``LithosError`` is
    treated as a recoverable per-candidate failure (#234).  Anything
    else (e.g. a bug in our own partition code) must surface."""
    bound = _make_bound()
    client = MagicMock()
    client.cache_lookup_for_item_body = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await dedup_scored_candidates(
            [bound],
            client=client,
            profile="p1",
            skip_cache_hits=False,
        )


# ── #234: per-candidate LithosError degrades instead of aborting ───


def _lithos_error(detail: str = "TypeError: '<' not supported") -> LithosError:
    return LithosError("cache_lookup failed", operation="cache_lookup", detail=detail)


@pytest.mark.asyncio
async def test_lithos_error_skips_candidate_and_continues() -> None:
    """One candidate's LithosError no longer aborts the batch (#234).

    The errored candidate lands in *neither* partition (recoverable —
    re-checked next scheduled run); the rest of the batch processes.
    """
    bounds = [
        _make_bound(item_id="ok-1", title="First"),
        _make_bound(item_id="poisoned", title="Poisoned"),
        _make_bound(item_id="ok-2", title="Third"),
    ]
    client = MagicMock()
    client.cache_lookup_for_item_body = AsyncMock(
        side_effect=[{"hit": False}, _lithos_error(), {"hit": False}]
    )

    outcome = await dedup_scored_candidates(
        bounds,
        client=client,
        profile="p1",
        skip_cache_hits=False,
    )

    assert [d.bound.scored.candidate.item_id for d in outcome.to_acquire] == [
        "ok-1",
        "ok-2",
    ]
    assert outcome.hits_to_skip == ()
    assert outcome.lookup_errors == 1
    assert client.cache_lookup_for_item_body.await_count == 3


@pytest.mark.asyncio
async def test_lithos_error_records_dedup_lookup_error() -> None:
    """The skipped candidate is recorded on the run's telemetry list
    with the bounded source label and the Lithos ``detail`` text."""
    errors: list[dict[str, str]] = []
    token = current_dedup_lookup_errors.set(errors)
    try:
        bound = _make_bound(source_label="rss:Mozilla Hacks")
        ok_bound = _make_bound(item_id="ok", source_label="arxiv")
        client = MagicMock()
        client.cache_lookup_for_item_body = AsyncMock(
            side_effect=[_lithos_error("server exploded"), {"hit": False}]
        )

        await dedup_scored_candidates(
            [bound, ok_bound],
            client=client,
            profile="p1",
            skip_cache_hits=False,
        )
    finally:
        current_dedup_lookup_errors.reset(token)

    assert errors == [
        {"source": "rss", "kind": "cache_lookup", "detail": "server exploded"}
    ]


@pytest.mark.asyncio
async def test_lithos_error_increments_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """``metrics.dedup_lookup_errors()`` ticks once per skipped candidate
    with bounded labels."""
    counter = MagicMock()
    monkeypatch.setattr("influx.metrics.dedup_lookup_errors", lambda: counter)

    bounds = [_make_bound(item_id="bad", source_label="rss:Feed"), _make_bound()]
    client = MagicMock()
    client.cache_lookup_for_item_body = AsyncMock(
        side_effect=[_lithos_error(), {"hit": False}]
    )

    await dedup_scored_candidates(
        bounds,
        client=client,
        profile="p1",
        skip_cache_hits=False,
    )

    counter.add.assert_called_once_with(1, {"profile": "p1", "source": "rss"})


@pytest.mark.asyncio
async def test_all_lookups_failed_raises() -> None:
    """Total failure still fails the run (#234 safeguard).

    Post-#232 connection failures are normalised to ``LithosError``, so
    a fully-down Lithos would otherwise produce a silently-empty
    "degraded" run indistinguishable from a quiet feed window.
    """
    bounds = [_make_bound(item_id="a"), _make_bound(item_id="b")]
    client = MagicMock()
    client.cache_lookup_for_item_body = AsyncMock(
        side_effect=[_lithos_error("first"), _lithos_error("second")]
    )

    with pytest.raises(LithosError, match="all 2 candidates") as excinfo:
        await dedup_scored_candidates(
            bounds,
            client=client,
            profile="p1",
            skip_cache_hits=False,
        )
    assert isinstance(excinfo.value.__cause__, LithosError)
    assert excinfo.value.detail == "second"


@pytest.mark.asyncio
async def test_all_lookups_failed_emits_no_swallowed_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A total outage raises WITHOUT recording skip-and-degrade signals.

    The telemetry record / metric / per-candidate warning all describe a
    *swallowed* skip; when the helper aborts the run instead, emitting
    them would overcount degradations that were never recovered from.
    """
    counter = MagicMock()
    monkeypatch.setattr("influx.metrics.dedup_lookup_errors", lambda: counter)
    errors: list[dict[str, str]] = []
    token = current_dedup_lookup_errors.set(errors)
    try:
        bounds = [_make_bound(item_id="a"), _make_bound(item_id="b")]
        client = MagicMock()
        client.cache_lookup_for_item_body = AsyncMock(
            side_effect=[_lithos_error("first"), _lithos_error("second")]
        )

        with pytest.raises(LithosError, match="all 2 candidates"):
            await dedup_scored_candidates(
                bounds,
                client=client,
                profile="p1",
                skip_cache_hits=False,
            )
    finally:
        current_dedup_lookup_errors.reset(token)

    assert errors == []
    counter.add.assert_not_called()


@pytest.mark.asyncio
async def test_all_lookups_failed_propagates_underlying_operation() -> None:
    """The all-failed summary error carries the underlying operation —
    e.g. ``connect`` when Lithos was unreachable (#232) — so the
    formatted ledger error points operators at what actually failed."""
    bound = _make_bound()
    client = MagicMock()
    client.cache_lookup_for_item_body = AsyncMock(
        side_effect=LithosError(
            "connection_failed", operation="connect", detail="refused"
        )
    )

    with pytest.raises(LithosError, match="all 1 candidates") as excinfo:
        await dedup_scored_candidates(
            [bound],
            client=client,
            profile="p1",
            skip_cache_hits=False,
        )
    assert excinfo.value.operation == "connect"
    assert excinfo.value.detail == "refused"


@pytest.mark.asyncio
async def test_metric_incremented_per_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``metrics.cache_hits()`` is incremented exactly once per cache hit
    (skip path and merge path both count)."""
    counter = MagicMock()
    monkeypatch.setattr("influx.metrics.cache_hits", lambda: counter)

    bounds = [
        _make_bound(item_id="hit-1"),
        _make_bound(item_id="miss-1"),
        _make_bound(item_id="hit-2"),
    ]
    client = _client_with_responses(
        {"hit": True},
        {"hit": False},
        {"hit": True},
    )

    await dedup_scored_candidates(
        bounds,
        client=client,
        profile="p1",
        skip_cache_hits=False,
    )

    assert counter.add.call_count == 2


@pytest.mark.asyncio
async def test_metric_source_label_bounded_for_rss_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-feed RSS bounds carry ``source_label="rss:<feed_name>"`` for
    log richness, but the ``source`` *metric* label must collapse to the
    bounded ``"rss"`` per ``influx/metrics.py`` cardinality discipline.
    Otherwise per-feed names would leak into metric series and skew any
    dashboard / alert keyed on the bounded set."""
    counter = MagicMock()
    monkeypatch.setattr("influx.metrics.cache_hits", lambda: counter)

    rss_bound = _make_bound(item_id="rss-hit", source_label="rss:Mozilla Hacks")
    arxiv_bound = _make_bound(item_id="arxiv-hit", source_label="arxiv")
    client = _client_with_responses({"hit": True}, {"hit": True})

    await dedup_scored_candidates(
        [rss_bound, arxiv_bound],
        client=client,
        profile="p1",
        skip_cache_hits=True,
    )

    label_sets = [call.args[1] for call in counter.add.call_args_list]
    assert {"profile": "p1", "source": "rss"} in label_sets
    assert {"profile": "p1", "source": "arxiv"} in label_sets
    # No per-feed cardinality leak.
    for labels in label_sets:
        assert ":" not in labels["source"]
