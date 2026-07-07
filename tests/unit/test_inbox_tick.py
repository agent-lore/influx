"""Unit tests for the InboxTick orchestrator (Inbox v1 slice 1).

Exercises the novel orchestration logic with a lightweight fake
``LithosClient`` and patched seams (``acquire_inbox_bytes``, the filter
scorer, ``build_filter_prompt``, ``dispatch_profile``):

- happy path: list → claim → score → dispatch → update + complete with
  ``cited_nodes`` and a structured ``inbox_result``;
- filtered-out: no dispatch, terminal "filtered out" completion;
- invalid submission / invalid source_tag: terminal error completion;
- already-claimed task: skipped silently;
- lithos circuit open: whole tick skipped, no list/claim;
- per-item failure isolation: a crashing item does not sink siblings.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from influx.config import (
    AppConfig,
    InboxConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    StorageConfig,
)
from influx.coordinator import Coordinator
from influx.inbox import InboxTick, _extract_note_id
from influx.run import RunOutcome
from influx.source import Candidate, ScoredCandidate
from influx.sources.inbox import InboxAcquisition


def _make_config(
    profiles: list[tuple[str, int]] | None = None,
) -> AppConfig:
    """Config with the given ``(profile_name, relevance_threshold)`` profiles
    (defaults to a single ``alpha`` profile at threshold 7)."""
    specs = profiles if profiles is not None else [("alpha", 7)]
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        profiles=[
            ProfileConfig(
                name=name,
                description=f"{name} profile",
                thresholds=ProfileThresholds(relevance=threshold),
            )
            for name, threshold in specs
        ],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
    )


class FakeClient:
    """Minimal LithosClient double for the tick's task lifecycle calls."""

    def __init__(
        self,
        *,
        tasks: list[dict[str, Any]],
        claim_success: bool = True,
        note_id: str | None = "note-xyz",
        existing_note_id: str | None = None,
        existing_note: dict[str, Any] | None = None,
    ) -> None:
        self._tasks = tasks
        self._claim_success = claim_success
        # ``note_id`` is what post-dispatch recovery resolves for a *fresh*
        # item; ``existing_note_id`` (+ ``existing_note``) drive the
        # cache-hit gate + read_note for replay tests.  cache_lookup is
        # called twice on the fresh path (gate miss, then recovery hit), so
        # the first call models the gate and later calls model recovery.
        self._note_id = note_id
        self._existing_note_id = existing_note_id
        self._existing_note = existing_note or {"content": "", "tags": []}
        self._gated: set[str] = set()
        self.claimed: list[str] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.completed: list[dict[str, Any]] = []
        self.list_calls = 0

    async def task_list_body(self, **_: Any) -> dict[str, Any]:
        self.list_calls += 1
        return {"tasks": self._tasks}

    async def task_claim_body(self, *, task_id: str, **_: Any) -> dict[str, Any]:
        self.claimed.append(task_id)
        return {"success": self._claim_success}

    async def task_update_body(
        self, *, task_id: str, agent: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        self.updated.append((task_id, metadata))
        return {"success": True}

    async def task_complete_body(
        self,
        *,
        task_id: str,
        agent: str,
        outcome: str | None = None,
        cited_nodes: list[str] | None = None,
    ) -> dict[str, Any]:
        self.completed.append(
            {"task_id": task_id, "outcome": outcome, "cited_nodes": cited_nodes}
        )
        return {"status": "completed"}

    async def cache_lookup_by_url_body(self, *, source_url: str) -> dict[str, Any]:
        # First lookup for a URL is the cache-hit gate; later lookups for the
        # same URL are post-dispatch note-id recovery (fresh path).
        if source_url not in self._gated:
            self._gated.add(source_url)
            if self._existing_note_id:
                return {"hit": True, "id": self._existing_note_id}
            return {"hit": False}
        if self._note_id:
            return {"hit": True, "id": self._note_id}
        return {"hit": False}

    async def read_note(self, *, note_id: str) -> dict[str, Any]:
        return self._existing_note

    async def close(self) -> None:
        return None


def _acquisition() -> InboxAcquisition:
    return InboxAcquisition(
        source_url="https://example.com/article",
        url_hash="abc1234567",
        archive_path="inbox/2026/06/abc1234567.html",
        archive_missing=False,
        extracted_text="body text",
        summary="body text",
        text_flavour="html",
    )


def _scored(c: Candidate, score: int) -> ScoredCandidate:
    return ScoredCandidate(
        candidate=c, score=score, confidence=0.9, reason="relevant", filter_tags=("t",)
    )


def _scorer_returning(score: int):
    """Raw batch scorer that returns *score* for every profile."""

    async def _scorer(
        candidates: list[Candidate], profile: str, filter_prompt: str
    ) -> dict[str, ScoredCandidate]:
        return {c.item_id: _scored(c, score) for c in candidates}

    return _scorer


def _scorer_by_profile(scores: dict[str, int], *, raise_for: set[str] | None = None):
    """Raw batch scorer keyed by profile name.

    A profile absent from *scores* returns an empty dict (model omitted the
    item); a profile in *raise_for* raises to exercise filter-error isolation.
    """
    raises = raise_for or set()

    async def _scorer(
        candidates: list[Candidate], profile: str, filter_prompt: str
    ) -> dict[str, ScoredCandidate]:
        if profile in raises:
            raise RuntimeError(f"filter boom for {profile}")
        if profile not in scores:
            return {}
        return {c.item_id: _scored(c, scores[profile]) for c in candidates}

    return _scorer


def _task(
    *,
    task_id: str = "task-1",
    kind: str = "url",
    url: str | None = "https://example.com/article",
    source_tag: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"kind": kind, "submitted_by": "agent:test"}
    if url is not None:
        metadata["url"] = url
    if source_tag is not None:
        metadata["source_tag"] = source_tag
    return {"id": task_id, "metadata": metadata}


def _tick(client: FakeClient, config: AppConfig | None = None) -> InboxTick:
    return InboxTick(
        config=config or _make_config(),
        coordinator=Coordinator(),
        probe_loop=None,
        ledger=None,
        client_factory=lambda: client,  # type: ignore[arg-type]
    )


async def test_happy_path_ingests_and_completes_with_cited_nodes() -> None:
    client = FakeClient(tasks=[_task()], note_id="note-xyz")
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(8),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch(
            "influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)
        ) as mock_dispatch,
    ):
        await _tick(client).execute()

    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.args[0] == "alpha"
    assert client.claimed == ["task-1"]
    assert len(client.completed) == 1
    done = client.completed[0]
    assert "ingested into 1 profile(s): alpha" in done["outcome"]
    assert done["cited_nodes"] == ["note-xyz"]
    # Structured inbox_result attached before completion.
    assert client.updated[0][1]["inbox_result"]["per_profile"]["alpha"]["ingested"]


