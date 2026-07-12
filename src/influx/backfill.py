"""Backfill request validation.

Backfill *execution* is just a Run with ``RunKind.BACKFILL`` — it flows
through the same ``run_profile`` → ``RunService`` path as any other run,
with the kind driving the backfill-specific gating downstream:

- Repair sweep is skipped (FR-REP-2).
- Webhook POST is skipped (FR-NOT-4).
- Cache-hit items are skipped entirely (FR-BF-2).
- ArXiv pacing is enforced by the arXiv fetcher (FR-BF-3).
- Same-profile serialisation is enforced by the coordinator (AC-M3-7).

There is therefore no backfill-specific run entry point; the only piece
that is genuinely backfill-shaped is validating the CLI's requested
date range, which lives here.
"""

from __future__ import annotations

__all__ = [
    "BackfillRangeError",
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
        raise BackfillRangeError("Supply exactly one of --days or (--from, --to)")
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
