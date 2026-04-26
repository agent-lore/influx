"""Integration tests for multi-profile shared source merging (US-005).

Drives a two-profile run against items that match both profiles,
asserting that one Lithos note ends up carrying both ``profile:*``
tags and two ``## Profile Relevance`` entries merged by profile name
(AC-M3-2, FR-NOTE-6).

Also covers the negative/disjoint case (AC-M3-3) and the preservation
test that running one profile alone does not remove another profile's
tag or entry.
"""

from __future__ import annotations

import json
import time
from collections.abc import Generator, Iterable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from influx.config import (
    AppConfig,
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
from influx.coordinator import Coordinator, RunKind
from influx.http_api import router
from influx.notes import ProfileRelevanceEntry, render_note
from influx.probes import ProbeLoop
from influx.scheduler import InfluxScheduler
from tests.contract.test_lithos_client import FakeLithosServer

# ── Constants ─────────────────────────────────────────────────────────

PROFILE_A = "ai-robotics"
PROFILE_B = "web-tech"
SHARED_URL = "https://arxiv.org/abs/2601.00001"
SHARED_TITLE = "Shared Paper: Attention Is All You Need"


# ── Helpers ───────────────────────────────────────────────────────────


def _make_config(lithos_url: str) -> AppConfig:
    """Build an AppConfig with two profiles."""
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name=PROFILE_A,
                description="AI and robotics",
                thresholds=ProfileThresholds(notify_immediate=8),
            ),
            ProfileConfig(
                name=PROFILE_B,
                description="Browser and web standards",
                thresholds=ProfileThresholds(notify_immediate=8),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(
                text=(
                    "Filter: {profile_description} "
                    "{negative_examples} "
                    "{min_score_in_results}"
                ),
            ),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        feedback=FeedbackConfig(negative_examples_per_profile=20),
    )


def _render_item_content(
    *,
    profile_name: str,
    score: int,
    reason: str,
    tags: list[str],
) -> str:
    """Render a minimal canonical note for a profile item."""
    return render_note(
        title=SHARED_TITLE,
        source_url=SHARED_URL,
        tags=tags,
        confidence=0.8,
        archive_path=None,
        summary="A shared paper about attention mechanisms.",
        keywords=[],
        profile_entries=[
            ProfileRelevanceEntry(
                profile_name=profile_name,
                score=score,
                reason=reason,
            ),
        ],
    )


def _make_items_for_profile(profile: str) -> list[dict[str, Any]]:
    """Build item list for a specific profile."""
    tags = [
        f"profile:{profile}",
        "source:arxiv",
        "ingested-by:influx",
        "schema:v1",
        "arxiv-id:2601.00001",
    ]
    score = 8 if profile == PROFILE_A else 7
    reason = (
        "Relevant to AI robotics."
        if profile == PROFILE_A
        else "Relevant to web tech."
    )
    content = _render_item_content(
        profile_name=profile, score=score, reason=reason, tags=tags
    )
    return [
        {
            "title": SHARED_TITLE,
            "source_url": SHARED_URL,
            "content": content,
            "tags": tags,
            "score": score,
            "confidence": 0.8,
            "path": "papers/arxiv/2026/04",
            "abstract_or_summary": "A shared paper about attention mechanisms.",
        }
    ]


