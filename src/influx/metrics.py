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
* ``outcome`` — for run metrics, ``"success"`` / ``"failure"`` /
  ``"degraded"``.  :func:`inbox_items_processed` reuses the ``outcome``
  attribute name with a *distinct* bounded enum (see its docstring).
* ``phase`` — inbox task-lifecycle stage: ``"list"`` / ``"claim"`` /
  ``"update"`` / ``"complete"`` (:func:`inbox_task_call_failures`).

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
    "archive_policy_failures",
    "articles_filtered",
    "articles_inspected",
    "cache_hits",
    "cache_hits_via_url_fallback",
    "candidates_fetched",
    "dedup_lookup_errors",
    "lithos_writes",
    "llm_validation_failures",
    "repair_candidates",
    "run_completions",
    "run_duration",
    "ingestion_stalls",
    "rss_items_rejected_invalid_url",
    "run_starts",
    "runs_skipped",
    "slug_collision_dedup_recovery",
    "slug_collision_reclaimed",
    "slug_collision_unresolved",
    "slug_collision_url_recovery",
    "empty_source_writes",
    "inbox_tick_started",
    "inbox_tasks_listed",
    "inbox_tasks_claimed",
    "inbox_items_processed",
    "inbox_task_call_failures",
    "source_acquisition_errors",
    "source_cooldown_skips",
    "summary_thin_drops",
    "tier3_fallbacks",
]


def run_starts() -> Any:
    """Counter incremented once when a run begins.

    Labels: ``profile``, ``run_type``.
    """
    return get_meter().counter(
        "influx_run_starts_total",
        description="Number of influx runs started.",
    )


def inbox_tick_started() -> Any:
    """Counter incremented once per inbox poll tick that runs.

    No labels.  See ``docs/plans/inbox.md`` §13.5.
    """
    return get_meter().counter(
        "influx_inbox_tick_started_total",
        description="Number of inbox poll ticks started.",
    )


def inbox_tasks_listed() -> Any:
    """Counter of InboxTasks returned by ``lithos_task_list`` per tick.

    No labels.
    """
    return get_meter().counter(
        "influx_inbox_tasks_listed_total",
        description="InboxTasks returned by lithos_task_list across ticks.",
    )


def inbox_tasks_claimed() -> Any:
    """Counter of InboxTasks successfully claimed for processing.

    No labels.
    """
    return get_meter().counter(
        "influx_inbox_tasks_claimed_total",
        description="InboxTasks claimed by the inbox tick.",
    )


def inbox_items_processed() -> Any:
    """Counter of inbox items processed, by terminal outcome.

    Labels: ``outcome`` — one of ``ingested`` | ``filtered_out`` |
    ``cache_hit`` | ``profile_busy_skipped`` | ``error`` |
    ``invalid_submission`` | ``invalid_source_tag`` | ``pdf_rejected``.  The
    last three (#212) cover validation-terminal completions so a bad-submission
    rate is visible in metrics rather than only in logs.
    """
    return get_meter().counter(
        "influx_inbox_items_processed_total",
        description="Inbox items processed, labelled by terminal outcome.",
    )


