"""Integration test for paywalled-archive convergence (issue #282).

Walks the production shape end-to-end: an RSS note whose ``source_url``
sits behind a paywall, so ``archive_download`` returns HTTP 403 on every
pass, and whose text extraction already ran and settled on
``text:abstract-only``.

That combination outlived both prior write-amplification fixes.  #278
gave each stage a terminal waiver but nothing ever flipped the archive
one, because #282's classifier flattened 403 to a bare ``"http"`` and
called it transient.  #281 made ``unsupported_source`` counted, which
this note is not — it has a perfectly good reacquirer, the download is
genuinely attempted, and it genuinely fails.

Two things have to hold for it to converge, and the second is easy to
miss: the 403 must advance the archive cap, *and* the unreachable
archive must waive the text condition in
:func:`influx.repair.compute_clearing`.  Without the waiver the note
gains ``influx:archive-terminal`` and keeps ``influx:repair-needed``
forever — visibly "terminal" while still being rewritten twice a day.

Sibling of ``test_repair_sweep_rss_archive_terminal`` (the same cap
walk driven by ``oversize``, with ``text:html`` so clearing is never in
question) and of ``test_repair_sweep_rss_http_transient`` (which pins
the other half of the partition: 5xx must never converge).
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

_FEED_SLUG = "hacker-news-vibe-coding"
_URL_HASH = "cec8df9e"
_NOTE_ID = f"rss-{_FEED_SLUG}-{_URL_HASH}"
_NOTE_PATH = f"articles/rss-{_FEED_SLUG}/2026/07"
_SOURCE_URL = "https://www.ft.com/content/cec8df9e"


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
                name="ai-coding",
                description="AI Coding",
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


def _make_rss_note_content(score: int = 5) -> str:
    return (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        f"source_url: {_SOURCE_URL}\n"
        "tags:\n"
        "  - profile:ai-coding\n"
        f"  - source:rss-{_FEED_SLUG}\n"
        f"  - feed-slug:{_FEED_SLUG}\n"
        "confidence: 0.7\n"
        "---\n"
        "# Who cleans up after the vibe-coding party?\n"
        "\n"
        "## Archive\n"
        "\n"
        "## Summary\n"
        "An RSS article summary.\n"
        "\n"
        "## Profile Relevance\n"
        "### ai-coding\n"
        f"Score: {score}/10\n"
        "Relevant.\n"
        "\n"
        "## User Notes\n"
    )


def _make_rss_note_dict(*, tags: list[str], score: int = 5) -> dict[str, Any]:
    return {
        "id": _NOTE_ID,
        "title": "Who cleans up after the vibe-coding party?",
        "content": _make_rss_note_content(score=score),
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


def _archive_download_403(note: dict[str, object]) -> str:
    """What the production hook now raises for a paywalled URL.

    ``stage`` carries the archive layer's own ``failure_kind`` rather
    than the flattened ``"http"`` it used to.
    """
    raise ExtractionError(
        f"archive_download retry failed: HTTP 403 for {_SOURCE_URL}",
        url=str(note.get("source_url", "")),
        stage="http_403",
    )


def _archive_attempt_count(content: str) -> int:
    """Pull the ``- archive_attempts: N`` value from the ``## Repair`` body."""
    marker = "- archive_attempts:"
    idx = content.find(marker)
    if idx == -1:
        return 0
    line_end = content.find("\n", idx)
    line = content[idx:line_end] if line_end != -1 else content[idx:]
    return int(line.split(":", 1)[1].strip())


# ── Tests ──────────────────────────────────────────────────────────


class TestRssPaywalledArchiveConverges:
    """Three 403s flip the terminal tag and release the note entirely."""

    async def test_three_passes_converge_and_stop_rewriting(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        tags = [
            "profile:ai-coding",
            "influx:repair-needed",
            "influx:archive-missing",
            f"source:rss-{_FEED_SLUG}",
            f"feed-slug:{_FEED_SLUG}",
            # The distinguishing pair: text extraction ran and settled
            # on abstract-only, and nothing set influx:text-terminal.
            "text:abstract-only",
            "influx:tier2-terminal",
        ]
        note = _make_rss_note_dict(tags=tags, score=8)

        config = _make_config(lithos_url=fake_lithos_url)
        client = LithosClient(url=fake_lithos_url)
        hooks = SweepHooks(archive_download=_archive_download_403)

        try:
            # ── Pass 1: counted failure #1 → attempts=1, no terminal. ──
            _queue_single_note(fake_lithos, note)
            await sweep("ai-coding", client=client, config=config, hooks=hooks)
            p1 = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")
            assert _archive_attempt_count(p1["content"]) == 1
            assert 'archive_last_kind: "http_403"' in p1["content"]
            assert "influx:archive-terminal" not in p1["tags"]
            assert "influx:repair-needed" in p1["tags"]

            # ── Pass 2: counted failure #2 → attempts=2. ──
            fake_lithos.calls.clear()
            _queue_single_note(fake_lithos, _next_pass_note(note, p1))
            await sweep("ai-coding", client=client, config=config, hooks=hooks)
            p2 = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")
            assert _archive_attempt_count(p2["content"]) == 2
            assert "influx:archive-terminal" not in p2["tags"]

            # ── Pass 3: at the cap → terminal AND released. ──
            fake_lithos.calls.clear()
            _queue_single_note(fake_lithos, _next_pass_note(note, p2))
            await sweep("ai-coding", client=client, config=config, hooks=hooks)
            p3 = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")
            assert _archive_attempt_count(p3["content"]) == 3
            assert "influx:archive-terminal" in p3["tags"]
            # The assertion the whole issue is about.
            assert "influx:repair-needed" not in p3["tags"]
            # Both remain factually true and visible to an operator: no
            # archive was stored, and the text never got past the
            # abstract.
            assert "influx:archive-missing" in p3["tags"]
            assert "text:abstract-only" in p3["tags"]

            # ── Pass 4: no longer a sweep candidate at all. ──
            #
            # The selector queries on influx:repair-needed, so a released
            # note is never read or rewritten again — which is the point:
            # its updated_at stops being pinned to "now" and it stops
            # outranking fresh material in retrieval.
            fake_lithos.calls.clear()
            fake_lithos.list_responses.append(json.dumps({"items": []}))
            await sweep("ai-coding", client=client, config=config, hooks=hooks)
            assert [c for c in fake_lithos.calls if c[0] == "lithos_write"] == []
        finally:
            await client.close()
