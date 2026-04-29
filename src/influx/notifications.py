"""Outbound notification payload builders and typed webhook dispatch.

Influx emits notification events after completed non-backfill runs.
Notifications are configured as a list of typed webhook sinks under
``[notifications]``. Each sink can receive either a full run digest or
per-article events.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from influx.config import (
    AppConfig,
    NotificationsConfig,
    NotificationWebhookConfig,
    ProfileThresholds,
)
from influx.coordinator import RunKind

logger = logging.getLogger(__name__)

__all__ = [
    "HighlightItem",
    "IngestedItem",
    "ProfileRunResult",
    "RunStats",
    "build_digest",
    "configured_webhooks",
    "dispatch_notifications",
    "send_digest",
]


@dataclass(frozen=True)
class HighlightItem:
    """One ingested item that can appear in digests or article notifications."""

    id: str
    title: str
    score: int
    tags: list[str]
    reason: str
    url: str
    related_in_lithos: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict matching the digest schema."""
        return {
            "id": self.id,
            "title": self.title,
            "score": self.score,
            "tags": list(self.tags),
            "reason": self.reason,
            "url": self.url,
            "related_in_lithos": list(self.related_in_lithos),
        }


@dataclass(frozen=True)
class IngestedItem:
    """One ingested item for the ``all_ingested`` array."""

    id: str
    title: str
    score: int
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "score": self.score,
            "url": self.url,
        }


@dataclass(frozen=True)
class RunStats:
    """Aggregate stats for a single profile run."""

    sources_checked: int
    ingested: int


@dataclass(frozen=True)
class ProfileRunResult:
    """The data a profile run produces, consumed by notification dispatch."""

    run_date: str  # YYYY-MM-DD
    profile: str
    stats: RunStats
    items: list[HighlightItem] = field(default_factory=list)


def build_digest(
    result: ProfileRunResult,
    *,
    notify_immediate_threshold: int,
) -> dict[str, Any]:
    """Build the run-digest payload used by ``generic_digest`` sinks."""
    if result.stats.ingested == 0:
        return _build_quiet_digest(result)
    return _build_full_digest(result, notify_immediate_threshold)


def _build_quiet_digest(result: ProfileRunResult) -> dict[str, Any]:
    return {
        "type": "influx_digest",
        "run_date": result.run_date,
        "profile": result.profile,
        "stats": {
            "sources_checked": result.stats.sources_checked,
            "ingested": 0,
        },
        "message": "No new relevant content found today.",
    }


def _build_full_digest(
    result: ProfileRunResult,
    notify_immediate_threshold: int,
) -> dict[str, Any]:
    highlights = [
        item for item in result.items if item.score >= notify_immediate_threshold
    ]
    all_ingested = [
        IngestedItem(
            id=item.id,
            title=item.title,
            score=item.score,
            url=item.url,
        )
        for item in result.items
    ]
    return {
        "type": "influx_digest",
        "run_date": result.run_date,
        "profile": result.profile,
        "stats": {
            "sources_checked": result.stats.sources_checked,
            "ingested": result.stats.ingested,
            "high_relevance": len(highlights),
        },
        "highlights": [item.to_dict() for item in highlights],
        "all_ingested": [item.to_dict() for item in all_ingested],
    }


def configured_webhooks(
    notifications: NotificationsConfig,
) -> list[NotificationWebhookConfig]:
    """Return explicit webhook sinks, or a legacy generic-digest fallback."""
    if notifications.webhooks:
        return list(notifications.webhooks)
    if not notifications.webhook_url:
        return []
    return [
        NotificationWebhookConfig(
            name="legacy-generic-digest",
            type="generic_digest",
            url=notifications.webhook_url,
        )
    ]


def dispatch_notifications(
    result: ProfileRunResult,
    config: AppConfig,
    *,
    kind: RunKind,
    run_id: str | None = None,
) -> None:
    """Best-effort fan-out of typed notifications for a completed run."""
    webhooks = configured_webhooks(config.notifications)
    if not webhooks:
        return
    profile_cfg = next(
        (profile for profile in config.profiles if profile.name == result.profile),
        None,
    )
    notify_immediate_threshold = (
        profile_cfg.thresholds.notify_immediate
        if profile_cfg is not None
        else ProfileThresholds().notify_immediate
    )

    for webhook in webhooks:
        if not webhook.enabled or kind.value not in webhook.notify_on:
            continue
        if webhook.event_mode == "digest":
            if _digest_is_below_threshold(result, webhook.min_score):
                continue
            payload = _build_payload(
                webhook,
                result,
                item=None,
                notify_immediate_threshold=notify_immediate_threshold,
            )
            _deliver_payload(
                webhook,
                payload,
                config=config,
                profile=result.profile,
                kind=kind,
                run_id=run_id,
                item_id=None,
            )
            continue

        for item in result.items:
            if webhook.min_score is not None and item.score < webhook.min_score:
                continue
            payload = _build_payload(
                webhook,
                result,
                item=item,
                notify_immediate_threshold=notify_immediate_threshold,
            )
            _deliver_payload(
                webhook,
                payload,
                config=config,
                profile=result.profile,
                kind=kind,
                run_id=run_id,
                item_id=item.id,
            )