async def test_filtered_out_does_not_dispatch() -> None:
    client = FakeClient(tasks=[_task()])
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(3),  # below threshold 7
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile") as mock_dispatch,
    ):
        await _tick(client).execute()

    mock_dispatch.assert_not_called()
    assert "filtered out" in client.completed[0]["outcome"]
    assert client.completed[0]["cited_nodes"] is None
    # #212: the filtered-out payload carries processing_time_ms like the
    # ingested/cache-hit payloads.
    assert "processing_time_ms" in client.updated[0][1]["inbox_result"]


async def test_invalid_submission_completes_with_error() -> None:
    client = FakeClient(tasks=[_task(kind="pdf", url=None)])
    with patch("influx.inbox.dispatch_profile") as mock_dispatch:
        await _tick(client).execute()
    mock_dispatch.assert_not_called()
    assert "invalid submission" in client.completed[0]["outcome"]


async def test_invalid_url_scheme_completes_with_error() -> None:
    client = FakeClient(tasks=[_task(url="file:///etc/passwd")])
    with patch("influx.inbox.dispatch_profile") as mock_dispatch:
        await _tick(client).execute()
    mock_dispatch.assert_not_called()
    assert "invalid_url_scheme" in client.completed[0]["outcome"]


async def test_submitter_is_sanitised_before_dispatch() -> None:
    task = _task()
    task["metadata"]["submitted_by"] = "agent name\nwith spaces"
    client = FakeClient(tasks=[task])
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(8),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch(
            "influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)
        ) as mock_dispatch,
    ):
        await _tick(client).execute()
    assert mock_dispatch.call_args.kwargs["submitted_by"] == "agent-name-with-spaces"


async def test_skipped_run_reported_as_skipped_without_note_recovery() -> None:
    client = FakeClient(tasks=[_task()], note_id="should-not-be-used")
    skipped = RunOutcome(skipped=True, skip_reason="lithos_unhealthy", ingested=0)
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(8),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile", return_value=skipped),
    ):
        await _tick(client).execute()
    done = client.completed[0]
    assert "not ingested" in done["outcome"]
    assert "lithos_unhealthy" in done["outcome"]
    # No write happened → no note-id recovery → empty cited_nodes.
    assert done["cited_nodes"] is None


async def test_invalid_source_tag_completes_terminally() -> None:
    client = FakeClient(tasks=[_task(source_tag="Not A Slug")])
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch("influx.inbox.dispatch_profile") as mock_dispatch,
    ):
        await _tick(client).execute()
    mock_dispatch.assert_not_called()
    assert client.completed[0]["outcome"] == "error: invalid_source_tag"


async def test_already_claimed_task_is_skipped() -> None:
    client = FakeClient(tasks=[_task()], claim_success=False)
    with patch("influx.inbox.dispatch_profile") as mock_dispatch:
        await _tick(client).execute()
    mock_dispatch.assert_not_called()
    assert client.completed == []


async def test_circuit_open_skips_whole_tick() -> None:
    class _Probe:
        def lithos_circuit_open(self) -> bool:
            return True

    client = FakeClient(tasks=[_task()])
    tick = InboxTick(
        config=_make_config(),
        coordinator=Coordinator(),
        probe_loop=_Probe(),
        client_factory=lambda: client,  # type: ignore[arg-type]
    )
    await tick.execute()
    assert client.list_calls == 0
    assert client.completed == []


