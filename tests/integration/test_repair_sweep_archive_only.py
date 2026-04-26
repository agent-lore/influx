"""Integration tests for archive-only repair flow (AC-X-4).

Seeds a note tagged ``influx:repair-needed`` + ``influx:archive-missing``
with empty ``## Archive``, runs the sweep with a fake archive download
hook, and asserts the exact ``## Archive`` body in both initial and
post-repair states.

Covers: AC-X-4 (full), AC-06-A (archive independently of text:* tag).
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
    score: int = 5,
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
        "Somewhat relevant.\n"
        "\n"
        "## User Notes\n"
    )


def _make_note_dict(
    *,
    note_id: str = "note-001",
    tags: list[str],
    archive_path: str | None = None,
    score: int = 5,
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


def _fake_archive_download(note: dict[str, object]) -> str:
    """Fake archive download hook that always succeeds."""
    return _ARCHIVE_PATH


# ── Tests ──────────────────────────────────────────────────────────


class TestArchiveOnlyRepairFlow:
    """AC-X-4: archive-only repair flow via the sweep."""

    async def test_initial_archive_body_is_empty(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Seed note has exact empty ## Archive body (AC-X-4 initial)."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "influx:archive-missing",
            "ingested-by:influx",
            "source:arxiv",
            "text:html",
        ]
        note = _make_note_dict(tags=tags)

        # Assert the exact initial ## Archive body is empty.
        content = note["content"]
        archive_start = content.index("## Archive\n")
        after_heading = archive_start + len("## Archive\n")
        # Next section starts with "## Summary"
        next_section = content.index("## Summary", after_heading)
        archive_body = content[after_heading:next_section].strip()
        assert archive_body == "", (
            f"Initial ## Archive body should be empty, got: {archive_body!r}"
        )

    async def test_archive_download_writes_path_and_clears_tags(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Sweep retries archive, writes path:, clears both tags."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "influx:archive-missing",
            "ingested-by:influx",
            "source:arxiv",
            "text:html",
        ]
        note = _make_note_dict(tags=tags)
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(archive_download=_fake_archive_download)

        client = LithosClient(url=fake_lithos_url)
        try:
            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited) == 1

            # Verify the write call.
            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == 1
            payload = write_calls[0][1]

            # AC-X-4: exact post-repair ## Archive body.
            written_content = payload["content"]
            assert f"path: {_ARCHIVE_PATH}" in written_content

            # Verify exact ## Archive section body.
            arch_start = written_content.index("## Archive\n")
            after_h = arch_start + len("## Archive\n")
            next_sec = written_content.index("## Summary", after_h)
            archive_body = written_content[after_h:next_sec].strip()
            assert archive_body == f"path: {_ARCHIVE_PATH}"

            # Tags: both cleared.
            assert "influx:archive-missing" not in payload["tags"]
            assert "influx:repair-needed" not in payload["tags"]

            # Other tags preserved.
            assert "profile:ai-robotics" in payload["tags"]
            assert "text:html" in payload["tags"]
        finally:
            await client.close()

    async def test_archive_repair_with_text_html_does_not_select_text_stage(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """AC-06-A: text:html + archive-missing → archive only, not text."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "influx:archive-missing",
            "ingested-by:influx",
            "source:arxiv",
            "text:html",
        ]
        note = _make_note_dict(tags=tags)
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)

        # Track whether archive hook was called.
        archive_called = []

        def tracking_archive_hook(note: dict[str, object]) -> str:
            archive_called.append(True)
            return _ARCHIVE_PATH

        hooks = SweepHooks(archive_download=tracking_archive_hook)

        client = LithosClient(url=fake_lithos_url)
        try:
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )

            # Archive hook was called (archive stage selected).
            assert len(archive_called) == 1

            # Verify written tags — text:html preserved (text-extraction
            # stage was NOT selected because text:html already present).
            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == 1
            assert "text:html" in write_calls[0][1]["tags"]
        finally:
            await client.close()

    async def test_archive_download_failure_keeps_tags(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Archive download failure → tags stay, note still rewritten."""
        from influx.errors import ExtractionError

        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "influx:archive-missing",
            "ingested-by:influx",
            "source:arxiv",
            "text:html",
        ]
        note = _make_note_dict(tags=tags)
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)

        def failing_archive_hook(note: dict[str, object]) -> str:
            raise ExtractionError("download failed")

        hooks = SweepHooks(archive_download=failing_archive_hook)

        client = LithosClient(url=fake_lithos_url)
        try:
            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            assert len(visited) == 1

            # Note is still rewritten (retry-order advancement).
            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == 1
            payload = write_calls[0][1]

            # Tags NOT cleared — archive download failed.
            assert "influx:archive-missing" in payload["tags"]
            assert "influx:repair-needed" in payload["tags"]

            # Archive body still empty.
            assert f"path: {_ARCHIVE_PATH}" not in payload["content"]
        finally:
            await client.close()

    async def test_repair_needed_kept_when_other_stages_outstanding(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """repair-needed stays when archive succeeds but other stages remain.

        High-score note (score=9 >= full_text threshold=8) missing
        ``full-text`` → Tier 2 still outstanding → repair-needed kept.
        """
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "influx:archive-missing",
            "ingested-by:influx",
            "source:arxiv",
            "text:html",
        ]
        # Score=9 makes Tier 2 (threshold=8) and Tier 3 (threshold=9)
        # required; without full-text and influx:deep-extracted, clearing
        # will NOT remove influx:repair-needed.
        note = _make_note_dict(tags=tags, score=9)
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(archive_download=_fake_archive_download)

        client = LithosClient(url=fake_lithos_url)
        try:
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )

            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == 1
            payload = write_calls[0][1]

            # archive-missing cleared (archive succeeded).
            assert "influx:archive-missing" not in payload["tags"]

            # repair-needed stays — Tier 2/3 outstanding.
            assert "influx:repair-needed" in payload["tags"]
        finally:
            await client.close()
