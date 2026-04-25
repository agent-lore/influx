"""Integration tests for full arXiv → Lithos → webhook path (US-019).

Drives the real production ``run_profile`` body through ``POST /runs``
against a local fake Lithos SSE server and a local fake webhook
receiver — no monkeypatching of ``influx.http_api.run_profile``.
Source acquisition is supplied by the ``app.state.item_provider``
seam that PRD 04 will replace with the real arXiv + RSS fetcher.

Covers: AC-M1-7, AC-M1-9, AC-M1-10, AC-M1-11, AC-05-H, AC-05-I.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
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
        self._srv = http.server.HTTPServer(("127.0.0.1", 0), _WebhookHandler)
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
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
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

_SAMPLE_ITEMS: list[dict[str, Any]] = [
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
        "path": "papers/arxiv/2026/04",
        "abstract_or_summary": (
            "We propose a new architecture called the Transformer."
        ),
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
        "path": "papers/arxiv/2026/04",
        "abstract_or_summary": (
            "BERT obtains new state-of-the-art results on a range of tasks."
        ),
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
            description=f"Profile {name}",
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


def _make_item_provider(
    items: list[dict[str, Any]],
    captured: dict[str, Any] | None = None,
) -> Any:
    """Build an item provider that yields *items* and captures the prompt."""

    async def provider(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> Iterable[dict[str, Any]]:
        del profile, kind, run_range
        if captured is not None:
            captured["filter_prompt"] = filter_prompt
        return list(items)

    return provider


def _make_app(
    config: AppConfig,
    *,
    items: list[dict[str, Any]] | None = None,
    captured: dict[str, Any] | None = None,
) -> FastAPI:
    """Create a FastAPI app wired for integration testing.

    The injected ``app.state.item_provider`` plugs source acquisition
    into the real production ``run_profile`` so tests do NOT
    monkeypatch ``influx.http_api.run_profile`` (US-019).
    """
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
    if items is not None:
        app.state.item_provider = _make_item_provider(items, captured)
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


# ── AC-M1-7, AC-M1-10: full path with lithos_write + webhook ─────


class TestFullPath:
    """POST /runs → lithos_write + webhook POST (AC-M1-7, AC-M1-10)."""

    def test_post_runs_records_lithos_write_and_webhook(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
    ) -> None:
        """AC-M1-7: lithos_write calls recorded + digest POST received."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        app = _make_app(config, items=_SAMPLE_ITEMS)

        with TestClient(app) as tc:
            resp = tc.post("/runs", json={"profile": "ai-robotics"})
            assert resp.status_code == 202
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        # AC-M1-7: fake Lithos recorded lithos_write calls.
        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
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
    ) -> None:
        """AC-M1-10: lithos_write payloads contain FR-MCP-6 fields."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        app = _make_app(config, items=_SAMPLE_ITEMS[:1])

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
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
    ) -> None:
        """AC-05-I: highlights filtered by notify_immediate threshold."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        app = _make_app(config, items=_SAMPLE_ITEMS)

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
    ) -> None:
        """AC-M1-9: second run → cache_lookup returns hit → no write."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        items = _SAMPLE_ITEMS[:1]

        # --- First run: item is new (cache miss → write). ---
        app = _make_app(config, items=items)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        first_write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
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
        app2 = _make_app(config, items=items)

        with TestClient(app2) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app2.state.coordinator, "ai-robotics")

        # cache_lookup was called.
        lookup_calls = [c for c in fake_lithos.calls if c[0] == "lithos_cache_lookup"]
        assert len(lookup_calls) >= 1

        # No lithos_write — item was skipped.
        second_write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
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
    ) -> None:
        """AC-05-H: filter prompt has titles from lithos_list rejections."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )

        # Repair sweep calls lithos_list first (influx:repair-needed);
        # queue an empty response so it does not consume the rejection
        # list response below.
        fake_lithos.list_responses.append(json.dumps({"items": []}))
        # Queue lithos_list to return 3 rejection items.
        fake_lithos.list_responses.append(
            json.dumps(
                {
                    "items": [
                        {"id": "r1", "title": "Rejected Paper A"},
                        {"id": "r2", "title": "Rejected Paper B"},
                        {"id": "r3", "title": "Rejected Paper C"},
                    ]
                }
            )
        )

        captured: dict[str, Any] = {}
        app = _make_app(config, items=_SAMPLE_ITEMS[:1], captured=captured)

        with TestClient(app) as tc:
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

        # AC-05-H: filter prompt contains the rejection titles.
        prompt = captured["filter_prompt"]
        assert "Rejected Paper A" in prompt
        assert "Rejected Paper B" in prompt
        assert "Rejected Paper C" in prompt

        # lithos_list was called: first by repair sweep, then by feedback.
        list_calls = [c for c in fake_lithos.calls if c[0] == "lithos_list"]
        assert len(list_calls) == 2
        # First call: repair sweep (influx:repair-needed).
        assert list_calls[0][1]["tags"] == [
            "influx:repair-needed",
            "profile:ai-robotics",
        ]
        # Second call: feedback rejection list.
        assert list_calls[1][1]["tags"] == ["influx:rejected:ai-robotics"]


# ── AC-05-I: webhook fires for non-backfill, skips backfill ──────


class TestWebhookAutoFire:
    """Webhook fires automatically after non-backfill runs (AC-05-I)."""

    def test_webhook_fires_for_manual_run(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
        fake_webhook: FakeWebhookServer,
    ) -> None:
        """AC-05-I: POST /runs (manual) → webhook POST auto-fires."""
        config = _make_config(
            lithos_url=fake_lithos_url,
            webhook_url=fake_webhook.url,
        )
        app = _make_app(config, items=_SAMPLE_ITEMS[:1])

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
    ) -> None:
        """AC-M1-11: POST /runs aborts when Lithos is unreachable."""
        port = self._find_unreachable_port()
        unreachable_url = f"http://127.0.0.1:{port}/sse"
        config = _make_config(
            lithos_url=unreachable_url,
            webhook_url=fake_webhook.url,
        )
        # Provide one item so run_profile attempts a real Lithos call.
        app = _make_app(config, items=_SAMPLE_ITEMS[:1])

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
    ) -> None:
        """AC-M1-11: service stays alive after an aborted run."""
        port = self._find_unreachable_port()
        unreachable_url = f"http://127.0.0.1:{port}/sse"
        config = _make_config(
            lithos_url=unreachable_url,
            webhook_url=fake_webhook.url,
        )
        app = _make_app(config, items=_SAMPLE_ITEMS[:1])

        with TestClient(app) as tc:
            # First run aborts.
            tc.post("/runs", json={"profile": "ai-robotics"})
            _wait_for_idle(app.state.coordinator, "ai-robotics")

            # Service is still responsive.
            assert tc.get("/live").status_code == 200
            assert tc.get("/status").status_code == 200
            body = tc.get("/status").json()
            assert body["status"] == "degraded"
