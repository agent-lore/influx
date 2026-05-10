"""Integration tests for the RSS archive-recovery repair flow (issue #138).

Mirrors :mod:`tests.integration.test_repair_sweep_archive_only` and the
archive→re-extract→tier2→tier3 walk in
:mod:`tests.integration.test_repair_sweep_abstract_only`, but with an
RSS-shaped note (``source:rss-<feed>`` + ``feed-slug:<slug>`` +
``articles/rss-<feed>/`` path) and a non-arxiv ``source_url``.

The flow exercised here:

- Pass 1: ``influx:archive-missing`` + ``text:abstract-only`` →
  ``archive_download`` succeeds → ``archive-missing`` cleared,
  ``## Archive`` populated with ``path:``.
- Pass 2: feed run-1 written note back → ``re_extract_archive`` returns
  ``UPGRADE`` → ``text:abstract-only`` replaced by ``text:html`` →
  ``tier2_enrich`` adds ``full-text`` → ``tier3_extract`` adds
  ``influx:deep-extracted`` → ``influx:repair-needed`` cleared.
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
    score: int = 9,
) -> str:
    """Build canonical RSS note content with optional archive path."""
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
    score: int = 9,
) -> dict[str, Any]:
    """Build an RSS note dict as returned by ``lithos_read``."""
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
    """Queue list + read + write responses for a single-note sweep."""
    fake_lithos.list_responses.append(
        json.dumps({"items": [{"id": note["id"], "title": note["title"]}]})
    )
    fake_lithos.read_responses.append(json.dumps(note))
    fake_lithos.write_responses.append('{"status": "updated"}')


def _next_pass_note(
    base_note: dict[str, Any],
    write_payload: dict[str, Any],
) -> dict[str, Any]:
    """Build the next sweep pass's read response from a prior write payload."""
    return {
        **base_note,
        "content": write_payload["content"],
        "tags": list(write_payload["tags"]),
    }


# ── Stub hooks ────────────────────────────────────────────────────


def _archive_download_ok(note: dict[str, object]) -> str:
    return _ARCHIVE_PATH


def _re_extract_upgrade_html(
    note: dict[str, object],
    archive_path: str,
) -> ReExtractionResult:
    return ReExtractionResult(
        outcome=ExtractionOutcome.UPGRADE,
        upgraded_text_tag="text:html",
    )


def _add_tag_in_place(note: dict[str, object], tag: str) -> None:
    raw = note.get("tags") or []
    tags = list(raw) if isinstance(raw, list) else []
    if tag not in tags:
        tags.append(tag)
    note["tags"] = tags


def _tier2_enrich_adds_full_text(note: dict[str, object]) -> None:
    _add_tag_in_place(note, "full-text")


def _tier3_extract_adds_deep_extracted(note: dict[str, object]) -> None:
    _add_tag_in_place(note, "influx:deep-extracted")


# ── Tests ──────────────────────────────────────────────────────────


class TestRssArchiveRecoveryFlow:
    """End-to-end RSS repair recovery across two sweep passes."""

    async def test_pass1_archive_lands_clears_archive_missing(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Pass 1: archive_download succeeds → archive-missing cleared,
        ``path:`` written, text:abstract-only preserved (text stage waits
        for archive to land first)."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "influx:archive-missing",
            f"source:rss-{_FEED_SLUG}",
            f"feed-slug:{_FEED_SLUG}",
            "text:abstract-only",
        ]
        note = _make_rss_note_dict(tags=tags)
        _queue_single_note(fake_lithos, note)

        config = _make_config(lithos_url=fake_lithos_url)
        hooks = SweepHooks(archive_download=_archive_download_ok)

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

            assert "influx:archive-missing" not in payload["tags"]
            assert f"path: {_ARCHIVE_PATH}" in payload["content"]

            assert "text:abstract-only" in payload["tags"]
            assert f"source:rss-{_FEED_SLUG}" in payload["tags"]
            assert f"feed-slug:{_FEED_SLUG}" in payload["tags"]
        finally:
            await client.close()

    async def test_full_recovery_two_passes_converges_to_stable_state(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Pass 1 lands archive; pass 2 upgrades text + adds full-text +
        deep-extracted → influx:repair-needed cleared."""
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "influx:archive-missing",
            f"source:rss-{_FEED_SLUG}",
            f"feed-slug:{_FEED_SLUG}",
            "text:abstract-only",
        ]
        note = _make_rss_note_dict(tags=tags, score=9)

        # ── Pass 1: archive lands. ──
        _queue_single_note(fake_lithos, note)
        config = _make_config(lithos_url=fake_lithos_url)
        hooks_p1 = SweepHooks(archive_download=_archive_download_ok)

        client = LithosClient(url=fake_lithos_url)
        try:
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks_p1,
            )
            write_calls_p1 = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls_p1) == 1
            p1_payload = write_calls_p1[0][1]
            assert "influx:archive-missing" not in p1_payload["tags"]
            assert "influx:repair-needed" in p1_payload["tags"]

            # ── Pass 2: re-extract upgrades text, tier2/tier3 fill in. ──
            fake_lithos.calls.clear()
            note_p2 = _next_pass_note(note, p1_payload)
            _queue_single_note(fake_lithos, note_p2)

            hooks_p2 = SweepHooks(
                re_extract_archive=_re_extract_upgrade_html,
                tier2_enrich=_tier2_enrich_adds_full_text,
                tier3_extract=_tier3_extract_adds_deep_extracted,
            )
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=hooks_p2,
            )

            write_calls_p2 = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
            assert len(write_calls_p2) == 1
            p2_payload = write_calls_p2[0][1]

            assert "text:abstract-only" not in p2_payload["tags"]
            assert "text:html" in p2_payload["tags"]
            assert "full-text" in p2_payload["tags"]
            assert "influx:deep-extracted" in p2_payload["tags"]

            assert "influx:repair-needed" not in p2_payload["tags"]

            assert f"source:rss-{_FEED_SLUG}" in p2_payload["tags"]
            assert f"feed-slug:{_FEED_SLUG}" in p2_payload["tags"]
        finally:
            await client.close()