def inbox_task_call_failures() -> Any:
    """Counter of failed Lithos task-lifecycle calls in the inbox tick (#212).

    Labels: ``phase`` (``list`` | ``claim`` | ``update`` | ``complete``).
    Lets an operator distinguish "claims are failing" from "queue is backing
    up" without log-spelunking.  Severity differs by phase: ``update``
    failures are non-fatal (the task still completes; only the structured
    ``inbox_result`` metadata is lost), whereas ``complete`` failures leave the
    task open for a later tick to re-claim.
    """
    return get_meter().counter(
        "influx_inbox_task_call_failures_total",
        description="Failed Lithos task-lifecycle calls in the inbox tick.",
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


def rss_items_rejected_invalid_url() -> Any:
    """Counter of RSS items rejected pre-acquisition for invalid URL (#131).

    Bumped from :func:`influx.sources.rss.parse_feed` when an entry's
    ``link`` fails :func:`influx.urls.classify_article_url` — typically
    upstream-malformed URLs such as ``http://localhost:5174/`` from a
    misconfigured feed.

    Labels: ``profile``, ``source`` (always ``"rss"``), ``reason``
    (``malformed`` | ``scheme`` | ``no_host`` | ``loopback`` |
    ``link_local`` | ``private`` | ``multicast``).
    """
    return get_meter().counter(
        "influx_rss_items_rejected_invalid_url_total",
        description=(
            "RSS items rejected pre-acquisition because the entry link "
            "failed syntactic URL validation."
        ),
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


def cache_hits_via_url_fallback() -> Any:
    """Counter of pre-write dedup cache hits resolved by source-URL fallback (#128).

    Increments when the primary ``compose_dedup_query``-based cache
    lookup misses but a follow-up exact-``source_url`` lookup hits —
    i.e. a note for the same URL is already in Lithos but its title
    or first-sentence abstract drifted enough for the text query to
    miss.  Sustained non-zero in steady state indicates either Lithos
    server-side dedup needs strengthening or upstream feeds are
    rewriting titles/summaries between runs.

    Labels: ``profile``, ``source``.
    """
    return get_meter().counter(
        "influx_cache_hits_via_url_fallback_total",
        description=(
            "pre-write dedup cache hits that required a source_url "
            "fallback lookup after the primary title-based query missed"
        ),
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


def tier3_fallbacks() -> Any:
    """Counter of Tier 3 extraction failures classified by impact (#151).

    Tier 3 sits at the top of the cascade and frequently fails on
    long-tail papers; many of those failures are harmless when Tier 1
    + Tier 2 already produced a usable note.  This instrument
    separates those harmless fallbacks from materially-degraded
    failures so dashboards and alerts can target the latter.

    Increments alongside :func:`llm_validation_failures` (``tier="3"``)
    on every Tier 3 failure; the ``fallback_kind`` label distinguishes
    ``"harmless"`` (Tier 1 produced a summary AND Tier 2 produced full
    text) from ``"degraded"`` (Tier 1 also failed).

    Labels: ``profile``, ``fallback_kind`` (``"harmless"`` |
    ``"degraded"``).
    """
    return get_meter().counter(
        "influx_tier3_fallbacks_total",
        description=(
            "Tier 3 extraction failures split by whether lower tiers "
            "produced acceptable note content (issue #151)."
        ),
    )


def archive_missing() -> Any:
    """Counter of items tagged ``influx:archive-missing`` during a run.

    Bumped by the source adapters *after* the issue #166 thin-summary
    suppression check returns the item — items that the suppression
    rule drops never reach the write loop and so never receive the
    tag, and are accounted for separately by
    :func:`summary_thin_drops`.

    Labels: ``profile``, ``source``.
    """
    return get_meter().counter(
        "influx_archive_missing_total",
        description="Items tagged influx:archive-missing during a run.",
    )


def summary_thin_drops() -> Any:
    """Counter of items suppressed by the thin-summary rule (#166).

    Increments once per item the source adapter drops because the
    archive fetch did not produce a body AND the feed-provided
    summary (after extraction fallback) was thin per
    :func:`influx.thin_summary.is_thin_summary`.

    Distinct from :func:`archive_missing` and
    :func:`archive_policy_failures` so dashboards can pivot
    suppressions independently of archive-failure counts — the dropped
    items never reach the Lithos write loop and so never receive an
    ``influx:archive-missing`` tag.  Per the issue #166 review,
    ``archive_missing`` / ``archive_policy_failures`` are deferred past
    the suppression decision in the source adapters so they only count
    items that actually got written with the corresponding tag; this
    counter and the archive-failure counters therefore partition the
    set of archive-not-OK items rather than double-counting them.
    Also deliberately excluded from ``archive_failures_total`` /
    ``source_acquisition_errors`` / ``archive_acquisition`` degraded
    reasons in the run ledger.

    Labels:

    - ``profile`` — profile name.
    - ``source`` — the source-adapter identity, matching the existing
      convention used by :func:`archive_policy_failures`: for arXiv
      this is the literal ``"arxiv"``; for RSS this is the per-feed
      ``source_tag`` configured under ``[[profiles.sources.rss]]``,
      which defaults to ``"rss"`` but operators may override to
      e.g. ``"blog"`` / ``"tech-news"`` for cardinality-friendly
      per-feed-class dashboards.
    - ``failure_kind`` — the archive failure_kind that triggered the
      suppression: ``http_404`` / ``timeout`` / ``http_5xx`` /
      ``blocked`` / ``rate_limited`` / ``missing_by_policy`` /
      ``non_html_source`` / ``unsupported`` / ``oversize`` /
      ``content_type_mismatch`` / ``write`` / ``ssrf`` / ``dns`` /
      ``network`` / ``terminal`` (arXiv terminal-flipped) / ``unknown``.
    - ``rule`` — ``length`` | ``title_equality`` | ``boilerplate``,
      the thin-summary rule that fired first.
    """
    return get_meter().counter(
        "influx_summary_thin_drops_total",
        description=(
            "Items suppressed by the thin-summary rule because their "
            "archive fetch failed and the feed-provided summary was "
            "too thin to keep (issue #166)."
        ),
    )


def empty_source_writes() -> Any:
    """Counter of notes written despite having no usable source (#189).

    Increments once per note the source builders write whose ``source:``
    tag is blank AND whose URL is not one a source can be inferred from
    (a non-arxiv link) — the would-be ``influx:source-invalid`` zombie
    population that a #187-class metadata loss would later terminalise.

    Observe-only: the note is **always still written** (these items are
    often legitimate content with a working link but no reconstructable
    feed-slug tag), so this counts *writes*, not drops.  Deliberately
    excluded from ``archive_failures_total`` / ``source_acquisition_errors``
    and any degraded run reason — a quality/metadata signal, mirroring
    :func:`summary_thin_drops`.

    Labels:

    - ``profile`` — profile name.
    - ``source`` — the source-adapter identity, matching the convention
      used by :func:`summary_thin_drops`: for arXiv the literal
      ``"arxiv"`` (this never fires there — the abstract carries a
      ``source:arxiv`` tag and an arxiv URL); for RSS the per-feed
      ``source_tag``, falling back to ``"rss"`` when that tag is the
      empty value that triggered the count.
    - ``reason`` — why the source was unusable; currently the single
      value ``"no_usable_source"`` (blank tag and no arxiv-inferable
      URL), carried as a label so future narrower reasons can pivot.
    """
    return get_meter().counter(
        "influx_empty_source_writes_total",
        description=(
            "Notes written despite having no usable source tag or "
            "inferable source URL (issue #189, observe-only)."
        ),
    )


def archive_policy_failures() -> Any:
    """Counter of archive failures classified by domain-aware policy (#149).

    Increments alongside :func:`archive_missing` whenever
    :class:`~influx.archive_policy.ArchivePolicy` reclassifies an
    archive failure into a structural shape (``blocked`` /
    ``rate_limited`` / ``missing_by_policy``).  ``kind="generic"`` is
    reserved for the (large, expected) baseline of unclassified
    failures so dashboards can compute the ratio without subtracting
    series.

    Per the issue #166 review this counter is deferred past the
    thin-summary suppression check (the source adapters bump it only
    for items that actually get written with the policy tag), keeping
    it in lock-step with :func:`archive_missing`.  Items dropped by the
    suppression rule are accounted for by :func:`summary_thin_drops`.

    Labels: ``profile``, ``source``, ``kind``
    (``"blocked"`` | ``"rate_limited"`` | ``"missing_by_policy"`` |
    ``"http_403"`` | ``"http_429"`` | ``"http_404"`` | ``"http_4xx"`` |
    ``"http_5xx"`` | ``"oversize"`` | ``"timeout"`` | ``"ssrf"`` |
    ``"dns"`` | ``"network"`` | ``"content_type_mismatch"`` |
    ``"write"`` | ``"unknown"``).
    """
    return get_meter().counter(
        "influx_archive_policy_failures_total",
        description=(
            "Archive download failures classified by domain-aware policy; "
            "the ``kind`` label distinguishes blocked / rate-limited / "
            "missing-by-policy from generic HTTP / network shapes (issue "
            "#149)."
        ),
    )


def ingestion_stalls() -> Any:
    """Counter of runs flagged with a stall ``degraded_reasons`` value.

    Combines the #36 ``ingestion_stall``, #50 ``fetch_stall``, #85
    ``filter_stall``, and #85-review ``filter_error`` signals on a
    single instrument, split by the ``reason`` label so dashboards
    can alert on each independently.  The three stall reasons
    (``ingestion_stall``/``fetch_stall``/``filter_stall``) are mutually
    exclusive on a single run; ``filter_error`` is mutually exclusive
    with ``filter_stall`` but orthogonal to the rest.

    ``reason="ingestion_stall"`` (#36): increments once per scheduled
    run that completes with ``ingested == 0 AND sources_checked > 0``
    AND the immediately prior scheduled run for the same profile
    also matched that shape — TWO consecutive
    inspect-something-but-write-nothing sweeps.  Typical causes: every
    candidate hits ``slug_collision`` / ``duplicate``, all writes are
    cache-merge with no new content, etc.

    ``reason="fetch_stall"`` (#50): increments once per scheduled run
    that completes with ``fetched_total == 0`` (no source returned
    any items at all) AND the immediately prior scheduled run for
    the same profile also saw the same shape AND the profile has
    historically seen ``sources_checked > 0`` within the recent
    ledger window.  Typical cause: too-narrow ``lookback_days`` or
    an upstream feed shape change leaving the fetch path empty.

    ``reason="filter_stall"`` (#85): increments once per scheduled
    run that completes with ``fetched_total > 0 AND
    sources_checked == 0`` (sources fetched normally and the LLM
    filter ran cleanly but rejected every candidate) AND the
    immediately prior scheduled run for the same profile also
    matched that shape AND the profile has historically seen
    ``sources_checked > 0`` within the recent ledger window.
    Typical cause: profile description drift, filter prompt
    regression, or a ``min_score_in_results`` set too high.

    ``reason="filter_error"`` (#85, post-review): increments once per
    run (any kind, scheduled or otherwise) where the LLM filter
    scorer raised :class:`FilterScorerError` at least once —
    transport, parse, or provider failure.  Single-run signal: no
    consecutive-runs gate, no historical ratchet, no kind gate.
    Distinct from ``filter_stall`` (clean scorer rejected all);
    routes operator attention at provider config / model
    availability / response schema.

    Labels: ``profile``, ``reason``
    (``"ingestion_stall"`` | ``"fetch_stall"`` |
    ``"filter_stall"`` | ``"filter_error"``).
    """
    return get_meter().counter(
        "influx_ingestion_stall_runs_total",
        description=(
            "runs flagged with a stall reason — ingestion_stall (two "
            "consecutive scheduled runs inspected items but ingested "
            "zero), fetch_stall (two consecutive scheduled runs "
            "fetched zero items despite prior non-zero history), "
            "filter_stall (two consecutive scheduled runs fetched "
            "items but the filter cleanly rejected every candidate, "
            "despite prior non-zero history), or filter_error (any "
            "run whose LLM filter scorer raised FilterScorerError "
            "at least once)"
        ),
    )


def runs_skipped() -> Any:
    """Counter of runs short-circuited before any work was done (#40).

    Increments once per cron-tick run that the Lithos circuit breaker
    refused.  ``reason`` distinguishes skip causes so future short-
    circuit paths (e.g. provider-credential outage) can share the
    metric without label collisions.

    Labels: ``profile``, ``reason`` (``"lithos_unhealthy"`` today; new
    values added under-test only when the breaker grows new arms).
    """
    return get_meter().counter(
        "influx_runs_skipped_total",
        description=(
            "runs short-circuited before any source-fetch / LLM / write "
            "work was done, broken down by reason"
        ),
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


def slug_collision_url_recovery() -> Any:
    """Counter of slug collisions short-circuited via source-URL identity (#148).

    Increments when the pre-suffix-retry source-URL ``cache_lookup`` hits,
    proving Lithos already owns a note for the incoming write's URL.
    Influx treats the write as a duplicate without descending into the
    squatter-shape dispatch (squatter inspection, suffix retry,
    unresolved-collision backlog).

    A steadily non-zero value indicates Lithos's URL/cache dedup is
    missing items that source-URL equality would catch — file a Lithos
    issue if the rate is significant.

    No labels: low-volume signal that mostly tracks the quality of
    upstream URL dedup.
    """
    return get_meter().counter(
        "influx_slug_collision_url_recovery_total",
        description=(
            "slug_collision events short-circuited via source-URL identity "
            "lookup (Lithos already had the URL; no squatter inspection "
            "needed)"
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


def dedup_lookup_errors() -> Any:
    """Counter of swallowed pre-acquire ``cache_lookup`` failures (#234).

    Mirrors the ledger's ``dedup_cache_lookup`` degraded reason: every
    increment corresponds to one entry written through
    :func:`~influx.telemetry.record_dedup_lookup_error`.  Distinct from
    :func:`source_acquisition_errors` because the failed call is
    Influx→Lithos (the lookup tool errored server-side), not
    Influx→upstream — operators should look at Lithos health, not the
    feed.

    Labels: ``profile``, ``source``.
    """
    return get_meter().counter(
        "influx_dedup_lookup_errors_total",
        description="Swallowed pre-acquire cache_lookup failures per run.",
    )


def source_cooldown_skips() -> Any:
    """Counter of source fetches skipped by an adaptive cooldown (#146).

    Mirrors the ledger's ``source_cooldown_skip`` degraded reason:
    every increment corresponds to one entry written through
    :func:`~influx.telemetry.record_source_cooldown_skip`.  Distinct
    from :func:`source_acquisition_errors` because the run deliberately
    chose not to call upstream, so the failure mode is operational
    (we backed off) rather than upstream (they refused us).

    Labels: ``profile``, ``source``.
    """
    return get_meter().counter(
        "influx_source_cooldown_skips_total",
        description="Source fetches skipped by an adaptive cooldown.",
    )
