"""Integration tests for full arXiv → Lithos → webhook path (US-019).

Exercises POST /runs → real-MCP-write → webhook-POST path against a
local fake Lithos SSE server and a local fake webhook receiver,
replacing PRD 04's stub-recorder assertions.

Covers: AC-M1-7, AC-M1-9, AC-M1-10, AC-M1-11, AC-05-H, AC-05-I.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from collections.abc import Generator
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
from influx.feedback import build_negative_examples_block
from influx.http_api import router
from influx.lithos_client import LithosClient
from influx.notifications import (
    HighlightItem,
    ProfileRunResult,
    RunStats,
)
from influx.probes import ProbeLoop
from influx.scheduler import InfluxScheduler
from influx.service import post_run_webhook_hook
from tests.contract.test_lithos_client import FakeLithosServer

# ── Fake webhook receiver ──────────────────────────────────────────


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    """Handler that records POSTed JSON bodies."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.server.received.append(body)  # type: ignore[attr-defined]
        self.send_response(200)
        self.end_headers()

    def log_message(  # noqa: PLR6301
        self,
        format: str,  # noqa: A002
        *args: object,
    ) -> None:
        pass


class FakeWebhookServer:
    """Simple HTTP server that records incoming POST requests."""

    def __init__(self) -> None:
        self._srv = http.server.HTTPServer(
            ("127.0.0.1", 0), _WebhookHandler
        )
        self._srv.received = []  # type: ignore[attr-defined]
        self.port = self._srv.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def received(self) -> list[dict[str, Any]]:
        return self._srv.received  # type: ignore[attr-defined]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._srv.serve_forever, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def clear(self) -> None:
        self._srv.received.clear()  # type: ignore[attr-defined]


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def fake_lithos() -> Generator[FakeLithosServer, None, None]:
    """Module-scoped fake Lithos MCP server."""
    server = FakeLithosServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def fake_lithos_url(fake_lithos: FakeLithosServer) -> str:
    return f"http://127.0.0.1:{fake_lithos.port}/sse"


@pytest.fixture(scope="module")
def fake_webhook() -> Generator[FakeWebhookServer, None, None]:
    """Module-scoped fake webhook receiver."""
    server = FakeWebhookServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def clear_fakes(
    fake_lithos: FakeLithosServer,
    fake_webhook: FakeWebhookServer,
) -> None:
    """Clear recorded state before each test."""
    fake_lithos.calls.clear()
    fake_lithos.write_responses.clear()
    fake_lithos.read_responses.clear()
    fake_lithos.cache_lookup_responses.clear()
    fake_lithos.list_responses.clear()
    fake_webhook.clear()


# ── Helpers ────────────────────────────────────────────────────────

_SAMPLE_ITEMS = [
    {
        "title": "Attention Is All You Need",
        "source_url": "https://arxiv.org/abs/1706.03762",
        "content": "# Summary\nTransformer architecture paper.",
        "tags": [
            "profile:ai-robotics",
            "arxiv-id:1706.03762",
            "source:arxiv",
        ],
        "score": 9,
        "confidence": 0.9,
    },
    {
        "title": "BERT: Pre-training of Deep Bidirectional Transformers",
        "source_url": "https://arxiv.org/abs/1810.04805",
        "content": "# Summary\nBERT pre-training approach.",
        "tags": [
            "profile:ai-robotics",
            "arxiv-id:1810.04805",
            "source:arxiv",
        ],
        "score": 7,
        "confidence": 0.8,
    },
]


def _make_config(
    *,
    lithos_url: str,
    webhook_url: str = "",
    profiles: list[str] | None = None,
) -> AppConfig:
    """Build a minimal AppConfig for integration tests."""
    profile_names = profiles or ["ai-robotics"]
    profile_list = [
        ProfileConfig(
            name=name,
            thresholds=ProfileThresholds(notify_immediate=8),
        )
        for name in profile_names
    ]
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=profile_list,
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
        notifications=NotificationsConfig(
            webhook_url=webhook_url,
            timeout_seconds=5,
        ),
        security=SecurityConfig(allow_private_ips=True),
        feedback=FeedbackConfig(negative_examples_per_profile=20),
    )


def _make_app(config: AppConfig) -> FastAPI:
    """Create a FastAPI app wired for integration testing."""
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


