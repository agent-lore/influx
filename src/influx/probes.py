"""Background probe loop with cached Lithos + LLM-credentials state.

Provides a background probe loop (≥30s cadence) that caches reachability
and credential state in memory with timestamps, so that ``/ready`` and
``/status`` never issue fresh probes per request (FR-HTTP-7, §5.3).

The Lithos probe opens an SSE connection to the configured Lithos
endpoint and verifies HTTP 200 (PRD 05, US-013).  The probe is
side-effect-free — it does NOT call ``lithos_agent_register``.

The LLM-credentials probe reports on the presence of each configured
provider's ``api_key_env`` environment variable.  Providers whose
``api_key_env`` is the empty string are skipped (keyless providers
like Ollama).  No remote call is made.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx

from influx.config import AppConfig, ProviderConfig

__all__ = [
    "ProbeResult",
    "ProbeState",
    "ProbeLoop",
    "ToolLister",
    "REQUIRED_LCMA_TOOLS",
]

logger = logging.getLogger(__name__)

ProbeStatus = Literal["ok", "degraded"]


# The five LCMA tools every Influx-compatible Lithos deployment must
# expose.  Probed at startup + every probe interval; missing tools flip
# the ``lcma_unknown_tool_failure`` latch so ``Run.execute()`` (and
# today's ``run_profile``) can skip the run with
# ``reason="lcma_tools_unavailable"`` — replacing the legacy per-call
# ``LCMAError("unknown_tool")`` latch (issue #69).
REQUIRED_LCMA_TOOLS: frozenset[str] = frozenset(
    {
        "lithos_retrieve",
        "lithos_edge_upsert",
        "lithos_cache_lookup",
        "lithos_task_create",
        "lithos_task_complete",
    }
)


# Async callable returning the tool names the connected Lithos exposes.
# Wired in production from :meth:`influx.lithos_client.LithosClient.list_tools`.
# Unit tests inject a stub so the probe behaviour can be exercised
# without standing up a real MCP transport.
ToolLister = Callable[[], Awaitable[list[str]]]


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single probe check."""

    status: ProbeStatus
    detail: str = ""
    timestamp: float = 0.0


@dataclass
class ProbeState:
    """Aggregated cached probe state read by ``/ready`` and ``/status``.

    ``max_age`` is the staleness cutoff in seconds; cached probe results
    whose ``timestamp`` is older than ``now - max_age`` are treated as
    stale, forcing ``is_ready`` to ``False`` and ``overall_status`` to
    ``degraded`` (US-002 stale-cache requirement).  A ``max_age`` of
    ``0.0`` disables the check.

    ``repair_write_failure`` is a sticky latch raised by terminal
    sweep-write failures (US-011, §5.4 failure mode 1) — when set,
    ``is_ready`` returns ``False`` and ``overall_status`` becomes
    ``degraded`` until the next successful repair sweep clears it.
    """

    lithos: ProbeResult = field(
        default_factory=lambda: ProbeResult(status="ok", timestamp=0.0)
    )
    llm_credentials: ProbeResult = field(
        default_factory=lambda: ProbeResult(status="ok", timestamp=0.0)
    )
    max_age: float = 0.0
    repair_write_failure: bool = False
    repair_write_failure_detail: str = ""
    lcma_unknown_tool_failure: bool = False
    lcma_unknown_tool_failure_detail: str = ""

    def _has_run(self) -> bool:
        """Return ``True`` once at least one probe cycle has completed.

        ``0.0`` is the unset sentinel; ``time.monotonic()`` is implementation-
        defined and may return small or negative values on freshly-booted
        hosts, so we test inequality against the sentinel rather than ``> 0``.
        """
        return self.lithos.timestamp != 0.0 or self.llm_credentials.timestamp != 0.0

    def is_stale(self, now: float | None = None) -> bool:
        """Return ``True`` when any cached probe result is older than ``max_age``.

        Returns ``False`` when ``max_age == 0.0`` (staleness check
        disabled) or before the first probe cycle has run (``starting``
        handles that case separately).
        """
        if self.max_age <= 0.0:
            return False
        if not self._has_run():
            return False
        t = time.monotonic() if now is None else now
        cutoff = t - self.max_age
        for result in (self.lithos, self.llm_credentials):
            if result.timestamp != 0.0 and result.timestamp < cutoff:
                return True
        return False

    @property
    def is_ready(self) -> bool:
        """Return ``True`` when all probes report ``ok`` AND are fresh."""
        if self.is_stale():
            return False
        if self.repair_write_failure:
            return False
        if self.lcma_unknown_tool_failure:
            return False
        return self.lithos.status == "ok" and self.llm_credentials.status == "ok"

    @property
    def overall_status(self) -> Literal["ok", "degraded", "starting"]:
        """Derive the top-level ``/status.status`` value.

        - ``starting`` before the first probe cycle completes
        - ``degraded`` when any probe fails OR cached results are stale
          OR a terminal repair-sweep write failure is latched
        - ``ok`` when all probes pass, are fresh, and no latched failure
        """
        if not self._has_run():
            return "starting"
        if self.is_stale():
            return "degraded"
        if self.repair_write_failure:
            return "degraded"
        if self.lcma_unknown_tool_failure:
            return "degraded"
        if self.lithos.status == "ok" and self.llm_credentials.status == "ok":
            return "ok"
        return "degraded"


