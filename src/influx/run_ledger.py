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

from influx.run_assessment import (
    RunCompletionAssessment,
    RunEvidence,
    build_degradation_summary,
    single_run_reasons,
)

logger = logging.getLogger(__name__)

RunEntry = dict[str, Any]

# How many prior runs the stall checks look back over: roughly a day of
# hourly scheduled runs, so the historical ratchet (#50 / #85) tolerates a
# half-day of zero-checked runs before stale history disqualifies a
# profile.
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
            # Issue #166: per-run total of items the source adapters
            # suppressed via the thin-summary rule.  Distinct from
            # ``archive_failures_total`` — these items never reached the
            # write loop and so never received ``influx:archive-missing``.
            # Stays ``None`` while the run is in flight; populated from
            # the per-run contextvar at ``complete`` time.
            "summary_thin_drops_total": None,
            # Issue #189: per-run total of notes written despite having no
            # usable source (blank source tag AND no arxiv-inferable URL).
            # Observe-only signal, distinct from any failure total and
            # never a degraded reason.  Stays ``None`` in flight; populated
            # from the per-run contextvar at ``complete`` time.
            "empty_source_writes_total": None,
            "error": None,
            "degraded": False,
            "degraded_reasons": [],
            # Issue #164: severity bucket on top of the flat
            # ``degraded_reasons`` list.  Starts at ``"success"`` while
            # the run is in flight; finalised by ``_finish`` once
            # ``degraded_reasons`` is known.
            "degradation_severity": "success",
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
            # #151: per-run counts of Tier 3 enrichment fallbacks,
            # split by classification.  Shape:
            # ``{"harmless": int, "degraded": int}``.  Empty dict at
            # start; populated from the per-run contextvar at
            # ``complete`` time so run summaries / ``/runs/recent``
            # can distinguish "Tier 3 noise that didn't break notes"
            # from "Tier 3 + Tier 1 both failed — note materially
            # incomplete".
            "tier3_fallbacks": {},
            # #152: bounded, top-N degradation summary aggregated at
            # ``complete`` time.  Stays ``None`` while the run is in
            # flight; populated by ``complete`` with archive +
            # source-acquisition + retry breakdowns.  See
            # :func:`build_degradation_summary` for the full shape.
            "degradation_summary": None,
        }
        try:
            active = self._read_active()
            active[run_id] = entry
            self._write_json_atomic(self.active_path, active)
        except OSError:
            logger.warning("failed to update active run ledger", exc_info=True)

    def complete(
        self, *, run_id: str, evidence: RunEvidence
    ) -> RunCompletionAssessment:
        """Finalise an active run as ``completed`` and return its assessment.

        The history-free reasons come from
        :func:`influx.run_assessment.single_run_reasons`; the ledger adds
        the scheduled-only stall family it alone can see
        (:meth:`_scheduled_stall_reasons`).  The assessment is what gets
        persisted and what the run service derives metrics and logs from.
        """
        entry = self._read_active().get(run_id, {})
        profile = entry.get("profile")
        kind = entry.get("kind")
        reasons = single_run_reasons(evidence)
        if isinstance(profile, str) and kind == "scheduled":
            reasons.extend(
                self._scheduled_stall_reasons(
                    profile=profile, run_id=run_id, evidence=evidence
                )
            )
        assessment = RunCompletionAssessment(
            run_id=run_id,
            profile=profile if isinstance(profile, str) else None,
            kind=kind if isinstance(kind, str) else None,
            status="completed",
            evidence=evidence,
            reasons=tuple(reasons),
            degradation_summary=build_degradation_summary(evidence),
        )
        self._finish(assessment)
        return assessment

    def _scheduled_stall_reasons(
        self, *, profile: str, run_id: str, evidence: RunEvidence
    ) -> list[str]:
        """The history-dependent reasons, scheduled runs only.

        Backfills legitimately ingest 0 when every candidate is a cache
        hit, manual runs may use a deliberately narrow lookback, and
        inbox runs are single-item — so none of them ratchet.

        - ``ingestion_stall`` (#36): this and the prior scheduled run
          both ingested 0 with ``sources_checked > 0``.
        - ``fetch_stall`` (#50) / ``filter_stall`` (#85) partition the
          ``sources_checked == 0`` space by ``fetched_total``, each
          needing a 2-run streak plus the historical ratchet ("this
          profile has seen ``sources_checked > 0`` before").
          ``filter_stall`` yields to the more specific ``filter_error``
          and ``invalid_url_stall``; ``fetch_stall`` is fully explained
          by a cooldown skip on the current run (#163 review) and
          ignores cooldown-skipped priors in its streak.
        """
        reasons: list[str] = []
        sources_checked = evidence.sources_checked
        if not isinstance(sources_checked, int):
            return reasons
        if sources_checked > 0:
            if evidence.ingested == 0:
                streak = 1 + self._consecutive_zero_ingestion_runs(
                    profile=profile, exclude_run_id=run_id
                )
                if streak >= 2:
                    reasons.append("ingestion_stall")
            return reasons
        if isinstance(evidence.fetched_total, int) and evidence.fetched_total > 0:
            if evidence.has_filter_error or evidence.has_invalid_url_stall:
                return reasons
            streak = 1 + self._consecutive_zero_check_with_fetch_runs(
                profile=profile, exclude_run_id=run_id
            )
            if streak >= 2 and self._has_prior_non_zero_fetch(
                profile=profile, exclude_run_id=run_id
            ):
                reasons.append("filter_stall")
            return reasons
        if evidence.source_cooldown_skips:
            return reasons
        streak = 1 + self._consecutive_zero_fetch_runs(
            profile=profile, exclude_run_id=run_id
        )
        if streak >= 2 and self._has_prior_non_zero_fetch(
            profile=profile, exclude_run_id=run_id
        ):
            reasons.append("fetch_stall")
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

        Issue #163 review: prior runs whose zeros are explained by a
        ``source_cooldown_skip`` (cooldown short-circuited the fetch)
        are *skipped* — neither counted toward the streak nor used to
        terminate it.  Continuing past these entries keeps the streak
        meaningful for the genuine "fetch tried and got nothing"
        pattern that ``fetch_stall`` exists to surface.
        """
        count = 0
        for prior in self.recent(limit=_STALL_HISTORY_LIMIT, profile=profile):
            if prior.get("run_id") == exclude_run_id:
                continue
            if prior.get("kind") != "scheduled":
                continue
            # Issue #163: don't let cooldown-skipped runs pollute the
            # zero-fetch streak.  Their zeros are deliberate, not a
            # symptom of upstream silence.
            if prior.get("source_cooldown_skips"):
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

    def fail(self, *, run_id: str, error: str) -> RunCompletionAssessment:
        """Finalise an active run as ``failed`` (the body raised)."""
        return self._finish_without_evidence(run_id, status="failed", error=error)

    def skip(self, *, run_id: str, reason: str) -> RunCompletionAssessment:
        """Finalise an active run as ``skipped`` (#40).

        Distinct from ``fail``: the run never started any source-fetch /
        LLM / write work because a circuit breaker opened first.
        ``reason`` lands in the entry's ``error`` field.
        """
        return self._finish_without_evidence(run_id, status="skipped", error=reason)

    def _finish_without_evidence(
        self, run_id: str, *, status: str, error: str
    ) -> RunCompletionAssessment:
        entry = self._read_active().get(run_id, {})
        profile = entry.get("profile")
        kind = entry.get("kind")
        assessment = RunCompletionAssessment(
            run_id=run_id,
            profile=profile if isinstance(profile, str) else None,
            kind=kind if isinstance(kind, str) else None,
            status="failed" if status == "failed" else "skipped",
            evidence=RunEvidence(),
            reasons=(),
            error=error,
        )
        self._finish(assessment)
        return assessment

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
                        # Issue #164: preserve the in-flight severity
                        # on abandon — typically ``"success"`` since
                        # the field is only finalised by ``_finish``,
                        # but a restart hand-off should not silently
                        # downgrade an already-set value.
                        "degradation_severity": entry.get(
                            "degradation_severity", "success"
                        ),
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
                        # #152: preserve any in-flight degradation
                        # summary on abandon (typically ``None`` since
                        # the summary is only populated by ``complete``,
                        # but allow restart hand-offs to keep partial
                        # data).
                        "degradation_summary": entry.get("degradation_summary"),
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

    def _finish(self, assessment: RunCompletionAssessment) -> None:
        """Move the run from the active snapshot to history, as assessed."""
        evidence = assessment.evidence
        try:
            active = self._read_active()
            entry = active.pop(assessment.run_id, None)
            if entry is None:
                entry = {
                    "run_id": assessment.run_id,
                    "profile": None,
                    "kind": None,
                    "status": "running",
                    "run_range": {},
                    "started_at": None,
                }

            completed_at = datetime.now(UTC)
            entry.update(
                {
                    "status": assessment.status,
                    "completed_at": completed_at.isoformat(),
                    "duration_seconds": self._duration_seconds(
                        entry.get("started_at"),
                        completed_at,
                    ),
                    "sources_checked": evidence.sources_checked,
                    "ingested": evidence.ingested,
                    "fetched_total": evidence.fetched_total,
                    "filter_errors_total": evidence.filter_errors_total,
                    "invalid_url_rejections_total": (
                        evidence.invalid_url_rejections_total
                    ),
                    "archive_failures_total": evidence.archive_failures_total,
                    "summary_thin_drops_total": evidence.summary_thin_drops_total,
                    "empty_source_writes_total": evidence.empty_source_writes_total,
                    "error": assessment.error,
                    "degraded": assessment.degraded,
                    "degraded_reasons": list(assessment.reasons),
                    "degradation_severity": assessment.severity,
                    "source_acquisition_errors": list(
                        evidence.source_acquisition_errors
                    ),
                    "source_cooldown_skips": list(evidence.source_cooldown_skips),
                    "dedup_lookup_errors": list(evidence.dedup_lookup_errors),
                    # Copies, so later mutation of the run's contextvar
                    # buckets cannot leak into the persisted entry.
                    "source_retry_counts": {
                        source: dict(by_kind)
                        for source, by_kind in evidence.source_retry_counts.items()
                    },
                    "tier3_fallbacks": dict(evidence.tier3_fallbacks),
                    "degradation_summary": assessment.degradation_summary,
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