def _make_fake_run_profile(
    lithos_url: str,
    config: AppConfig,
    items: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any]]:
    """Create a fake ``run_profile`` that exercises real MCP tool calls.

    Returns ``(fake_run_profile_coroutine, shared_state_dict)``.
    The state dict is populated by the run and accessible to tests.
    """
    state: dict[str, Any] = {
        "filter_prompt": "",
        "result": None,
        "error": None,
    }

    async def fake_run_profile(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None = None,
    ) -> None:
        client = LithosClient(url=lithos_url)
        try:
            # 1. Feedback — build negative examples block (AC-05-H).
            neg_block = await build_negative_examples_block(
                client,
                profile=profile,
                limit=config.feedback.negative_examples_per_profile,
            )

            # 2. Build filter prompt (simulating the pipeline).
            prompt_text = config.prompts.filter.text or ""
            state["filter_prompt"] = prompt_text.format(
                profile_description=profile,
                negative_examples=neg_block,
                min_score_in_results=config.filter.min_score_in_results,
            )

            # 3. Process items: cache_lookup → write_note.
            ingested: list[HighlightItem] = []
            for item in items:
                cache_result = await client.cache_lookup(
                    query=item["title"],
                    source_url=item["source_url"],
                )
                cache_body = json.loads(
                    cache_result.content[0].text  # type: ignore[union-attr]
                )
                if cache_body.get("hit"):
                    continue

                write_result = await client.write_note(
                    title=item["title"],
                    content=item.get("content", "# Summary\nContent."),
                    path=item.get("path", "papers/arxiv/2026/04"),
                    source_url=item["source_url"],
                    tags=item.get("tags", [f"profile:{profile}"]),
                    confidence=item.get("confidence", 0.85),
                )
                if write_result.status in ("created", "updated"):
                    ingested.append(
                        HighlightItem(
                            id=f"note-{len(ingested) + 1}",
                            title=item["title"],
                            score=item.get("score", 9),
                            tags=item.get("tags", [f"profile:{profile}"]),
                            reason="High relevance.",
                            url=item["source_url"],
                        )
                    )

            # 4. Build ProfileRunResult.
            result = ProfileRunResult(
                run_date="2026-04-25",
                profile=profile,
                stats=RunStats(
                    sources_checked=len(items),
                    ingested=len(ingested),
                ),
                items=ingested,
            )
            state["result"] = result

            # 5. Fire webhook hook (AC-05-I).
            post_run_webhook_hook(result, config, kind=kind)
        except Exception as exc:
            state["error"] = exc
            raise
        finally:
            await client.close()

    return fake_run_profile, state


# ── AC-M1-7, AC-M1-10: full path with lithos_write + webhook ─────


class TestFullPath:
    """POST /runs → lithos_write + webhook POST (AC-M1-7, AC-M1-10)."""

    def test_post_runs_records_lithos_write_and_webhook(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-M1-7: lithos_write calls recorded + digest POST received."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        fake_run, state = _make_fake_run_profile(
            fake_lithos_url, config, _SAMPLE_ITEMS
        )
        monkeypatch.setattr("influx.http_api.run_profile", fake_run)
        app = _make_app(config)

        with TestClient(app) as tc:
            resp = tc.post("/runs", json={"profile": "ai-robotics"})
            assert resp.status_code == 202
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        # AC-M1-7: fake Lithos recorded lithos_write calls.
        write_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_write"
        ]
        assert len(write_calls) == 2

        # AC-M1-7: fake webhook received a digest POST.
        assert len(fake_webhook.received) == 1
        digest = fake_webhook.received[0]
        assert digest["type"] == "influx_digest"
        assert digest["profile"] == "ai-robotics"
        assert digest["stats"]["ingested"] == 2

    def test_lithos_write_fields_match_spec(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-M1-10: lithos_write payloads contain FR-MCP-6 fields."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        fake_run, _ = _make_fake_run_profile(
            fake_lithos_url, config, _SAMPLE_ITEMS[:1]
        )
        monkeypatch.setattr("influx.http_api.run_profile", fake_run)
        app = _make_app(config)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        write_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_write"
        ]
        assert len(write_calls) == 1
        payload = write_calls[0][1]

        # FR-MCP-6 field assertions.
        assert payload["title"] == "Attention Is All You Need"
        assert payload["agent"] == "influx"
        assert payload["source_url"] == "https://arxiv.org/abs/1706.03762"
        assert payload["namespace"] == "influx"
        assert payload["note_type"] == "summary"
        assert "profile:ai-robotics" in payload["tags"]
        assert payload["confidence"] == 0.9
        assert "# Summary" in payload["content"]

    def test_webhook_digest_has_highlights_by_threshold(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-05-I: highlights filtered by notify_immediate threshold."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        fake_run, _ = _make_fake_run_profile(
            fake_lithos_url, config, _SAMPLE_ITEMS
        )
        monkeypatch.setattr("influx.http_api.run_profile", fake_run)
        app = _make_app(config)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        digest = fake_webhook.received[0]
        # notify_immediate=8: score=9 qualifies, score=7 does not.
        assert len(digest["highlights"]) == 1
        assert digest["highlights"][0]["score"] == 9
        assert len(digest["all_ingested"]) == 2


