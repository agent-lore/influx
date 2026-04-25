"""Tests for webhook sender + service.py post-run hook (US-016).

Covers:
- AC-05-I: digest POST reaches receiver with FR-NOT-2 body keys,
  highlights selected by score >= threshold from config
- AC-05-J: empty AGENT_ZERO_WEBHOOK_URL → silently skipped
- FR-NOT-4: kind == "backfill" → no-op (zero requests)
- Timeout/no-retry: hanging receiver → one attempt, timeout logged
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from influx.config import (
    AppConfig,
    NotificationsConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    SecurityConfig,
)
from influx.coordinator import RunKind
from influx.notifications import (
    HighlightItem,
    ProfileRunResult,
    RunStats,
    build_digest,
    send_digest,
)
from influx.service import post_run_webhook_hook

# ── Fake webhook receiver ────────────────────────────────────────────


class _RecordingHandler(BaseHTTPRequestHandler):
    """HTTP handler that records POST bodies."""

    received: list[dict[str, Any]]
    request_count: int

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.request_count += 1
        self.__class__.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # silence request logging


class _HangingHandler(BaseHTTPRequestHandler):
    """HTTP handler that hangs without responding."""

    request_count: int

    def do_POST(self) -> None:  # noqa: N802
        self.__class__.request_count += 1
        # Sleep longer than any configured timeout
        time.sleep(30)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture()
def fake_webhook_url() -> Generator[str]:
    """Start a local HTTP server that records POST requests."""
    _RecordingHandler.received = []
    _RecordingHandler.request_count = 0

    srv = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/webhook"
    srv.shutdown()


@pytest.fixture()
def hanging_webhook_url() -> Generator[str]:
    """Start a local HTTP server that hangs on POST."""
    _HangingHandler.request_count = 0

    srv = HTTPServer(("127.0.0.1", 0), _HangingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/webhook"
    srv.shutdown()


# ── Helpers ──────────────────────────────────────────────────────────


def _make_item(
    *,
    id: str = "2603.12939",
    title: str = "RoboStream: Real-Time Robot Memory",
    score: int = 10,
    url: str = "https://arxiv.org/abs/2603.12939",
) -> HighlightItem:
    return HighlightItem(
        id=id,
        title=title,
        score=score,
        tags=["robot-memory"],
        reason="Highly relevant",
        url=url,
    )


def _make_result(
    *,
    ingested: int = 2,
    items: list[HighlightItem] | None = None,
) -> ProfileRunResult:
    return ProfileRunResult(
        run_date="2026-04-25",
        profile="ai-robotics",
        stats=RunStats(sources_checked=50, ingested=ingested),
        items=items if items is not None else [_make_item()],
    )


def _make_config(
    *,
    webhook_url: str = "",
    timeout_seconds: int = 5,
    notify_immediate: int = 8,
    allow_private_ips: bool = True,
) -> AppConfig:
    return AppConfig(
        notifications=NotificationsConfig(
            webhook_url=webhook_url,
            timeout_seconds=timeout_seconds,
        ),
        security=SecurityConfig(allow_private_ips=allow_private_ips),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                thresholds=ProfileThresholds(
                    notify_immediate=notify_immediate,
                ),
            ),
        ],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="test"),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
    )


# ── AC-05-I: digest POST reaches receiver ───────────────────────────


class TestWebhookSendDigest:
    """AC-05-I: webhook sender POSTs digest to receiver."""

    def test_digest_post_reaches_receiver(
        self, fake_webhook_url: str
    ) -> None:
        """Digest POST reaches the fake receiver with FR-NOT-2 body keys."""
        result = _make_result(
            items=[_make_item(score=10), _make_item(id="low", score=5, url="https://l")],
        )
        digest = build_digest(result, notify_immediate_threshold=8)
        send_digest(
            digest,
            webhook_url=fake_webhook_url,
            timeout_seconds=5,
            allow_private_ips=True,
        )

        assert _RecordingHandler.request_count == 1
        body = _RecordingHandler.received[0]

        # FR-NOT-2 body keys
        assert body["type"] == "influx_digest"
        assert body["run_date"] == "2026-04-25"
        assert body["profile"] == "ai-robotics"
        assert "stats" in body
        assert "highlights" in body
        assert "all_ingested" in body

    def test_highlights_selected_by_threshold_from_config(
        self, fake_webhook_url: str
    ) -> None:
        """AC-05-I: highlights use score >= notify_immediate from config."""
        items = [
            _make_item(id="a", score=9, url="https://a"),
            _make_item(id="b", score=7, url="https://b"),
            _make_item(id="c", score=10, url="https://c"),
        ]
        result = _make_result(ingested=3, items=items)
        digest = build_digest(result, notify_immediate_threshold=9)
        send_digest(
            digest,
            webhook_url=fake_webhook_url,
            timeout_seconds=5,
            allow_private_ips=True,
        )

        body = _RecordingHandler.received[0]
        highlight_ids = {h["id"] for h in body["highlights"]}
        assert highlight_ids == {"a", "c"}
        assert body["stats"]["high_relevance"] == 2


# ── AC-05-J: empty webhook URL → silent skip ────────────────────────


class TestWebhookEmptyUrl:
    """AC-05-J: empty URL → silently skipped."""

    def test_empty_url_skips(self, fake_webhook_url: str) -> None:
        """With empty webhook_url, no request is sent."""
        digest = build_digest(_make_result(), notify_immediate_threshold=8)
        send_digest(
            digest,
            webhook_url="",
            timeout_seconds=5,
            allow_private_ips=True,
        )
        assert _RecordingHandler.request_count == 0

    def test_send_digest_returns_without_raising(self) -> None:
        """Empty URL returns cleanly — no exception."""
        digest = build_digest(_make_result(), notify_immediate_threshold=8)
        # Should not raise
        send_digest(digest, webhook_url="", timeout_seconds=5)


# ── FR-NOT-4: backfill → no-op ──────────────────────────────────────


class TestWebhookBackfillNoop:
    """FR-NOT-4: post_run_webhook_hook is no-op for backfills."""

    def test_backfill_sends_no_request(
        self, fake_webhook_url: str
    ) -> None:
        """kind == backfill → zero requests recorded."""
        config = _make_config(
            webhook_url=fake_webhook_url,
            allow_private_ips=True,
        )
        result = _make_result()
        post_run_webhook_hook(result, config, kind=RunKind.BACKFILL)
        assert _RecordingHandler.request_count == 0

    def test_manual_run_sends_request(
        self, fake_webhook_url: str
    ) -> None:
        """kind == manual → request sent (positive control)."""
        config = _make_config(
            webhook_url=fake_webhook_url,
            allow_private_ips=True,
        )
        result = _make_result()
        post_run_webhook_hook(result, config, kind=RunKind.MANUAL)
        assert _RecordingHandler.request_count == 1

    def test_scheduled_run_sends_request(
        self, fake_webhook_url: str
    ) -> None:
        """kind == scheduled → request sent (positive control)."""
        config = _make_config(
            webhook_url=fake_webhook_url,
            allow_private_ips=True,
        )
        result = _make_result()
        post_run_webhook_hook(result, config, kind=RunKind.SCHEDULED)
        assert _RecordingHandler.request_count == 1


# ── Timeout / no-retry ──────────────────────────────────────────────


class TestWebhookTimeout:
    """Timeout/no-retry: hanging receiver → one attempt, logged."""

    def test_timeout_logs_and_returns(
        self,
        hanging_webhook_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Hanging receiver → timeout, log, return (no retry)."""
        digest = build_digest(_make_result(), notify_immediate_threshold=8)
        with caplog.at_level(logging.WARNING, logger="influx.notifications"):
            send_digest(
                digest,
                webhook_url=hanging_webhook_url,
                timeout_seconds=1,
                allow_private_ips=True,
            )

        # Exactly ONE request attempt (no retry)
        assert _HangingHandler.request_count == 1
        # Warning was logged
        assert any("failed" in r.message.lower() for r in caplog.records)

    def test_timeout_does_not_raise(
        self, hanging_webhook_url: str
    ) -> None:
        """Sender catches timeout — does not propagate."""
        digest = build_digest(_make_result(), notify_immediate_threshold=8)
        # Should not raise
        send_digest(
            digest,
            webhook_url=hanging_webhook_url,
            timeout_seconds=1,
            allow_private_ips=True,
        )


# ── Post-run hook via service.py ─────────────────────────────────────


class TestPostRunWebhookHook:
    """post_run_webhook_hook invoked directly — sender integration."""

    def test_hook_posts_digest_with_threshold_from_config(
        self, fake_webhook_url: str
    ) -> None:
        """AC-05-I via hook: digest has highlights per config threshold."""
        config = _make_config(
            webhook_url=fake_webhook_url,
            notify_immediate=9,
            allow_private_ips=True,
        )
        items = [
            _make_item(id="high", score=10, url="https://h"),
            _make_item(id="at", score=9, url="https://a"),
            _make_item(id="low", score=7, url="https://l"),
        ]
        result = _make_result(ingested=3, items=items)
        post_run_webhook_hook(result, config, kind=RunKind.MANUAL)

        assert _RecordingHandler.request_count == 1
        body = _RecordingHandler.received[0]
        highlight_ids = {h["id"] for h in body["highlights"]}
        assert highlight_ids == {"high", "at"}

    def test_hook_empty_url_skips(self) -> None:
        """AC-05-J via hook: empty URL → no request."""
        config = _make_config(webhook_url="")
        result = _make_result()
        # Should return without raising
        post_run_webhook_hook(result, config, kind=RunKind.MANUAL)
