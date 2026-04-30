"""Tests for typed notification webhook dispatch."""

from __future__ import annotations

import hashlib
import hmac
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
    NotificationWebhookConfig,
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
    dispatch_notifications,
    send_digest,
)
from influx.service import post_run_webhook_hook


class _RecordingHandler(BaseHTTPRequestHandler):
    """HTTP handler that records POST bodies and headers."""

    received: list[dict[str, Any]]
    headers_seen: list[dict[str, str]]
    request_count: int

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.request_count += 1
        self.__class__.received.append(json.loads(body))
        self.__class__.headers_seen.append(
            {key.lower(): value for key, value in self.headers.items()}
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


class _HangingHandler(BaseHTTPRequestHandler):
    """HTTP handler that hangs without responding."""

    request_count: int

    def do_POST(self) -> None:  # noqa: N802
        self.__class__.request_count += 1
        time.sleep(30)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


class _RedirectHandler(BaseHTTPRequestHandler):
    """HTTP handler that returns a redirect response."""

    request_count: int

    def do_POST(self) -> None:  # noqa: N802
        self.__class__.request_count += 1
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture()
def fake_webhook_url() -> Generator[str]:
    _RecordingHandler.received = []
    _RecordingHandler.headers_seen = []
    _RecordingHandler.request_count = 0

    srv = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/webhook"
    srv.shutdown()


@pytest.fixture()
def hanging_webhook_url() -> Generator[str]:
    _HangingHandler.request_count = 0

    srv = HTTPServer(("127.0.0.1", 0), _HangingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/webhook"
    srv.shutdown()


@pytest.fixture()
def redirect_webhook_url() -> Generator[str]:
    _RedirectHandler.request_count = 0

    srv = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/webhook"
    srv.shutdown()


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
    timeout_seconds: int = 5,
    notify_immediate: int = 8,
    allow_private_ips: bool = True,
    webhook_url: str = "",
    webhooks: list[NotificationWebhookConfig] | None = None,
) -> AppConfig:
    return AppConfig(
        notifications=NotificationsConfig(
            webhook_url=webhook_url,
            timeout_seconds=timeout_seconds,
            webhooks=webhooks or [],
        ),
        security=SecurityConfig(allow_private_ips=allow_private_ips),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                thresholds=ProfileThresholds(notify_immediate=notify_immediate),
            ),
        ],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="test"),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
    )


class TestGenericDigestNotifications:
    def test_legacy_send_digest_posts_expected_body(
        self, fake_webhook_url: str
    ) -> None:
        result = _make_result(
            items=[
                _make_item(score=10),
                _make_item(id="low", score=5, url="https://l"),
            ],
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
        assert body["type"] == "influx_digest"
        assert body["profile"] == "ai-robotics"
        assert body["stats"]["high_relevance"] == 1

    def test_dispatch_uses_legacy_generic_webhook_url(
        self, fake_webhook_url: str
    ) -> None:
        config = _make_config(webhook_url=fake_webhook_url)
        dispatch_notifications(
            _make_result(),
            config,
            kind=RunKind.MANUAL,
            run_id="run-1",
        )

        assert _RecordingHandler.request_count == 1
        assert _RecordingHandler.received[0]["type"] == "influx_digest"

    def test_generic_digest_webhook_supports_min_score_gate(
        self, fake_webhook_url: str
    ) -> None:
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="generic",
                    type="generic_digest",
                    url=fake_webhook_url,
                    min_score=9,
                )
            ]
        )
        result = _make_result(items=[_make_item(score=8)])
        dispatch_notifications(result, config, kind=RunKind.MANUAL, run_id="run-2")

        assert _RecordingHandler.request_count == 0


class TestAgentZeroNotifications:
    def test_agent_zero_message_async_article_payload(
        self, fake_webhook_url: str
    ) -> None:
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="agent-zero-inbox",
                    type="agent_zero_message_async",
                    url=fake_webhook_url,
                    event_mode="article",
                    context="ctx-123",
                    min_score=8,
                )
            ]
        )
        result = _make_result(
            items=[_make_item(score=9), _make_item(id="low", score=6, url="https://l")]
        )

        dispatch_notifications(result, config, kind=RunKind.MANUAL, run_id="run-3")

        assert _RecordingHandler.request_count == 1
        body = _RecordingHandler.received[0]
        assert body["context"] == "ctx-123"
        assert "Influx ingested a document." in body["text"]
        assert "Score: 9/10" in body["text"]

    def test_agent_zero_rfc_article_payload(
        self,
        fake_webhook_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AGENT_ZERO_RFC_PASSWORD", "rfc-secret")
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="agent-zero-rfc",
                    type="agent_zero_rfc_message",
                    url=fake_webhook_url,
                    event_mode="article",
                    context="InfluxIn",
                    rfc_module="usr.influx_rfc",
                    rfc_function="enqueue_message",
                    rfc_password_env="AGENT_ZERO_RFC_PASSWORD",
                    min_score=8,
                )
            ]
        )

        dispatch_notifications(
            _make_result(),
            config,
            kind=RunKind.MANUAL,
            run_id="rfc-1",
        )

        assert _RecordingHandler.request_count == 1
        body = _RecordingHandler.received[0]
        rfc_input = json.loads(body["rfc_input"])
        assert rfc_input["module"] == "usr.influx_rfc"
        assert rfc_input["function_name"] == "enqueue_message"
        assert rfc_input["kwargs"]["context"] == "InfluxIn"
        assert "Influx ingested a document." in rfc_input["kwargs"]["text"]
        expected_hash = hmac.new(
            b"rfc-secret",
            body["rfc_input"].encode(),
            hashlib.sha256,
        ).hexdigest()
        assert body["hash"] == expected_hash

    def test_missing_rfc_password_skips_delivery(
        self,
        fake_webhook_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="agent-zero-rfc",
                    type="agent_zero_rfc_message",
                    url=fake_webhook_url,
                    event_mode="article",
                    context="InfluxIn",
                    rfc_module="usr.influx_rfc",
                    rfc_function="enqueue_message",
                    rfc_password_env="MISSING_AGENT_ZERO_RFC_PASSWORD",
                    min_score=8,
                )
            ]
        )

        with caplog.at_level(logging.WARNING, logger="influx.notifications"):
            dispatch_notifications(
                _make_result(),
                config,
                kind=RunKind.MANUAL,
                run_id="rfc-2",
            )

        assert _RecordingHandler.request_count == 0
        assert any("missing auth token" in record.message for record in caplog.records)

    def test_agent_zero_toast_article_payload(self, fake_webhook_url: str) -> None:
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="agent-zero-toast",
                    type="agent_zero_notification_create",
                    url=fake_webhook_url,
                    event_mode="article",
                    min_score=8,
                )
            ]
        )

        dispatch_notifications(_make_result(), config, kind=RunKind.MANUAL, run_id="4")

        assert _RecordingHandler.request_count == 1
        body = _RecordingHandler.received[0]
        assert body["title"] == "Influx: Score 10/10"
        assert body["message"] == "RoboStream: Real-Time Robot Memory"
        assert body["priority"] == "high"


