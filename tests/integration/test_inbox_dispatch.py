"""Integration test for the inbox per-Profile dispatch seam (Inbox v1 slice 1).

Drives :func:`influx.inbox.dispatch_profile` end-to-end through a real
:class:`~influx.run_service.RunService` and :class:`~influx.run_ledger.RunLedger`
against the in-process :class:`FakeLithosServer`.  Proves the §5.3 seam: a
pre-acquired, pre-scored inbox item flows through the unchanged Run pipeline
and lands as a ``RunKind.INBOX`` ledger entry with a real ``lithos_write``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path

import pytest

from influx.config import (
    AppConfig,
    ExtractionConfig,
    FeedbackConfig,
    LithosConfig,
    NotificationsConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.inbox import dispatch_profile
from influx.run_ledger import RunLedger
from influx.source import Candidate, ScoredCandidate
from influx.sources.inbox import InboxAcquisition
from tests.contract.test_lithos_client import FakeLithosServer

_URL = "https://example.com/article"


def _make_config(lithos_url: str) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="alpha",
                description="Alpha profile",
                thresholds=ProfileThresholds(
                    relevance=7, full_text=100, deep_extract=100
                ),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(),
        feedback=FeedbackConfig(),
    )


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
def _clear(fake_lithos: FakeLithosServer) -> None:
    fake_lithos.calls.clear()
    fake_lithos.write_responses.clear()
    fake_lithos.cache_lookup_responses.clear()
    fake_lithos.list_responses.clear()
    fake_lithos.task_create_responses.clear()
    fake_lithos.task_complete_responses.clear()


def _scored() -> ScoredCandidate:
    return ScoredCandidate(
        candidate=Candidate(
            item_id="inbox-abc1234567",
            title="A Submitted Article",
            abstract="body text here",
            source_url=_URL,
        ),
        score=8,
        confidence=0.9,
        reason="relevant to alpha",
        filter_tags=("ml",),
    )


def _acquired() -> InboxAcquisition:
    return InboxAcquisition(
        source_url=_URL,
        url_hash="abc1234567",
        archive_path="inbox/2026/06/abc1234567.html",
        archive_missing=False,
        extracted_text="A reasonably long article body that survives any thin checks.",
        summary="A reasonably long article body that survives any thin checks.",
        text_flavour="html",
    )


def _calls(fake: FakeLithosServer, tool: str) -> list[dict]:
    return [args for name, args in fake.calls if name == tool]


def test_dispatch_writes_note_and_records_inbox_ledger_entry(
    fake_lithos: FakeLithosServer,
    fake_lithos_url: str,
    tmp_path: Path,
) -> None:
    config = _make_config(fake_lithos_url)
    ledger = RunLedger(tmp_path)

    outcome = asyncio.run(
        dispatch_profile(
            "alpha",
            scored=_scored(),
            acquired=_acquired(),
            source_tag="inbox",
            submitted_by="agent:test",
            title_hint="A Submitted Article",
            config=config,
            probe_loop=None,
            ledger=ledger,
            run_id="run-inbox-1",
        )
    )

    # One note written through the unchanged ingest pipeline.
    assert outcome.ingested == 1
    write_calls = _calls(fake_lithos, "lithos_write")
    assert len(write_calls) == 1
    tags = write_calls[0]["tags"]
    assert "profile:alpha" in tags
    assert "source:inbox" in tags
    assert "submitter:agent:test" in tags
    assert write_calls[0]["source_url"] == _URL

    # Recorded as a real single-Profile RunKind.INBOX ledger entry.
    recent = ledger.recent(limit=5)
    assert len(recent) == 1
    entry = recent[0]
    assert entry["kind"] == "inbox"
    assert entry["profile"] == "alpha"
    assert entry["sources_checked"] == 1
    assert entry["ingested"] == 1