async def test_busy_profile_skipped_this_tick_not_completed() -> None:
    """A busy profile is skipped (no overlap) AND the task is left un-completed
    so a later tick retries it — not terminally dropped (§5.5 / §10)."""
    client = FakeClient(tasks=[_task()])
    coordinator = Coordinator()
    tick = InboxTick(
        config=_make_config(),
        coordinator=coordinator,
        probe_loop=None,
        ledger=None,
        client_factory=lambda: client,  # type: ignore[arg-type]
    )
    # Hold the lock for the whole tick so dispatch hits ProfileBusyError.
    async with coordinator.hold("alpha"):
        with (
            patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
            patch(
                "influx.inbox.make_default_batch_scorer",
                return_value=_scorer_returning(8),
            ),
            patch("influx.inbox.build_filter_prompt", return_value="prompt"),
            patch("influx.inbox.dispatch_profile") as mock_dispatch,
        ):
            await tick.execute()

    # The Run is never dispatched while the profile is busy …
    mock_dispatch.assert_not_called()
    # … the task was claimed but NOT completed (claim lease expires → retry).
    assert client.claimed == ["task-1"]
    assert client.completed == []


async def test_per_item_failure_isolation() -> None:
    """A hard crash on one item does not prevent siblings from completing.

    Acquisition raises for the first item — an error that propagates to the
    execute()-level per-item handler (not caught inside dispatch).
    """
    client = FakeClient(
        tasks=[
            _task(task_id="task-1", url="https://example.com/a"),
            _task(task_id="task-2", url="https://example.com/b"),
        ]
    )

    def _acquire_side_effect(*_a: Any, **_k: Any) -> InboxAcquisition:
        if client.claimed and client.claimed[-1] == "task-1":
            raise RuntimeError("boom acquiring first item")
        return _acquisition()

    with (
        patch("influx.inbox.acquire_inbox_bytes", side_effect=_acquire_side_effect),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(8),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)),
    ):
        await _tick(client).execute()

    # Both claimed; task-1 crashed (left for retry), task-2 still completed.
    assert client.claimed == ["task-1", "task-2"]
    completed_ids = {c["task_id"] for c in client.completed}
    assert completed_ids == {"task-2"}


async def test_per_profile_dispatch_failure_isolated() -> None:
    """A dispatch failure for one profile is contained; the item completes.

    Profile b's dispatch raises (non-busy); profile a still ingests and the
    task completes with a partial outcome rather than crashing the item
    (which would orphan a's write and double its ledger entry on retry)."""
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(tasks=[_task()], note_id="note-1")

    async def _dispatch(profile: str, **_: Any) -> RunOutcome:
        if profile == "b":
            raise RuntimeError("write blew up for b")
        return RunOutcome(ingested=1)

    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"a": 8, "b": 8}),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile", side_effect=_dispatch),
    ):
        await _tick(client, config).execute()

    assert len(client.completed) == 1
    outcome = client.completed[0]["outcome"]
    assert "ingested into 1/2 profiles (a)" in outcome
    assert "b failed" in outcome
    per_profile = client.updated[0][1]["inbox_result"]["per_profile"]
    assert per_profile["a"]["ingested"] is True
    assert per_profile["b"]["ingested"] is False


def test_extract_note_id_key_fallback() -> None:
    """note-id recovery tolerates id / note_id / existing_id; miss → None."""
    assert _extract_note_id({"hit": True, "id": "n1"}) == "n1"
    assert _extract_note_id({"hit": True, "note_id": "n2"}) == "n2"
    assert _extract_note_id({"hit": True, "existing_id": "n3"}) == "n3"
    assert _extract_note_id({"hit": False, "id": "n1"}) is None
    assert _extract_note_id({"hit": True}) is None


# ── Slice 2: multi-profile fan-out ──────────────────────────────────


async def test_fans_out_to_all_clearing_profiles() -> None:
    """Every profile clearing threshold is dispatched; outcome lists them."""
    config = _make_config([("a", 7), ("b", 7), ("c", 7)])
    client = FakeClient(tasks=[_task()], note_id="note-1")
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"a": 8, "b": 9, "c": 7}),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch(
            "influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)
        ) as mock_dispatch,
    ):
        await _tick(client, config).execute()

    dispatched_profiles = sorted(call.args[0] for call in mock_dispatch.call_args_list)
    assert dispatched_profiles == ["a", "b", "c"]
    done = client.completed[0]
    assert done["outcome"].startswith("ingested into 3 profile(s):")
    per_profile = client.updated[0][1]["inbox_result"]["per_profile"]
    assert {p for p in per_profile if per_profile[p]["ingested"]} == {"a", "b", "c"}
    # All profiles merge into one canonical note → single cited node.
    assert done["cited_nodes"] == ["note-1"]


