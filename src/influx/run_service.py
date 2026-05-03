"""RunService — request-lifecycle wrapper around :class:`influx.run.Run`.

The collaborator that owns "build :class:`RunPlan` → execute
:class:`Run` → record outcome → tick lifecycle metrics" for one
request (CONTEXT.md ``RunService``).

Architecture (after #58 → #59 → #60 → #61)::

    RunService.execute(plan)
        ├─ skip-gate checks (#40 circuit breaker, #69 LCMA tools)
        ├─ ledger.start + run-level metrics + contextvars + tracer span
        ├─ Run(plan, deps).execute()      ← five stages + Lithos task CM
        └─ ledger.complete / ledger.fail / ledger.skip + metric tick

The scheduler's three entry points (scheduled tick, ``POST /runs``,
``POST /backfills``) become thin :class:`RunPlan` builders that hand
off to :class:`RunService`.

This module replaces the legacy ``_run_profile_body`` orchestrator
plus the inline prelude/postlude that lived in
``scheduler.run_profile``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from influx import metrics
from influx.config import AppConfig
from influx.notifications import ProfileRunResult
from influx.run import (
    ItemProvider,
    Run,
    RunDeps,
    RunOutcome,
    RunPlan,
    default_item_provider,
)
from influx.run_ledger import RunLedger
from influx.telemetry import (
    current_run_id,
    current_source_acquisition_errors,
    get_tracer,
)

__all__ = ["RunService", "ledger_lifecycle"]

logger = logging.getLogger(__name__)


# ── Outer ledger lifecycle (Q2 grilling — Option B, structural CM) ──


@dataclass(slots=True)
class _LedgerSession:
    """Mutable handle :func:`ledger_lifecycle` yields to its body.

    The body sets ``skip_reason`` to record a skip-before-task case
    (circuit breaker, LCMA tools unavailable); the CM writes
    ``ledger.skip(...)`` on exit instead of ``ledger.complete(...)``.
    ``outcome`` flows back from the body so the CM can hand the
    correct ``sources_checked`` / ``ingested`` to ``ledger.complete``.
    """

    run_id: str
    started_at: datetime
    skip_reason: str | None = None
    outcome: RunOutcome | None = None
    error: BaseException | None = None


@asynccontextmanager
async def ledger_lifecycle(
    plan: RunPlan, deps: RunDeps
) -> AsyncIterator[_LedgerSession]:
    """Outer lifecycle CM — ledger entry + run-level metrics + contextvars.

    Mirrors the prelude/postlude block from the legacy
    ``run_profile``: ``ledger.start`` on enter, ``ledger.complete``
    or ``ledger.fail`` on exit, with metrics ticked at both ends.
    Skip cases (circuit breaker / LCMA tools unavailable) set
    ``session.skip_reason`` and the CM writes a ``ledger.skip`` entry.
    """
    config = deps.config
    profile = plan.profile
    run_id = deps.run_id or str(uuid.uuid4())
    ledger = deps.ledger or RunLedger(Path(config.storage.state_dir))

    started_at = datetime.now(UTC)
    session = _LedgerSession(run_id=run_id, started_at=started_at)
    run_id_token = current_run_id.set(run_id)
    source_errors_token = current_source_acquisition_errors.set([])
    metric_attrs = {"profile": profile, "run_type": plan.kind.value}

    ledger.start(
        run_id=run_id,
        profile=profile,
        kind=plan.kind.value,
        run_range=plan.date_window,
    )
    metrics.run_starts().add(1, metric_attrs)
    metrics.active_runs().add(1, {"profile": profile})
    logger.info(
        "run started profile=%s kind=%s run_id=%s range=%s",
        profile,
        plan.kind.value,
        run_id,
        plan.date_window or {},
    )

    tracer = get_tracer()
    try:
        with tracer.span(
            "influx.run",
            attributes={
                "influx.profile": profile,
                "influx.run_id": run_id,
                "influx.run_type": plan.kind.value,
            },
        ):
            try:
                yield session
            except BaseException as exc:
                session.error = exc
                raise
        # Telemetry span exited cleanly; finalise the ledger entry.
        elapsed = (datetime.now(UTC) - started_at).total_seconds()
        source_errors = current_source_acquisition_errors.get() or []

        if session.skip_reason is not None:
            ledger.skip(run_id=run_id, reason=session.skip_reason)
            metrics.runs_skipped().add(
                1, {"profile": profile, "reason": session.skip_reason}
            )
            metrics.run_duration().record(elapsed, metric_attrs)
            metrics.run_completions().add(1, {**metric_attrs, "outcome": "skipped"})
            logger.warning(
                "run skipped profile=%s kind=%s run_id=%s reason=%s",
                profile,
                plan.kind.value,
                run_id,
                session.skip_reason,
            )
            return

        outcome = session.outcome
        sources_checked = outcome.sources_checked if outcome is not None else None
        ingested = outcome.ingested if outcome is not None else None
        degraded_reasons = ledger.complete(
            run_id=run_id,
            sources_checked=sources_checked,
            ingested=ingested,
            source_acquisition_errors=source_errors,
        )
        run_outcome = "degraded" if source_errors else "success"
        if "ingestion_stall" in degraded_reasons:
            metrics.ingestion_stalls().add(1, {"profile": profile})
            if run_outcome == "success":
                run_outcome = "degraded"
            logger.warning(
                "run flagged ingestion_stall profile=%s kind=%s run_id=%s "
                "(this + prior scheduled run both ingested 0 with "
                "sources_checked > 0)",
                profile,
                plan.kind.value,
                run_id,
            )
        metrics.run_duration().record(elapsed, metric_attrs)
        metrics.run_completions().add(1, {**metric_attrs, "outcome": run_outcome})

        if outcome is not None:
            logger.info(
                "run completed profile=%s kind=%s run_id=%s duration=%.1fs "
                "sources_checked=%d ingested=%d degraded=%s",
                profile,
                plan.kind.value,
                run_id,
                elapsed,
                outcome.sources_checked,
                outcome.ingested,
                bool(source_errors),
            )
        else:
            logger.info(
                "run completed profile=%s kind=%s run_id=%s duration=%.1fs result=none",
                profile,
                plan.kind.value,
                run_id,
                elapsed,
            )
    except BaseException:
        # The body raised — finalise as failure.
        elapsed = (datetime.now(UTC) - started_at).total_seconds()
        exc = session.error
        logger.exception(
            "run failed profile=%s kind=%s run_id=%s duration=%.1fs",
            profile,
            plan.kind.value,
            run_id,
            elapsed,
        )
        ledger.fail(
            run_id=run_id,
            error=f"{type(exc).__name__}: {exc}" if exc is not None else "unknown",
        )
        metrics.run_duration().record(elapsed, metric_attrs)
        metrics.run_completions().add(1, {**metric_attrs, "outcome": "failure"})
        raise
    finally:
        metrics.active_runs().add(-1, {"profile": profile})
        current_run_id.reset(run_id_token)
        current_source_acquisition_errors.reset(source_errors_token)


# ── RunService ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunService:
    """Request-lifecycle wrapper that drives one :class:`Run` execution.

    Built once per request from a :class:`AppConfig` plus the
    long-lived dependencies (item_provider, probe_loop, ledger).  Call
    :meth:`execute` with a :class:`RunPlan`; returns the
    :class:`RunOutcome`.

    Each :meth:`execute` call:

    1. Checks the two probe-time skip gates (``lithos_circuit_open``,
       ``lcma_tools_unavailable``).  When either is set, writes a
       ``ledger.skip`` entry, ticks the ``runs_skipped`` metric, and
       returns ``RunOutcome(skipped=True, ...)`` without entering the
       Run body.
    2. Opens :func:`ledger_lifecycle` (ledger entry + metrics +
       contextvars + tracer span).
    3. Delegates to ``Run(plan, deps).execute()`` for the body.
    4. The CM finalises ``ledger.complete`` / ``ledger.fail`` based on
       success / exception path.
    """

    config: AppConfig
    item_provider: ItemProvider | None = None
    probe_loop: Any | None = None
    ledger: RunLedger | None = None

    async def execute(
        self,
        plan: RunPlan,
        *,
        run_id: str | None = None,
    ) -> RunOutcome:
        """Run the full request lifecycle and return the outcome."""
        provider = (
            self.item_provider
            if self.item_provider is not None
            else default_item_provider
        )
        deps = RunDeps(
            config=self.config,
            item_provider=provider,
            probe_loop=self.probe_loop,
            ledger=self.ledger,
            run_id=run_id,
        )

        # ── Skip gates ────────────────────────────────────────────
        skip_reason = self._skip_reason_for(plan)
        if skip_reason is not None:
            return await self._record_skip(plan, deps, skip_reason)

        # ── Lifecycle CM + body ──────────────────────────────────
        async with ledger_lifecycle(plan, deps) as session:
            outcome = await Run(plan, deps).execute()
            session.outcome = outcome
            return outcome

    def _skip_reason_for(self, plan: RunPlan) -> str | None:
        """Return the ``ledger.skip`` reason if a probe latch is set."""
        probe_loop = self.probe_loop
        if probe_loop is None:
            return None
        if (
            hasattr(probe_loop, "lithos_circuit_open")
            and probe_loop.lithos_circuit_open()
        ):
            return "lithos_unhealthy"
        if (
            hasattr(probe_loop, "lcma_tools_unavailable")
            and probe_loop.lcma_tools_unavailable()
        ):
            return "lcma_tools_unavailable"
        return None

    async def _record_skip(
        self, plan: RunPlan, deps: RunDeps, skip_reason: str
    ) -> RunOutcome:
        """Write a ``ledger.skip`` entry and tick the runs_skipped metric.

        Goes through :func:`ledger_lifecycle` so the skip path emits
        the same metric/log shape (``ledger.start`` + ``ledger.skip``
        + ``runs_skipped`` + ``run_completions{outcome=skipped}``) as
        the legacy ``run_profile`` prelude did.
        """
        async with ledger_lifecycle(plan, deps) as session:
            session.skip_reason = skip_reason
        return RunOutcome(skipped=True, skip_reason=skip_reason)


# ── Backwards-compatibility helper for run_profile callers ──────────


async def run_via_service(
    profile: str,
    kind: Any,
    run_range: dict[str, str | int] | None = None,
    *,
    config: AppConfig,
    item_provider: ItemProvider | None = None,
    probe_loop: Any | None = None,
    run_id: str | None = None,
    run_ledger: RunLedger | None = None,
) -> ProfileRunResult | None:
    """Build a :class:`RunPlan`, drive a :class:`RunService`, return the
    legacy :class:`ProfileRunResult` for backward-compatible callers.

    ``run_profile`` (in :mod:`influx.scheduler`) delegates to this
    helper; the three scheduler entry points (cron tick, ``POST /runs``,
    ``POST /backfills``) ultimately route through here.

    The mapping from ``RunKind`` to ``RunPlan`` flag values mirrors the
    body that #58 / #59 / #60 migrated:

    - ``BACKFILL`` → ``skip_repair=True``, ``skip_cache_hits=True``,
      ``notify=False``.
    - everything else → all flags False (run_repair, write cache hits,
      notify) except ``notify=True``.
    """
    from influx.coordinator import RunKind

    is_backfill = kind == RunKind.BACKFILL
    plan = RunPlan(
        profile=profile,
        kind=kind,
        date_window=run_range,
        skip_repair=is_backfill,
        skip_cache_hits=is_backfill,
        notify=not is_backfill,
    )
    service = RunService(
        config=config,
        item_provider=item_provider,
        probe_loop=probe_loop,
        ledger=run_ledger,
    )
    outcome = await service.execute(plan, run_id=run_id)
    # Legacy ``_run_profile_body`` could return ``None`` (used by tests
    # that patched the body out).  Preserve that contract here.
    if outcome is None:
        return None
    return outcome.profile_run_result
