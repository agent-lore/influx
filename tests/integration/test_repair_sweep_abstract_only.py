"""Integration tests for abstract-only re-extraction three outcomes (AC-X-7).

Seeds a note tagged ``text:abstract-only`` + ``influx:repair-needed`` with a
non-empty ``path:`` line in ``## Archive``, runs the sweep with a fake
``re_extract_archive`` hook configured for each outcome, and verifies the
resulting tag set end-to-end.

Covers: AC-X-7 (a) Upgrade, (b) Terminal, (c) Transient failure.
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
from influx.repair import (
    ExtractionOutcome,
    ReExtractionResult,
    SweepHooks,
    sweep,
)
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


# ── Fake re_extract_archive hooks ─────────────────────────────────


def _upgrade_hook(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    """Fake hook: always returns Upgrade with text:html."""
    return ReExtractionResult(
        outcome=ExtractionOutcome.UPGRADE,
        upgraded_text_tag="text:html",
    )


def _terminal_hook(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    """Fake hook: always returns Terminal."""
    return ReExtractionResult(outcome=ExtractionOutcome.TERMINAL)


def _transient_hook(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    """Fake hook: always returns Transient."""
    return ReExtractionResult(outcome=ExtractionOutcome.TRANSIENT)


def _transient_error_hook(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    """Fake hook: raises ExtractionError (treated as Transient)."""
    from influx.errors import ExtractionError

    raise ExtractionError("extraction failed transiently")


# ── Tests ──────────────────────────────────────────────────────────


class TestAbstractOnlyReExtractionUpgrade:
    """AC-X-7 (a): Upgrade outcome."""

    async def test_upgrade_replaces_tag_and_clears_repair(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Upgrade replaces text:abstract-only with text:html and clears."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
        )
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(re_extract_archive=_upgrade_hook)

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

            # text:abstract-only replaced by text:html.
            assert "text:abstract-only" not in payload["tags"]
            assert "text:html" in payload["tags"]

            # influx:text-terminal NOT added.
            assert "influx:text-terminal" not in payload["tags"]

            # influx:repair-needed cleared (no other outstanding stage).
            assert "influx:repair-needed" not in payload["tags"]

            # Other tags preserved.
            assert "profile:ai-robotics" in payload["tags"]
        finally:
            await client.close()


class TestAbstractOnlyReExtractionTerminal:
    """AC-X-7 (b): Terminal outcome."""

    async def test_terminal_keeps_abstract_only_adds_text_terminal(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Terminal keeps text:abstract-only and adds influx:text-terminal."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
        )
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(re_extract_archive=_terminal_hook)

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

            # text:abstract-only kept.
            assert "text:abstract-only" in payload["tags"]

            # influx:text-terminal ADDED.
            assert "influx:text-terminal" in payload["tags"]
        finally:
            await client.close()

    async def test_terminal_low_score_clears_repair_needed(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Terminal with low score clears repair-needed (Tier 2/3 not required)."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
            score=5,
        )
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(re_extract_archive=_terminal_hook)

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

            # Terminal + low score → text:abstract-only + text-terminal is
            # enough to clear repair-needed (no Tier 2/3 needed at low score).
            assert "influx:text-terminal" in payload["tags"]
            assert "influx:repair-needed" not in payload["tags"]
        finally:
            await client.close()


class TestAbstractOnlyReExtractionTransient:
    """AC-X-7 (c): Transient failure outcome."""

    async def test_transient_keeps_tags_and_repair_needed(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Transient keeps text:abstract-only + repair-needed, no terminal."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
        )
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(re_extract_archive=_transient_hook)

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

            # text:abstract-only kept.
            assert "text:abstract-only" in payload["tags"]

            # influx:repair-needed kept.
            assert "influx:repair-needed" in payload["tags"]

            # influx:text-terminal NOT added.
            assert "influx:text-terminal" not in payload["tags"]
        finally:
            await client.close()

    async def test_extraction_error_treated_as_transient(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """ExtractionError is treated as Transient (same tag result)."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
        )
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(re_extract_archive=_transient_error_hook)

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

            # Same assertions as Transient outcome.
            assert "text:abstract-only" in payload["tags"]
            assert "influx:repair-needed" in payload["tags"]
            assert "influx:text-terminal" not in payload["tags"]
        finally:
            await client.close()

    async def test_transient_note_reenters_sweep_on_next_run(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """After transient failure, note still matches sweep filter."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
        ]
        note = _make_note_dict(
            tags=tags,
            archive_path=_ARCHIVE_PATH,
        )
        # Run 1: transient failure.
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(re_extract_archive=_transient_hook)

        client = LithosClient(url=fake_lithos_url)
        try:
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )

            # Verify run 1 kept repair-needed.
            write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls) == 1
            run1_tags = write_calls[0][1]["tags"]
            assert "influx:repair-needed" in run1_tags

            # Run 2: same note re-enters sweep, now upgrade succeeds.
            fake_lithos.calls.clear()
            note_run2 = _make_note_dict(
                tags=run1_tags,
                archive_path=_ARCHIVE_PATH,
            )
            _queue_single_note(fake_lithos, note_run2)

            upgrade_hooks = SweepHooks(re_extract_archive=_upgrade_hook)
            visited = await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=upgrade_hooks,
            )
            assert len(visited) == 1

            write_calls_r2 = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls_r2) == 1
            r2_tags = write_calls_r2[0][1]["tags"]

            # Now upgraded to text:html and cleared.
            assert "text:html" in r2_tags
            assert "text:abstract-only" not in r2_tags
            assert "influx:repair-needed" not in r2_tags
            assert "influx:text-terminal" not in r2_tags
        finally:
            await client.close()
