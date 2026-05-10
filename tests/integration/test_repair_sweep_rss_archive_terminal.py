"""Integration tests for the RSS archive-terminal counted-cap flip (issue #138).

Walks an RSS note through three sequential sweep passes, each one
hitting an ``ExtractionError(stage="oversize")`` from the
``archive_download`` hook (a counted-class stage per
:data:`influx.repair_counters._COUNTED_STAGES`).  After
``REPAIR_COUNTED_CAP`` (=3) counted failures the sweep flips
``influx:archive-terminal``.  A fourth pass confirms the terminal
gate from :func:`influx.repair.select_stages` skips
``archive_retry`` even though ``influx:archive-missing`` is still
present.

There is no equivalent for this flow in the existing arxiv integration
suite — the chronic-oversize file handles ``content_too_large`` from
``lithos_write``, a different code path.
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
from influx.errors import ExtractionError
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


# ── RSS-shaped helpers ────────────────────────────────────────────

_FEED_SLUG = "techcrunch"
_URL_HASH = "abc123def"
_NOTE_ID = f"rss-{_FEED_SLUG}-{_URL_HASH}"
_NOTE_PATH = f"articles/rss-{_FEED_SLUG}/2026/05"
_SOURCE_URL = "https://example.com/article-42"


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


def _make_rss_note_content(
    *,
    archive_path: str | None = None,
    score: int = 5,
) -> str:
    archive_body = f"path: {archive_path}\n" if archive_path is not None else ""
    return (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        f"source_url: {_SOURCE_URL}\n"
        "tags:\n"
        "  - profile:ai-robotics\n"
        f"  - source:rss-{_FEED_SLUG}\n"
        f"  - feed-slug:{_FEED_SLUG}\n"
        "confidence: 0.7\n"
        "---\n"
        "# RSS Article Title\n"
        "\n"
        "## Archive\n"
        f"{archive_body}"
        "\n"
        "## Summary\n"
        "An RSS article summary.\n"
        "\n"
        "## Profile Relevance\n"
        "### ai-robotics\n"
        f"Score: {score}/10\n"
        "Relevant.\n"
        "\n"
        "## User Notes\n"
    )


def _make_rss_note_dict(
    *,
    tags: list[str],
    archive_path: str | None = None,
    score: int = 5,
) -> dict[str, Any]:
    return {
        "id": _NOTE_ID,
        "title": "RSS Article Title",
        "content": _make_rss_note_content(
            archive_path=archive_path,
            score=score,
        ),
        "tags": tags,
        "version": 1,
        "source_url": _SOURCE_URL,
        "path": _NOTE_PATH,
        "confidence": 0.7,
        "note_type": "summary",
        "namespace": "influx",
    }


def _queue_single_note(
    fake_lithos: FakeLithosServer,
    note: dict[str, Any],
) -> None:
    fake_lithos.list_responses.append(
        json.dumps({"items": [{"id": note["id"], "title": note["title"]}]})
    )
    fake_lithos.read_responses.append(json.dumps(note))
    fake_lithos.write_responses.append('{"status": "updated"}')


def _next_pass_note(
    base_note: dict[str, Any],
    write_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        **base_note,
        "content": write_payload["content"],
        "tags": list(write_payload["tags"]),
    }


def _archive_download_oversize(note: dict[str, object]) -> str:
    raise ExtractionError(
        "archive too large",
        url=str(note.get("source_url", "")),
        stage="oversize",
    )


def _archive_attempt_count(content: str) -> int:
    """Pull the ``- archive_attempts: N`` value from ``## Repair`` body."""
    marker = "- archive_attempts:"
    idx = content.find(marker)
    if idx == -1:
        return 0
    line_end = content.find("\n", idx)
    line = content[idx:line_end] if line_end != -1 else content[idx:]
    return int(line.split(":", 1)[1].strip())


# ── Tests ──────────────────────────────────────────────────────────


class TestRssArchiveTerminalCountedCap:
    """Three counted ``oversize`` failures flip ``influx:archive-terminal``."""

    async def test_three_passes_flip_terminal_then_skip_archive_retry(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "influx:archive-missing",
            f"source:rss-{_FEED_SLUG}",
            f"feed-slug:{_FEED_SLUG}",
            "text:html",
        ]
        note = _make_rss_note_dict(tags=tags)

        config = _make_config(lithos_url=fake_lithos_url)
        client = LithosClient(url=fake_lithos_url)

        try:
            # ── Pass 1: counted failure #1 → attempts=1, no terminal. ──
            _queue_single_note(fake_lithos, note)
            hooks = SweepHooks(archive_download=_archive_download_oversize)
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            p1_payload = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")
            assert _archive_attempt_count(p1_payload["content"]) == 1
            assert "influx:archive-terminal" not in p1_payload["tags"]
            assert "influx:archive-missing" in p1_payload["tags"]

            # ── Pass 2: counted failure #2 → attempts=2, no terminal. ──
            fake_lithos.calls.clear()
            note_p2 = _next_pass_note(note, p1_payload)
            _queue_single_note(fake_lithos, note_p2)
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            p2_payload = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")
            assert _archive_attempt_count(p2_payload["content"]) == 2
            assert "influx:archive-terminal" not in p2_payload["tags"]

            # ── Pass 3: counted failure #3 → attempts=3, terminal flipped. ──
            fake_lithos.calls.clear()
            note_p3 = _next_pass_note(note, p2_payload)
            _queue_single_note(fake_lithos, note_p3)
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks,
            )
            p3_payload = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")
            assert _archive_attempt_count(p3_payload["content"]) == 3
            assert "influx:archive-terminal" in p3_payload["tags"]
            # archive-missing stays — terminal records "we gave up", not "fixed".
            assert "influx:archive-missing" in p3_payload["tags"]

            # ── Pass 4: terminal-tagged note → archive_retry skipped. ──
            fake_lithos.calls.clear()
            archive_calls: list[bool] = []

            def _track_archive(note: dict[str, object]) -> str:
                archive_calls.append(True)
                raise ExtractionError(
                    "should not be called",
                    url=str(note.get("source_url", "")),
                    stage="oversize",
                )

            note_p4 = _next_pass_note(note, p3_payload)
            _queue_single_note(fake_lithos, note_p4)
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(archive_download=_track_archive),
            )

            # archive_retry is gated by ``not is_archive_terminal`` in
            # repair.py:381-385, so the hook must NOT run on pass 4.
            assert archive_calls == []
        finally:
            await client.close()