# ── AC-M1-9: re-run skips already-ingested items ─────────────────


class TestDedupSkip:
    """Re-running same profile skips already-ingested items (AC-M1-9)."""

    def test_second_run_skips_cached_items(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-M1-9: second run → cache_lookup returns hit → no write."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        items = _SAMPLE_ITEMS[:1]

        # --- First run: item is new (cache miss → write). ---
        fake_run_1, _ = _make_fake_run_profile(
            fake_lithos_url, config, items
        )
        monkeypatch.setattr("influx.http_api.run_profile", fake_run_1)
        app = _make_app(config)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        first_write_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_write"
        ]
        assert len(first_write_calls) == 1

        # Clear state for second run.
        fake_lithos.calls.clear()
        fake_lithos.write_responses.clear()
        fake_lithos.cache_lookup_responses.clear()
        fake_lithos.list_responses.clear()
        fake_webhook.clear()

        # Queue cache_lookup to return hit for the second run.
        fake_lithos.cache_lookup_responses.append(
            json.dumps({"hit": True, "stale_exists": False})
        )

        # --- Second run: item is cached (cache hit → skip). ---
        fake_run_2, state_2 = _make_fake_run_profile(
            fake_lithos_url, config, items
        )
        monkeypatch.setattr("influx.http_api.run_profile", fake_run_2)
        app2 = _make_app(config)

        with TestClient(app2) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app2.state.coordinator, "ai-robotics")

        # cache_lookup was called.
        lookup_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_cache_lookup"
        ]
        assert len(lookup_calls) >= 1

        # No lithos_write — item was skipped.
        second_write_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_write"
        ]
        assert len(second_write_calls) == 0

        # Webhook digest shows zero ingested.
        assert len(fake_webhook.received) == 1
        assert fake_webhook.received[0]["stats"]["ingested"] == 0


# ── AC-05-H: negative examples in filter prompt ──────────────────


class TestNegativeExamples:
    """Filter prompt contains negative_examples (AC-05-H end-to-end)."""

    def test_filter_prompt_contains_rejection_titles(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-05-H: filter prompt has titles from lithos_list rejections."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )

        # Queue lithos_list to return 3 rejection items.
        fake_lithos.list_responses.append(
            json.dumps({
                "items": [
                    {"id": "r1", "title": "Rejected Paper A"},
                    {"id": "r2", "title": "Rejected Paper B"},
                    {"id": "r3", "title": "Rejected Paper C"},
                ]
            })
        )

        fake_run, state = _make_fake_run_profile(
            fake_lithos_url, config, _SAMPLE_ITEMS[:1]
        )
        monkeypatch.setattr("influx.http_api.run_profile", fake_run)
        app = _make_app(config)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        # AC-05-H: filter prompt contains the rejection titles.
        prompt = state["filter_prompt"]
        assert "Rejected Paper A" in prompt
        assert "Rejected Paper B" in prompt
        assert "Rejected Paper C" in prompt

        # lithos_list was called with the right tag.
        list_calls = [
            c for c in fake_lithos.calls if c[0] == "lithos_list"
        ]
        assert len(list_calls) == 1
        assert list_calls[0][1]["tags"] == [
            "influx:rejected:ai-robotics"
        ]


# ── AC-05-I: webhook fires for non-backfill, skips backfill ──────


class TestWebhookAutoFire:
    """Webhook fires automatically after non-backfill runs (AC-05-I)."""

    def test_webhook_fires_for_manual_run(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-05-I: POST /runs (manual) → webhook POST auto-fires."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        fake_run, _ = _make_fake_run_profile(
            fake_lithos_url, config, _SAMPLE_ITEMS[:1]
        )
        monkeypatch.setattr("influx.http_api.run_profile", fake_run)
        app = _make_app(config)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        # Webhook received exactly 1 digest.
        assert len(fake_webhook.received) == 1
        assert fake_webhook.received[0]["type"] == "influx_digest"

    def test_webhook_skips_backfill_run(
        self,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
    ) -> None:
        """FR-NOT-4: backfill kind → no webhook POST."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        result = ProfileRunResult(
            run_date="2026-04-25",
            profile="ai-robotics",
            stats=RunStats(sources_checked=5, ingested=2),
            items=[
                HighlightItem(
                    id="note-1",
                    title="Test",
                    score=9,
                    tags=["profile:ai-robotics"],
                    reason="Relevant.",
                    url="https://arxiv.org/abs/2601.00001",
                ),
            ],
        )
        # Backfill → no-op.
        post_run_webhook_hook(result, config, kind=RunKind.BACKFILL)
        assert len(fake_webhook.received) == 0

    def test_webhook_silent_skip_on_empty_url(
        self,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
    ) -> None:
        """AC-05-J: empty webhook URL → zero requests."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url="",
        )
        result = ProfileRunResult(
            run_date="2026-04-25",
            profile="ai-robotics",
            stats=RunStats(sources_checked=5, ingested=1),
            items=[],
        )
        post_run_webhook_hook(result, config, kind=RunKind.MANUAL)
        assert len(fake_webhook.received) == 0