async def test_below_threshold_profiles_excluded_and_top_score_reported() -> None:
    """No profile clears → filtered_out outcome reports the top score."""
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(tasks=[_task()])
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"a": 4, "b": 5}),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile") as mock_dispatch,
    ):
        await _tick(client, config).execute()

    mock_dispatch.assert_not_called()
    outcome = client.completed[0]["outcome"]
    assert outcome == "filtered out: top score 5 (b) below threshold 7"
    per_profile = client.updated[0][1]["inbox_result"]["per_profile"]
    assert per_profile["a"] == {
        "score": 4,
        "ingested": False,
        "reason": "below_threshold",
    }


async def test_partial_fanout_one_clears_one_below() -> None:
    """Mixed scores: only the clearing profile dispatches."""
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(tasks=[_task()], note_id="note-1")
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"a": 8, "b": 3}),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch(
            "influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)
        ) as mock_dispatch,
    ):
        await _tick(client, config).execute()

    assert [c.args[0] for c in mock_dispatch.call_args_list] == ["a"]
    assert client.completed[0]["outcome"] == "ingested into 1 profile(s): a"
    per_profile = client.updated[0][1]["inbox_result"]["per_profile"]
    assert per_profile["a"]["ingested"] is True
    assert per_profile["b"]["reason"] == "below_threshold"


async def test_filter_failure_isolated_to_one_profile() -> None:
    """A filter error on profile b does not sink profile a's ingestion."""
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(tasks=[_task()], note_id="note-1")
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"a": 8}, raise_for={"b"}),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch(
            "influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)
        ) as mock_dispatch,
    ):
        await _tick(client, config).execute()

    assert [c.args[0] for c in mock_dispatch.call_args_list] == ["a"]
    # The filter failure is reported in the human outcome (#196), not just
    # the structured payload.
    assert client.completed[0]["outcome"] == (
        "ingested into 1/2 profiles (a); b filter failed"
    )
    per_profile = client.updated[0][1]["inbox_result"]["per_profile"]
    assert per_profile["b"] == {"ingested": False, "reason": "filter_error"}


async def test_one_clears_one_busy_partial_outcome() -> None:
    """A busy clearing profile is reported; the free profile still ingests."""
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(tasks=[_task()], note_id="note-1")
    coordinator = Coordinator()
    tick = InboxTick(
        config=config,
        coordinator=coordinator,
        client_factory=lambda: client,  # type: ignore[arg-type]
    )

    async def _dispatch(profile: str, **_: Any) -> RunOutcome:
        return RunOutcome(ingested=1)

    async with coordinator.hold("b"):  # b is busy for the whole tick
        with (
            patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
            patch(
                "influx.inbox.make_default_batch_scorer",
                return_value=_scorer_by_profile({"a": 8, "b": 8}),
            ),
            patch("influx.inbox.build_filter_prompt", return_value="prompt"),
            patch("influx.inbox.dispatch_profile", side_effect=_dispatch),
        ):
            await tick.execute()

    outcome = client.completed[0]["outcome"]
    assert outcome == "ingested into 1/2 profiles (a); b profile_busy"
    per_profile = client.updated[0][1]["inbox_result"]["per_profile"]
    assert per_profile["b"]["reason"] == "profile_busy"


async def test_all_clearing_profiles_busy_skips_tick() -> None:
    """When every clearing profile is busy, the task is left for retry."""
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(tasks=[_task()])
    coordinator = Coordinator()
    tick = InboxTick(
        config=config,
        coordinator=coordinator,
        client_factory=lambda: client,  # type: ignore[arg-type]
    )
    async with coordinator.hold("a"), coordinator.hold("b"):
        with (
            patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
            patch(
                "influx.inbox.make_default_batch_scorer",
                return_value=_scorer_by_profile({"a": 8, "b": 8}),
            ),
            patch("influx.inbox.build_filter_prompt", return_value="prompt"),
            patch("influx.inbox.dispatch_profile") as mock_dispatch,
        ):
            await tick.execute()

    mock_dispatch.assert_not_called()
    assert client.claimed == ["task-1"]
    assert client.completed == []  # left un-completed → retried next tick


async def test_bytes_acquired_once_for_all_profiles() -> None:
    """acquire_inbox_bytes runs exactly once regardless of profile count."""
    config = _make_config([("a", 7), ("b", 7), ("c", 7)])
    client = FakeClient(tasks=[_task()], note_id="note-1")
    with (
        patch(
            "influx.inbox.acquire_inbox_bytes", return_value=_acquisition()
        ) as mock_acquire,
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"a": 8, "b": 8, "c": 8}),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)),
    ):
        await _tick(client, config).execute()

    mock_acquire.assert_called_once()


# ── Slice 3: cache-hit replay (§6) ──────────────────────────────────


def _note_content(ingested: list[str]) -> str:
    """Render note body whose ## Profile Relevance lists *ingested* profiles."""
    from influx.renderer import ProfileRelevanceEntry, render_note

    return render_note(
        title="Existing",
        tags=["ingested-by:influx"],
        confidence=1.0,
        archive_path=None,
        summary="s",
        keywords=[],
        profile_entries=[
            ProfileRelevanceEntry(profile_name=n, score=8, reason="r") for n in ingested
        ],
    )


