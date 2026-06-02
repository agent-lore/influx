"""Inbox manual-submission orchestrator (``docs/plans/inbox.md``).

The InboxTick is a scheduler-side orchestrator that sits *above* the Run
layer.  It claims pending InboxTasks (Lithos tasks tagged
``influx:inbox``), acquires each submitted URL once, scores it with the
existing :class:`~influx.filter.Filter`, and dispatches a real
single-Profile :class:`~influx.run_service.RunService` execution with
``RunKind.INBOX`` for each clearing profile.  It is NOT a ``Run``: it
never calls ``RunLedger.start`` for itself and produces no
``ProfileRunResult``.

The per-Profile dispatch reuses the unchanged Run pipeline by *injecting*
a single-item :data:`~influx.run.ItemProvider` — there is no parallel
ingestion pipeline.  See §5.3 of the plan.

**Scope.** A URL is scored against *every* enabled profile (§5/§8) and
dispatched to each that clears its threshold, merging into one canonical
note via the existing ``write_note`` path.  Each dispatch holds the
per-Profile coordinator lock (so inbox never overlaps a scheduled Run for
the same profile); when every clearing profile is busy the item is left
for a later tick.  A resubmitted URL is a cache-hit replay (§6): only the
*complement* (profiles that have neither ingested it nor been suppressed)
is re-scored, which is how a busy-skipped profile is picked up on a later
tick (§10).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from influx import metrics
from influx.config import AppConfig, InboxConfig
from influx.coordinator import Coordinator, ProfileBusyError, RunKind
from influx.errors import LCMAError, LithosError
from influx.feedback import build_filter_prompt
from influx.filter import make_default_batch_scorer
from influx.lithos_client import LithosClient
from influx.notes import parse_note, parse_profile_relevance
from influx.run import ItemProvider, RunOutcome, RunPlan
from influx.run_service import RunService
from influx.source import BoundScoredCandidate, Candidate, ScoredCandidate
from influx.sources.inbox import (
    InboxAcquisition,
    acquire_inbox_bytes,
    acquire_inbox_pdf,
    build_inbox_note_item,
)
from influx.storage import ArchivePathError, resolve_within_root
from influx.urls import normalise_url

if TYPE_CHECKING:
    from influx.run_ledger import RunLedger

logger = logging.getLogger(__name__)

_SOURCE_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Submitter ids are free-text agent identifiers (e.g. ``daily-report:ai-news``,
# ``manual:<user>``) so colons are allowed, but the value reaches a Lithos
# note tag — strip anything outside a conservative charset to keep tags
# single-token and free of control characters (§3.2 / §13.2).
_SUBMITTER_STRIP_RE = re.compile(r"[^A-Za-z0-9:._-]+")
_DEFAULT_SOURCE_TAG = "inbox"
_CLAIM_ASPECT = "ingest"
_ALLOWED_URL_SCHEMES = ("http", "https")
_REJECTED_TAG_PREFIX = "influx:rejected:"


def _valid_source_tag(tag: str) -> bool:
    """Conservative slug check for a submitter-provided ``source_tag`` (§13.1)."""
    return bool(_SOURCE_TAG_RE.match(tag))


def _sanitise_submitter(value: str) -> str:
    """Reduce a submitter id to a safe, single-token tag value (≤64 chars)."""
    cleaned = _SUBMITTER_STRIP_RE.sub("-", value).strip("-")[:64]
    return cleaned or "unknown"


def _extract_note_id(body: dict[str, Any]) -> str | None:
    """Pull a note id out of a ``lithos_cache_lookup`` body (mirrors #148)."""
    if not body.get("hit"):
        return None
    for key in ("id", "note_id", "existing_id"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ── Seam: single-item provider + per-Profile dispatch ───────────────


def make_inbox_item_provider(
    *,
    scored: ScoredCandidate,
    acquired: InboxAcquisition,
    source_tag: str,
    submitted_by: str,
    title_hint: str | None,
    config: AppConfig,
) -> ItemProvider:
    """Build a single-item :data:`ItemProvider` for one (item, profile).

    Yields exactly one already-scored :class:`BoundScoredCandidate` whose
    ``acquire`` closure returns the prebuilt ``ProfileItem`` from the shared
    acquisition — so the unchanged Run Acquire/Ingest stages run the
    cache-lookup + write + merge + LCMA wiring without modification.  The
    rendered ``filter_prompt`` is ignored: filtering already happened at
    tick level.
    """

    async def _provider(
        profile: str,
        kind: RunKind,
        date_window: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> list[BoundScoredCandidate]:
        async def _acquire() -> dict[str, Any] | None:
            return build_inbox_note_item(
                acquired=acquired,
                profile_name=profile,
                score=scored.score,
                confidence=scored.confidence,
                reason=scored.reason,
                filter_tags=scored.filter_tags,
                source_tag=source_tag,
                submitted_by=submitted_by,
                title_hint=title_hint,
                config=config,
            )

        return [
            BoundScoredCandidate(
                scored=scored,
                acquire=_acquire,
                source_label="inbox",
            )
        ]

    return _provider


async def dispatch_profile(
    profile: str,
    *,
    scored: ScoredCandidate,
    acquired: InboxAcquisition,
    source_tag: str,
    submitted_by: str,
    title_hint: str | None,
    config: AppConfig,
    probe_loop: Any | None,
    ledger: RunLedger | None,
    run_id: str,
) -> RunOutcome:
    """Dispatch one real single-Profile ``RunKind.INBOX`` Run for this item.

    The first dispatch for a URL creates the canonical note; later profiles
    (slice 2+) hit ``cache_lookup`` and merge via ``write_note``.
    ``skip_repair=False``, ``skip_cache_hits=False``, ``notify=True`` per
    §11.2.
    """
    provider = make_inbox_item_provider(
        scored=scored,
        acquired=acquired,
        source_tag=source_tag,
        submitted_by=submitted_by,
        title_hint=title_hint,
        config=config,
    )
    service = RunService(
        config=config,
        item_provider=provider,
        probe_loop=probe_loop,
        ledger=ledger,
    )
    plan = RunPlan(
        profile=profile,
        kind=RunKind.INBOX,
        skip_repair=False,
        skip_cache_hits=False,
        notify=True,
    )
    return await service.execute(plan, run_id=run_id)


# ── Orchestrator ────────────────────────────────────────────────────


@dataclass
class InboxStatus:
    """Mutable snapshot of inbox-tick state for the ``/status`` endpoint.

    Written by the tick, read by the ``/status`` handler — so ``/status``
    never issues a per-request ``lithos_task_list`` (FR-HTTP-7, §13.6).
    ``pending`` is the open-task count as of the last tick's list call.
    """

    enabled: bool = False
    pending: int = 0
    in_flight: int = 0
    last_tick_at: str | None = None
    last_tick_outcome: str | None = None

    def snapshot(self) -> dict[str, Any]:
        # ``last_tick_outcome`` reflects the most recently *completed* tick; it
        # is unchanged while a tick is in progress (``in_flight > 0``).
        return {
            "enabled": self.enabled,
            "pending": self.pending,
            "in_flight": self.in_flight,
            "last_tick_at": self.last_tick_at,
            "last_tick_outcome": self.last_tick_outcome,
        }


@dataclass(frozen=True)
class _ItemOutcome:
    """Internal per-item result used to build the task completion."""

    outcome: str
    cited_nodes: list[str]
    inbox_result: dict[str, Any]


@dataclass(frozen=True)
class _ProfileScore:
    """One profile's filter result for an inbox item (pre-dispatch).

    ``scored`` is the raw :class:`ScoredCandidate` from the filter model
    (carrying the 1–10 score regardless of threshold), or ``None`` when the
    model omitted the item.  ``error`` is ``True`` when the filter call
    raised — kept distinct so a per-profile filter failure isolates to that
    profile rather than sinking the whole item (§5.5).
    """

    name: str
    threshold: int
    scored: ScoredCandidate | None
    error: bool

    @property
    def clears(self) -> bool:
        return self.scored is not None and self.scored.score >= self.threshold


@dataclass
class InboxTick:
    """One execution of the inbox-tick orchestrator.

    Built once in the service layer with the shared coordinator, probe
    loop, and run ledger; :meth:`execute` is invoked on the inbox poll
    cadence.  ``client_factory`` allows tests to inject a fake
    :class:`LithosClient`.
    """

    config: AppConfig
    # Shared per-Profile lock authority — each dispatch is wrapped in
    # ``coordinator.hold(profile)`` so inbox Runs never overlap scheduled /
    # manual / backfill Runs for the same profile (FR-SCHED-2/3).
    coordinator: Coordinator
    probe_loop: Any | None = None
    ledger: RunLedger | None = None
    client_factory: Callable[[], LithosClient] | None = None
    # Shared snapshot the /status handler reads (FR-HTTP-7); the tick writes
    # pending / in_flight / last_tick_* here so /status never lists tasks.
    status: InboxStatus | None = None

    def _make_client(self) -> LithosClient:
        if self.client_factory is not None:
            return self.client_factory()
        return LithosClient(
            url=self.config.lithos.url,
            transport=self.config.lithos.transport,
        )

    async def execute(self) -> None:
        """List → claim → process pending InboxTasks for this tick.

        Gated on the same ``lithos_circuit_open`` latch scheduled runs use
        (§12): when Lithos is unhealthy the tick performs no list/claim/
        dispatch.  Per-item failures are isolated — one bad item never
        aborts the rest of the tick.
        """
        status = self.status
        if status is not None:
            status.last_tick_at = datetime.now(UTC).isoformat()

        probe = self.probe_loop
        circuit_open = getattr(probe, "lithos_circuit_open", None)
        if circuit_open is not None and circuit_open():
            logger.info("inbox tick skipped: lithos circuit open")
            if status is not None:
                status.last_tick_outcome = "skipped: lithos circuit open"
            return

        metrics.inbox_tick_started().add(1)
        client = self._make_client()
        tick_outcome = "success"
        try:
            try:
                body = await client.task_list_body(
                    tags=[InboxConfig.TASK_TAG],
                    status="open",
                )
            except (LithosError, LCMAError):
                logger.warning("inbox task_list failed; skipping tick", exc_info=True)
                metrics.inbox_task_call_failures().add(1, {"phase": "list"})
                if status is not None:
                    status.last_tick_outcome = "error: task_list failed"
                return

            all_tasks = list(body.get("tasks", []))
            if status is not None:
                status.pending = len(all_tasks)
            tasks = all_tasks[: self.config.inbox.max_items_per_tick]
            # Record the full open backlog returned by task_list (not just the
            # processed slice) so queue pressure is observable (#212); the
            # per-tick processed count is ``inbox_tasks_claimed``.  Both are
            # monotonic counters — compare their per-tick *deltas*
            # (e.g. ``increase(listed) - increase(claimed)`` over the poll
            # window), not raw cumulative totals.
            metrics.inbox_tasks_listed().add(len(all_tasks))

            for task in tasks:
                if status is not None:
                    status.in_flight += 1
                try:
                    await self._process_task(task, client)
                except Exception:  # noqa: BLE001 — per-item failure isolation (§5.5)
                    # A crash before _complete leaves the claim to expire and
                    # the task ``open``; a later tick re-claims it.  Re-ingest
                    # is safe because ``lithos_write`` dedupes on source_url.
                    tick_outcome = "error"
                    logger.exception(
                        "inbox item processing crashed task_id=%s",
                        task.get("id") if isinstance(task, dict) else "<?>",
                    )
                finally:
                    if status is not None:
                        status.in_flight -= 1
            if status is not None:
                status.last_tick_outcome = tick_outcome
        finally:
            await client.close()

    async def _process_task(self, task: dict[str, Any], client: LithosClient) -> None:
        """Claim and process one InboxTask end-to-end."""
        task_id = str(task.get("id") or "")
        if not task_id:
            return
        agent = self.config.inbox.agent_id

        # ── Claim ──────────────────────────────────────────────────
        try:
            claim = await client.task_claim_body(
                task_id=task_id, agent=agent, aspect=_CLAIM_ASPECT
            )
        except (LithosError, LCMAError):
            logger.warning("inbox claim failed task_id=%s", task_id, exc_info=True)
            metrics.inbox_task_call_failures().add(1, {"phase": "claim"})
            return
        if not claim.get("success"):
            # Already claimed by another influx instance — skip silently.
            return
        metrics.inbox_tasks_claimed().add(1)

        # ── Parse + validate metadata ──────────────────────────────
        metadata = task.get("metadata") or {}
        kind = metadata.get("kind")
        submitted_by = _sanitise_submitter(
            str(metadata.get("submitted_by") or "unknown")
        )
        title_hint = metadata.get("title")
        summary_hint = metadata.get("summary")
        source_tag = metadata.get("source_tag") or _DEFAULT_SOURCE_TAG

        if kind == "url":
            result = await self._process_url_task(
                client,
                task_id,
                url=metadata.get("url"),
                submitted_by=submitted_by,
                title_hint=title_hint,
                summary_hint=summary_hint,
                source_tag=source_tag,
            )
        elif kind == "pdf":
            result = await self._process_pdf_task(
                client,
                task_id,
                local_path=metadata.get("local_path"),
                submitted_by=submitted_by,
                title_hint=title_hint,
                summary_hint=summary_hint,
                source_tag=source_tag,
            )
        else:
            await self._complete_terminal(
                client,
                task_id,
                outcome="error: invalid submission (kind must be 'url' or 'pdf')",
                metric_outcome="invalid_submission",
                inbox_result={"kind": kind, "error": "invalid_submission"},
            )
            return

        # ``_process_*_task`` returns ``None`` either because it already
        # completed the task terminally (validation error) or as the
        # skip-this-tick signal (every clearing profile busy): in both cases
        # leave completion alone here.  A real per-item outcome is completed.
        if result is None:
            return
        await self._complete(client, task_id, result)

    async def _process_url_task(
        self,
        client: LithosClient,
        task_id: str,
        *,
        url: Any,
        submitted_by: str,
        title_hint: str | None,
        summary_hint: str | None,
        source_tag: str,
    ) -> _ItemOutcome | None:
        """Validate a ``kind="url"`` submission and ingest it (v1 path).

        Returns the per-item outcome, or ``None`` when the task was already
        completed terminally (validation error) or should be skipped this
        tick (profile busy).
        """
        if not isinstance(url, str) or not url:
            await self._complete_terminal(
                client,
                task_id,
                outcome="error: invalid submission (kind 'url' needs a url)",
                metric_outcome="invalid_submission",
                inbox_result={"source_url": url, "error": "invalid_submission"},
            )
            return None
        if urlparse(url).scheme not in _ALLOWED_URL_SCHEMES:
            await self._complete_terminal(
                client,
                task_id,
                outcome="error: invalid_url_scheme (only http/https accepted)",
                metric_outcome="invalid_submission",
                inbox_result={"source_url": url, "error": "invalid_url_scheme"},
            )
            return None
        if not _valid_source_tag(source_tag):
            await self._complete_terminal(
                client,
                task_id,
                outcome="error: invalid_source_tag",
                metric_outcome="invalid_source_tag",
                inbox_result={"source_url": url, "error": "invalid_source_tag"},
            )
            return None

        return await self._ingest_item(
            client=client,
            source_url=normalise_url(url),
            acquire=lambda: acquire_inbox_bytes(
                url, config=self.config, summary_hint=summary_hint
            ),
            submitted_by=submitted_by,
            title_hint=title_hint,
            source_tag=source_tag,
        )

    async def _process_pdf_task(
        self,
        client: LithosClient,
        task_id: str,
        *,
        local_path: Any,
        submitted_by: str,
        title_hint: str | None,
        summary_hint: str | None,
        source_tag: str,
    ) -> _ItemOutcome | None:
        """Validate a ``kind="pdf"`` submission and ingest it (v2 §16).

        Validates ``local_path`` against ``[inbox] pdf_root`` and the file's
        existence **before** acquisition, completing the task terminally (no
        auto-retry) on any failure.  Unlike the URL path, the file is acquired
        (read + hashed + extracted — all local, cheap) before the cache lookup,
        because the synthetic ``source_url`` is only known after hashing and
        the cache-hit replay needs the body anyway (§16.4).
        """
        if not isinstance(local_path, str) or not local_path:
            await self._complete_terminal(
                client,
                task_id,
                outcome="error: invalid submission (kind 'pdf' needs local_path)",
                metric_outcome="invalid_submission",
                inbox_result={"local_path": local_path, "error": "invalid_submission"},
            )
            return None
        if not _valid_source_tag(source_tag):
            await self._complete_terminal(
                client,
                task_id,
                outcome="error: invalid_source_tag",
                metric_outcome="invalid_source_tag",
                inbox_result={"local_path": local_path, "error": "invalid_source_tag"},
            )
            return None
        pdf_root = self.config.inbox.pdf_root
        if not pdf_root:
            await self._complete_terminal(
                client,
                task_id,
                outcome="error: pdf_root_not_configured",
                metric_outcome="pdf_rejected",
                inbox_result={
                    "local_path": local_path,
                    "error": "pdf_root_not_configured",
                },
            )
            return None
        try:
            resolved = resolve_within_root(local_path, Path(pdf_root))
        except ArchivePathError:
            await self._complete_terminal(
                client,
                task_id,
                outcome="error: path_not_in_pdf_root",
                metric_outcome="pdf_rejected",
                inbox_result={
                    "local_path": local_path,
                    "error": "path_not_in_pdf_root",
                },
            )
            return None
        try:
            is_file = resolved.is_file()
            size = resolved.stat().st_size if is_file else 0
        except OSError:
            is_file = False
            size = 0
        if not is_file:
            await self._complete_terminal(
                client,
                task_id,
                outcome=f"file_missing: {local_path}",
                metric_outcome="pdf_rejected",
                inbox_result={"local_path": local_path, "error": "file_missing"},
            )
            return None
        # Bound memory: a local PDF is read whole into memory to hash +
        # extract, so cap it by the same knob the URL fetch uses.
        max_bytes = self.config.storage.max_download_bytes
        if size > max_bytes:
            await self._complete_terminal(
                client,
                task_id,
                outcome=f"error: pdf_too_large ({size} > {max_bytes} bytes)",
                metric_outcome="pdf_rejected",
                inbox_result={"local_path": local_path, "error": "pdf_too_large"},
            )
            return None

        # Read happens in the worker thread; guard the TOCTOU window (the file
        # could vanish/become unreadable between the stat above and the read)
        # so a doomed read completes the task terminally rather than crashing
        # the item and leaving the claim to be re-tried (§5.6 no auto-retry).
        try:
            acquired = await asyncio.to_thread(
                acquire_inbox_pdf,
                resolved,
                config=self.config,
                summary_hint=summary_hint,
            )
        except OSError:
            logger.warning(
                "inbox pdf read failed task_id=%s path=%s",
                task_id,
                local_path,
                exc_info=True,
            )
            await self._complete_terminal(
                client,
                task_id,
                outcome=f"error: file_read_error: {local_path}",
                metric_outcome="pdf_rejected",
                inbox_result={"local_path": local_path, "error": "file_read_error"},
            )
            return None
        return await self._ingest_item(
            client=client,
            source_url=acquired.source_url,
            acquired=acquired,
            submitted_by=submitted_by,
            title_hint=title_hint,
            source_tag=source_tag,
        )

    async def _ingest_item(
        self,
        *,
        client: LithosClient,
        source_url: str,
        submitted_by: str,
        title_hint: str | None,
        source_tag: str,
        acquire: Callable[[], InboxAcquisition] | None = None,
        acquired: InboxAcquisition | None = None,
    ) -> _ItemOutcome | None:
        """Acquire once, score every enabled profile, fan out, and report.

        The item is identified by *source_url* (used for the cache lookup).
        Acquisition is supplied either pre-computed via *acquired* (the PDF
        path, which must hash the file to know its source_url before the
        lookup) or lazily via the *acquire* thunk (the URL path, run only
        after a cache miss so a full cache-hit skips the network fetch).

        Scores the item against all enabled profiles (in parallel, reusing
        the single acquisition), dispatches a ``RunKind.INBOX`` Run for each
        profile that clears its threshold (sequentially, so the first creates
        the canonical note and the rest merge into it), and aggregates the
        result.

        Returns the per-item outcome used to complete the task, or ``None``
        to signal skip-this-tick — used when *every* clearing profile is busy
        (§5.5 / §10): the task is left un-completed so a later tick retries.
        """
        started = time.monotonic()
        profiles = self.config.profiles
        scorer = make_default_batch_scorer(self.config)
        if not profiles or scorer is None:
            metrics.inbox_items_processed().add(1, {"outcome": "error"})
            reason = "no_profiles" if not profiles else "no_filter_model"
            return _ItemOutcome(
                outcome=f"error: {reason}",
                cited_nodes=[],
                inbox_result={"source_url": source_url, "error": reason},
            )

        # ── Cache-hit replay (§6) ─────────────────────────────────────
        # If this URL is already a note, score + dispatch only the
        # *complement* — profiles that have neither ingested it nor been
        # operator-suppressed (``influx:rejected:<profile>``).  This both
        # avoids re-dispatching profiles already present and re-scores
        # profiles a previous tick left out (e.g. busy-skipped), which is
        # how the §10 try-acquire-skip work is picked up on a later tick.
        cache_note_id = await self._lookup_existing(client, source_url)
        candidate_profiles = profiles
        if cache_note_id is not None:
            candidate_profiles = await self._complement_profiles(
                client, cache_note_id, profiles
            )
            if not candidate_profiles:
                metrics.inbox_items_processed().add(1, {"outcome": "cache_hit"})
                # The URL path skips acquisition on a full cache-hit (archive
                # null); the PDF path pre-acquired (it had to hash the file to
                # know source_url), so report its real archive_path.
                return _ItemOutcome(
                    outcome=(
                        f"cache_hit: existing note {cache_note_id}; "
                        "no new profiles to consider"
                    ),
                    cited_nodes=[cache_note_id],
                    inbox_result={
                        "source_url": source_url,
                        "archive_path": acquired.archive_path if acquired else None,
                        "cache_hit": True,
                        "note_id": cache_note_id,
                        "per_profile": {},
                        "processing_time_ms": int((time.monotonic() - started) * 1000),
                    },
                )

        # Acquire once (blocking download/extract → thread); bytes + extracted
        # text are reused across every per-profile filter + dispatch below.
        # The PDF path pre-acquires (it hashes the file to derive source_url
        # before the lookup); the URL path acquires lazily here, after the
        # cache miss, so a full cache-hit skips the network fetch entirely.
        if acquired is None:
            if acquire is None:  # exactly one of acquire/acquired is always set
                raise ValueError(
                    "_ingest_item requires exactly one of acquire/acquired"
                )
            acquired = await asyncio.to_thread(acquire)
        candidate = Candidate(
            item_id=f"inbox-{acquired.url_hash}",
            title=title_hint or acquired.source_url,
            abstract=acquired.summary,
            source_url=acquired.source_url,
        )

        # ── Filter fan-out: score every profile in parallel (§8) ──────
        # Call the batch scorer directly (not Filter.score) so the raw
        # per-profile score is available even below threshold — needed for
        # the per_profile payload (§7.3) and the "top score K" outcome.
        # Trade-off: inbox scoring does not emit the per-decision
        # ``articles_filtered`` metric that Filter.score does (the inbox
        # surfaces outcomes via ``inbox_items_processed`` instead).
        async def _score(profile_cfg: Any) -> _ProfileScore:
            threshold = profile_cfg.thresholds.relevance
            try:
                prompt = await build_filter_prompt(
                    self.config, client, profile=profile_cfg.name
                )
                scores = await scorer([candidate], profile_cfg.name, prompt)
                return _ProfileScore(
                    name=profile_cfg.name,
                    threshold=threshold,
                    scored=scores.get(candidate.item_id),
                    error=False,
                )
            except Exception:  # noqa: BLE001 — per-profile filter isolation (§5.5)
                logger.warning(
                    "inbox filter failed for profile %s",
                    profile_cfg.name,
                    exc_info=True,
                )
                return _ProfileScore(
                    name=profile_cfg.name, threshold=threshold, scored=None, error=True
                )

        # gather preserves config order; profile count is small (§17 leaves a
        # tighter concurrency cap as a future tuning knob).
        scored_profiles = list(
            await asyncio.gather(*(_score(p) for p in candidate_profiles))
        )
        clearing = [ps for ps in scored_profiles if ps.clears]

        if not clearing:
            return self._filtered_out_outcome(
                acquired, scored_profiles, cache_note_id, started
            )

        # ── Per-(item, Profile) dispatch: sequential so the first write
        # creates the canonical note and the rest merge into it ────────
        dispatched: dict[str, tuple[RunOutcome, str]] = {}
        busy: list[str] = []
        dispatch_failed: dict[str, str] = {}
        for ps in clearing:
            if ps.scored is None:  # ps.clears guarantees this; defensive guard
                continue
            run_id = uuid.uuid4().hex
            try:
                async with self.coordinator.hold(ps.name):
                    outcome = await dispatch_profile(
                        ps.name,
                        scored=ps.scored,
                        acquired=acquired,
                        source_tag=source_tag,
                        submitted_by=submitted_by,
                        title_hint=title_hint,
                        config=self.config,
                        probe_loop=self.probe_loop,
                        ledger=self.ledger,
                        run_id=run_id,
                    )
                dispatched[ps.name] = (outcome, run_id)
            except ProfileBusyError:
                busy.append(ps.name)
                logger.info("inbox profile %s busy; skipping this tick", ps.name)
            except Exception:  # noqa: BLE001 — per-profile dispatch isolation (§5.5)
                # A dispatch failure for one profile must not abandon the item:
                # the lock is already released, sibling profiles still run, and
                # the item completes with a partial outcome.  Abandoning here
                # would orphan an earlier profile's write and double its ledger
                # entry on the retry.
                logger.warning(
                    "inbox dispatch failed for profile %s", ps.name, exc_info=True
                )
                dispatch_failed[ps.name] = "dispatch_error"

        if not dispatched and not dispatch_failed:
            # Every clearing profile was busy — skip this tick and let a later
            # tick retry the whole item (§5.5 / §10); do NOT complete.
            metrics.inbox_items_processed().add(1, {"outcome": "profile_busy_skipped"})
            return None

        ingested_names = [
            name for name, (o, _rid) in dispatched.items() if o.ingested > 0
        ]
        # On a cache hit the canonical note id is already known; otherwise
        # recover it after the first write created it.
        note_id = cache_note_id
        if note_id is None and ingested_names:
            note_id = await self._recover_note_id(client, acquired.source_url)

        per_profile = self._build_per_profile(
            scored_profiles, dispatched, busy, dispatch_failed, note_id
        )
        filter_errors = [ps.name for ps in scored_profiles if ps.error]
        outcome_str = self._build_outcome_string(
            ingested_names,
            dispatched,
            busy,
            dispatch_failed,
            filter_errors,
            cache_note_id,
        )
        if cache_note_id is not None:
            metric_outcome = "cache_hit"
        else:
            metric_outcome = "ingested" if ingested_names else "error"
        metrics.inbox_items_processed().add(1, {"outcome": metric_outcome})

        return _ItemOutcome(
            outcome=outcome_str,
            cited_nodes=[note_id] if note_id else [],
            inbox_result={
                "source_url": acquired.source_url,
                "archive_path": acquired.archive_path,
                "cache_hit": cache_note_id is not None,
                "per_profile": per_profile,
                "processing_time_ms": int((time.monotonic() - started) * 1000),
            },
        )

    def _filtered_out_outcome(
        self,
        acquired: InboxAcquisition,
        scored_profiles: list[_ProfileScore],
        cache_note_id: str | None,
        started: float,
    ) -> _ItemOutcome:
        """Build the terminal outcome when no candidate profile cleared (§7.1).

        Frames as a cache-hit replay (``no new profiles matched``) when this
        URL is already a note, else a fresh ``filtered out: top score …``.
        """
        if cache_note_id is not None:
            metrics.inbox_items_processed().add(1, {"outcome": "cache_hit"})
            outcome = (
                f"cache_hit: existing note {cache_note_id}; no new profiles matched"
            )
        else:
            metrics.inbox_items_processed().add(1, {"outcome": "filtered_out"})
            # (score, name, threshold) for every profile the model scored.
            scored = [
                (ps.scored.score, ps.name, ps.threshold)
                for ps in scored_profiles
                if ps.scored is not None
            ]
            if scored:
                top_score, top_name, top_threshold = max(scored)
                outcome = (
                    f"filtered out: top score {top_score} ({top_name}) "
                    f"below threshold {top_threshold}"
                )
            else:
                outcome = "filtered out: no profile scored the item"
        return _ItemOutcome(
            outcome=outcome,
            cited_nodes=[cache_note_id] if cache_note_id else [],
            inbox_result={
                "source_url": acquired.source_url,
                "archive_path": acquired.archive_path,
                "cache_hit": cache_note_id is not None,
                "per_profile": self._build_per_profile(
                    scored_profiles, {}, [], {}, None
                ),
                "processing_time_ms": int((time.monotonic() - started) * 1000),
            },
        )

    async def _lookup_existing(
        self, client: LithosClient, source_url: str
    ) -> str | None:
        """Return the existing canonical note id for *source_url*, or ``None``.

        Best-effort: a lookup failure is treated as a miss so the cache-hit
        replay (§6) is purely an optimisation, never a correctness gate.
        """
        try:
            body = await client.cache_lookup_by_url_body(source_url=source_url)
        except (LithosError, LCMAError):
            logger.warning(
                "inbox cache lookup failed for %s; treating as miss",
                source_url,
                exc_info=True,
            )
            return None
        return _extract_note_id(body)

    async def _complement_profiles(
        self, client: LithosClient, note_id: str, profiles: list[Any]
    ) -> list[Any]:
        """Profiles eligible for cache-hit replay (§6): enabled minus those
        that already ingested the note minus operator-suppressed
        (``influx:rejected:<profile>``).

        Best-effort: on read OR parse failure, fall back to replaying all
        profiles (the in-Run ``write_note`` merge dedupes already-present
        entries idempotently).  Cache-hit replay is an optimisation, never a
        correctness gate — a malformed / non-Influx note sharing the URL must
        not poison the item (which would crash every retry forever).
        """
        try:
            note = await client.read_note(note_id=note_id)
            parsed = parse_note(str(note.get("content") or ""))
            ingested = {e.profile_name for e in parse_profile_relevance(parsed)}
        except (LithosError, LCMAError):
            logger.warning(
                "inbox read_note failed for %s; replaying all profiles",
                note_id,
                exc_info=True,
            )
            return profiles
        except Exception:  # noqa: BLE001 — parse of a non-canonical note must not poison
            logger.warning(
                "inbox could not parse existing note %s; replaying all profiles",
                note_id,
                exc_info=True,
            )
            return profiles
        tags = note.get("tags") or []
        suppressed = {
            tag[len(_REJECTED_TAG_PREFIX) :]
            for tag in tags
            if isinstance(tag, str) and tag.startswith(_REJECTED_TAG_PREFIX)
        }
        excluded = ingested | suppressed
        return [p for p in profiles if p.name not in excluded]

    @staticmethod
    def _build_per_profile(
        scored_profiles: list[_ProfileScore],
        dispatched: dict[str, tuple[RunOutcome, str]],
        busy: list[str],
        dispatch_failed: dict[str, str],
        note_id: str | None,
    ) -> dict[str, Any]:
        """Assemble the structured ``per_profile`` payload (§7.3)."""
        per_profile: dict[str, Any] = {}
        for ps in scored_profiles:
            # score is present for every dispatched / busy / failed profile
            # (all drawn from ``clearing``, which requires a non-None score).
            score = ps.scored.score if ps.scored is not None else None
            if ps.name in dispatched:
                outcome, run_id = dispatched[ps.name]
                ingested = outcome.ingested > 0
                entry: dict[str, Any] = {
                    "score": score,
                    "ingested": ingested,
                    "run_id": run_id,
                }
                if ingested:
                    entry["note_id"] = note_id
                else:
                    entry["reason"] = (
                        outcome.skip_reason or outcome.error or "not_ingested"
                    )
                per_profile[ps.name] = entry
            elif ps.name in dispatch_failed:
                per_profile[ps.name] = {
                    "score": score,
                    "ingested": False,
                    "reason": dispatch_failed[ps.name],
                }
            elif ps.name in busy:
                per_profile[ps.name] = {
                    "score": score,
                    "ingested": False,
                    "reason": "profile_busy",
                }
            elif ps.error:
                per_profile[ps.name] = {"ingested": False, "reason": "filter_error"}
            elif ps.scored is None:
                per_profile[ps.name] = {"ingested": False, "reason": "not_scored"}
            else:
                per_profile[ps.name] = {
                    "score": score,
                    "ingested": False,
                    "reason": "below_threshold",
                }
        return per_profile

    @staticmethod
    def _build_outcome_string(
        ingested_names: list[str],
        dispatched: dict[str, tuple[RunOutcome, str]],
        busy: list[str],
        dispatch_failed: dict[str, str],
        filter_errors: list[str],
        cache_note_id: str | None = None,
    ) -> str:
        """Build the free-text outcome string (§7.1).

        ``total`` counts profiles that were ingestion candidates or failed
        being made into one — cleared-and-dispatched, busy, dispatch-failed,
        and filter-errored.  Profiles that scored below threshold are not
        candidates and are excluded (they surface only in ``per_profile``).
        Filter failures (§5.5 / #196) are reported as ``X filter failed``.
        When *cache_note_id* is set the item is a cache-hit replay (§6) and
        the wording shifts to ``cache_hit: … added N profile entr…``.
        """
        total = len(dispatched) + len(busy) + len(dispatch_failed) + len(filter_errors)
        failed = [
            (name, (o.skip_reason or o.error or "no note written"))
            for name, (o, _rid) in dispatched.items()
            if o.ingested == 0
        ]
        failed += list(dispatch_failed.items())

        issues: list[str] = []
        if busy:
            issues.append(f"{', '.join(busy)} profile_busy")
        for name, reason in failed:
            issues.append(f"{name} failed: {reason}")
        if filter_errors:
            issues.append(f"{', '.join(filter_errors)} filter failed")

        tail = ("; " + "; ".join(issues)) if issues else ""
        names = ", ".join(ingested_names)

        if cache_note_id is not None:
            prefix = f"cache_hit: existing note {cache_note_id}; "
            if not ingested_names:
                return prefix + f"no new profiles matched{tail}"
            entry_word = "entry" if len(ingested_names) == 1 else "entries"
            return (
                prefix
                + f"added {len(ingested_names)} profile {entry_word}: {names}{tail}"
            )

        if not ingested_names:
            return f"not ingested ({'; '.join(issues) or 'no note written'})"
        if not issues and len(ingested_names) == total:
            return f"ingested into {len(ingested_names)} profile(s): {names}"
        return f"ingested into {len(ingested_names)}/{total} profiles ({names}){tail}"

    async def _recover_note_id(
        self, client: LithosClient, source_url: str
    ) -> str | None:
        """Recover the canonical note id for ``cited_nodes`` (§7.2).

        ``RunOutcome`` does not surface the written note id, so re-resolve
        it by exact source-URL cache lookup after the dispatch.  Best-effort
        — a recovery miss simply leaves ``cited_nodes`` empty.
        """
        try:
            body = await client.cache_lookup_by_url_body(source_url=source_url)
        except (LithosError, LCMAError):
            logger.debug("note-id recovery failed for %s", source_url, exc_info=True)
            return None
        return _extract_note_id(body)

    async def _complete(
        self, client: LithosClient, task_id: str, result: _ItemOutcome
    ) -> None:
        """Attach the structured ``inbox_result`` then complete the task.

        ``task_update`` failures are non-fatal: the structured payload is a
        convenience, the completion + outcome string is the contract.
        """
        agent = self.config.inbox.agent_id
        try:
            await client.task_update_body(
                task_id=task_id,
                agent=agent,
                metadata={"inbox_result": result.inbox_result},
            )
        except (LithosError, LCMAError):
            logger.warning(
                "inbox task_update failed task_id=%s; completing anyway",
                task_id,
                exc_info=True,
            )
            metrics.inbox_task_call_failures().add(1, {"phase": "update"})
        try:
            await client.task_complete_body(
                task_id=task_id,
                agent=agent,
                outcome=result.outcome,
                cited_nodes=result.cited_nodes or None,
            )
        except (LithosError, LCMAError):
            logger.warning(
                "inbox task_complete failed task_id=%s", task_id, exc_info=True
            )
            metrics.inbox_task_call_failures().add(1, {"phase": "complete"})

    async def _complete_terminal(
        self,
        client: LithosClient,
        task_id: str,
        *,
        outcome: str,
        metric_outcome: str,
        inbox_result: dict[str, Any],
    ) -> None:
        """Record a validation-terminal outcome (#212) then complete the task.

        Centralises the ``inbox_items_processed`` increment for the no-retry
        validation failures (invalid submission / source_tag / pdf rejection)
        that otherwise never reached metrics.  These are pre-flight rejections
        (before acquisition/scoring), so the ``inbox_result`` payload
        intentionally omits ``processing_time_ms`` — consumers should treat it
        as optional and read it with ``.get``.
        """
        metrics.inbox_items_processed().add(1, {"outcome": metric_outcome})
        await self._complete(
            client,
            task_id,
            _ItemOutcome(outcome=outcome, cited_nodes=[], inbox_result=inbox_result),
        )
