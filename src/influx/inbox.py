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

**Slice 1 scope.** This is the thin end-to-end happy path: one URL is
scored against the *first* enabled profile only, and dispatched if it
clears threshold.  The dispatch already holds the per-Profile coordinator
lock (so inbox never overlaps a scheduled Run for the same profile);
multi-profile fan-out (§5/§8), cache-hit replay (§6), and the richer §10
try-acquire-skip reporting arrive in later slices.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from influx import metrics
from influx.config import AppConfig, InboxConfig
from influx.coordinator import Coordinator, ProfileBusyError, RunKind
from influx.errors import LCMAError, LithosError
from influx.feedback import build_filter_prompt
from influx.filter import Filter, make_default_batch_scorer
from influx.lithos_client import LithosClient
from influx.run import ItemProvider, RunOutcome, RunPlan
from influx.run_service import RunService
from influx.source import BoundScoredCandidate, Candidate, ScoredCandidate
from influx.sources.inbox import (
    InboxAcquisition,
    acquire_inbox_bytes,
    build_inbox_note_item,
)

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


@dataclass(frozen=True)
class _ItemOutcome:
    """Internal per-item result used to build the task completion."""

    outcome: str
    cited_nodes: list[str]
    inbox_result: dict[str, Any]


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
        probe = self.probe_loop
        circuit_open = getattr(probe, "lithos_circuit_open", None)
        if circuit_open is not None and circuit_open():
            logger.info("inbox tick skipped: lithos circuit open")
            return

        metrics.inbox_tick_started().add(1)
        client = self._make_client()
        try:
            try:
                body = await client.task_list_body(
                    tags=[InboxConfig.TASK_TAG],
                    status="open",
                )
            except (LithosError, LCMAError):
                logger.warning("inbox task_list failed; skipping tick", exc_info=True)
                return

            tasks = list(body.get("tasks", []))[: self.config.inbox.max_items_per_tick]
            metrics.inbox_tasks_listed().add(len(tasks))

            for task in tasks:
                try:
                    await self._process_task(task, client)
                except Exception:  # noqa: BLE001 — per-item failure isolation (§5.5)
                    # A crash before _complete leaves the claim to expire and
                    # the task ``open``; a later tick re-claims it.  Re-ingest
                    # is safe because ``lithos_write`` dedupes on source_url.
                    logger.exception(
                        "inbox item processing crashed task_id=%s",
                        task.get("id") if isinstance(task, dict) else "<?>",
                    )
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
            return
        if not claim.get("success"):
            # Already claimed by another influx instance — skip silently.
            return
        metrics.inbox_tasks_claimed().add(1)

        # ── Parse + validate metadata ──────────────────────────────
        metadata = task.get("metadata") or {}
        kind = metadata.get("kind")
        url = metadata.get("url")
        submitted_by = _sanitise_submitter(
            str(metadata.get("submitted_by") or "unknown")
        )
        title_hint = metadata.get("title")
        summary_hint = metadata.get("summary")
        source_tag = metadata.get("source_tag") or _DEFAULT_SOURCE_TAG

        if kind != "url" or not isinstance(url, str) or not url:
            await self._complete(
                client,
                task_id,
                _ItemOutcome(
                    outcome="error: invalid submission (kind must be 'url' with a url)",
                    cited_nodes=[],
                    inbox_result={"source_url": url, "error": "invalid_submission"},
                ),
            )
            return
        if urlparse(url).scheme not in _ALLOWED_URL_SCHEMES:
            await self._complete(
                client,
                task_id,
                _ItemOutcome(
                    outcome="error: invalid_url_scheme (only http/https accepted)",
                    cited_nodes=[],
                    inbox_result={"source_url": url, "error": "invalid_url_scheme"},
                ),
            )
            return
        if not _valid_source_tag(source_tag):
            await self._complete(
                client,
                task_id,
                _ItemOutcome(
                    outcome="error: invalid_source_tag",
                    cited_nodes=[],
                    inbox_result={"source_url": url, "error": "invalid_source_tag"},
                ),
            )
            return

        result = await self._ingest_item(
            client=client,
            url=url,
            submitted_by=submitted_by,
            title_hint=title_hint,
            summary_hint=summary_hint,
            source_tag=source_tag,
        )
        await self._complete(client, task_id, result)

    async def _ingest_item(
        self,
        *,
        client: LithosClient,
        url: str,
        submitted_by: str,
        title_hint: str | None,
        summary_hint: str | None,
        source_tag: str,
    ) -> _ItemOutcome:
        """Acquire, score (first profile), dispatch, and report one item.

        Slice 1 scores only the first enabled profile.  Returns the
        per-item outcome used to complete the task.
        """
        started = time.monotonic()
        profiles = self.config.profiles
        if not profiles:
            metrics.inbox_items_processed().add(1, {"outcome": "error"})
            return _ItemOutcome(
                outcome="error: no profiles configured",
                cited_nodes=[],
                inbox_result={"source_url": url, "error": "no_profiles"},
            )

        # Acquire once (blocking download/extract → thread).
        acquired = await asyncio.to_thread(
            acquire_inbox_bytes, url, config=self.config, summary_hint=summary_hint
        )

        profile_cfg = profiles[0]
        scorer = make_default_batch_scorer(self.config)
        filt = Filter(config=self.config, profile_cfg=profile_cfg, scorer=scorer)
        filter_prompt = await build_filter_prompt(
            self.config, client, profile=profile_cfg.name
        )
        candidate = Candidate(
            item_id=f"inbox-{acquired.url_hash}",
            title=title_hint or acquired.source_url,
            abstract=acquired.summary,
            source_url=acquired.source_url,
        )
        scored_list = await filt.score(
            [candidate], filter_prompt=filter_prompt, source="inbox"
        )

        if not scored_list:
            metrics.inbox_items_processed().add(1, {"outcome": "filtered_out"})
            return _ItemOutcome(
                outcome=(
                    f"filtered out: below threshold "
                    f"({profile_cfg.name} threshold {profile_cfg.thresholds.relevance})"
                ),
                cited_nodes=[],
                inbox_result={
                    "source_url": acquired.source_url,
                    "per_profile": {
                        profile_cfg.name: {
                            "ingested": False,
                            "reason": "below_threshold",
                        }
                    },
                },
            )

        scored = scored_list[0]
        run_id = uuid.uuid4().hex
        # Hold the per-Profile coordinator lock so an inbox dispatch never
        # overlaps a scheduled / manual / backfill Run for the same profile
        # (the service-wide "at most one Run per profile" invariant,
        # FR-SCHED-2/3).  On contention skip this profile this tick and
        # report it — the richer §10 try-acquire-skip reporting + cache-hit
        # replay land in a later slice.
        try:
            async with self.coordinator.hold(profile_cfg.name):
                outcome = await dispatch_profile(
                    profile_cfg.name,
                    scored=scored,
                    acquired=acquired,
                    source_tag=source_tag,
                    submitted_by=submitted_by,
                    title_hint=title_hint,
                    config=self.config,
                    probe_loop=self.probe_loop,
                    ledger=self.ledger,
                    run_id=run_id,
                )
        except ProfileBusyError:
            metrics.inbox_items_processed().add(1, {"outcome": "profile_busy_skipped"})
            return _ItemOutcome(
                outcome=(
                    f"profile_busy: {profile_cfg.name} already running; "
                    "resubmit to retry"
                ),
                cited_nodes=[],
                inbox_result={
                    "source_url": acquired.source_url,
                    "per_profile": {
                        profile_cfg.name: {
                            "score": scored.score,
                            "ingested": False,
                            "reason": "profile_busy",
                        }
                    },
                },
            )

        ingested = outcome.ingested > 0
        # Only recover the note id when a write actually happened — a skipped
        # or write-less run would otherwise attach a stale/missing lookup.
        note_id = (
            await self._recover_note_id(client, acquired.source_url)
            if ingested
            else None
        )
        cited = [note_id] if note_id else []
        elapsed_ms = int((time.monotonic() - started) * 1000)

        per_profile: dict[str, Any] = {
            profile_cfg.name: {
                "score": scored.score,
                "ingested": ingested,
                "note_id": note_id,
                "run_id": run_id,
            }
        }
        inbox_result = {
            "source_url": acquired.source_url,
            "archive_path": acquired.archive_path,
            "per_profile": per_profile,
            "processing_time_ms": elapsed_ms,
        }

        if ingested:
            metrics.inbox_items_processed().add(1, {"outcome": "ingested"})
            outcome_str = f"ingested into 1 profile(s): {profile_cfg.name}"
        elif outcome.skipped:
            metrics.inbox_items_processed().add(1, {"outcome": "error"})
            outcome_str = (
                f"skipped ({profile_cfg.name}): {outcome.skip_reason or 'unknown'}"
            )
        else:
            metrics.inbox_items_processed().add(1, {"outcome": "error"})
            detail = outcome.error or "no note written"
            outcome_str = f"not ingested ({profile_cfg.name}): {detail}"

        return _ItemOutcome(
            outcome=outcome_str, cited_nodes=cited, inbox_result=inbox_result
        )

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