class TestOpenClawNotifications:
    def test_openclaw_agent_digest_payload_and_bearer_auth(
        self,
        fake_webhook_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("INFLUX_OPENCLAW_TOKEN", "secret-token")
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="openclaw-whatsapp",
                    type="openclaw_agent",
                    url=fake_webhook_url,
                    event_mode="digest",
                    auth_token_env="INFLUX_OPENCLAW_TOKEN",
                    deliver=True,
                    channel="whatsapp",
                    sender_name="Influx",
                )
            ]
        )

        dispatch_notifications(
            _make_result(), config, kind=RunKind.SCHEDULED, run_id="5"
        )

        assert _RecordingHandler.request_count == 1
        body = _RecordingHandler.received[0]
        headers = _RecordingHandler.headers_seen[0]
        assert body["name"] == "Influx"
        assert body["deliver"] is True
        assert body["channel"] == "whatsapp"
        assert "Influx run completed." in body["message"]
        assert headers["authorization"] == "Bearer secret-token"

    def test_openclaw_agent_includes_wake_mode(
        self,
        fake_webhook_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("INFLUX_OPENCLAW_TOKEN", "secret-token")
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="openclaw-whatsapp",
                    type="openclaw_agent",
                    url=fake_webhook_url,
                    event_mode="digest",
                    auth_token_env="INFLUX_OPENCLAW_TOKEN",
                    deliver=True,
                    wake_mode="now",
                    sender_name="Influx",
                )
            ]
        )

        dispatch_notifications(
            _make_result(), config, kind=RunKind.SCHEDULED, run_id="5b"
        )

        assert _RecordingHandler.request_count == 1
        body = _RecordingHandler.received[0]
        assert body["deliver"] is True
        assert body["wakeMode"] == "now"

    def test_missing_auth_token_skips_delivery(
        self,
        fake_webhook_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="openclaw",
                    type="openclaw_agent",
                    url=fake_webhook_url,
                    event_mode="digest",
                    auth_token_env="MISSING_OPENCLAW_TOKEN",
                )
            ]
        )

        with caplog.at_level(logging.WARNING, logger="influx.notifications"):
            dispatch_notifications(
                _make_result(),
                config,
                kind=RunKind.MANUAL,
                run_id="6",
            )

        assert _RecordingHandler.request_count == 0
        assert any("missing auth token" in record.message for record in caplog.records)


class TestNotificationDeliveryBehavior:
    def test_backfill_sends_no_request(self, fake_webhook_url: str) -> None:
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="generic",
                    type="generic_digest",
                    url=fake_webhook_url,
                )
            ]
        )
        post_run_webhook_hook(_make_result(), config, kind=RunKind.BACKFILL)
        assert _RecordingHandler.request_count == 0

    def test_timeout_logs_and_returns(
        self,
        hanging_webhook_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = _make_config(
            timeout_seconds=1,
            webhooks=[
                NotificationWebhookConfig(
                    name="generic",
                    type="generic_digest",
                    url=hanging_webhook_url,
                )
            ],
        )

        with caplog.at_level(logging.WARNING, logger="influx.notifications"):
            post_run_webhook_hook(_make_result(), config, kind=RunKind.MANUAL)

        assert _HangingHandler.request_count == 1
        assert any("delivery failed" in record.message for record in caplog.records)

    def test_webhook_failures_do_not_raise(self, hanging_webhook_url: str) -> None:
        config = _make_config(
            timeout_seconds=1,
            webhooks=[
                NotificationWebhookConfig(
                    name="generic",
                    type="generic_digest",
                    url=hanging_webhook_url,
                )
            ],
        )

        post_run_webhook_hook(_make_result(), config, kind=RunKind.MANUAL)

    def test_redirect_status_is_logged_as_failure(
        self,
        redirect_webhook_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = _make_config(
            webhooks=[
                NotificationWebhookConfig(
                    name="agent-zero-toast",
                    type="agent_zero_notification_create",
                    url=redirect_webhook_url,
                    event_mode="article",
                    min_score=8,
                )
            ]
        )

        with caplog.at_level(logging.WARNING, logger="influx.notifications"):
            dispatch_notifications(
                _make_result(),
                config,
                kind=RunKind.MANUAL,
                run_id="redirect-1",
            )

        assert _RedirectHandler.request_count == 1
        assert any(
            "unexpected HTTP status" in record.message for record in caplog.records
        )
