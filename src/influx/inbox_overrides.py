"""Submitter overrides on an InboxTask: ``force`` and ``tier`` (ADR 0002).

The contract half of the feature: reading and validating the two optional
metadata fields, the tier gates an overridden dispatch runs under, and the
``inbox_result.override`` payload.  Which profile hosts a forced item, and
the dispatch itself, stay with the orchestrator in :mod:`influx.inbox`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from influx.config import ProfileThresholds


class InvalidInboxOverride(ValueError):
    """A ``force`` / ``tier`` metadata value Influx does not accept."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class InboxOverrides:
    """Opt-in submitter overrides on an InboxTask; both default off.

    ``force`` writes the note even when no profile clears its threshold;
    ``tier="full"`` asks for full text and deep extraction whatever the score.
    """

    force: bool = False
    tier: Literal["full"] | None = None

    @property
    def requested(self) -> bool:
        return self.force or self.tier is not None


NO_OVERRIDES = InboxOverrides()


def parse_inbox_overrides(metadata: dict[str, Any]) -> InboxOverrides:
    """Read ``force`` / ``tier`` from task metadata; absent or ``null`` is off.

    Any other value raises :class:`InvalidInboxOverride`: a mistyped override
    must fail loudly rather than quietly get feed behaviour.
    """
    force = metadata.get("force")
    if force is not None and not isinstance(force, bool):
        raise InvalidInboxOverride("force", "force must be true or false")
    tier = metadata.get("tier")
    if tier is not None and tier != "full":
        raise InvalidInboxOverride("tier", 'tier must be "full"')
    return InboxOverrides(force=force is True, tier="full" if tier == "full" else None)


def effective_thresholds(
    base: ProfileThresholds, score: int, *, forced: bool, tier: str | None
) -> ProfileThresholds:
    """The tier gates one inbox dispatch runs under.

    An override lowers only its own gates, and only to the item's score, so
    the Cascade's ``score >= gate`` checks pass: ``forced`` opens Tier 1 for
    a below-threshold host, ``tier="full"`` opens Tiers 1–3.
    ``notify_immediate`` is never lowered, so an override is not an alert.
    """
    update: dict[str, int] = {}
    if forced or tier == "full":
        update["relevance"] = min(base.relevance, score)
    if tier == "full":
        update["full_text"] = min(base.full_text, score)
        update["deep_extract"] = min(base.deep_extract, score)
    return base.model_copy(update=update) if update else base


def override_block(
    overrides: InboxOverrides,
    *,
    forced_profile: str | None = None,
    forced: bool = False,
    tier_achieved: str | None = None,
) -> dict[str, Any]:
    """The ``inbox_result.override`` payload, so a forced note is
    distinguishable from one that passed on merit.

    ``forced_profile`` names the profile a below-threshold item was
    dispatched to; ``forced`` says whether that note was actually written.
    """
    return {
        "force_requested": overrides.force,
        "forced": forced,
        "forced_profile": forced_profile,
        "tier_requested": overrides.tier,
        "tier_achieved": tier_achieved,
    }


def tier_achieved(tags: list[str]) -> str:
    """``full`` (full text + Tier 3), ``full_text``, or ``summary``."""
    if "full-text" not in tags:
        return "summary"
    return "full" if "influx:deep-extracted" in tags else "full_text"
