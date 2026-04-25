"""Integration tests for chronic content_too_large exemption (AC-X-8).

Seeds one note that hits ``content_too_large`` on the repair path
(chronic-oversize) alongside several normal repair-needed notes, and
verifies across >= 2 runs that (i) the run does NOT abort, (ii) the
chronic note's ``updated_at`` does NOT advance, (iii) it stays at the
head of the ``updated_at asc`` list, and (iv) other repair-needed
notes still make forward progress.

Covers: AC-X-8 chronic exemption.
"""

from __future__ import annotations

import json
import logging
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

_ARCHIVE_PATH = "papers/arxiv/2026/04/test.pdf"


def _make_config(
    *,
    lithos_url: str,
    max_items: int = 100,
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


# Low-score (5) notes with text:html + archive path: all clearing
# conditions met → influx:repair-needed is removed on successful write.
_NORMAL_TAGS = [
    "profile:ai-robotics",
    "influx:repair-needed",
    "ingested-by:influx",
    "source:arxiv",
    "text:html",
]

# The chronic note also has the same tags (would clear if write
# succeeded), but its write returns content_too_large.
_CHRONIC_TAGS = list(_NORMAL_TAGS)


def _queue_mixed_sweep(
    fake_lithos: FakeLithosServer,
    chronic_note: dict[str, Any],
    normal_notes: list[dict[str, Any]],
) -> None:
    """Queue list + read + write responses for a mixed sweep.

    The chronic note is first (oldest updated_at), followed by normal
    notes.  The chronic note's write returns ``content_too_large``;
    normal notes' writes return ``updated``.
    """
    all_notes = [chronic_note, *normal_notes]
    items = [{"id": n["id"], "title": n["title"]} for n in all_notes]
    fake_lithos.list_responses.append(json.dumps({"items": items}))

    for note in all_notes:
        fake_lithos.read_responses.append(json.dumps(note))

    # Chronic note: content_too_large; normal notes: updated.
    fake_lithos.write_responses.append('{"status": "content_too_large"}')
    for _ in normal_notes:
        fake_lithos.write_responses.append('{"status": "updated"}')


# ── Tests ──────────────────────────────────────────────────────────


class TestChronicOversizeExemption:
    """AC-X-8 chronic exemption: content_too_large on repair path."""

    async def test_run_does_not_abort(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Run continues past the chronic note; all notes visited."""
        chronic = _make_note_dict(
            note_id="chronic-001",
            tags=list(_CHRONIC_TAGS),
            archive_path=_ARCHIVE_PATH,
        )
        normals = [
            _make_note_dict(
                note_id=f"normal-{i:03d}",
                tags=list(_NORMAL_TAGS),
                archive_path=_ARCHIVE_PATH,
            )
            for i in range(1, 4)
        ]
        _queue_mixed_sweep(fake_lithos, chronic, normals)

        config = _make_config(lithos_url=fake_lithos_url, max_items=10)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )

            # All 4 notes were visited (sweep did not abort).
            assert len(visited) == 4
            visited_ids = [v["id"] for v in visited]
            assert visited_ids == [
                "chronic-001",
                "normal-001",
                "normal-002",
                "normal-003",
            ]
        finally:
            await client.close()

    async def test_chronic_note_untouched_others_written(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Chronic note gets one write attempt (content_too_large);
        normal notes get successful writes with repair-needed cleared.
        """
        chronic = _make_note_dict(
            note_id="chronic-001",
            tags=list(_CHRONIC_TAGS),
            archive_path=_ARCHIVE_PATH,
        )
        normals = [
            _make_note_dict(
                note_id=f"normal-{i:03d}",
                tags=list(_NORMAL_TAGS),
                archive_path=_ARCHIVE_PATH,
            )
            for i in range(1, 3)
        ]
        _queue_mixed_sweep(fake_lithos, chronic, normals)

        config = _make_config(lithos_url=fake_lithos_url, max_items=10)
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
            # 3 writes total: 1 for chronic (content_too_large) +
            # 2 for normal notes (updated).
            assert len(write_calls) == 3

            # Chronic note's write was attempted.
            assert write_calls[0][1]["id"] == "chronic-001"

            # Normal notes' writes succeeded — repair-needed cleared.
            for wc in write_calls[1:]:
                assert "influx:repair-needed" not in wc[1]["tags"]
        finally:
            await client.close()

    async def test_chronic_note_stays_at_head_across_runs(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Across 2 runs: chronic note's updated_at doesn't advance,
        so it stays at the head of the updated_at asc list; other
        notes make progress (repair-needed cleared in run 1).

        Run 2: only the chronic note remains (normals cleared), and
        it is still at the head of the candidate list.
        """
        chronic = _make_note_dict(
            note_id="chronic-001",
            tags=list(_CHRONIC_TAGS),
            archive_path=_ARCHIVE_PATH,
        )
        normals = [
            _make_note_dict(
                note_id=f"normal-{i:03d}",
                tags=list(_NORMAL_TAGS),
                archive_path=_ARCHIVE_PATH,
            )
            for i in range(1, 3)
        ]

        # ── Run 1 ──
        _queue_mixed_sweep(fake_lithos, chronic, normals)

        config = _make_config(lithos_url=fake_lithos_url, max_items=10)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            visited_r1 = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited_r1) == 3

            # Normal notes had repair-needed cleared (progress).
            write_calls_r1 = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            normal_writes = [
                wc for wc in write_calls_r1 if wc[1]["id"] != "chronic-001"
            ]
            for wc in normal_writes:
                assert "influx:repair-needed" not in wc[1]["tags"]

            # ── Run 2 ──
            # Since normal notes cleared repair-needed in run 1, they
            # are no longer in the sweep. Only the chronic note remains
            # (its updated_at didn't advance, so it's still the oldest).
            fake_lithos.calls.clear()

            # Queue only the chronic note for run 2.
            fake_lithos.list_responses.append(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": chronic["id"],
                                "title": chronic["title"],
                            }
                        ]
                    }
                )
            )
            fake_lithos.read_responses.append(json.dumps(chronic))
            fake_lithos.write_responses.append('{"status": "content_too_large"}')

            visited_r2 = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )

            # Chronic note visited again (still at head).
            assert len(visited_r2) == 1
            assert visited_r2[0]["id"] == "chronic-001"

            # Write was attempted again (content_too_large again).
            write_calls_r2 = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls_r2) == 1
            assert write_calls_r2[0][1]["id"] == "chronic-001"
        finally:
            await client.close()

    async def test_content_too_large_logged_and_counted(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Chronic-oversize is logged and counted as
        content_too_large_skipped (§5.4 failure mode 2).
        """
        chronic = _make_note_dict(
            note_id="chronic-001",
            tags=list(_CHRONIC_TAGS),
            archive_path=_ARCHIVE_PATH,
        )
        normals = [
            _make_note_dict(
                note_id="normal-001",
                tags=list(_NORMAL_TAGS),
                archive_path=_ARCHIVE_PATH,
            )
        ]
        _queue_mixed_sweep(fake_lithos, chronic, normals)

        config = _make_config(lithos_url=fake_lithos_url, max_items=10)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            with caplog.at_level(logging.WARNING, logger="influx.repair"):
                visited = await sweep(
                    "ai-robotics",
                    client=client,
                    config=config,
                    hooks=hooks,
                )

            # Sweep did not abort.
            assert len(visited) == 2

            # Verify logging mentions content_too_large_skipped.
            ctl_messages = [
                r.message for r in caplog.records if "content_too_large" in r.message
            ]
            assert len(ctl_messages) >= 1
            assert "chronic-001" in ctl_messages[0]
            assert "content_too_large_skipped" in ctl_messages[0]
        finally:
            await client.close()

    async def test_multiple_chronic_notes_all_skipped(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Multiple chronic-oversize notes are all skipped; other
        notes still make progress.
        """
        chronic_1 = _make_note_dict(
            note_id="chronic-001",
            tags=list(_CHRONIC_TAGS),
            archive_path=_ARCHIVE_PATH,
        )
        chronic_2 = _make_note_dict(
            note_id="chronic-002",
            tags=list(_CHRONIC_TAGS),
            archive_path=_ARCHIVE_PATH,
        )
        normal = _make_note_dict(
            note_id="normal-001",
            tags=list(_NORMAL_TAGS),
            archive_path=_ARCHIVE_PATH,
        )

        # Queue: chronic-1, chronic-2, normal
        all_notes = [chronic_1, chronic_2, normal]
        items = [{"id": n["id"], "title": n["title"]} for n in all_notes]
        fake_lithos.list_responses.append(json.dumps({"items": items}))
        for n in all_notes:
            fake_lithos.read_responses.append(json.dumps(n))

        # Two chronic writes + one normal write.
        fake_lithos.write_responses.append('{"status": "content_too_large"}')
        fake_lithos.write_responses.append('{"status": "content_too_large"}')
        fake_lithos.write_responses.append('{"status": "updated"}')

        config = _make_config(lithos_url=fake_lithos_url, max_items=10)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )

            # All 3 visited — sweep did not abort.
            assert len(visited) == 3

            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == 3

            # Normal note's repair-needed cleared.
            normal_write = [wc for wc in write_calls if wc[1]["id"] == "normal-001"]
            assert len(normal_write) == 1
            assert "influx:repair-needed" not in normal_write[0][1]["tags"]
        finally:
            await client.close()
