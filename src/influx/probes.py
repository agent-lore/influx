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
import os
import time
from dataclasses import dataclass, field
from typing import Literal

import httpx

from influx.config import AppConfig, ProviderConfig

__all__ = [
    "ProbeResult",
    "ProbeState",
    "ProbeLoop",
]

ProbeStatus = Literal["ok", "degraded"]


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
        # LCMA unknown_tool failure latch (PRD 08 FR-LCMA-6).
        self._lcma_unknown_tool_failure = False
        self._lcma_unknown_tool_failure_detail = ""
        self._state = ProbeState(max_age=self._max_age)
        self._task: asyncio.Task[None] | None = None

    @property
    def state(self) -> ProbeState:
        """Return the latest cached probe state (read by HTTP handlers)."""
        return self._state

    def run_once(self) -> None:
        """Execute a single probe cycle (synchronous, no scheduling).

        Useful for tests and for running an initial probe before the
        background loop starts.
        """
        self._state = ProbeState(
            lithos=_probe_lithos(self._config.lithos.url),
            llm_credentials=_probe_llm_credentials(self._config.providers),
            max_age=self._max_age,
            repair_write_failure=self._repair_write_failure,
            repair_write_failure_detail=self._repair_write_failure_detail,
            lcma_unknown_tool_failure=self._lcma_unknown_tool_failure,
            lcma_unknown_tool_failure_detail=self._lcma_unknown_tool_failure_detail,
        )

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

    def mark_lcma_unknown_tool_failure(
        self,
        *,
        profile: str = "",
        detail: str = "",
    ) -> None:
        """Latch an LCMA unknown_tool failure (PRD 08, FR-LCMA-6).

        Called by ``run_profile`` when ``LCMAError("unknown_tool")``
        propagates from an LCMA-dependent call.  Flips
        ``ProbeState.lcma_unknown_tool_failure`` so ``/ready`` reports
        degraded.
        """
        self._lcma_unknown_tool_failure = True
        self._lcma_unknown_tool_failure_detail = detail or f"profile={profile!r}"
        self._state.lcma_unknown_tool_failure = True
        self._state.lcma_unknown_tool_failure_detail = (
            self._lcma_unknown_tool_failure_detail
        )

    def clear_lcma_unknown_tool_failure(self) -> None:
        """Clear the LCMA unknown_tool failure latch."""
        self._lcma_unknown_tool_failure = False
        self._lcma_unknown_tool_failure_detail = ""
        self._state.lcma_unknown_tool_failure = False
        self._state.lcma_unknown_tool_failure_detail = ""

    async def start(self) -> None:
        """Start the background probe loop as an ``asyncio.Task``."""
        if self._task is not None:
            return
        # Run one probe cycle immediately so state is populated.
        self.run_once()
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
        """Internal loop — runs ``run_once()`` at fixed intervals."""
        while True:
            await asyncio.sleep(self._interval)
            self.run_once()
