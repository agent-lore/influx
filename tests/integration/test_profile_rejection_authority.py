"""Integration tests for per-profile rejection authority on the ingest path (US-006).

Exercises the rejection-authority post-conditions:

- **AC-M3-6**: ``influx:rejected:<profile>`` blocks re-adding
  ``profile:<profile>`` even when the source matches that profile again.
- **AC-09-K**: A different profile that separately scores the same item
  >= ``relevance`` IS added, while the rejection on the first profile is
  preserved.
- **AC-M3-5**: A rejected note's title appears in the ``NEGATIVE
  EXAMPLES`` block of the filter prompt for the rejected profile.
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


def _make_capturing_item_provider(
    profile_items: dict[str, list[dict[str, Any]]],
    captured_prompts: dict[str, str],
) -> Any:
    """Build an item provider that captures filter_prompt per profile."""

    async def provider(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> Iterable[dict[str, Any]]:
        del kind, run_range
        captured_prompts[profile] = filter_prompt
        return list(profile_items.get(profile, []))

    return provider


def _make_app(
    config: AppConfig,
    profile_items: dict[str, list[dict[str, Any]]],
    captured_prompts: dict[str, str] | None = None,
) -> FastAPI:
    """Create a FastAPI app wired for rejection-authority testing."""
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

    if captured_prompts is not None:
        app.state.item_provider = _make_capturing_item_provider(
            profile_items, captured_prompts
        )
    else:

        async def _provider(
            profile: str,
            kind: RunKind,
            run_range: dict[str, str | int] | None,
            filter_prompt: str,
        ) -> Iterable[dict[str, Any]]:
            del kind, run_range, filter_prompt
            return list(profile_items.get(profile, []))

        app.state.item_provider = _provider

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


# ── Existing note with influx:rejected:ai-robotics ──────────────────

def _existing_rejected_note_tags() -> list[str]:
    """Tags for the existing note where profile-A is rejected."""
    return [
        "source:arxiv",
        "ingested-by:influx",
        "schema:v1",
        "arxiv-id:2601.00001",
        "influx:rejected:ai-robotics",
    ]


def _existing_rejected_note_content() -> str:
    """Content for existing note (Profile Relevance for the rejected profile)."""
    return render_note(
        title=SHARED_TITLE,
        source_url=SHARED_URL,
        tags=_existing_rejected_note_tags(),
        confidence=0.8,
        archive_path=None,
        summary="A shared paper about attention mechanisms.",
        keywords=[],
        profile_entries=[
            ProfileRelevanceEntry(
                profile_name=PROFILE_A,
                score=6,
                reason="Previously scored for AI robotics.",
            ),
        ],
    )


# ── AC-M3-6 + AC-09-K: rejection authority on ingest ────────────────


class TestRejectionAuthorityIngest:
    """Two-profile run where profile-A is rejected and profile-B is not.

    Asserts:
    - ``influx:rejected:ai-robotics`` present in final tags
    - ``profile:ai-robotics`` absent from final tags (AC-M3-6)
    - ``profile:web-tech`` present in final tags (AC-09-K)
    - Profile Relevance entry for ai-robotics NOT refreshed (old score/reason)
    """

    def test_rejected_profile_not_readded_accepted_profile_added(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """AC-M3-6 + AC-09-K end-to-end."""
        config = _make_config(lithos_url=fake_lithos_url)

        profile_items = {
            PROFILE_A: _make_items_for_profile(PROFILE_A),
            PROFILE_B: _make_items_for_profile(PROFILE_B),
        }

        existing_tags = _existing_rejected_note_tags()
        existing_content = _existing_rejected_note_content()

        # ── Profile A run ──
        # lithos_list (repair sweep) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # lithos_list (feedback: influx:rejected:ai-robotics) → the rejected note
        fake_lithos.list_responses.append(
            json.dumps(
                {
                    "items": [
                        {"id": "note-rejected-001", "title": SHARED_TITLE},
                    ]
                }
            )
        )
        # cache_lookup → HIT (item already in Lithos from prior run)
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )
        # write (cache-hit path) → version_conflict
        fake_lithos.write_responses.append(
            json.dumps(
                {"status": "version_conflict", "note_id": "note-rejected-001"}
            )
        )
        # read (version_conflict retry) → existing note with rejection tag
        fake_lithos.read_responses.append(
            json.dumps(
                {
                    "id": "note-rejected-001",
                    "content": existing_content,
                    "tags": existing_tags,
                    "version": 1,
                }
            )
        )
        # write retry → updated
        fake_lithos.write_responses.append(
            json.dumps({"status": "updated", "note_id": "note-rejected-001"})
        )

        app = _make_app(config, profile_items)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": PROFILE_A})
            _wait_for_idle(app.state.coordinator, PROFILE_A)

        # Verify Profile A's retry write: profile:ai-robotics must be absent
        write_calls_a = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls_a) >= 2, "Expected initial write + retry write"

        retry_write_a = write_calls_a[-1][1]
        assert "influx:rejected:ai-robotics" in retry_write_a["tags"], (
            "Rejection tag must be preserved"
        )
        assert f"profile:{PROFILE_A}" not in retry_write_a["tags"], (
            "Rejected profile:ai-robotics must NOT be re-added (AC-M3-6)"
        )

        # Capture Profile A's merged content + tags for Profile B's read
        merged_content_a = retry_write_a["content"]
        merged_tags_a = retry_write_a["tags"]

        # Profile Relevance: ai-robotics entry should be OLD (score=6, not 8)
        from influx.notes import parse_note, parse_profile_relevance

        parsed_a = parse_note(merged_content_a)
        entries_a = parse_profile_relevance(parsed_a)
        by_name_a = {e.profile_name: e for e in entries_a}
        assert PROFILE_A in by_name_a, (
            "Old Profile Relevance entry for rejected profile must be preserved"
        )
        assert by_name_a[PROFILE_A].score == 6, (
            "Rejected profile's score must NOT be refreshed (old=6, new would be 8)"
        )

        # ── Profile B run ──
        fake_lithos.calls.clear()
        fake_lithos.write_responses.clear()
        fake_lithos.read_responses.clear()
        fake_lithos.cache_lookup_responses.clear()
        fake_lithos.list_responses.clear()

        # lithos_list (repair sweep) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # lithos_list (feedback: influx:rejected:web-tech) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # cache_lookup → HIT
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )
        # write (cache-hit path) → version_conflict
        fake_lithos.write_responses.append(
            json.dumps(
                {"status": "version_conflict", "note_id": "note-rejected-001"}
            )
        )
        # read (version_conflict retry) → note as left by Profile A's run
        fake_lithos.read_responses.append(
            json.dumps(
                {
                    "id": "note-rejected-001",
                    "content": merged_content_a,
                    "tags": merged_tags_a,
                    "version": 2,
                }
            )
        )
        # write retry → updated
        fake_lithos.write_responses.append(
            json.dumps({"status": "updated", "note_id": "note-rejected-001"})
        )

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": PROFILE_B})
            _wait_for_idle(app.state.coordinator, PROFILE_B)

        # Verify Profile B's retry write
        write_calls_b = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls_b) >= 2

        retry_write_b = write_calls_b[-1][1]
        final_tags = retry_write_b["tags"]

        # AC-09-K: profile:web-tech IS added
        assert f"profile:{PROFILE_B}" in final_tags, (
            "Non-rejected profile:web-tech must be added (AC-09-K)"
        )
        # AC-M3-6: profile:ai-robotics still absent
        assert f"profile:{PROFILE_A}" not in final_tags, (
            "Rejected profile:ai-robotics must remain absent (AC-M3-6)"
        )
        # Rejection tag preserved
        assert "influx:rejected:ai-robotics" in final_tags, (
            "Rejection tag must be preserved through both runs"
        )

        # Profile Relevance: both entries present
        parsed_b = parse_note(retry_write_b["content"])
        entries_b = parse_profile_relevance(parsed_b)
        by_name_b = {e.profile_name: e for e in entries_b}

        assert PROFILE_B in by_name_b, (
            "Profile B relevance entry must be present"
        )
        assert by_name_b[PROFILE_B].score == 7
        assert PROFILE_A in by_name_b, (
            "Rejected profile's old relevance entry must be preserved"
        )
        assert by_name_b[PROFILE_A].score == 6, (
            "Rejected profile's score must still be the old value (6)"
        )


# ── AC-M3-5: rejected title in NEGATIVE EXAMPLES ────────────────────


class TestNegativeExamplesInFilterPrompt:
    """Rejected note title appears in NEGATIVE EXAMPLES for the rejected profile."""

    def test_rejected_title_in_filter_prompt(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """AC-M3-5: rejected title appears in filter prompt."""
        config = _make_config(lithos_url=fake_lithos_url)

        # No items — we only care about the filter prompt construction
        profile_items: dict[str, list[dict[str, Any]]] = {PROFILE_A: []}
        captured_prompts: dict[str, str] = {}

        # lithos_list (repair sweep) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # lithos_list (feedback: influx:rejected:ai-robotics) → the rejected note
        fake_lithos.list_responses.append(
            json.dumps(
                {
                    "items": [
                        {"id": "note-rej-001", "title": "Bad Paper About AI"},
                        {"id": "note-rej-002", "title": "Another Rejected Paper"},
                    ]
                }
            )
        )

        app = _make_app(config, profile_items, captured_prompts=captured_prompts)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": PROFILE_A})
            _wait_for_idle(app.state.coordinator, PROFILE_A)

        assert PROFILE_A in captured_prompts, (
            "Filter prompt must have been captured for profile A"
        )
        prompt = captured_prompts[PROFILE_A]
        assert "Bad Paper About AI" in prompt, (
            "Rejected title must appear in the filter prompt (AC-M3-5)"
        )
        assert "Another Rejected Paper" in prompt, (
            "Second rejected title must appear in the filter prompt"
        )
        assert "(rejected)" in prompt, (
            "Rejected titles must be annotated with (rejected)"
        )

    def test_non_rejected_profile_no_negative_examples(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Non-rejected profile gets no negative examples from the other profile."""
        config = _make_config(lithos_url=fake_lithos_url)

        profile_items: dict[str, list[dict[str, Any]]] = {PROFILE_B: []}
        captured_prompts: dict[str, str] = {}

        # lithos_list (repair sweep) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # lithos_list (feedback: influx:rejected:web-tech) → empty
        fake_lithos.list_responses.append(json.dumps({"items": []}))

        app = _make_app(config, profile_items, captured_prompts=captured_prompts)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": PROFILE_B})
            _wait_for_idle(app.state.coordinator, PROFILE_B)

        assert PROFILE_B in captured_prompts
        prompt = captured_prompts[PROFILE_B]
        assert "(rejected)" not in prompt, (
            "Non-rejected profile must not see rejected examples from other profiles"
        )
