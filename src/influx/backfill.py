"""Backfill orchestration module.

Houses the backfill-specific run flow, range validation, estimator
integration, and the :func:`run_backfill` entry point that drives
``run_profile(kind=BACKFILL)`` with the proper ``run_range``.

Cache-lookup skip (FR-BF-2): items already ingested are skipped
in ``run_profile`` when ``kind == RunKind.BACKFILL`` — the write
attempt is elided entirely so that large backfills avoid redundant
network traffic.

ArXiv pacing (FR-BF-3): the arXiv item provider sleeps for
``ResilienceConfig.arxiv_request_min_interval_seconds`` before each
backfill fetch when ``kind == RunKind.BACKFILL`` so a multi-day
backfill does not burst against the arXiv API.  The provider also
threads the resolved :class:`~influx.sources.arxiv.BackfillRange`
into ``fetch_arxiv``, replacing the standard lookback window with
the requested historical bounds.

Same-profile serialisation (AC-M3-7): enforced by the coordinator
— ``POST /backfills`` acquires the profile lock before launching
``run_backfill``, and the scheduler fire path does the same.
"""

from __future__ import annotations

from typing import Any

from influx.config import AppConfig
from influx.coordinator import RunKind
from influx.http_api import estimate_backfill_items
from influx.notifications import ProfileRunResult
from influx.scheduler import ItemProvider, run_profile

__all__ = [
    "estimate_backfill_items",
    "run_backfill",
    "validate_backfill_range",
]


class BackfillRangeError(Exception):
    """Raised when the backfill range inputs are invalid."""


def validate_backfill_range(
    *,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, str | int]:
    """Validate and normalise backfill range inputs into a ``run_range`` dict.

    Enforces mutual exclusivity between ``--days N`` and
    ``--from YYYY-MM-DD --to YYYY-MM-DD`` (FR-BF-1).

    Returns
    -------
    dict[str, str | int]
        A ``run_range`` dict suitable for ``run_profile``.

    Raises
    ------
    BackfillRangeError
        If the inputs are invalid (both forms supplied, neither supplied,
        or incomplete date range).
    """
    has_days = days is not None
    has_range = date_from is not None or date_to is not None

    if has_days and has_range:
        raise BackfillRangeError(
            "Supply exactly one of --days or (--from, --to), not both"
        )
    if not has_days and not has_range:
        raise BackfillRangeError(
            "Supply exactly one of --days or (--from, --to)"
        )
    if has_range and (date_from is None or date_to is None):
        raise BackfillRangeError(
            "Both --from and --to are required when using date range"
        )

    run_range: dict[str, str | int] = {}
    if days is not None:
        run_range["days"] = days
    else:
        assert date_from is not None and date_to is not None
        run_range["from"] = date_from
        run_range["to"] = date_to

    return run_range


async def run_backfill(
    profile: str,
    *,
    run_range: dict[str, str | int],
    config: AppConfig,
    item_provider: ItemProvider | None = None,
    probe_loop: Any | None = None,
) -> ProfileRunResult | None:
    """Execute a backfill run for a single profile.

    Delegates to :func:`~influx.scheduler.run_profile` with
    ``kind=RunKind.BACKFILL``.  The run_profile function handles
    backfill-specific gating:

    - Repair sweep is skipped (FR-REP-2).
    - Webhook POST is skipped (FR-NOT-4).
    - Cache-hit items are skipped entirely (FR-BF-2) — no write
      attempt is made for items that ``lithos_cache_lookup`` reports
      as already ingested.
    - ArXiv pacing is enforced by the arXiv fetcher (FR-BF-3).
    - Same-profile serialisation is enforced by the coordinator
      (AC-M3-7).

    Parameters
    ----------
    profile:
        Profile name from the loaded config.
    run_range:
        Date-range dict (e.g. ``{"days": 7}`` or
        ``{"from": "2026-04-01", "to": "2026-04-08"}``).
    config:
        Loaded :class:`~influx.config.AppConfig`.
    item_provider:
        Optional override for the item provider.
    probe_loop:
        Optional probe loop for readiness tracking.
    """
    return await run_profile(
        profile,
        RunKind.BACKFILL,
        run_range=run_range,
        config=config,
        item_provider=item_provider,
        probe_loop=probe_loop,
    )
