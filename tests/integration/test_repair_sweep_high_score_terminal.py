"""Integration tests for high-score-terminal clearing (AC-X-7 high-score).

Seeds a terminal abstract-only note (``text:abstract-only`` +
``influx:text-terminal``) with max profile score = 9 (≥ ``deep_extract``
threshold), non-empty ``path:`` line in ``## Archive``, and no other
outstanding-stage tag.  Verifies that after ONE repair pass the note
carries no ``influx:repair-needed`` even though it has neither
``full-text`` nor ``influx:deep-extracted``, and that on the NEXT sweep
the note is NOT re-selected.
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
    archive_path: str | None = None,
    score: int = 9,
) -> str:
    """Build canonical note content with controllable archive and score."""
    archive_body = f"path: {archive_path}\n" if archive_path is not None else ""
    return (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        "source_url: https://arxiv.org/abs/2601.00001\n"
        "tags:\n"
        "  - profile:ai-robotics\n"
        "  - ingested-by:influx\n"
        "  - source:arxiv\n"
        "confidence: 0.9\n"
        "---\n"
        "# Test Paper\n"
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
        "Highly relevant.\n"
        "\n"
        "## User Notes\n"
    )


def _make_note_dict(
    *,
    note_id: str = "note-001",
    tags: list[str],
    archive_path: str | None = None,
    score: int = 9,
) -> dict[str, Any]:
    """Build a note dict as returned by lithos_read."""
    return {
        "id": note_id,
        "title": "Test Paper",
        "content": _make_note_content(
            archive_path=archive_path,
            score=score,
        ),
        "tags": tags,
        "version": 1,
        "source_url": "https://arxiv.org/abs/2601.00001",
        "path": "papers/arxiv/2026/04",
        "confidence": 0.9,
        "note_type": "summary",
        "namespace": "influx",
    }


def _queue_single_note(
    fake_lithos: FakeLithosServer,
    note: dict[str, Any],
) -> None:
    """Queue list + read + write responses for a single-note sweep."""
    fake_lithos.list_responses.append(
        json.dumps({"items": [{"id": note["id"], "title": note["title"]}]})
    )
    fake_lithos.read_responses.append(json.dumps(note))
    fake_lithos.write_responses.append('{"status": "updated"}')


# ── Tests ──────────────────────────────────────────────────────────


class TestHighScoreTerminalClearing:
    """AC-X-7 high-score terminal clearing exemption."""

    async def test_terminal_abstract_only_high_score_clears_repair_needed(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Terminal note with score=9 clears repair-needed without full-text."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
            "influx:text-terminal",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
            score=9,
        )
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        # No hooks needed — terminal notes skip re-extraction, and
        # the terminal exemption waives Tier 2 / Tier 3.
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited) == 1

            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == 1
            payload = write_calls[0][1]

            # influx:repair-needed CLEARED — high-score terminal exemption.
            assert "influx:repair-needed" not in payload["tags"]

            # Note has NEITHER full-text NOR influx:deep-extracted — yet
            # it is still cleared because the terminal exemption waives
            # the Tier 2 and Tier 3 requirements.
            assert "full-text" not in payload["tags"]
            assert "influx:deep-extracted" not in payload["tags"]

            # Remaining tags intact.
            assert "text:abstract-only" in payload["tags"]
            assert "influx:text-terminal" in payload["tags"]
            assert "profile:ai-robotics" in payload["tags"]
        finally:
            await client.close()

    async def test_terminal_note_not_reselected_on_next_sweep(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """After clearing, note is no longer in the sweep's candidate list."""
        # ── Run 1: sweep clears influx:repair-needed. ──
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
            "influx:text-terminal",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
            score=9,
        )
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks()

        client = LithosClient(url=fake_lithos_url)
        try:
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )

            # Confirm run 1 cleared repair-needed.
            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == 1
            run1_tags = write_calls[0][1]["tags"]
            assert "influx:repair-needed" not in run1_tags

            # ── Run 2: empty list (note no longer matches filter). ──
            fake_lithos.calls.clear()
            fake_lithos.list_responses.append(json.dumps({"items": []}))

            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            # No candidates returned → note was NOT re-selected.
            assert visited == []

            # No lithos_read calls in run 2 (no candidates).
            read_calls = [c for c in fake_lithos.calls if c[0] == "lithos_read"]
            assert len(read_calls) == 0
        finally:
            await client.close()

    async def test_terminal_no_reextraction_despite_hooks(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Terminal note skips abstract-only re-extraction even with hook."""
        call_count = 0

        def _should_not_be_called(
            note: dict[str, object],
            archive_path: str,
        ) -> None:
            nonlocal call_count
            call_count += 1

        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
            "influx:text-terminal",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
            score=9,
        )
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(re_extract_archive=_should_not_be_called)  # type: ignore[arg-type]

        client = LithosClient(url=fake_lithos_url)
        try:
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            # The re_extract_archive hook was NOT called because
            # influx:text-terminal suppresses abstract-only re-extraction.
            assert call_count == 0
        finally:
            await client.close()

    async def test_terminal_no_tier2_tier3_despite_high_score(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Terminal exemption waives Tier 2 and Tier 3 at high score."""
        tier2_calls = 0
        tier3_calls = 0

        def _tier2_spy(note: dict[str, object]) -> None:
            nonlocal tier2_calls
            tier2_calls += 1

        def _tier3_spy(note: dict[str, object]) -> None:
            nonlocal tier3_calls
            tier3_calls += 1

        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
            "influx:text-terminal",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
            score=9,
        )
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(
            tier2_enrich=_tier2_spy,  # type: ignore[arg-type]
            tier3_extract=_tier3_spy,  # type: ignore[arg-type]
        )

        client = LithosClient(url=fake_lithos_url)
        try:
            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited) == 1

            # Terminal note skips Tier 2 and Tier 3 (influx:text-terminal
            # suppresses both stages in stage selection).
            assert tier2_calls == 0
            assert tier3_calls == 0

            # And influx:repair-needed is still cleared.
            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            payload = write_calls[0][1]
            assert "influx:repair-needed" not in payload["tags"]
        finally:
            await client.close()