async def test_cache_hit_replays_only_complement_profiles() -> None:
    """On a cache hit, only profiles that haven't ingested are re-dispatched."""
    config = _make_config([("a", 7), ("b", 7), ("c", 7)])
    client = FakeClient(
        tasks=[_task()],
        existing_note_id="note-1",
        existing_note={"content": _note_content(["a", "b"]), "tags": []},
    )
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"a": 8, "b": 8, "c": 8}),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch(
            "influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)
        ) as mock_dispatch,
    ):
        await _tick(client, config).execute()

    # a and b already ingested → only c is dispatched.
    assert [c.args[0] for c in mock_dispatch.call_args_list] == ["c"]
    outcome = client.completed[0]["outcome"]
    assert outcome == "cache_hit: existing note note-1; added 1 profile entry: c"
    assert client.completed[0]["cited_nodes"] == ["note-1"]


async def test_cache_hit_excludes_rejected_tag_profiles() -> None:
    """influx:rejected:<profile> suppresses that profile from replay (§6)."""
    config = _make_config([("a", 7), ("b", 7), ("c", 7)])
    client = FakeClient(
        tasks=[_task()],
        existing_note_id="note-1",
        existing_note={
            "content": _note_content(["a"]),
            "tags": ["influx:rejected:b"],
        },
    )
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"c": 8}),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch(
            "influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)
        ) as mock_dispatch,
    ):
        await _tick(client, config).execute()

    # a ingested, b operator-suppressed → only c is a candidate.
    assert [c.args[0] for c in mock_dispatch.call_args_list] == ["c"]


async def test_cache_hit_no_new_profiles_to_consider_skips_acquire() -> None:
    """When every profile already ingested, no acquire/dispatch happens."""
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(
        tasks=[_task()],
        existing_note_id="note-1",
        existing_note={"content": _note_content(["a", "b"]), "tags": []},
    )
    with (
        patch("influx.inbox.acquire_inbox_bytes") as mock_acquire,
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({}),
        ),
        patch("influx.inbox.dispatch_profile") as mock_dispatch,
    ):
        await _tick(client, config).execute()

    mock_acquire.assert_not_called()
    mock_dispatch.assert_not_called()
    assert client.completed[0]["outcome"] == (
        "cache_hit: existing note note-1; no new profiles to consider"
    )
    assert client.completed[0]["cited_nodes"] == ["note-1"]
    # Stable inbox_result shape even when acquisition is skipped.
    result = client.updated[0][1]["inbox_result"]
    assert result["cache_hit"] is True
    assert result["archive_path"] is None
    assert "processing_time_ms" in result


async def test_cache_hit_complement_scored_but_none_clear() -> None:
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(
        tasks=[_task()],
        existing_note_id="note-1",
        existing_note={"content": _note_content(["a"]), "tags": []},
    )
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"b": 3}),  # below threshold
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile") as mock_dispatch,
    ):
        await _tick(client, config).execute()

    mock_dispatch.assert_not_called()
    assert client.completed[0]["outcome"] == (
        "cache_hit: existing note note-1; no new profiles matched"
    )


async def test_cache_hit_unparseable_note_falls_back_to_all_profiles() -> None:
    """A same-URL note that read_note returns but parse can't handle must not
    poison the item — fall back to replaying all profiles (#202 review)."""
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(
        tasks=[_task()],
        existing_note_id="note-1",
        existing_note={"content": "garbage that is not a canonical note", "tags": []},
        note_id="note-1",
    )
    with (
        patch(
            "influx.inbox.parse_note", side_effect=RuntimeError("not a canonical note")
        ),
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({"a": 8, "b": 8}),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch(
            "influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)
        ) as mock_dispatch,
    ):
        await _tick(client, config).execute()

    # Parse failure → fell back to replaying ALL profiles (not crashed/skipped).
    assert sorted(c.args[0] for c in mock_dispatch.call_args_list) == ["a", "b"]
    assert len(client.completed) == 1


# ── Slice 4: InboxStatus updates across exit paths ──────────────────


async def test_status_records_circuit_open_skip() -> None:
    from influx.inbox import InboxStatus

    class _Probe:
        def lithos_circuit_open(self) -> bool:
            return True

    status = InboxStatus(enabled=True)
    client = FakeClient(tasks=[_task()])
    tick = InboxTick(
        config=_make_config(),
        coordinator=Coordinator(),
        probe_loop=_Probe(),
        status=status,
        client_factory=lambda: client,  # type: ignore[arg-type]
    )
    await tick.execute()
    assert status.last_tick_at is not None
    assert status.last_tick_outcome == "skipped: lithos circuit open"
    assert client.list_calls == 0


async def test_status_records_task_list_failure() -> None:
    from influx.errors import LithosError
    from influx.inbox import InboxStatus

    class _BadListClient(FakeClient):
        async def task_list_body(self, **_: Any) -> dict[str, Any]:
            raise LithosError("boom", operation="task_list")

    status = InboxStatus(enabled=True)
    client = _BadListClient(tasks=[])
    tick = InboxTick(
        config=_make_config(),
        coordinator=Coordinator(),
        status=status,
        client_factory=lambda: client,  # type: ignore[arg-type]
    )
    await tick.execute()
    assert status.last_tick_outcome == "error: task_list failed"


