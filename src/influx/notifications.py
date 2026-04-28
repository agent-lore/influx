"""Webhook notification digest builder and sender (§11, FR-NOT-1..6).

Builds the JSON digest body for Agent Zero webhook POSTs.  Two shapes:

- **Non-zero-ingest** (§11.1): full digest with ``highlights``,
  ``all_ingested``, and ``stats.high_relevance``.
- **Zero-ingest / quiet** (§11.2): minimal digest with ``message``
  and no ``highlights``/``all_ingested``.

The digest builder is pure — it accepts a run result and config and
returns a JSON-serialisable ``dict``.  The HTTP sender POSTs the digest
via the guarded HTTP client from PRD 02 (SSRF guard applies).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HighlightItem:
    """One high-relevance item for the digest ``highlights`` array."""

    id: str
    title: str
    score: int
    tags: list[str]
    reason: str
    url: str
    related_in_lithos: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict matching §11.1."""
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
        """Return a JSON-serialisable dict."""
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
    """The data a profile run produces, consumed by the digest builder."""

    run_date: str  # YYYY-MM-DD
    profile: str
    stats: RunStats
    items: list[HighlightItem] = field(default_factory=list)


def build_digest(
    result: ProfileRunResult,
    *,
    notify_immediate_threshold: int,
) -> dict[str, Any]:
    """Build the webhook digest body (§11.1 / §11.2).

    Parameters
    ----------
    result:
        The profile-run outcome containing stats and per-item data.
    notify_immediate_threshold:
        The ``profiles.thresholds.notify_immediate`` value from config.
        Items with ``score >= notify_immediate_threshold`` appear in
        ``highlights``.  The threshold is **not** hardcoded — it comes
        from config (AC-X-1, AC-05-I).

    Returns
    -------
    dict
        A JSON-serialisable dict matching §11.1 (non-zero ingest) or
        §11.2 (zero ingest / quiet run).
    """
    if result.stats.ingested == 0:
        return _build_quiet_digest(result)
    return _build_full_digest(result, notify_immediate_threshold)


def _build_quiet_digest(result: ProfileRunResult) -> dict[str, Any]:
    """§11.2 quiet-run digest — no highlights or all_ingested."""
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
    """§11.1 full digest with highlights and all_ingested."""
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
        "highlights": [h.to_dict() for h in highlights],
        "all_ingested": [i.to_dict() for i in all_ingested],
    }


def send_digest(
    digest: dict[str, Any],
    *,
    webhook_url: str,
    timeout_seconds: int | None = None,
    allow_private_ips: bool = False,
) -> None:
    """POST *digest* JSON to *webhook_url* via the guarded HTTP client.

    - When *webhook_url* is empty, silently returns (FR-NOT-5, AC-05-J).
    - Uses the SSRF guard from PRD 02 (FR-NOT-1).
    - Timeout is read from ``notifications.timeout_seconds`` in config;
      no retry on failure (FR-NOT-1).
    - ``timeout_seconds`` defaults to ``None``; when omitted it is
      resolved from the pydantic
      :class:`~influx.config.NotificationsConfig` field default so the
      only place this tunable lives is config-parsing code (AC-X-1).
      Production callers pass the loaded config value explicitly.
    - Failures (timeout, 5xx, network errors) are logged but do NOT
      raise — the caller is not interrupted.
    """
    if not webhook_url:
        return

    from influx.config import NotificationsConfig
    from influx.http_client import guarded_post_json

    if timeout_seconds is None:
        timeout_seconds = NotificationsConfig().timeout_seconds

    try:
        status = guarded_post_json(
            webhook_url,
            digest,
            allow_private_ips=allow_private_ips,
            timeout_seconds=timeout_seconds,
        )
        if status >= 400:
            logger.warning(
                "Webhook POST to %s returned HTTP %d",
                webhook_url,
                status,
            )
        else:
            logger.info("Webhook digest sent to %s (HTTP %d)", webhook_url, status)
    except Exception:
        logger.warning(
            "Webhook POST to %s failed",
            webhook_url,
            exc_info=True,
        )