def _probe_lithos(lithos_url: str) -> ProbeResult:
    """Probe Lithos by opening an SSE connection (PRD 05, US-013).

    Connects to the configured Lithos SSE endpoint and verifies
    HTTP 200.  Immediately closes the streaming connection after
    checking the status code.  Does NOT call ``lithos_agent_register``
    — the connection-establishment-only check is side-effect-free.
    """
    now = time.monotonic()
    if not lithos_url:
        return ProbeResult(
            status="degraded",
            detail="Lithos URL not configured",
            timestamp=now,
        )
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(3.0)) as client,
            client.stream("GET", lithos_url) as resp,
        ):
            if resp.status_code == 200:
                return ProbeResult(
                    status="ok",
                    detail="SSE connection ok",
                    timestamp=now,
                )
            return ProbeResult(
                status="degraded",
                detail=f"HTTP {resp.status_code}",
                timestamp=now,
            )
    except httpx.TimeoutException:
        return ProbeResult(
            status="degraded",
            detail=f"Lithos timeout ({lithos_url})",
            timestamp=now,
        )
    except Exception as exc:
        return ProbeResult(
            status="degraded",
            detail=f"Lithos unreachable ({lithos_url}): {exc}",
            timestamp=now,
        )


async def _probe_lcma_tools(tool_lister: ToolLister | None) -> ProbeResult:
    """Probe the LCMA tool surface (issue #69).

    Calls the injected *tool_lister* (typically
    :meth:`LithosClient.list_tools`) and asserts every name in
    :data:`REQUIRED_LCMA_TOOLS` is present.

    Behaviour:
    - ``tool_lister is None`` → probe is skipped.  Returns
      ``status="ok"``, ``detail="skipped"`` so downstream readiness
      checks treat a probe-less deployment (CLI, tests) the same as a
      healthy one rather than blocking it.
    - ``tool_lister`` raises → ``status="degraded"`` with the exception
      kind in ``detail``.  This subsumes connectivity failures (the
      existing ``_probe_lithos`` SSE check still runs in parallel and
      drives the circuit breaker independently).
    - Tool list missing one or more required names → ``status="degraded"``
      with the missing names enumerated in ``detail``.
    - All required names present → ``status="ok"``, ``detail="all
      required LCMA tools present"``.

    Unlike the previous mid-run latch, the result of this probe is
    **non-sticky** — every cycle re-evaluates and the latch flips on
    or off accordingly.
    """
    now = time.monotonic()
    if tool_lister is None:
        return ProbeResult(status="ok", detail="skipped", timestamp=now)
    try:
        tool_names = await tool_lister()
    except Exception as exc:
        return ProbeResult(
            status="degraded",
            detail=f"tools/list failed: {type(exc).__name__}: {exc}",
            timestamp=now,
        )
    missing = sorted(REQUIRED_LCMA_TOOLS - set(tool_names))
    if missing:
        return ProbeResult(
            status="degraded",
            detail=f"missing LCMA tools: {', '.join(missing)}",
            timestamp=now,
        )
    return ProbeResult(
        status="ok",
        detail="all required LCMA tools present",
        timestamp=now,
    )


