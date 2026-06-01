"""Inbox notification opt-in (Inbox v1 slice 2).

``"inbox"`` is a valid ``notify_on`` value; webhooks opt in to inbox-run
notifications by listing it.  Webhooks without it stay silent on inbox runs
(backwards-compatible with existing scheduled-only configs).
"""

from __future__ import annotations

from unittest.mock import patch

from influx.config import (
    AppConfig,
    NotificationsConfig,
    NotificationWebhookConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
)
from influx.coordinator import RunKind
from influx.notifications import ProfileRunResult, RunStats, dispatch_notifications


def _webhook(name: str, notify_on: list[str]) -> NotificationWebhookConfig:
    return NotificationWebhookConfig(
        name=name,
        type="generic_digest",
        url="https://hooks.example.com/x",
        notify_on=notify_on,  # type: ignore[arg-type]
        event_mode="digest",
    )


def _config(webhooks: list[NotificationWebhookConfig]) -> AppConfig:
    return AppConfig(
        profiles=[ProfileConfig(name="alpha")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        notifications=NotificationsConfig(webhooks=webhooks),
    )


def _result() -> ProfileRunResult:
    return ProfileRunResult(
        run_date="2026-06-01",
        profile="alpha",
        stats=RunStats(sources_checked=1, ingested=1),
        items=[],
    )


def test_inbox_run_only_notifies_opted_in_webhook() -> None:
    config = _config(
        [
            _webhook("inbox-hook", ["inbox"]),
            _webhook("scheduled-hook", ["scheduled"]),
        ]
    )
    with patch("influx.notifications._deliver_payload") as deliver:
        dispatch_notifications(_result(), config, kind=RunKind.INBOX)

    delivered = {call.args[0].name for call in deliver.call_args_list}
    assert delivered == {"inbox-hook"}


def test_scheduled_only_webhook_silent_on_inbox_run() -> None:
    """A pre-existing scheduled-only webhook stays silent (backwards-compat)."""
    config = _config([_webhook("scheduled-hook", ["scheduled"])])
    with patch("influx.notifications._deliver_payload") as deliver:
        dispatch_notifications(_result(), config, kind=RunKind.INBOX)
    deliver.assert_not_called()
