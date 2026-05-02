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
        """JSONL of slug collisions that exhausted ``_retry_slug_collision`` (#31).

        Each line is one entry with ``timestamp``, ``run_id``,
        ``profile``, ``source``, ``source_url``, ``title``, ``detail``.
        Append-only; the ``squatters`` diagnose subcommand consumes it
        so an operator can see every still-unresolved collision in one
        place rather than scraping log buffers.
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
            "error": None,
            "degraded": False,
            "source_acquisition_errors": [],
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
        source_acquisition_errors: list[dict[str, str]] | None = None,
    ) -> None:
        """Mark an active run as completed and append it to history.

        *source_acquisition_errors* records source-fetch failures that
        were swallowed during the run (e.g. arxiv HTTP 5xx, RSS
        timeout).  When non-empty the run is flagged ``degraded=True``
        in the ledger so dashboards can distinguish a partial-failure
        run from a genuinely quiet window (issue #20).
        """
        errors = list(source_acquisition_errors or [])
        self._finish(
            run_id=run_id,
            status="completed",
            sources_checked=sources_checked,
            ingested=ingested,
            error=None,
            degraded=bool(errors),
            source_acquisition_errors=errors,
        )

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

        Called by the scheduler when ``_retry_slug_collision`` exhausts
        its chain.  Each entry is a self-contained JSON object so the
        diagnose script can stream the file without parsing run-ledger
        context.  Best-effort: a write error is logged but does not
        propagate, mirroring the run-ledger discipline.
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
                        "source_acquisition_errors": list(
                            entry.get("source_acquisition_errors") or []
                        ),
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
        degraded: bool = False,
        source_acquisition_errors: list[dict[str, str]] | None = None,
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
                    "error": error,
                    "degraded": degraded,
                    "source_acquisition_errors": list(source_acquisition_errors or []),
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
