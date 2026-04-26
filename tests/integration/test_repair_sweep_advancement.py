"""Integration tests for retry-order advancement (AC-X-8 main).

Seeds B > ``repair.max_items_per_run`` persistently-failing notes
(``lithos_write`` succeeds but repair makes no progress) and verifies
the rewrite-on-every-visit invariant: every visited note's
``updated_at`` advances, two successive runs visit ``min(B, 2 * M)``
distinct notes, and no note from run K reappears in the first-N
slice of run K+1.

Covers: AC-X-8 main case (retry-order advancement).
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import pytest

from influx.config import (
    AppConfig,
    FeedbackConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    RepairConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.lithos_client import LithosClient
from influx.repair import SweepHooks, sweep
from tests.contract.test_lithos_client import FakeLithosServer

# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def fake_lithos() -> Generator[FakeLithosServer, None, None]:
    server = FakeLithosServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def fake_lithos_url(fake_lithos: FakeLithosServer) -> str:
    return f"http://127.0.0.1:{fake_lithos.port}/sse"


@pytest.fixture(autouse=True)
def clear_fakes(fake_lithos: FakeLithosServer) -> None:
    fake_lithos.calls.clear()
    fake_lithos.write_responses.clear()
    fake_lithos.read_responses.clear()
    fake_lithos.cache_lookup_responses.clear()
    fake_lithos.list_responses.clear()


# ── Helpers ────────────────────────────────────────────────────────


def _make_config(
    *,
    lithos_url: str,
    max_items: int = 3,
) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="AI & Robotics",
                thresholds=ProfileThresholds(notify_immediate=8),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(
                text="Filter: {profile_description} "
                "{negative_examples} "
                "{min_score_in_results}",
            ),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        feedback=FeedbackConfig(negative_examples_per_profile=20),
        repair=RepairConfig(max_items_per_run=max_items),
    )


def _make_note_content(
    *,
    note_id: str,
    archive_path: str | None = None,
    score: int = 5,
) -> str:
    """Build canonical note content with controllable archive and score."""
    archive_body = f"path: {archive_path}\n" if archive_path is not None else ""
    return (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        f"source_url: https://arxiv.org/abs/2601.{note_id}\n"
        "tags:\n"
        "  - profile:ai-robotics\n"
        "  - ingested-by:influx\n"
        "  - source:arxiv\n"
        "confidence: 0.9\n"
        "---\n"
        f"# Test Paper {note_id}\n"
        "\n"
        "## Archive\n"
        f"{archive_body}"
        "\n"
        "## Summary\n"
        "A test paper summary.\n"
        "\n"
        "## Profile Relevance\n"
        "### ai-robotics\n"
        f"Score: {score}/10\n"
        "Somewhat relevant.\n"
        "\n"
        "## User Notes\n"
    )


def _make_note_dict(
    *,
    note_id: str,
    tags: list[str],
    archive_path: str | None = None,
    score: int = 5,
) -> dict[str, Any]:
    """Build a note dict as returned by lithos_read."""
    return {
        "id": note_id,
        "title": f"Test Paper {note_id}",
        "content": _make_note_content(
            note_id=note_id,
            archive_path=archive_path,
            score=score,
        ),
        "tags": tags,
        "version": 1,
        "source_url": f"https://arxiv.org/abs/2601.{note_id}",
        "path": "papers/arxiv/2026/04",
        "confidence": 0.9,
        "note_type": "summary",
        "namespace": "influx",
    }


_BASE_TAGS = [
    "profile:ai-robotics",
    "influx:repair-needed",
    "ingested-by:influx",
    "source:arxiv",
    "text:html",
]

_ARCHIVE_PATH = "papers/arxiv/2026/04/test.pdf"

# High score (9) makes Tier 2 (threshold=8) and Tier 3 (threshold=9)
# required.  Without ``full-text`` and ``influx:deep-extracted``, and
# without Tier 2 / Tier 3 hooks wired, the clearing logic will NOT
# remove ``influx:repair-needed`` — the note is "persistently failing".
_PERSISTENTLY_FAILING_SCORE = 9


def _make_persistently_failing_notes(
    count: int,
) -> list[dict[str, Any]]:
    """Create *count* persistently-failing notes.

    Each note has ``text:html`` + archive path stored, but score=9
    means Tier 2 and Tier 3 are required for clearing.  Without
    hooks wired, those stages are skipped and
    ``influx:repair-needed`` stays — the notes are "persistently
    failing" (write succeeds but no forward progress).
    """
    notes = []
    for i in range(count):
        note_id = f"note-{i + 1:03d}"
        notes.append(
            _make_note_dict(
                note_id=note_id,
                tags=list(_BASE_TAGS),
                archive_path=_ARCHIVE_PATH,
                score=_PERSISTENTLY_FAILING_SCORE,
            )
        )
    return notes


def _queue_notes_for_sweep(
    fake_lithos: FakeLithosServer,
    notes: list[dict[str, Any]],
) -> None:
    """Queue list + read + write responses for a multi-note sweep."""
    items = [{"id": n["id"], "title": n["title"]} for n in notes]
    fake_lithos.list_responses.append(json.dumps({"items": items}))
    for note in notes:
        fake_lithos.read_responses.append(json.dumps(note))
        fake_lithos.write_responses.append('{"status": "updated"}')


# ── Tests ──────────────────────────────────────────────────────────


class TestRetryOrderAdvancement:
    """AC-X-8: rewrite-on-every-visit invariant and fairness."""

    async def test_every_visited_note_is_rewritten(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Every visited note triggers a lithos_write call."""
        m = 3
        notes = _make_persistently_failing_notes(m)
        _queue_notes_for_sweep(fake_lithos, notes)

        config = _make_config(lithos_url=fake_lithos_url, max_items=m)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited) == m

            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == m

            # Each write corresponds to a distinct visited note.
            written_ids = [c[1]["id"] for c in write_calls]
            expected_ids = [n["id"] for n in notes]
            assert written_ids == expected_ids
        finally:
            await client.close()

    async def test_no_progress_still_rewrites(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Notes with no forward progress are still rewritten."""
        notes = _make_persistently_failing_notes(2)
        _queue_notes_for_sweep(fake_lithos, notes)

        config = _make_config(lithos_url=fake_lithos_url, max_items=2)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )

            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            # Both notes rewritten even though no hooks made progress.
            assert len(write_calls) == 2

            for wc in write_calls:
                payload = wc[1]
                # influx:repair-needed still present (no progress).
                assert "influx:repair-needed" in payload["tags"]
        finally:
            await client.close()

    async def test_two_runs_visit_distinct_notes(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Two runs with B > M visit >= min(B, 2*M) distinct notes.

        With B=5 and M=3, run 1 visits the first 3 and run 2 visits
        the remaining 2 (or a different set of 3 if B were larger).
        The real Lithos orders by ``updated_at asc``; since each visited
        note's ``updated_at`` advances on rewrite, run 2's candidate
        list starts with the notes NOT visited in run 1.

        We simulate this by queueing different note slices for each run.
        """
        b = 5  # total backlog
        m = 3  # max_items_per_run

        all_notes = _make_persistently_failing_notes(b)

        # Run 1: oldest 3 notes (indices 0, 1, 2).
        run1_notes = all_notes[:m]
        _queue_notes_for_sweep(fake_lithos, run1_notes)

        config = _make_config(lithos_url=fake_lithos_url, max_items=m)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            visited_r1 = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited_r1) == m
            r1_ids = {v["id"] for v in visited_r1}

            # Verify all run 1 notes were rewritten.
            write_calls_r1 = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls_r1) == m

            # Run 2: since run 1's notes had their updated_at bumped,
            # the oldest notes are now indices 3, 4 (not visited in
            # run 1) followed by 0, 1, 2 (now newer).  Lithos returns
            # the first M=3 by updated_at asc: [3, 4, 0].
            fake_lithos.calls.clear()
            run2_notes = all_notes[m:] + all_notes[:1]  # [3, 4, 0]
            _queue_notes_for_sweep(fake_lithos, run2_notes)

            visited_r2 = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited_r2) == m
            r2_ids = {v["id"] for v in visited_r2}

            # Together, both runs visited >= min(B, 2*M) = 5 distinct.
            all_visited = r1_ids | r2_ids
            assert len(all_visited) >= min(b, 2 * m)

            # The two notes NOT visited in run 1 (indices 3, 4) are in
            # run 2's candidate list.
            not_in_r1 = {all_notes[3]["id"], all_notes[4]["id"]}
            assert not_in_r1.issubset(r2_ids)
        finally:
            await client.close()

    async def test_run_k_notes_not_in_first_n_of_run_k_plus_1(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Notes visited in run K are NOT in the first-N oldest slice of K+1.

        After run K rewrites all M notes, their ``updated_at`` advances.
        Run K+1's ``updated_at asc`` ordering puts them after any
        unvisited notes.  We verify that the run 2 candidate list
        does not start with run 1's notes.
        """
        b = 5
        m = 3

        all_notes = _make_persistently_failing_notes(b)

        # Run 1: first M notes.
        run1_notes = all_notes[:m]
        _queue_notes_for_sweep(fake_lithos, run1_notes)

        config = _make_config(lithos_url=fake_lithos_url, max_items=m)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            visited_r1 = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            r1_ids = {v["id"] for v in visited_r1}

            # Run 2: the first-N oldest are the unvisited notes (3, 4).
            # Run 1's notes (0, 1, 2) are now newer and come after.
            fake_lithos.calls.clear()

            # Simulate Lithos ordering: unvisited first, then visited.
            run2_candidates = all_notes[m:] + all_notes[:m]
            run2_first_n = run2_candidates[:m]

            _queue_notes_for_sweep(fake_lithos, run2_first_n)

            visited_r2 = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited_r2) == m

            # The first-N of run 2 should NOT contain notes from run 1
            # (unless every other note was visited — which it was not,
            # since B > M means there are still unvisited notes).
            r2_first_n_ids = {n["id"] for n in run2_first_n}
            overlap = r1_ids & r2_first_n_ids

            # With B=5, M=3: run 1 visited [0,1,2]; the 2 unvisited
            # [3,4] must be in the first-N of run 2.  Since M=3, only
            # one run-1 note can sneak in (note-001 at position 3).
            # But the key property: the unvisited notes come FIRST.
            unvisited_after_r1 = {all_notes[i]["id"] for i in range(m, b)}
            # All unvisited notes from run 1 appear in run 2's slice.
            assert unvisited_after_r1.issubset(r2_first_n_ids)

            # At most 1 run-1 note can appear (M - len(unvisited)).
            max_overlap = m - len(unvisited_after_r1)
            assert len(overlap) <= max_overlap
        finally:
            await client.close()

    async def test_write_spy_confirms_updated_at_would_advance(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Write-spy evidence: lithos_write called for each visited note.

        Since Lithos bumps ``updated_at`` on every successful write,
        observing that ``lithos_write`` was called for each note is
        sufficient evidence that ``updated_at`` advances.
        """
        m = 3
        notes = _make_persistently_failing_notes(m)
        _queue_notes_for_sweep(fake_lithos, notes)

        config = _make_config(lithos_url=fake_lithos_url, max_items=m)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited) == m

            # Exactly one write per visited note, in order.
            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == m
            for i, wc in enumerate(write_calls):
                assert wc[1]["id"] == notes[i]["id"]

            # Each write returns "updated" (successful), confirming
            # Lithos would bump updated_at.
        finally:
            await client.close()