def _probe_llm_credentials(
    providers: dict[str, ProviderConfig],
) -> ProbeResult:
    """Check that each configured provider's ``api_key_env`` is set.

    Providers whose ``api_key_env`` is the empty string are skipped
    (keyless case, §5.3).  No remote call is made.
    """
    now = time.monotonic()
    missing: list[str] = []
    for name, provider in providers.items():
        if not provider.api_key_env:
            continue  # keyless provider — skip
        if not os.environ.get(provider.api_key_env):
            missing.append(f"{name} ({provider.api_key_env})")

    if missing:
        return ProbeResult(
            status="degraded",
            detail=f"Missing provider credentials: {', '.join(missing)}",
            timestamp=now,
        )
    return ProbeResult(status="ok", detail="all credentials present", timestamp=now)


class ProbeLoop:
    """Background probe loop that caches Lithos + LLM-credentials state.

    Parameters
    ----------
    config:
        The loaded ``AppConfig`` — used to enumerate providers.
    interval:
        Probe interval in seconds (must be ≥ 30).
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        interval: float = 30.0,
        max_age: float | None = None,
        tool_lister: ToolLister | None = None,
    ) -> None:
        if interval < 30.0:
            raise ValueError("Probe interval must be >= 30 seconds")
        self._config = config
        self._interval = interval
        # Default staleness cutoff is 3× the interval — a missed cycle
        # or two shouldn't immediately flip readiness, but an
        # indefinitely stuck loop must.
        self._max_age = max_age if max_age is not None else interval * 3.0
        # Sweep-write-failure latch (US-011, §5.4 failure mode 1).
        # Persisted across probe cycles until ``clear_repair_write_failure``
        # is invoked by a successful sweep.
        self._repair_write_failure = False
        self._repair_write_failure_detail = ""
        # LCMA tools-availability latch (issue #69).  Driven each cycle
        # by ``_probe_lcma_tools`` — non-sticky.  Replaces the legacy
        # mid-run ``LCMAError("unknown_tool")`` latch.
        self._lcma_unknown_tool_failure = False
        self._lcma_unknown_tool_failure_detail = ""
        # Async tool-lister wired by the service factory; ``None`` skips
        # the LCMA-tools probe (e.g. CLI / tests with no MCP transport).
        self._tool_lister = tool_lister
        # Consecutive count of degraded Lithos probes (#40 circuit breaker).
        # Reset to 0 on the first ``ok`` probe; ``lithos_circuit_open``
        # returns True once it crosses the configured threshold so the
        # scheduler can short-circuit further runs and stop burning LLM
        # tokens against a write path that will fail.
        self._lithos_unhealthy_consecutive = 0
        self._state = ProbeState(max_age=self._max_age)
        self._task: asyncio.Task[None] | None = None

    @property
    def state(self) -> ProbeState:
        """Return the latest cached probe state (read by HTTP handlers)."""
        return self._state

    def run_once(self) -> None:
        """Execute the synchronous portion of a probe cycle.

        Refreshes the Lithos SSE probe + LLM-credentials probe.  The
        async LCMA-tools probe is **not** run here — its cached latch
        state is preserved.  Use :meth:`run_once_async` to refresh
        every probe in one cycle.

        Kept as a sync API for backward compatibility with tests that
        drive the loop synchronously and for the legacy bootstrap path.
        Production wiring uses :meth:`run_once_async` via :meth:`start`.
        """
        lithos_result = _probe_lithos(self._config.lithos.url)
        # Update the consecutive-degraded counter (#40 circuit breaker).
        # Reset to 0 on the first ``ok`` so the breaker closes
        # automatically the moment Lithos is reachable again.
        if lithos_result.status == "ok":
            self._lithos_unhealthy_consecutive = 0
        else:
            self._lithos_unhealthy_consecutive += 1
        self._state = ProbeState(
            lithos=lithos_result,
            llm_credentials=_probe_llm_credentials(self._config.providers),
            max_age=self._max_age,
            repair_write_failure=self._repair_write_failure,
            repair_write_failure_detail=self._repair_write_failure_detail,
            lcma_unknown_tool_failure=self._lcma_unknown_tool_failure,
            lcma_unknown_tool_failure_detail=self._lcma_unknown_tool_failure_detail,
        )

    async def run_once_async(self) -> None:
        """Execute a full probe cycle including the async LCMA-tools probe.

        Drives the LCMA tool-availability latch directly from the
        probe result — non-sticky.  When ``tool_lister`` is ``None``
        the LCMA latch state is left at its previous value (typically
        cleared) so a probe-less deployment never spuriously reports
        ``lcma_tools_unavailable``.
        """
        # Synchronous probes first so the Lithos SSE probe state and
        # circuit-breaker counter are refreshed before the LCMA-tools
        # probe runs.  ``_probe_lithos`` also catches transport
        # failures, so a totally-down Lithos shows up as a degraded
        # probe before the LCMA-tools probe reports its own failure.
        self.run_once()
        if self._tool_lister is not None:
            lcma_result = await _probe_lcma_tools(self._tool_lister)
            if lcma_result.status == "degraded":
                self._lcma_unknown_tool_failure = True
                self._lcma_unknown_tool_failure_detail = lcma_result.detail
            else:
                self._lcma_unknown_tool_failure = False
                self._lcma_unknown_tool_failure_detail = ""
            self._state = ProbeState(
                lithos=self._state.lithos,
                llm_credentials=self._state.llm_credentials,
                max_age=self._max_age,
                repair_write_failure=self._repair_write_failure,
                repair_write_failure_detail=self._repair_write_failure_detail,
                lcma_unknown_tool_failure=self._lcma_unknown_tool_failure,
                lcma_unknown_tool_failure_detail=(
                    self._lcma_unknown_tool_failure_detail
                ),
            )

    def lcma_tools_unavailable(self) -> bool:
        """Return ``True`` when the latest probe found the LCMA surface missing.

        The scheduler / Run module consults this before kicking off
        the body; when set, the run is recorded as ``skipped`` with
        ``reason="lcma_tools_unavailable"`` and no source-fetch /
        write work is done.  The latch is non-sticky — the next
        probe cycle re-evaluates from a fresh ``tools/list`` call.
        """
        return self._lcma_unknown_tool_failure

    @property
    def lithos_unhealthy_consecutive(self) -> int:
        """Consecutive ``degraded`` Lithos probes since the last ``ok`` (#40)."""
        return self._lithos_unhealthy_consecutive

    def lithos_circuit_open(self, *, threshold: int = 3) -> bool:
        """Return ``True`` when Lithos has been unhealthy for ``threshold+`` probes.

        The scheduler consults this before kicking off ``_run_profile_body``;
        when the breaker is open, the run is recorded as ``skipped`` with
        ``reason="lithos_unhealthy"`` and no source-fetch / LLM-filter /
        write work is done.  As soon as a probe returns ``ok`` the
        counter resets and the breaker closes automatically.

        ``threshold=3`` with the default 30-second probe interval gives
        ~90 seconds of sustained Lithos failure before the breaker
        opens — long enough to weather a Lithos restart or transient
        network blip without spuriously skipping a sweep.
        """
        return self._lithos_unhealthy_consecutive >= threshold

    def mark_repair_write_failure(
        self,
        *,
        profile: str = "",
        detail: str = "",
    ) -> None:
        """Latch a terminal sweep-write failure (US-011).

        Called by ``run_profile`` when ``SweepWriteError`` propagates
        out of the repair sweep.  Flips ``ProbeState.repair_write_failure``
        so ``/ready`` reports degraded until the next successful sweep
        clears it via :meth:`clear_repair_write_failure`.
        """
        self._repair_write_failure = True
        self._repair_write_failure_detail = detail or f"profile={profile!r}"
        # Reflect the latch into the cached state so ``/ready`` sees it
        # before the next probe cycle runs.
        self._state.repair_write_failure = True
        self._state.repair_write_failure_detail = self._repair_write_failure_detail

    def clear_repair_write_failure(self) -> None:
        """Clear the sweep-write-failure latch on next successful sweep."""
        self._repair_write_failure = False
        self._repair_write_failure_detail = ""
        self._state.repair_write_failure = False
        self._state.repair_write_failure_detail = ""

    async def start(self) -> None:
        """Start the background probe loop as an ``asyncio.Task``."""
        if self._task is not None:
            return
        # Run one full probe cycle (including the async LCMA-tools
        # probe) so the latch state is populated before any HTTP
        # handler reads ``/ready``.
        await self.run_once_async()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the background task and wait for it to finish."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        """Internal loop — runs a full probe cycle at fixed intervals."""
        while True:
            await asyncio.sleep(self._interval)
            await self.run_once_async()