async def test_status_records_success_and_pending() -> None:
    from influx.inbox import InboxStatus

    status = InboxStatus(enabled=True)
    client = FakeClient(tasks=[_task(), _task(task_id="task-2")], note_id="n")
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(8),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)),
    ):
        tick = InboxTick(
            config=_make_config(),
            coordinator=Coordinator(),
            status=status,
            client_factory=lambda: client,  # type: ignore[arg-type]
        )
        await tick.execute()
    assert status.last_tick_outcome == "success"
    assert status.pending == 2
    assert status.in_flight == 0  # balanced after the tick


# ── v2 local-PDF tick path (docs/plans/inbox.md §16) ─────────────────


def _make_pdf_config(
    pdf_root: Path,
    archive_dir: Path,
    profiles: list[tuple[str, int]] | None = None,
    *,
    max_download_bytes: int = 52_428_800,
) -> AppConfig:
    specs = profiles if profiles is not None else [("alpha", 7)]
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        storage=StorageConfig(
            archive_dir=str(archive_dir), max_download_bytes=max_download_bytes
        ),
        inbox=InboxConfig(pdf_root=str(pdf_root)),
        profiles=[
            ProfileConfig(
                name=name,
                description=f"{name} profile",
                thresholds=ProfileThresholds(relevance=threshold),
            )
            for name, threshold in specs
        ],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
    )


def _task_pdf(
    *,
    local_path: str,
    task_id: str = "task-1",
    source_tag: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "kind": "pdf",
        "submitted_by": "agent:test",
        "local_path": local_path,
    }
    if source_tag is not None:
        metadata["source_tag"] = source_tag
    return {"id": task_id, "metadata": metadata}


def _pdf_acquisition() -> InboxAcquisition:
    return InboxAcquisition(
        source_url="inbox-pdf:sha256:deadbeef",
        url_hash="deadbeef",
        archive_path="inbox-pdf/2026/06/deadbeef.pdf",
        archive_missing=False,
        extracted_text="pdf body text",
        summary="pdf body text",
        text_flavour="pdf",
    )


async def test_pdf_without_local_path_completes_with_error() -> None:
    client = FakeClient(tasks=[{"id": "t", "metadata": {"kind": "pdf"}}])
    with patch("influx.inbox.dispatch_profile") as mock_dispatch:
        await _tick(client).execute()
    mock_dispatch.assert_not_called()
    assert "invalid submission" in client.completed[0]["outcome"]


async def test_pdf_root_not_configured_completes_terminally() -> None:
    # Default _make_config leaves pdf_root unset.
    client = FakeClient(tasks=[_task_pdf(local_path="/somewhere/paper.pdf")])
    with patch("influx.inbox.dispatch_profile") as mock_dispatch:
        await _tick(client).execute()
    mock_dispatch.assert_not_called()
    assert client.completed[0]["outcome"] == "error: pdf_root_not_configured"


