"""OTEL metric instrument helpers for influx (issue #6).

Centralises the named instruments and their bounded label sets so call
sites do not have to remember instrument names, units, or descriptions.
Every helper returns the cached instrument from the module-level
:class:`~influx.telemetry.InfluxMeter` singleton — when OTEL is disabled
the meter returns the shared no-op instrument, so call sites pay zero
work in the disabled path (AC-10-A discipline extended to metrics).

Cardinality discipline
----------------------
Label values are bounded by construction:

* ``profile`` — known profile names from the loaded config.
* ``run_type`` — :class:`~influx.coordinator.RunKind` values.
* ``source`` — ``"arxiv"`` / ``"rss"``.
* ``status`` — Lithos write outcomes from
  :func:`~influx.lithos_client._parse_write_response`.
* ``decision`` — ``"pass"`` / ``"drop"``.
* ``kind`` — repair stage names or source-acquisition error kinds.
* ``tier`` — ``"1"`` / ``"3"``.
* ``outcome`` — ``"success"`` / ``"failure"`` / ``"degraded"``.

Per-item identifiers (``run_id``, ``note_id``, ``arxiv_id``,
``source_url``, ``title``) are **not** label values for any instrument
and must not be added without a fresh cardinality review.
"""

from __future__ import annotations

from typing import Any

from influx.telemetry import get_meter

__all__ = [
    "active_runs",
    "archive_missing",
    "articles_filtered",
    "articles_inspected",
    "cache_hits",
    "candidates_fetched",
    "lithos_writes",
    "llm_validation_failures",
    "repair_candidates",
    "run_completions",
    "run_duration",
    "run_starts",
    "slug_collision_dedup_recovery",
    "slug_collision_reclaimed",
    "slug_collision_unresolved",
    "source_acquisition_errors",
]


def run_starts() -> Any:
    """Counter incremented once when a run begins.

    Labels: ``profile``, ``run_type``.
    """
    return get_meter().counter(
        "influx_run_starts_total",
        description="Number of influx runs started.",
    )


def run_completions() -> Any:
    """Counter incremented once when a run reaches a terminal outcome.

    Labels: ``profile``, ``run_type``, ``outcome``
    (``success`` | ``failure`` | ``degraded``).
    """
    return get_meter().counter(
        "influx_run_completions_total",
        description="Number of influx runs that reached a terminal outcome.",
    )


def run_duration() -> Any:
    """Histogram of run wall-clock duration in seconds.

    Labels: ``profile``, ``run_type``.
    """
    return get_meter().histogram(
        "influx_run_duration_seconds",
        unit="s",
        description="Wall-clock duration of an influx run in seconds.",
    )


def active_runs() -> Any:
    """Up-down counter tracking currently in-flight runs.

    Labels: ``profile``.
    """
    return get_meter().up_down_counter(
        "influx_active_runs",
        description="Number of influx runs currently in flight.",
    )


def candidates_fetched() -> Any:
    """Counter of candidate items returned by source acquisition.

    Labels: ``profile``, ``source``.
    """
    return get_meter().counter(
        "influx_source_candidates_fetched_total",
        description="Candidate items returned by a source fetch.",
    )


def articles_filtered() -> Any:
    """Counter of items processed by the LLM filter.

    Labels: ``profile``, ``decision`` (``pass`` | ``drop``).
    """
    return get_meter().counter(
        "influx_articles_filtered_total",
        description="Items processed by the LLM filter, broken down by decision.",
    )


def articles_inspected() -> Any:
    """Counter of items the scheduler inspected (post-filter).

    Labels: ``profile``, ``source``.
    """
    return get_meter().counter(
        "influx_articles_inspected_total",
        description="Items inspected by the scheduler write loop.",
    )


def cache_hits() -> Any:
    """Counter of Lithos cache hits during the inspection loop.

    Labels: ``profile``, ``source``.
    """
    return get_meter().counter(
        "influx_cache_hits_total",
        description="Lithos cache hits during the scheduler write loop.",
    )


