"""Integration tests for RSS text_extraction convergence (issue #138).

Walks a textless RSS note through three sweep passes:

- Pass 1: no ``text:*`` tag → ``text_extraction`` stub returns
  ``"text:abstract-only"`` (the cascade fall-through path documented
  on :class:`influx.repair.TextExtractionHook` and implemented for
  RSS by :func:`influx.repair_hooks._run_rss_text_extraction`).
  The note exits the text-extraction stage with
  ``text:abstract-only`` stamped.

- Pass 2: feed the run-1 note back.  Both ``text_extraction`` and
  ``re_extract_archive`` stubs are tracked; neither should be called
  (text stage no longer selected because ``text:abstract-only`` is
  present, and there is no archive yet to drive
  abstract-only re-extraction).

- Pass 3: a later landing scenario — re-introduce
  ``influx:archive-missing`` so an ``archive_download`` stub lands an
  archive on this pass, and a ``re_extract_archive`` stub upgrades
  ``text:abstract-only`` to ``text:html``.

This is the integration-level proof of the convergence contract that
unit tests cannot demonstrate: that the *sweep* state machine
converges across passes when the production hook returns
``"text:abstract-only"`` on cascade fall-through, rather than
re-entering ``text_extraction_retry`` forever.
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


class TestRssTextExtractionConvergence:
    """text_extraction returns ``text:abstract-only`` → sweep converges."""

    async def test_pass1_converges_to_abstract_only_then_pass2_skips_text_stage(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """The textless note gets ``text:abstract-only`` after pass 1
        and the text_extraction stage is no longer selected on pass 2.
        """
        # Textless RSS note: no text:* tag, no archive-missing.
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            f"source:rss-{_FEED_SLUG}",
            f"feed-slug:{_FEED_SLUG}",
        ]
        note = _make_rss_note_dict(tags=tags)

        config = _make_config(lithos_url=fake_lithos_url)
        client = LithosClient(url=fake_lithos_url)

        text_extraction_calls: list[str] = []

        def _text_extraction_converge(note: dict[str, object]) -> str:
            """Mirrors the production cascade fall-through return."""
            text_extraction_calls.append(str(note.get("id", "?")))
            return "text:abstract-only"

        try:
            # ── Pass 1: convergence stamps text:abstract-only. ──
            _queue_single_note(fake_lithos, note)
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(text_extraction=_text_extraction_converge),
            )
            assert text_extraction_calls == [_NOTE_ID]

            p1_payload = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")
            assert "text:abstract-only" in p1_payload["tags"]

            # ── Pass 2: re-feed the run-1 note; text stage must not run. ──
            fake_lithos.calls.clear()
            text_extraction_calls.clear()
            re_extract_calls: list[str] = []

            def _re_extract_should_not_run(
                note: dict[str, object],
                archive_path: str,
            ) -> ReExtractionResult:
                re_extract_calls.append(str(note.get("id", "?")))
                return ReExtractionResult(outcome=ExtractionOutcome.TRANSIENT)

            note_p2 = _next_pass_note(note, p1_payload)
            _queue_single_note(fake_lithos, note_p2)
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(
                    text_extraction=_text_extraction_converge,
                    re_extract_archive=_re_extract_should_not_run,
                ),
            )

            assert text_extraction_calls == []
            # No archive landed yet → re_extract_archive can't run either.
            assert re_extract_calls == []
        finally:
            await client.close()

    async def test_later_archive_landing_upgrades_abstract_only_to_html(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """After convergence, a later landing of the archive (via
        ``archive_download``) followed by ``re_extract_archive`` UPGRADE
        replaces ``text:abstract-only`` with ``text:html``.
        """
        # Start in convergence-result state: text:abstract-only present,
        # archive-missing present (e.g. inspector noticed archive gap on a
        # later sweep), repair-needed kept.
        tags = [
            "profile:ai-robotics",
            "influx:repair-needed",
            "influx:archive-missing",
            f"source:rss-{_FEED_SLUG}",
            f"feed-slug:{_FEED_SLUG}",
            "text:abstract-only",
        ]
        note = _make_rss_note_dict(tags=tags)

        config = _make_config(lithos_url=fake_lithos_url)
        client = LithosClient(url=fake_lithos_url)

        def _archive_lands(note: dict[str, object]) -> str:
            return _ARCHIVE_PATH

        def _re_extract_upgrade(
            note: dict[str, object],
            archive_path: str,
        ) -> ReExtractionResult:
            return ReExtractionResult(
                outcome=ExtractionOutcome.UPGRADE,
                upgraded_text_tag="text:html",
            )

        try:
            # Single sweep pass: archive lands AND abstract-only upgrades.
            _queue_single_note(fake_lithos, note)
            await sweep(
                "ai-robotics",
                client=client,
                config=config,
                hooks=SweepHooks(
                    archive_download=_archive_lands,
                    re_extract_archive=_re_extract_upgrade,
                ),
            )

            payload = next(c[1] for c in fake_lithos.calls if c[0] == "lithos_write")
            assert "influx:archive-missing" not in payload["tags"]
            assert f"path: {_ARCHIVE_PATH}" in payload["content"]

            assert "text:abstract-only" not in payload["tags"]
            assert "text:html" in payload["tags"]
        finally:
            await client.close()