async def test_pdf_path_outside_pdf_root_rejected(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF")
    config = _make_pdf_config(pdf_root, tmp_path / "archive")
    client = FakeClient(tasks=[_task_pdf(local_path=str(outside))])
    with patch("influx.inbox.dispatch_profile") as mock_dispatch:
        await _tick(client, config).execute()
    mock_dispatch.assert_not_called()
    assert client.completed[0]["outcome"] == "error: path_not_in_pdf_root"


async def test_pdf_file_missing_completes_terminally(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    missing = pdf_root / "missing.pdf"
    config = _make_pdf_config(pdf_root, tmp_path / "archive")
    client = FakeClient(tasks=[_task_pdf(local_path=str(missing))])
    with patch("influx.inbox.dispatch_profile") as mock_dispatch:
        await _tick(client, config).execute()
    mock_dispatch.assert_not_called()
    assert client.completed[0]["outcome"].startswith("file_missing:")


async def test_pdf_happy_path_ingests(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    pdf = pdf_root / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")
    config = _make_pdf_config(pdf_root, tmp_path / "archive")
    client = FakeClient(tasks=[_task_pdf(local_path=str(pdf))], note_id="note-pdf")
    with (
        patch(
            "influx.inbox.acquire_inbox_pdf", return_value=_pdf_acquisition()
        ) as mock_acquire,
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(8),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch(
            "influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)
        ) as mock_dispatch,
    ):
        await _tick(client, config).execute()

    mock_acquire.assert_called_once()
    # The resolved (absolute) path inside pdf_root was handed to acquisition.
    assert Path(mock_acquire.call_args.args[0]) == pdf.resolve()
    mock_dispatch.assert_called_once()
    assert "ingested into 1 profile(s): alpha" in client.completed[0]["outcome"]
    assert client.completed[0]["cited_nodes"] == ["note-pdf"]


async def test_pdf_relative_local_path_resolves_against_pdf_root(
    tmp_path: Path,
) -> None:
    """Regression #209: a relative local_path resolves under pdf_root, not CWD."""
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    pdf = pdf_root / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")
    config = _make_pdf_config(pdf_root, tmp_path / "archive")
    # Submitter sends a path relative to pdf_root.
    client = FakeClient(tasks=[_task_pdf(local_path="paper.pdf")], note_id="note-pdf")
    with (
        patch(
            "influx.inbox.acquire_inbox_pdf", return_value=_pdf_acquisition()
        ) as mock_acquire,
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(8),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)),
    ):
        await _tick(client, config).execute()

    mock_acquire.assert_called_once()
    assert Path(mock_acquire.call_args.args[0]) == pdf.resolve()
    assert "ingested into 1 profile(s): alpha" in client.completed[0]["outcome"]


async def test_pdf_dedup_cache_hit_no_new_profiles(tmp_path: Path) -> None:
    """A re-submitted PDF whose note already lists the profile is a cache hit.

    Unlike the URL path, acquisition still runs (the synthetic source_url is
    only known after hashing), but no new dispatch happens (§16.4).
    """
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    pdf = pdf_root / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")
    config = _make_pdf_config(pdf_root, tmp_path / "archive")
    client = FakeClient(
        tasks=[_task_pdf(local_path=str(pdf))],
        existing_note_id="note-1",
        existing_note={"content": _note_content(["alpha"]), "tags": []},
    )
    with (
        patch(
            "influx.inbox.acquire_inbox_pdf", return_value=_pdf_acquisition()
        ) as mock_acquire,
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_by_profile({}),
        ),
        patch("influx.inbox.dispatch_profile") as mock_dispatch,
    ):
        await _tick(client, config).execute()

    mock_acquire.assert_called_once()  # PDF acquires before the cache lookup
    mock_dispatch.assert_not_called()
    assert client.completed[0]["outcome"] == (
        "cache_hit: existing note note-1; no new profiles to consider"
    )


async def test_pdf_too_large_completes_terminally(tmp_path: Path) -> None:
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    pdf = pdf_root / "huge.pdf"
    pdf.write_bytes(b"%PDF-1.4 " + b"x" * 100)
    config = _make_pdf_config(pdf_root, tmp_path / "archive", max_download_bytes=10)
    client = FakeClient(tasks=[_task_pdf(local_path=str(pdf))])
    with patch("influx.inbox.dispatch_profile") as mock_dispatch:
        await _tick(client, config).execute()
    mock_dispatch.assert_not_called()
    assert client.completed[0]["outcome"].startswith("error: pdf_too_large")


async def test_pdf_read_error_completes_terminally(tmp_path: Path) -> None:
    """A read failure after validation completes terminally (no auto-retry)."""
    pdf_root = tmp_path / "pdfs"
    pdf_root.mkdir()
    pdf = pdf_root / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")
    config = _make_pdf_config(pdf_root, tmp_path / "archive")
    client = FakeClient(tasks=[_task_pdf(local_path=str(pdf))])
    with (
        patch("influx.inbox.acquire_inbox_pdf", side_effect=OSError("vanished")),
        patch("influx.inbox.dispatch_profile") as mock_dispatch,
    ):
        await _tick(client, config).execute()
    mock_dispatch.assert_not_called()
    # A post-validation read failure is distinct from a truly missing file.
    assert client.completed[0]["outcome"].startswith("error: file_read_error")
    assert client.updated[0][1]["inbox_result"]["error"] == "file_read_error"


# ── Observability metrics (#212) ─────────────────────────────────────


def _outcomes(mock_counter: Any) -> list[str]:
    """Collect the {"outcome": ...} labels passed to a patched counter."""
    return [
        call.args[1]["outcome"]
        for call in mock_counter.return_value.add.call_args_list
        if len(call.args) > 1 and isinstance(call.args[1], dict)
    ]


def _phases(mock_counter: Any) -> list[str]:
    return [
        call.args[1]["phase"]
        for call in mock_counter.return_value.add.call_args_list
        if len(call.args) > 1 and isinstance(call.args[1], dict)
    ]


async def test_invalid_kind_increments_items_processed() -> None:
    client = FakeClient(tasks=[{"id": "t", "metadata": {"kind": "bogus"}}])
    with patch("influx.metrics.inbox_items_processed") as m:
        await _tick(client).execute()
    assert "invalid_submission" in _outcomes(m)


async def test_invalid_source_tag_increments_items_processed() -> None:
    # The source_tag guard short-circuits before acquisition, so no
    # acquire/dispatch patches are needed.
    client = FakeClient(tasks=[_task(source_tag="Not A Slug")])
    with patch("influx.metrics.inbox_items_processed") as m:
        await _tick(client).execute()
    assert "invalid_source_tag" in _outcomes(m)


async def test_pdf_rejected_increments_items_processed() -> None:
    # pdf_root unset on the default config → pdf_rejected.
    client = FakeClient(tasks=[_task_pdf(local_path="/somewhere/x.pdf")])
    with patch("influx.metrics.inbox_items_processed") as m:
        await _tick(client).execute()
    assert "pdf_rejected" in _outcomes(m)