def lithos_writes() -> Any:
    """Counter of Lithos write attempts, broken down by outcome.

    Labels: ``profile``, ``source``, ``status`` (Lithos write status —
    ``created``, ``updated``, ``duplicate``, ``slug_collision``,
    ``version_conflict``, ``invalid_input``, ``content_too_large_skipped``,
    or any other status returned by ``lithos_write``).
    """
    return get_meter().counter(
        "influx_lithos_writes_total",
        description="Lithos write attempts broken down by outcome status.",
    )


def repair_candidates() -> Any:
    """Counter of repair-sweep candidates visited per stage.

    Labels: ``profile``, ``kind``
    (``archive`` | ``text_extraction`` | ``tier2`` | ``tier3``).
    """
    return get_meter().counter(
        "influx_repair_candidates_total",
        description="Repair-sweep candidates visited per stage.",
    )


def llm_validation_failures() -> Any:
    """Counter of LLM enrichment failures.

    Labels: ``profile``, ``tier`` (``1`` | ``3``).
    """
    return get_meter().counter(
        "influx_llm_validation_failures_total",
        description="LLM enrichment failures (Tier 1 or Tier 3).",
    )


def archive_missing() -> Any:
    """Counter of items tagged ``influx:archive-missing`` during a run.

    Labels: ``profile``, ``source``.
    """
    return get_meter().counter(
        "influx_archive_missing_total",
        description="Items tagged influx:archive-missing during a run.",
    )


def slug_collision_dedup_recovery() -> Any:
    """Counter of slug collisions recovered as duplicates (#31).

    Increments when a ``slug_collision`` envelope's squatter doc was
    actually the same paper as the incoming write — Lithos's URL or
    cache dedup missed it (typically because the squatter lacks a
    matching ``source_url`` or ``arxiv-id`` tag), and Influx routed
    the write through the ``duplicate`` path instead of creating a
    near-duplicate note.

    No labels: this is a low-volume signal that mostly tracks the
    quality of upstream dedup.  When non-zero in steady state, file
    a Lithos-side issue (see agent-lore/lithos#222).
    """
    return get_meter().counter(
        "influx_slug_collision_dedup_recovery_total",
        description=(
            "slug_collision events recovered as duplicates after squatter "
            "inspection (the squatter shares arxiv-id or source_url with "
            "the incoming write)"
        ),
    )


def slug_collision_reclaimed() -> Any:
    """Counter of slug collisions resolved by reclaiming an empty squatter (#31).

    Increments when ``slug_collision`` was caused by a stale residue
    (no tags, no source_url, empty body — typically an aborted prior
    write that committed a slug but never landed metadata).  Influx
    deletes the residue and re-issues the write.

    No labels: low-volume operational signal.  Sustained non-zero
    means there's a write-path bug somewhere upstream that's
    repeatedly leaving residues; investigate.
    """
    return get_meter().counter(
        "influx_slug_collision_reclaimed_total",
        description=(
            "slug_collision events resolved by deleting an empty stale "
            "squatter and re-issuing the write"
        ),
    )


def slug_collision_unresolved() -> Any:
    """Counter of slug collisions that exhausted the recovery chain (#31).

    A non-zero value here means even after squatter inspection +
    suffix retry the write was permanently dropped from this run.
    The scheduler also persists each unresolved entry to the local
    backlog file (``${state_dir}/unresolved-slug-collisions.jsonl``)
    so operators can intervene via
    ``./scripts/influx-diagnose.py slug-collision-backlog``.

    Labels: ``profile``, ``source``.
    """
    return get_meter().counter(
        "influx_slug_collision_unresolved_total",
        description=(
            "slug collisions that exhausted the recovery chain "
            "(inspect + reclaim + suffix retry) and dropped the write"
        ),
    )


def source_acquisition_errors() -> Any:
    """Counter of swallowed source-acquisition errors.

    Mirrors the ledger's ``source_acquisition_errors`` field (issue
    #20): every increment corresponds to one entry written through
    :func:`~influx.telemetry.record_source_acquisition_error`.

    Labels: ``profile``, ``source``, ``kind``
    (network-error kind from :class:`~influx.http_client.NetworkError`,
    e.g. ``"timeout"``, ``"ssrf"``, ``"http"``, ``"unknown"``).
    """
    return get_meter().counter(
        "influx_source_acquisition_errors_total",
        description="Swallowed source-acquisition errors per run.",
    )
