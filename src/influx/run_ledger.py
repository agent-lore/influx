"""Local run ledger for operator visibility.

The ledger is intentionally local process state rather than Lithos
knowledge.  It records operational facts about Influx runs so the
admin API and support scripts can answer "what has this deployment
been doing?" without making run history part of the user's knowledge
base.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RunEntry = dict[str, Any]

# How many prior runs to consider when computing the stall flags.
#
# Twenty is chosen to roughly match a *day* of scheduled runs at the
# typical hourly cadence, so the historical-ratchet for #50
# (``fetch_stall``) and #85 (``filter_stall``) tolerates a half-day
# window of zero-checked runs before stale history disqualifies the
# profile from ratcheting.  It also matches the historical default of
# :meth:`recent` which the ``ingestion_stall`` (#36) check used
# implicitly.
_STALL_HISTORY_LIMIT = 20


@dataclass(frozen=True)
class RunLedger:
    """Append-only local run ledger backed by JSON files."""

    state_dir: Path

    @property
    def runs_path(self) -> Path:
        return self.state_dir / "runs.jsonl"

    @property
    def active_path(self) -> Path:
        return self.state_dir / "active-runs.json"

    @property
    def unresolved_slug_collisions_path(self) -> Path:
        """JSONL of slug collisions that exhausted the recovery chain (#31).

        Each line is one entry with ``timestamp``, ``run_id``,
        ``profile``, ``source``, ``source_url``, ``title``, ``detail``.
        Append-only; the ``slug-collision-backlog`` diagnose subcommand
        consumes it so an operator can see every still-unresolved
        collision in one place rather than scraping log buffers.
        """
        return self.state_dir / "unresolved-slug-collisions.jsonl"

    def start(
        self,
        *,
        run_id: str,
        profile: str,
        kind: str,
        run_range: dict[str, str | int] | None,
    ) -> None:
        """Record a run as active."""
        started_at = datetime.now(UTC).isoformat()
        entry: RunEntry = {
            "run_id": run_id,
            "profile": profile,
            "kind": kind,
            "status": "running",
            "run_range": run_range or {},
            "started_at": started_at,
            "completed_at": None,
            "duration_seconds": None,
            "sources_checked": None,
            "ingested": None,
            "fetched_total": None,
            "filter_errors_total": None,
            "invalid_url_rejections_total": None,
            "archive_failures_total": None,
            "error": None,
            "degraded": False,
            "degraded_reasons": [],
            "source_acquisition_errors": [],
            # Issue #146: cooldown skips are surfaced in the entry
            # alongside ``source_acquisition_errors`` so the active
            # snapshot has the same shape as a completed entry.
            "source_cooldown_skips": [],
            # #129: per-source counter of *recovered* retries (i.e.
            # retries that did not produce a swallowed error).  Shape:
            # ``{"arxiv": {"rate_limit": 2, "timeout": 1}}``.  Empty
            # dict at start; populated from the per-run contextvar at
            # ``complete`` time.
            "source_retry_counts": {},
        }
        try:
            active = self._read_active()
            active[run_id] = entry
            self._write_json_atomic(self.active_path, active)
        except OSError:
            logger.warning("failed to update active run ledger", exc_info=True)

    def complete(
        self,
        *,
        run_id: str,
        sources_checked: int | None,
        ingested: int | None,
        fetched_total: int | None = None,
        filter_errors_total: int | None = None,
        invalid_url_rejections_total: int | None = None,
        archive_failures_total: int | None = None,
        source_acquisition_errors: list[dict[str, str]] | None = None,
        source_cooldown_skips: list[dict[str, str]] | None = None,
        source_retry_counts: dict[str, dict[str, int]] | None = None,
    ) -> list[str]:
        """Mark an active run as completed and append it to history.

        Returns the structured ``degraded_reasons`` list that was
        recorded.  Possible values:

        - ``"source_acquisition"`` (issue #20) — at least one
          source-fetch failure was swallowed.
        - ``"source_cooldown_skip"`` (issue #146) — at least one
          source fetch was deliberately skipped because the
          source-specific adaptive 429 cooldown was active.  Distinct
          from ``source_acquisition`` because the run *chose* not to
          call upstream rather than tried and lost.  Single-run
          signal — like ``filter_error`` — so operators see the skip
          immediately even on the first cooldown-suppressed run.
        - ``"filter_error"`` (issue #85 review) — the LLM filter
          scorer raised :class:`FilterScorerError` (transport, parse,
          or provider failure) at least once during this run.  Single-
          run signal — no consecutive-runs gate, since one filter
          execution failure is immediately actionable.  Mutually
          exclusive with ``filter_stall``: when both would apply,
          ``filter_error`` wins because the scorer-failure diagnosis
          is more specific.
        - ``"archive_acquisition"`` — at least one accepted item was
          ingested with ``influx:archive-missing`` after archive
          acquisition failed, so note quality degraded even though the
          run continued.
        - ``"ingestion_stall"`` (issue #36) — this and the immediately
          prior scheduled run both saw ``ingested == 0`` despite
          ``sources_checked > 0``.  Typical cause: every candidate hits
          ``slug_collision``/``duplicate``.
        - ``"fetch_stall"`` (issue #50) — this and the immediately
          prior scheduled run both saw ``fetched_total == 0`` (no
          source returned any items at all), AND the profile has
          historically seen ``sources_checked > 0``.  Typical cause:
          too-narrow ``lookback_days`` or an upstream feed shape change.
        - ``"filter_stall"`` (issue #85) — this and the immediately
          prior scheduled run both saw ``fetched_total > 0`` but
          ``sources_checked == 0`` (the LLM filter ran cleanly and
          rejected every candidate), AND the profile has historically
          seen ``sources_checked > 0``.  Typical cause: profile
          description drift, filter prompt regression, or
          ``min_score_in_results`` set too high.  Requires
          ``filter_errors_total == 0`` so a scorer failure produces
          ``filter_error`` rather than this, AND
          ``invalid_url_rejections_total < fetched_total`` so a feed
          whose entries were all URL-rejected pre-filter produces
          ``invalid_url_stall`` rather than this.
        - ``"invalid_url_stall"`` (issue #131) — the feed returned
          items but every URL was upstream-malformed (loopback,
          private, link-local, multicast, unparseable, or
          disallowed-scheme), so nothing reached the LLM filter.
          Single-run signal — like ``filter_error`` — because URL
          malformation is an immediately actionable upstream bug.

        ``fetch_stall`` and ``filter_stall`` partition the
        ``sources_checked == 0`` space by ``fetched_total``: they are
        mutually exclusive on a single run.  ``ingestion_stall``
        requires ``sources_checked > 0``, so it is mutually exclusive
        with both.  ``filter_error`` and ``filter_stall`` are mutually
        exclusive because they describe distinct failure shapes (scorer
        broken vs scorer-rejected-all); ``filter_error`` is independent
        of ``ingestion_stall`` and ``fetch_stall`` and may co-occur
        with either if a partial-failure run still ingested or
        legitimately fetched nothing from some sources.

        Returning the reasons lets the caller (the scheduler) emit
        per-reason metrics without re-deriving the logic.
        """
        errors = list(source_acquisition_errors or [])
        cooldown_skips = list(source_cooldown_skips or [])
        reasons: list[str] = []
        if errors:
            reasons.append("source_acquisition")
        # Issue #146: cooldown skips are reported as a *separate*
        # degraded reason so operator dashboards can distinguish
        # "we backed off on purpose" from "we tried and lost".  Both
        # may co-occur within a single run (e.g. arXiv cooldown, RSS
        # network failure) — the run-level summary surfaces both.
        if cooldown_skips:
            reasons.append("source_cooldown_skip")

        # #85 review: filter_error fires immediately on any
        # FilterScorerError catch, regardless of run kind.  Operators
        # need to know about scorer execution failures even on manual
        # / backfill runs (where the stall family doesn't apply).
        # Mutual exclusion with filter_stall is enforced below by
        # gating the filter_stall branch on filter_errors_total == 0.
        has_filter_error = (
            isinstance(filter_errors_total, int) and filter_errors_total > 0
        )
        if has_filter_error:
            reasons.append("filter_error")
        if isinstance(archive_failures_total, int) and archive_failures_total > 0:
            reasons.append("archive_acquisition")

        # #131 review concern 2: ``invalid_url_stall`` fires when the
        # feed returned items but every URL was upstream-malformed
        # (loopback / private / link-local / multicast / unparseable /
        # disallowed scheme), so nothing reached the LLM filter.
        # Single-run signal — like ``filter_error`` and
        # ``archive_acquisition`` — because URL malformation is a
        # clear-cut, immediately actionable upstream bug, not a
        # noisy/transient signal that needs a consecutive-runs ratchet.
        # Mutually exclusive with ``filter_stall`` (the gate below
        # enforces ``items_reached_filter > 0``) and with
        # ``fetch_stall`` (which requires ``fetched_total == 0``).
        items_reached_filter = (
            (fetched_total - invalid_url_rejections_total)
            if isinstance(fetched_total, int)
            and isinstance(invalid_url_rejections_total, int)
            else None
        )
        has_invalid_url_stall = (
            isinstance(invalid_url_rejections_total, int)
            and invalid_url_rejections_total > 0
            and isinstance(fetched_total, int)
            and fetched_total > 0
            and items_reached_filter == 0
        )
        if has_invalid_url_stall:
            reasons.append("invalid_url_stall")

        # Resolve profile + kind once for the stall checks.  All three
        # stall flags only apply to scheduled runs (backfills
        # legitimately ingest 0 when every candidate is a cache hit,
        # and operator-triggered manual runs may use a deliberately
        # narrow lookback).
        active = self._read_active()
        entry = active.get(run_id, {})
        profile = entry.get("profile")
        kind = entry.get("kind")
        is_scheduled = isinstance(profile, str) and kind == "scheduled"

        # #36 ingestion-stall detection: only meaningful when the run
        # actually inspected something.
        if (
            is_scheduled
            and isinstance(ingested, int)
            and ingested == 0
            and isinstance(sources_checked, int)
            and sources_checked > 0
        ):
            # Count consecutive prior scheduled runs for the same
            # profile that match the stall shape.  We add 1 for *this*
            # run since it hasn't been written yet.
            assert isinstance(profile, str)  # narrowed by is_scheduled
            consecutive = 1 + self._consecutive_zero_ingestion_runs(
                profile=profile, exclude_run_id=run_id
            )
            if consecutive >= 2:
                reasons.append("ingestion_stall")

        # #50 / #85: zero-sources_checked split.  ``fetch_stall`` keeps
        # the original semantics — *no items reached the filter* —
        # while ``filter_stall`` (#85) catches the case where sources
        # fetched normally but the filter rejected every candidate.
        # Both share the historical-ratchet ("profile has previously
        # seen ``sources_checked > 0``") that silences brand-new
        # profiles and profiles that genuinely never receive items.
        if is_scheduled and isinstance(sources_checked, int) and sources_checked == 0:
            assert isinstance(profile, str)  # narrowed by is_scheduled

            if isinstance(fetched_total, int) and fetched_total > 0:
                # #85 filter_stall: items WERE fetched, the filter
                # rejected them all.  Requires filter_errors_total == 0
                # — a scorer-execution failure has a distinct, more
                # specific reason (``filter_error``) and shouldn't
                # double-fire as a filter_stall (#85 review).  #131
                # review concern 2: also requires that items actually
                # reached the filter (i.e. weren't all dropped pre-acquire
                # by the URL validator); otherwise ``invalid_url_stall``
                # is the correct diagnosis.  Streak counts prior
                # scheduled runs that match the same shape
                # (sources_checked == 0 AND fetched_total > 0).
                if has_filter_error or has_invalid_url_stall:
                    pass  # more-specific reason already appended above
                else:
                    consecutive_filter = (
                        1
                        + self._consecutive_zero_check_with_fetch_runs(
                            profile=profile, exclude_run_id=run_id
                        )
                    )
                    if consecutive_filter >= 2 and self._has_prior_non_zero_fetch(
                        profile=profile, exclude_run_id=run_id
                    ):
                        reasons.append("filter_stall")
            else:
                # #50 fetch_stall: nothing was fetched at all.  Either
                # ``fetched_total`` was explicitly 0 or the caller
                # didn't supply one (legacy path) — both fall to
                # fetch_stall to preserve pre-#85 semantics.
                consecutive_zero_fetch = 1 + self._consecutive_zero_fetch_runs(
                    profile=profile, exclude_run_id=run_id
                )
                if consecutive_zero_fetch >= 2 and self._has_prior_non_zero_fetch(
                    profile=profile, exclude_run_id=run_id
                ):
                    reasons.append("fetch_stall")

        self._finish(
            run_id=run_id,
            status="completed",
            sources_checked=sources_checked,
            ingested=ingested,
            fetched_total=fetched_total,
            filter_errors_total=filter_errors_total,
            invalid_url_rejections_total=invalid_url_rejections_total,
            archive_failures_total=archive_failures_total,
            error=None,
            degraded=bool(reasons),
            source_acquisition_errors=errors,
            source_cooldown_skips=cooldown_skips,
            degraded_reasons=reasons,
            source_retry_counts=source_retry_counts,
        )
        return reasons

    def _consecutive_zero_ingestion_runs(
        self, *, profile: str, exclude_run_id: str
    ) -> int:
        """Count consecutive prior scheduled runs that match the stall shape.

        Walks ``recent()`` newest-first, counting runs where
        ``ingested == 0 AND sources_checked > 0`` for *profile* of kind
        ``scheduled``.  Stops at the first run that doesn't match.  Used
        by :meth:`complete` to compute the ``ingestion_stall`` degraded
        reason (#36).
        """
        count = 0
        for prior in self.recent(limit=_STALL_HISTORY_LIMIT, profile=profile):
            if prior.get("run_id") == exclude_run_id:
                continue
            if prior.get("kind") != "scheduled":
                # Backfills don't count — their ingest pattern is
                # different.  But they shouldn't break the streak
                # either; skip silently.
                continue
            ingested = prior.get("ingested")
            sources_checked = prior.get("sources_checked")
            if (
                isinstance(ingested, int)
                and ingested == 0
                and isinstance(sources_checked, int)
                and sources_checked > 0
            ):
                count += 1
                continue
            break
        return count

    def _consecutive_zero_fetch_runs(self, *, profile: str, exclude_run_id: str) -> int:
        """Count consecutive prior scheduled runs with ``fetched_total == 0``.

        Walks ``recent()`` newest-first.  Stops at the first scheduled
        run that doesn't match.  Used by :meth:`complete` to compute the
        ``fetch_stall`` degraded reason (#50).

        ``fetched_total`` was added in #85; legacy ledger entries
        without that field but with ``sources_checked == 0`` are
        treated as zero-fetch for backward-compatibility (the pre-#85
        invariant ``fetched_total == 0 ↔ sources_checked == 0`` holds
        by construction since the LLM-filter rejection was the only
        new way to land at ``sources_checked == 0`` despite a
        successful fetch).
        """
        count = 0
        for prior in self.recent(limit=_STALL_HISTORY_LIMIT, profile=profile):
            if prior.get("run_id") == exclude_run_id:
                continue
            if prior.get("kind") != "scheduled":
                continue
            if self._is_zero_fetch_entry(prior):
                count += 1
                continue
            break
        return count

    def _consecutive_zero_check_with_fetch_runs(
        self, *, profile: str, exclude_run_id: str
    ) -> int:
        """Count consecutive prior scheduled runs matching the filter_stall shape.

        Walks ``recent()`` newest-first, counting runs where
        ``sources_checked == 0 AND fetched_total > 0`` for *profile*
        of kind ``scheduled``.  Stops at the first run that doesn't
        match.  Used by :meth:`complete` to compute the
        ``filter_stall`` degraded reason (#85).
        """
        count = 0
        for prior in self.recent(limit=_STALL_HISTORY_LIMIT, profile=profile):
            if prior.get("run_id") == exclude_run_id:
                continue
            if prior.get("kind") != "scheduled":
                continue
            sources_checked = prior.get("sources_checked")
            fetched_total = prior.get("fetched_total")
            if (
                isinstance(sources_checked, int)
                and sources_checked == 0
                and isinstance(fetched_total, int)
                and fetched_total > 0
            ):
                count += 1
                continue
            break
        return count

    @staticmethod
    def _is_zero_fetch_entry(entry: RunEntry) -> bool:
        """Return ``True`` when *entry* matches the fetch-stall shape.

        ``fetched_total == 0`` is the explicit signal added in #85;
        legacy entries without that field but with
        ``sources_checked == 0`` are treated as zero-fetch.
        """
        sources_checked = entry.get("sources_checked")
        if not (isinstance(sources_checked, int) and sources_checked == 0):
            return False
        fetched_total = entry.get("fetched_total")
        if isinstance(fetched_total, int):
            return fetched_total == 0
        # Legacy entry written before #85 added ``fetched_total``.
        return True

    def _has_prior_non_zero_fetch(self, *, profile: str, exclude_run_id: str) -> bool:
        """Return whether *profile* has ever fetched items in recent history.

        Implements the #50 historical-ratchet: ``fetch_stall`` only
        fires for profiles that have previously seen
        ``sources_checked > 0`` within the last ``_STALL_HISTORY_LIMIT``
        runs.  This silences brand-new profiles (no history at all) and
        profiles that genuinely never receive items — for them, a
        run of zero fetches is the steady-state, not a regression.
        """
        for prior in self.recent(limit=_STALL_HISTORY_LIMIT, profile=profile):
            if prior.get("run_id") == exclude_run_id:
                continue
            sources_checked = prior.get("sources_checked")
            if isinstance(sources_checked, int) and sources_checked > 0:
                return True
        return False

    def fail(self, *, run_id: str, error: str) -> None:
        """Mark an active run as failed and append it to history."""
        self._finish(
            run_id=run_id,
            status="failed",
            sources_checked=None,
            ingested=None,
            error=error,
            degraded=False,
            source_acquisition_errors=[],
        )

    def skip(self, *, run_id: str, reason: str) -> None:
        """Mark an active run as ``skipped`` and append it to history (#40).

        Distinct from ``fail``: ``failed`` means the run started and
        crashed; ``skipped`` means the run never started any
        source-fetch / LLM / write work because a circuit breaker (e.g.
        Lithos extended outage) opened first.  ``reason`` is recorded
        so dashboards can distinguish skip causes.
        """
        self._finish(
            run_id=run_id,
            status="skipped",
            sources_checked=None,
            ingested=None,
            error=reason,
            degraded=False,
            source_acquisition_errors=[],
        )

    def record_unresolved_slug_collision(
        self,
        *,
        profile: str,
        source: str,
        source_url: str,
        title: str,
        detail: str,
        run_id: str,
    ) -> None:
        """Append one unresolved slug-collision entry to the backlog (#31).

        Called by the scheduler when ``LithosClient._retry_slug_collision``
        cannot recover the write (squatter is genuinely-distinct AND the
        suffix retry also collides).  Each entry is a self-contained JSON
        object so the diagnose script can stream the file without parsing
        run-ledger context.  Best-effort: a write error is logged but
        does not propagate, mirroring the run-ledger discipline.
        """
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "profile": profile,
            "source": source,
            "source_url": source_url,
            "title": title,
            "detail": detail,
        }
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with self.unresolved_slug_collisions_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            logger.warning(
                "failed to append unresolved slug-collision entry",
                exc_info=True,
            )

    def unresolved_slug_collisions(self) -> list[dict[str, Any]]:
        """Return every unresolved slug-collision entry from the backlog (#31).

        Newest-last to match the on-disk order.  Returns an empty list
        when the backlog file does not yet exist.
        """
        path = self.unresolved_slug_collisions_path
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
        except OSError:
            logger.warning(
                "failed to read unresolved slug-collision backlog",
                exc_info=True,
            )
        return entries

    def active_runs(self) -> list[RunEntry]:
        """Return currently active runs ordered by start time."""
        active = self._read_active()
        return sorted(
            active.values(),
            key=lambda entry: str(entry.get("started_at") or ""),
            reverse=True,
        )

    def abandon_active(self, *, reason: str) -> None:
        """Mark active runs from a previous process as abandoned."""
        try:
            active = self._read_active()
            if not active:
                return
            completed_at = datetime.now(UTC)
            for entry in active.values():
                entry.update(
                    {
                        "status": "abandoned",
                        "completed_at": completed_at.isoformat(),
                        "duration_seconds": self._duration_seconds(
                            entry.get("started_at"),
                            completed_at,
                        ),
                        "error": reason,
                        "degraded": entry.get("degraded", False),
                        "degraded_reasons": list(entry.get("degraded_reasons") or []),
                        "source_acquisition_errors": list(
                            entry.get("source_acquisition_errors") or []
                        ),
                        "source_cooldown_skips": list(
                            entry.get("source_cooldown_skips") or []
                        ),
                        "source_retry_counts": {
                            source: dict(by_kind)
                            for source, by_kind in (
                                entry.get("source_retry_counts") or {}
                            ).items()
                        },
                    }
                )
                self._append(entry)
            self._write_json_atomic(self.active_path, {})
        except OSError:
            logger.warning("failed to abandon stale active runs", exc_info=True)

    def recent(
        self,
        *,
        limit: int = 20,
        profile: str | None = None,
    ) -> list[RunEntry]:
        """Return recent completed or failed runs, newest first."""
        limit = max(1, min(limit, 100))
        entries: list[RunEntry] = []
        try:
            lines = self.runs_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("failed to read run ledger", exc_info=True)
            return []

        for line in reversed(lines):
            if len(entries) >= limit:
                break
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed run ledger line")
                continue
            if profile is not None and entry.get("profile") != profile:
                continue
            entries.append(entry)
        return entries

    def last_by_profile(self) -> dict[str, RunEntry]:
        """Return the most recent terminal run for each profile."""
        latest: dict[str, RunEntry] = {}
        for entry in self.recent(limit=100):
            profile = entry.get("profile")
            if isinstance(profile, str) and profile not in latest:
                latest[profile] = entry
        return latest

    def _finish(
        self,
        *,
        run_id: str,
        status: str,
        sources_checked: int | None,
        ingested: int | None,
        error: str | None,
        fetched_total: int | None = None,
        filter_errors_total: int | None = None,
        invalid_url_rejections_total: int | None = None,
        archive_failures_total: int | None = None,
        degraded: bool = False,
        source_acquisition_errors: list[dict[str, str]] | None = None,
        source_cooldown_skips: list[dict[str, str]] | None = None,
        degraded_reasons: list[str] | None = None,
        source_retry_counts: dict[str, dict[str, int]] | None = None,
    ) -> None:
        try:
            active = self._read_active()
            entry = active.pop(run_id, None)
            if entry is None:
                entry = {
                    "run_id": run_id,
                    "profile": None,
                    "kind": None,
                    "status": "running",
                    "run_range": {},
                    "started_at": None,
                }

            completed_at = datetime.now(UTC)

            entry.update(
                {
                    "status": status,
                    "completed_at": completed_at.isoformat(),
                    "duration_seconds": self._duration_seconds(
                        entry.get("started_at"),
                        completed_at,
                    ),
                    "sources_checked": sources_checked,
                    "ingested": ingested,
                    "fetched_total": fetched_total,
                    "filter_errors_total": filter_errors_total,
                    "invalid_url_rejections_total": invalid_url_rejections_total,
                    "archive_failures_total": archive_failures_total,
                    "error": error,
                    "degraded": degraded,
                    "degraded_reasons": list(degraded_reasons or []),
                    "source_acquisition_errors": list(source_acquisition_errors or []),
                    # Issue #146: persist cooldown skips alongside
                    # ``source_acquisition_errors`` so the same JSONL
                    # consumers can read both lists with the same shape.
                    "source_cooldown_skips": list(source_cooldown_skips or []),
                    # #129: deep-copy the per-source retry-count dict so
                    # later mutations of the contextvar bucket do not
                    # leak into the persisted ledger entry.
                    "source_retry_counts": {
                        source: dict(by_kind)
                        for source, by_kind in (source_retry_counts or {}).items()
                    },
                }
            )
            self._append(entry)
            self._write_json_atomic(self.active_path, active)
        except OSError:
            logger.warning("failed to update run ledger", exc_info=True)

    def _read_active(self) -> dict[str, RunEntry]:
        try:
            data = json.loads(self.active_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            logger.warning("active run ledger is malformed; resetting")
            return {}
        except OSError:
            logger.warning("failed to read active run ledger", exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(run_id): entry
            for run_id, entry in data.items()
            if isinstance(entry, dict)
        }

    def _append(self, entry: RunEntry) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.runs_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def _duration_seconds(
        self,
        started_at_raw: Any,
        completed_at: datetime,
    ) -> float | None:
        if not isinstance(started_at_raw, str):
            return None
        try:
            started_at = datetime.fromisoformat(started_at_raw)
        except ValueError:
            return None
        return (completed_at - started_at).total_seconds()

    def _write_json_atomic(self, path: Path, data: Any) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
