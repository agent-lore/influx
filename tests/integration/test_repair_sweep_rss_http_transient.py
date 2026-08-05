"""Integration tests for RSS archive HTTP-transient retry recovery (issue #138).

A recoverable status (``stage="http_5xx"``) classifies as transient per
:func:`influx.repair_counters.classify_failure`, which counts only
``{"parse", "validate", "oversize"}`` globally plus the permanent
archive-stage kinds (issue #282).  The sweep must therefore:

- not flip ``influx:archive-terminal``,
- not bump ``archive_attempts``,
- leave ``influx:archive-missing`` in place so the note re-enters the
  next sweep,
- recover cleanly when ``archive_download`` succeeds on the next pass.

This file mirrors the failure-keeps-tags shape of
``test_repair_sweep_archive_only.test_archive_download_failure_keeps_tags``
but adds an explicit recovery pass after the transient failure.
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
_ARCHIVE_PATH = f"rss-{_FEED_SLUG}/2026/05/{_FEED_SLUG}-{_URL_HASH}.html"


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


# ── Tests ──────────────────────────────────────────────────────────


class TestRssArchiveHttpTransientRetry:
    """5xx failures are transient — no terminal flip, recover next pass."""

    async def test_transient_keeps_tags_then_next_pass_recovers(
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

        def _archive_http_503(note: dict[str, object]) -> str:
            raise ExtractionError(
                "upstream 503",
                url=str(note.get("source_url", "")),
                stage="http_5xx",
            )

        def _archive_lands(note: dict[str, object]) -> str:
            return _ARCHIVE_PATH

        try:
            # ── Pass 1: transient HTTP 503 → tags unchanged. ──
            _queue_single_note(fake_lithos, note)
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(archive_download=_archive_http_503),
            )
            p1_payload = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")

            # No terminal flip and no counter bump.
            assert "influx:archive-terminal" not in p1_payload["tags"]
            assert "- archive_attempts:" not in p1_payload["content"]

            # archive-missing kept → note re-enters next sweep.
            assert "influx:archive-missing" in p1_payload["tags"]
            assert "influx:repair-needed" in p1_payload["tags"]

            # No archive path was written.
            assert f"path: {_ARCHIVE_PATH}" not in p1_payload["content"]

            # ── Pass 2: archive_download succeeds → recovery. ──
            fake_lithos.calls.clear()
            note_p2 = _next_pass_note(note, p1_payload)
            _queue_single_note(fake_lithos, note_p2)
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(archive_download=_archive_lands),
            )
            p2_payload = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")

            assert "influx:archive-missing" not in p2_payload["tags"]
            assert f"path: {_ARCHIVE_PATH}" in p2_payload["content"]
            assert "influx:archive-terminal" not in p2_payload["tags"]
        finally:
            await client.close()
