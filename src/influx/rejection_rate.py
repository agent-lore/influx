"""Per-profile rejection-rate logging (FR-OBS-5, AC-M4-5, AC-10-D).

Maintains an in-memory per-profile run counter and filter-result tag
store.  Every ``feedback.recalibrate_after_runs`` runs for a given
profile, emits a structured JSON log line containing per-tag rejection
rates computed against user-rejected items in Lithos.

The counter and tag store are in-memory only — a process restart resets
both (FR-OBS-5 is informative only, per PRD §2.2).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from influx.config import AppConfig
    from influx.lithos_client import LithosClient

__all__ = [
    "on_run_complete",
    "record_filter_result",
    "reset",
]

logger = logging.getLogger(__name__)

# ── In-memory state (restart resets) ────────────────────────────────

# Per-profile run counter: profile_name → count since last emission.
_run_counters: dict[str, int] = {}

# Per-profile filter-result tags: profile_name → list of (title, tags).
# Accumulates across runs until the cadence triggers emission, then
# clears for that profile.
_filter_result_tags: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)


def record_filter_result(
    profile: str,
    title: str,
    tags: Iterable[str],
) -> None:
    """Record the tags from a single filter-result item.

    Called once per item yielded by the item provider during
    ``_run_profile_body``.  The tags are retained in-memory for
    rejection-rate computation at the next cadence boundary.
    """
    _filter_result_tags[profile].append((title, set(tags)))


async def on_run_complete(
    profile: str,
    *,
    config: AppConfig,
    client: LithosClient,
    sources_checked: int,
    ingested: int,
) -> None:
    """Increment run counter and emit rejection-rate log if at cadence.

    Also emits a per-run structured log with ``filtered`` and
    ``ingested`` counts (AC-M4-4).

    Parameters
    ----------
    profile:
        Profile name for this run.
    config:
        Loaded app config (reads ``feedback.recalibrate_after_runs``).
    client:
        Lithos client for fetching rejected titles at cadence.
    sources_checked:
        Total items yielded by the provider (before dedup/write).
    ingested:
        Items that resulted in a ``created`` or ``updated`` write.
    """
    # ── AC-M4-4: per-run filtered + ingested structured log ──
    filtered = sources_checked - ingested
    logger.info(
        json.dumps(
            {
                "event": "influx.run.stats",
                "profile": profile,
                "filtered": filtered,
                "ingested": ingested,
                "sources_checked": sources_checked,
            }
        ),
    )

    # ── Per-profile run counter ──
    cadence = config.feedback.recalibrate_after_runs
    if cadence <= 0:
        return

    _run_counters[profile] = _run_counters.get(profile, 0) + 1
    count = _run_counters[profile]

    if count % cadence != 0:
        return

    # ── Cadence reached: compute and emit rejection rates ──
    await _emit_rejection_rates(profile, config=config, client=client)

    # Clear the tag store for this profile after emission.
    _filter_result_tags[profile] = []


async def _emit_rejection_rates(
    profile: str,
    *,
    config: AppConfig,
    client: LithosClient,
) -> None:
    """Compute per-tag rejection rates and emit a structured JSON log."""
    from influx.feedback import fetch_rejection_titles

    tag_store = _filter_result_tags.get(profile, [])
    if not tag_store:
        logger.info(
            json.dumps(
                {
                    "event": "influx.rejection_rate",
                    "profile": profile,
                    "rejection_rates": {},
                    "note": "no filter results recorded since last emission",
                }
            ),
        )
        return

    # Fetch user-rejected titles from Lithos.
    rejected_titles: list[str] = []
    try:
        rejected_titles = await fetch_rejection_titles(
            client,
            profile=profile,
            limit=config.feedback.negative_examples_per_profile,
        )
    except Exception:
        logger.warning(
            "Failed to fetch rejection titles for profile %r; "
            "emitting rejection rates with empty rejection set",
            profile,
            exc_info=True,
        )

    rejected_set = set(rejected_titles)

    # Compute per-tag rejection rates.
    tag_total: dict[str, int] = defaultdict(int)
    tag_rejected: dict[str, int] = defaultdict(int)

    for title, tags in tag_store:
        for tag in tags:
            tag_total[tag] += 1
            if title in rejected_set:
                tag_rejected[tag] += 1

    rejection_rates: dict[str, float] = {}
    for tag in sorted(tag_total):
        total = tag_total[tag]
        rejected = tag_rejected.get(tag, 0)
        rejection_rates[tag] = round(rejected / total, 4) if total > 0 else 0.0

    logger.info(
        json.dumps(
            {
                "event": "influx.rejection_rate",
                "profile": profile,
                "rejection_rates": rejection_rates,
            }
        ),
    )


def reset() -> None:
    """Reset all in-memory state.  Used by tests."""
    _run_counters.clear()
    _filter_result_tags.clear()
