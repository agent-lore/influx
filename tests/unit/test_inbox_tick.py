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

from typing import Any
from unittest.mock import patch

from influx.config import (
    AppConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
)
from influx.coordinator import Coordinator
from influx.inbox import InboxTick, _extract_note_id
from influx.run import RunOutcome
from influx.source import Candidate, ScoredCandidate
from influx.sources.inbox import InboxAcquisition


def _make_config() -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        profiles=[
            ProfileConfig(
                name="alpha",
                description="Alpha profile",
                thresholds=ProfileThresholds(relevance=7),
            )
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
    ) -> None:
        self._tasks = tasks
        self._claim_success = claim_success
        self._note_id = note_id
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
        if self._note_id:
            return {"hit": True, "id": self._note_id}
        return {"hit": False}

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


def _scorer_returning(score: int):
    async def _scorer(
        candidates: list[Candidate], profile: str, filter_prompt: str
    ) -> dict[str, ScoredCandidate]:
        return {
            c.item_id: ScoredCandidate(
                candidate=c,
                score=score,
                confidence=0.9,
                reason="relevant",
                filter_tags=("t",),
            )
            for c in candidates
        }

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


def _tick(client: FakeClient) -> InboxTick:
    return InboxTick(
        config=_make_config(),
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
    assert "skipped" in done["outcome"]
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


async def test_per_item_failure_isolation() -> None:
    """A crashing item does not prevent sibling items from completing."""
    client = FakeClient(
        tasks=[_task(task_id="task-1"), _task(task_id="task-2")],
    )

    def _dispatch_side_effect(profile: str, **_: Any) -> RunOutcome:
        if client.claimed and client.claimed[-1] == "task-1":
            raise RuntimeError("boom on first item")
        return RunOutcome(ingested=1)

    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer",
            return_value=_scorer_returning(8),
        ),
        patch("influx.inbox.build_filter_prompt", return_value="prompt"),
        patch("influx.inbox.dispatch_profile", side_effect=_dispatch_side_effect),
    ):
        await _tick(client).execute()

    # Both claimed; the second completed despite the first crashing.
    assert client.claimed == ["task-1", "task-2"]
    completed_ids = {c["task_id"] for c in client.completed}
    assert "task-2" in completed_ids


def test_extract_note_id_key_fallback() -> None:
    """note-id recovery tolerates id / note_id / existing_id; miss → None."""
    assert _extract_note_id({"hit": True, "id": "n1"}) == "n1"
    assert _extract_note_id({"hit": True, "note_id": "n2"}) == "n2"
    assert _extract_note_id({"hit": True, "existing_id": "n3"}) == "n3"
    assert _extract_note_id({"hit": False, "id": "n1"}) is None
    assert _extract_note_id({"hit": True}) is None