def _make_item_provider(
    profile_items: dict[str, list[dict[str, Any]]],
) -> Any:
    """Build an item provider that returns items per profile."""

    async def provider(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> Iterable[dict[str, Any]]:
        del kind, run_range, filter_prompt
        return list(profile_items.get(profile, []))

    return provider


def _make_app(
    config: AppConfig,
    profile_items: dict[str, list[dict[str, Any]]],
) -> FastAPI:
    """Create a FastAPI app wired for multi-profile testing."""
    app = FastAPI()
    app.include_router(router)
    coordinator = Coordinator()
    scheduler = InfluxScheduler(config, coordinator)
    probe_loop = ProbeLoop(config, interval=30.0)
    probe_loop.run_once()
    app.state.config = config
    app.state.coordinator = coordinator
    app.state.scheduler = scheduler
    app.state.probe_loop = probe_loop
    app.state.active_tasks = set()  # type: ignore[assignment]
    app.state.item_provider = _make_item_provider(profile_items)
    return app


def _wait_for_idle(
    coordinator: Coordinator,
    profile: str,
    timeout: float = 10.0,
) -> None:
    """Block until the coordinator releases the profile lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not coordinator.is_busy(profile):
            return
        time.sleep(0.05)
    raise TimeoutError(f"Profile {profile!r} still busy after {timeout}s")


# ── Fixtures ──────────────────────────────────────────────────────────


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
    fake_lithos.task_create_responses.clear()
    fake_lithos.task_complete_responses.clear()


# ── AC-M3-2: shared item → one note with both profile tags ──────────


class TestSharedSourceMerge:
    """Two profiles matching the same item produce ONE note with both tags.

    Profile A runs first (cache miss → write succeeds).
    Profile B runs second (cache hit → write triggers version_conflict →
    merge merges profile tags and Profile Relevance).
    """

    def test_two_profiles_same_item_produces_merged_note(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """AC-M3-2: both profile:* tags and two Profile Relevance."""
        config = _make_config(lithos_url=fake_lithos_url)

        profile_items = {
            PROFILE_A: _make_items_for_profile(PROFILE_A),
            PROFILE_B: _make_items_for_profile(PROFILE_B),
        }

        # Profile A: cache miss → write succeeds
        # Profile B: cache hit → write → version_conflict → merge

        # Queue: Profile A's cache_lookup miss + write success
        # (default behaviour: cache miss, write created)

        # Queue responses for Profile A run:
        # - lithos_list (repair sweep) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # - lithos_list (feedback) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # - cache_lookup → miss (default)
        # - lithos_write → created (default)

        app = _make_app(config, profile_items)

        # Run Profile A
        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": PROFILE_A})
            _wait_for_idle(app.state.coordinator, PROFILE_A)

        # Verify Profile A's write
        write_calls_a = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls_a) >= 1
        first_write = write_calls_a[0][1]
        assert f"profile:{PROFILE_A}" in first_write["tags"]

        # Capture Profile A's content for the read response
        profile_a_content = first_write["content"]
        profile_a_tags = first_write["tags"]

        # Clear for Profile B run
        fake_lithos.calls.clear()
        fake_lithos.write_responses.clear()
        fake_lithos.read_responses.clear()
        fake_lithos.cache_lookup_responses.clear()
        fake_lithos.list_responses.clear()

        # Queue responses for Profile B run:
        # - lithos_list (repair sweep) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # - lithos_list (feedback) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # - cache_lookup → HIT (item already ingested by Profile A)
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )
        # - lithos_write (from cache-hit path) → version_conflict
        fake_lithos.write_responses.append(
            json.dumps({"status": "version_conflict", "note_id": "note-shared-001"})
        )
        # - lithos_read (version_conflict retry reads existing note)
        fake_lithos.read_responses.append(
            json.dumps(
                {
                    "id": "note-shared-001",
                    "content": profile_a_content,
                    "tags": profile_a_tags,
                    "version": 1,
                }
            )
        )
        # - lithos_write retry → updated
        fake_lithos.write_responses.append(
            json.dumps({"status": "updated", "note_id": "note-shared-001"})
        )

        # Run Profile B against the same app (same config, same coordinator)
        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": PROFILE_B})
            _wait_for_idle(app.state.coordinator, PROFILE_B)

        # Find the final lithos_write call (the version_conflict retry)
        write_calls_b = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        n = len(write_calls_b)
        assert n >= 2, f"Expected ≥2 write calls, got {n}"

        # The retry (second write) should have merged tags
        retry_write = write_calls_b[-1][1]
        assert f"profile:{PROFILE_A}" in retry_write["tags"], (
            "Profile A tag must be preserved in merged write"
        )
        assert f"profile:{PROFILE_B}" in retry_write["tags"], (
            "Profile B tag must be added in merged write"
        )

        # The retry content should have both Profile Relevance entries
        from influx.notes import parse_note, parse_profile_relevance

        parsed = parse_note(retry_write["content"])
        entries = parse_profile_relevance(parsed)
        by_name = {e.profile_name: e for e in entries}

        assert PROFILE_A in by_name, "Profile A relevance entry must be preserved"
        assert PROFILE_B in by_name, "Profile B relevance entry must be present"
        assert by_name[PROFILE_A].score == 8
        assert by_name[PROFILE_B].score == 7


# ── AC-M3-3: disjoint matches → one note per profile ────────────────


class TestDisjointMatch:
    """Disjoint matches produce one note per profile (AC-M3-3)."""

    def test_disjoint_items_produce_separate_notes(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Two profiles with different items → two separate notes."""
        config = _make_config(lithos_url=fake_lithos_url)

        tags_a = [
            f"profile:{PROFILE_A}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        tags_b = [
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
        ]
        item_a = [
            {
                "title": "Only AI Paper",
                "source_url": "https://arxiv.org/abs/2601.99901",
                "content": "# Only AI content",
                "tags": tags_a,
                "score": 9,
                "confidence": 0.9,
                "path": "papers/arxiv/2026/04",
                "abstract_or_summary": "AI only.",
            }
        ]
        item_b = [
            {
                "title": "Only Web Paper",
                "source_url": "https://arxiv.org/abs/2601.99902",
                "content": "# Only Web content",
                "tags": tags_b,
                "score": 7,
                "confidence": 0.7,
                "path": "papers/arxiv/2026/04",
                "abstract_or_summary": "Web only.",
            }
        ]
        profile_items = {PROFILE_A: item_a, PROFILE_B: item_b}

        # Queue repair sweep + feedback for both profiles
        for _ in range(4):
            fake_lithos.list_responses.append(json.dumps({"items": []}))

        app = _make_app(config, profile_items)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": PROFILE_A})
            _wait_for_idle(app.state.coordinator, PROFILE_A)
            tc.post("/runs", json={"profile": PROFILE_B})
            _wait_for_idle(app.state.coordinator, PROFILE_B)

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 2

        titles = {c[1]["title"] for c in write_calls}
        assert "Only AI Paper" in titles
        assert "Only Web Paper" in titles

        # Each note has only its own profile tag
        for call in write_calls:
            payload = call[1]
            if payload["title"] == "Only AI Paper":
                assert f"profile:{PROFILE_A}" in payload["tags"]
                assert f"profile:{PROFILE_B}" not in payload["tags"]
            else:
                assert f"profile:{PROFILE_B}" in payload["tags"]
                assert f"profile:{PROFILE_A}" not in payload["tags"]


# ── Negative: single profile run preserves other profile's data ──────


class TestPreservationOnSingleProfileRun:
    """Running one profile alone does NOT remove another profile's tag or entry."""

    def test_running_profile_b_alone_preserves_profile_a(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Running web-tech alone preserves existing profile:ai-robotics."""
        config = _make_config(lithos_url=fake_lithos_url)

        # Existing note has both profiles (from a previous two-profile run)
        both_tags = [
            f"profile:{PROFILE_A}",
            f"profile:{PROFILE_B}",
            "source:arxiv",
            "ingested-by:influx",
            "schema:v1",
            "arxiv-id:2601.00001",
        ]
        existing_content = _render_item_content(
            profile_name=PROFILE_A,
            score=8,
            reason="AI robotics.",
            tags=both_tags,
        )
        # Manually fix: add Profile B's entry too
        from influx.lithos_client import (
            _replace_profile_relevance_section,
        )
        from influx.notes import (
            parse_note,
            parse_profile_relevance,
        )

        existing_content = _replace_profile_relevance_section(
            existing_content,
            [
                ProfileRelevanceEntry(PROFILE_A, 8, "AI robotics."),
                ProfileRelevanceEntry(PROFILE_B, 7, "Web tech."),
            ],
        )

        # Profile B runs alone, gets cache hit → writes → version_conflict → merge
        profile_items = {PROFILE_B: _make_items_for_profile(PROFILE_B)}

        # Queue repair + feedback
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        # Cache hit → write → version_conflict → read → retry
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )
        fake_lithos.write_responses.append(
            json.dumps({"status": "version_conflict", "note_id": "note-shared-002"})
        )
        fake_lithos.read_responses.append(
            json.dumps(
                {
                    "id": "note-shared-002",
                    "content": existing_content,
                    "tags": both_tags,
                    "version": 2,
                }
            )
        )
        fake_lithos.write_responses.append(
            json.dumps({"status": "updated", "note_id": "note-shared-002"})
        )

        app = _make_app(config, profile_items)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": PROFILE_B})
            _wait_for_idle(app.state.coordinator, PROFILE_B)

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) >= 2

        # The retry write should preserve profile:ai-robotics
        retry_write = write_calls[-1][1]
        assert f"profile:{PROFILE_A}" in retry_write["tags"]
        assert f"profile:{PROFILE_B}" in retry_write["tags"]

        # Profile Relevance should have both entries
        parsed = parse_note(retry_write["content"])
        entries = parse_profile_relevance(parsed)
        by_name = {e.profile_name: e for e in entries}
        assert PROFILE_A in by_name, "profile:ai-robotics entry must be preserved"
        assert PROFILE_B in by_name