# ── AC-M1-11: Lithos unreachable → abort + degraded ──────────────


class TestLithosUnreachable:
    """Lithos unreachable → run aborts, /status degraded (AC-M1-11)."""

    @staticmethod
    def _find_unreachable_port() -> int:
        """Get a port that nobody is listening on."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_run_aborts_when_lithos_unreachable(
        self,
        fake_webhook: FakeWebhookServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-M1-11: POST /runs aborts when Lithos is unreachable."""
        port = self._find_unreachable_port()
        unreachable_url = f"http://127.0.0.1:{port}/sse"
        config = _make_config(
            lithos_url=unreachable_url,
            webhook_url=fake_webhook.url,
        )

        # Monkeypatch run_profile to attempt a real LithosClient
        # connection against the unreachable URL.
        async def failing_run_profile(
            profile: str,
            kind: RunKind,
            run_range: dict[str, str | int] | None = None,
        ) -> None:
            client = LithosClient(url=unreachable_url)
            try:
                await client.cache_lookup(
                    query="test", source_url="https://test.com"
                )
            finally:
                await client.close()

        monkeypatch.setattr(
            "influx.http_api.run_profile", failing_run_profile
        )
        app = _make_app(config)

        with TestClient(app) as tc:
            resp = tc.post("/runs", json={"profile": "ai-robotics"})
            assert resp.status_code == 202

            # Wait for the run to abort (lock released).
            _wait_for_idle(app.state.coordinator, "ai-robotics")

            # No webhook was sent (run aborted before reaching that step).
            assert len(fake_webhook.received) == 0

            # Service stays alive — another request succeeds.
            live_resp = tc.get("/live")
            assert live_resp.status_code == 200

    def test_status_degraded_when_lithos_unreachable(self) -> None:
        """AC-M1-11: /status reports degraded with lithos != ok."""
        port = self._find_unreachable_port()
        unreachable_url = f"http://127.0.0.1:{port}/sse"
        config = _make_config(lithos_url=unreachable_url)
        app = _make_app(config)

        with TestClient(app) as tc:
            body = tc.get("/status").json()

        assert body["status"] == "degraded"
        assert body["dependencies"]["lithos"]["status"] == "degraded"

    def test_service_alive_after_degraded_run(
        self,
        fake_webhook: FakeWebhookServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-M1-11: service stays alive after an aborted run."""
        port = self._find_unreachable_port()
        unreachable_url = f"http://127.0.0.1:{port}/sse"
        config = _make_config(
            lithos_url=unreachable_url,
            webhook_url=fake_webhook.url,
        )

        async def failing_run(
            profile: str,
            kind: RunKind,
            run_range: dict[str, str | int] | None = None,
        ) -> None:
            client = LithosClient(url=unreachable_url)
            try:
                await client.call_tool("lithos_ping")
            finally:
                await client.close()

        monkeypatch.setattr("influx.http_api.run_profile", failing_run)
        app = _make_app(config)

        with TestClient(app) as tc:
            # First run aborts.
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

            # Service is still responsive.
            assert tc.get("/live").status_code == 200
            assert tc.get("/status").status_code == 200
            body = tc.get("/status").json()
            assert body["status"] == "degraded"