def send_digest(
    digest: dict[str, Any],
    *,
    webhook_url: str,
    timeout_seconds: int | None = None,
    allow_private_ips: bool = False,
) -> None:
    """Backward-compatible generic digest sender used by older tests/configs."""
    if not webhook_url:
        return

    webhook = NotificationWebhookConfig(
        name="legacy-generic-digest",
        type="generic_digest",
        url=webhook_url,
    )
    _deliver_json_payload(
        webhook,
        digest,
        timeout_seconds=timeout_seconds
        if timeout_seconds is not None
        else NotificationsConfig().timeout_seconds,
        allow_private_ips=allow_private_ips,
        profile=str(digest.get("profile", "")),
        kind=None,
        run_id=None,
        item_id=None,
    )


def _build_payload(
    webhook: NotificationWebhookConfig,
    result: ProfileRunResult,
    *,
    item: HighlightItem | None,
    notify_immediate_threshold: int,
) -> dict[str, Any]:
    if webhook.type == "generic_digest":
        return build_digest(
            result,
            notify_immediate_threshold=notify_immediate_threshold,
        )
    if webhook.type == "agent_zero_message_async":
        return {
            "text": _build_message_text(result, item=item),
            "context": webhook.context,
        }
    if webhook.type == "agent_zero_notification_create":
        assert item is not None
        return {
            "type": "info",
            "priority": "high" if item.score >= 9 else "normal",
            "title": f"Influx: Score {item.score}/10",
            "message": item.title,
            "detail": f"{result.profile} | {item.url}",
            "display_time": 8,
        }
    if webhook.type == "openclaw_agent":
        payload: dict[str, Any] = {
            "message": _build_message_text(result, item=item),
            "name": webhook.sender_name,
            "deliver": webhook.deliver,
        }
        if webhook.channel:
            payload["channel"] = webhook.channel
        return payload
    raise ValueError(f"Unsupported webhook type: {webhook.type}")


def _build_message_text(
    result: ProfileRunResult,
    *,
    item: HighlightItem | None,
) -> str:
    if item is None:
        lines = [
            "Influx run completed.",
            f"Profile: {result.profile}",
            f"Run date: {result.run_date}",
            f"Sources checked: {result.stats.sources_checked}",
            f"Ingested: {result.stats.ingested}",
        ]
        if result.items:
            lines.append("Highlights:")
            lines.extend(
                f"- [{candidate.score}/10] {candidate.title} ({candidate.url})"
                for candidate in result.items
            )
        else:
            lines.append("No new relevant content found.")
        return "\n".join(lines)

    lines = [
        "Influx ingested a document.",
        f"Profile: {result.profile}",
        f"Run date: {result.run_date}",
        f"Title: {item.title}",
        f"Score: {item.score}/10",
        f"URL: {item.url}",
    ]
    if item.reason:
        lines.append(f"Reason: {item.reason}")
    if item.tags:
        lines.append(f"Tags: {', '.join(item.tags)}")
    if item.related_in_lithos:
        related_titles = [
            rel.get("title", rel.get("id", ""))
            for rel in item.related_in_lithos
            if rel.get("title") or rel.get("id")
        ]
        if related_titles:
            lines.append(f"Related in Lithos: {', '.join(related_titles)}")
    return "\n".join(lines)


def _digest_is_below_threshold(
    result: ProfileRunResult,
    min_score: int | None,
) -> bool:
    if min_score is None:
        return False
    if not result.items:
        return True
    return max(item.score for item in result.items) < min_score


def _deliver_payload(
    webhook: NotificationWebhookConfig,
    payload: dict[str, Any],
    *,
    config: AppConfig,
    profile: str,
    kind: RunKind,
    run_id: str | None,
    item_id: str | None,
) -> None:
    _deliver_json_payload(
        webhook,
        payload,
        timeout_seconds=config.notifications.timeout_seconds,
        allow_private_ips=config.security.allow_private_ips,
        profile=profile,
        kind=kind.value,
        run_id=run_id,
        item_id=item_id,
    )


def _deliver_json_payload(
    webhook: NotificationWebhookConfig,
    payload: dict[str, Any],
    *,
    timeout_seconds: int,
    allow_private_ips: bool,
    profile: str,
    kind: str | None,
    run_id: str | None,
    item_id: str | None,
) -> None:
    from influx.http_client import guarded_post_json

    extra = {
        "webhook_name": webhook.name,
        "webhook_type": webhook.type,
        "event_mode": webhook.event_mode,
        "profile": profile,
        "run_kind": kind or "",
        "run_id": run_id or "",
        "item_id": item_id or "",
    }

    headers = _build_auth_headers(webhook)
    if headers is None:
        logger.warning("notification webhook skipped: missing auth token", extra=extra)
        return

    logger.info("notification webhook dispatch started", extra=extra)
    try:
        status = guarded_post_json(
            webhook.url,
            payload,
            headers=headers,
            allow_private_ips=allow_private_ips,
            timeout_seconds=timeout_seconds,
        )
        if status >= 400:
            logger.warning(
                "notification webhook returned HTTP error",
                extra={**extra, "status_code": status},
            )
        else:
            logger.info(
                "notification webhook delivered",
                extra={**extra, "status_code": status},
            )
    except Exception:
        logger.warning(
            "notification webhook delivery failed",
            extra=extra,
            exc_info=True,
        )


def _build_auth_headers(
    webhook: NotificationWebhookConfig,
) -> dict[str, str] | None:
    if not webhook.auth_token_env:
        return {}
    token = os.environ.get(webhook.auth_token_env, "")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}
