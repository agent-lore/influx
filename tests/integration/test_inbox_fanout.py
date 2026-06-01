"""Integration test for inbox multi-profile fan-out (Inbox v1 slice 2).

Drives the full :class:`~influx.inbox.InboxTick` end-to-end through real
:class:`~influx.run_service.RunService` dispatches and a real
:class:`~influx.run_ledger.RunLedger` against the in-process
:class:`FakeLithosServer`.  Proves the slice-2 acceptance: an item clearing
N profiles produces N ``RunKind.INBOX`` ledger entries (one per profile)
from a single acquisition.

Only the URL acquisition and the filter scorer are stubbed (no network /
no LLM); the dispatch, write, ledger, and task lifecycle are real.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ExtractionConfig,
    FeedbackConfig,
    InboxConfig,
    LithosConfig,
    NotificationsConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.coordinator import Coordinator
from influx.inbox import InboxTick
from influx.lithos_client import LithosClient
from influx.run_ledger import RunLedger
from influx.source import Candidate, ScoredCandidate
from influx.sources.inbox import InboxAcquisition
from tests.contract.test_lithos_client import FakeLithosServer

_URL = "https://example.com/article"
_PROFILES = ("ai-robotics", "web-tech", "ml-systems")


def _make_config(lithos_url: str) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name=name,
                description=f"{name} profile",
                thresholds=ProfileThresholds(
                    relevance=7, full_text=100, deep_extract=100
                ),
            )
            for name in _PROFILES
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
        inbox=InboxConfig(enabled=True),
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
    for queue in (
        fake_lithos.write_responses,
        fake_lithos.cache_lookup_responses,
        fake_lithos.list_responses,
        fake_lithos.task_create_responses,
        fake_lithos.task_complete_responses,
        fake_lithos.task_list_responses,
        fake_lithos.task_claim_responses,
        fake_lithos.task_update_responses,
    ):
        queue.clear()


def _acquisition() -> InboxAcquisition:
    return InboxAcquisition(
        source_url=_URL,
        url_hash="abc1234567",
        archive_path="inbox/2026/06/abc1234567.html",
        archive_missing=False,
        extracted_text="A sufficiently long article body to clear thin checks.",
        summary="A sufficiently long article body to clear thin checks.",
        text_flavour="html",
    )


def _clearing_scorer():
    async def _scorer(
        candidates: list[Candidate], profile: str, filter_prompt: str
    ) -> dict[str, ScoredCandidate]:
        return {
            c.item_id: ScoredCandidate(
                candidate=c, score=8, confidence=0.9, reason="r", filter_tags=()
            )
            for c in candidates
        }

    return _scorer


def _calls(fake: FakeLithosServer, tool: str) -> list[dict[str, Any]]:
    return [args for name, args in fake.calls if name == tool]


def test_fanout_produces_one_inbox_ledger_entry_per_clearing_profile(
    fake_lithos: FakeLithosServer,
    fake_lithos_url: str,
    tmp_path: Path,
) -> None:
    config = _make_config(fake_lithos_url)
    ledger = RunLedger(tmp_path)
    # One pending inbox task to claim.
    fake_lithos.task_list_responses.append(
        '{"tasks": [{"id": "task-1", "metadata": '
        '{"kind": "url", "url": "' + _URL + '", "submitted_by": "agent:test"}}]}'
    )

    tick = InboxTick(
        config=config,
        coordinator=Coordinator(),
        probe_loop=None,
        ledger=ledger,
        client_factory=lambda: LithosClient(url=fake_lithos_url),
    )

    with (
        patch("influx.inbox.acquire_inbox_bytes", return_value=_acquisition()),
        patch(
            "influx.inbox.make_default_batch_scorer", return_value=_clearing_scorer()
        ),
    ):
        asyncio.run(tick.execute())

    # One real RunKind.INBOX ledger entry per clearing profile.
    recent = ledger.recent(limit=10)
    assert len(recent) == 3
    assert {e["kind"] for e in recent} == {"inbox"}
    assert {e["profile"] for e in recent} == set(_PROFILES)
    assert all(e["sources_checked"] == 1 for e in recent)

    # Three real writes (the canonical note + per-profile merge attempts).
    assert len(_calls(fake_lithos, "lithos_write")) == 3

    # The task was claimed and completed with a multi-profile outcome.
    complete_calls = _calls(fake_lithos, "lithos_task_complete")
    inbox_complete = [c for c in complete_calls if c["agent"] == "influx-inbox"]
    assert len(inbox_complete) == 1
    assert "ingested into 3 profile(s)" in inbox_complete[0]["outcome"]