async def test_task_list_failure_increments_call_failures() -> None:
    from influx.errors import LithosError

    client = FakeClient(tasks=[])

    async def _boom(**_: Any) -> dict[str, Any]:
        raise LithosError("list boom")

    client.task_list_body = _boom  # type: ignore[method-assign]
    with patch("influx.metrics.inbox_task_call_failures") as m:
        await _tick(client).execute()
    assert "list" in _phases(m)


async def test_claim_failure_increments_call_failures() -> None:
    from influx.errors import LithosError

    client = FakeClient(tasks=[_task()])

    async def _boom(**_: Any) -> dict[str, Any]:
        raise LithosError("claim boom")

    client.task_claim_body = _boom  # type: ignore[method-assign]
    with patch("influx.metrics.inbox_task_call_failures") as m:
        await _tick(client).execute()
    assert "claim" in _phases(m)


async def test_tasks_listed_records_full_backlog_not_slice() -> None:
    """#212: inbox_tasks_listed records the full open backlog, not the slice."""
    config = AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        inbox=InboxConfig(max_items_per_tick=1),
        profiles=[
            ProfileConfig(
                name="alpha",
                description="alpha",
                thresholds=ProfileThresholds(relevance=7),
            )
        ],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
    )
    client = FakeClient(tasks=[_task(task_id="a"), _task(task_id="b")])
    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(3),  # filtered out → no dispatch
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile"),
        patch("influx.metrics.inbox_tasks_listed") as m_listed,
    ):
        await _tick(client, config).execute()
    # Two open tasks, slice of 1 processed, but the metric records the full 2.
    listed_total = sum(
        call.args[0] for call in m_listed.return_value.add.call_args_list
    )
    assert listed_total == 2
    assert client.claimed == ["a"]  # only the slice was claimed


# ── Per-item operational summary log ─────────────────────────────────────
# One INFO line per item so an operator can grep what was processed and how
# it resolved without metric-spelunking or reading Lithos task metadata.


def _summary_records(caplog: Any) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message.startswith("inbox item done")]


async def test_item_done_summary_logged_for_ingested(caplog: Any) -> None:
    client = FakeClient(tasks=[_task()], note_id="note-xyz")
    with (
        caplog.at_level(logging.INFO, logger="influx.inbox"),
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(8),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile", return_value=RunOutcome(ingested=1)),
    ):
        await _tick(client).execute()

    records = _summary_records(caplog)
    assert len(records) == 1
    rec = records[0]
    assert getattr(rec, "outcome", None) == "ingested"
    assert getattr(rec, "source_url", None) == "https://example.com/article"
    assert getattr(rec, "profiles", None) == ["alpha"]
    assert getattr(rec, "task_id", None) == "task-1"


async def test_item_done_summary_logged_for_filtered_out(caplog: Any) -> None:
    client = FakeClient(tasks=[_task()])
    with (
        caplog.at_level(logging.INFO, logger="influx.inbox"),
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(3),  # below threshold 7
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile"),
    ):
        await _tick(client).execute()

    records = _summary_records(caplog)
    assert len(records) == 1
    assert getattr(records[0], "outcome", None) == "filtered_out"
    assert getattr(records[0], "profiles", None) == []  # nothing ingested


async def test_item_done_summary_logged_for_invalid_submission(caplog: Any) -> None:
    client = FakeClient(tasks=[{"id": "t", "metadata": {"kind": "bogus"}}])
    with caplog.at_level(logging.INFO, logger="influx.inbox"):
        await _tick(client).execute()

    records = _summary_records(caplog)
    assert len(records) == 1
    assert getattr(records[0], "outcome", None) == "invalid_submission"


async def test_busy_skip_logs_deferral_with_source(caplog: Any) -> None:
    """The all-profiles-busy branch never completes, so it logs its own
    deferral line — otherwise the item would be invisible at INFO."""
    config = _make_config([("a", 7), ("b", 7)])
    client = FakeClient(tasks=[_task()])
    coordinator = Coordinator()
    tick = InboxTick(
        config=config,
        coordinator=coordinator,
        client_factory=lambda: client,  # type: ignore[arg-type]
    )
    async with coordinator.hold("a"), coordinator.hold("b"):
        with (
            caplog.at_level(logging.INFO, logger="influx.inbox"),
            patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
            patch(
                "influx.inbox.make_default_batch_scorer",
                return_value=_scorer_by_profile({"a": 8, "b": 8}),
            ),
            patch("influx.inbox.build_filter_prompt", return_value="prompt"),
            patch("influx.inbox.dispatch_profile"),
        ):
            await tick.execute()

    assert _summary_records(caplog) == []  # no completion → no "item done"
    deferred = [r for r in caplog.records if "deferred: all profiles busy" in r.message]
    assert len(deferred) == 1
    assert getattr(deferred[0], "source_url", None) == "https://example.com/article"
    assert sorted(getattr(deferred[0], "profiles", [])) == ["a", "b"]
