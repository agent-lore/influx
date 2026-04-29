"""App factory, lifecycle, and local-admin bind guard.

Composes the ASGI app, scheduler, coordinator, and probe loop into a
single startable/stoppable service.  The bind guard refuses non-loopback
bind hosts unless ``security.allow_remote_admin = true`` (AC-03-D).

Environment variables:
    ``INFLUX_ADMIN_BIND_HOST`` — bind host (default ``127.0.0.1``)
    ``INFLUX_ADMIN_PORT``      — bind port (default ``8080``)
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from influx.config import AppConfig, ProfileThresholds
from influx.coordinator import Coordinator, RunKind
from influx.errors import ConfigError
from influx.filter import make_default_arxiv_filter_scorer
from influx.http_api import install_exception_handlers, router
from influx.notifications import ProfileRunResult, build_digest, send_digest
from influx.probes import ProbeLoop
from influx.run_ledger import RunLedger
from influx.scheduler import InfluxScheduler
from influx.sources import FetchCache, make_item_provider
from influx.sources.arxiv import (
    ArxivFilterScorer,
    ArxivScorer,
)
from influx.telemetry import get_tracer

__all__ = [
    "InfluxService",
    "create_app",
    "post_run_webhook_hook",
    "resolve_bind_address",
    "validate_bind_host",
]

logger = logging.getLogger(__name__)

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8080


def resolve_bind_address() -> tuple[str, int]:
    """Read bind host and port from environment variables.

    Returns ``(host, port)`` with defaults applied.
    """
    host = os.environ.get("INFLUX_ADMIN_BIND_HOST", DEFAULT_BIND_HOST)
    port_str = os.environ.get("INFLUX_ADMIN_PORT", str(DEFAULT_BIND_PORT))
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ConfigError(
            f"INFLUX_ADMIN_PORT={port_str!r} is not a valid integer"
        ) from exc
    return host, port


def _is_loopback(host: str) -> bool:
    """Return ``True`` if *host* resolves to a loopback address."""
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        pass
    # Hostname — resolve it.
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        return all(ipaddress.ip_address(info[4][0]).is_loopback for info in infos)
    except (socket.gaierror, OSError):
        return False


def validate_bind_host(host: str, *, allow_remote_admin: bool) -> None:
    """Raise ``ConfigError`` if *host* is non-loopback and remote admin is not allowed.

    This is the local-admin bind guard described in AC-03-D.
    """
    if not _is_loopback(host) and not allow_remote_admin:
        raise ConfigError(
            f"Bind host {host!r} is not a loopback address and "
            "security.allow_remote_admin is not enabled. "
            "Set security.allow_remote_admin = true in influx.toml "
            "to allow non-loopback bind hosts."
        )


def create_app(
    config: AppConfig,
    lifespan: Any | None = None,
    *,
    arxiv_scorer: ArxivScorer | None = None,
    arxiv_filter_scorer: ArxivFilterScorer | None = None,
) -> FastAPI:
    """Build and return the FastAPI app with all dependencies on ``app.state``.

    Does NOT start the scheduler or probe loop — call
    :meth:`InfluxService.start` for that.

    Parameters
    ----------
    lifespan:
        Optional FastAPI lifespan context manager (async generator).
        When provided, wired into the app for startup/shutdown handling.
    arxiv_scorer:
        Per-item synchronous score-gating override used by tests that
        want deterministic scoring without standing up a real LLM.
        When set, takes precedence over *arxiv_filter_scorer*.  See
        :func:`~influx.sources.arxiv.make_arxiv_item_provider`.
    arxiv_filter_scorer:
        Optional override for the batched LLM filter scorer.  When
        ``None`` the production default
        :func:`~influx.filter.make_default_arxiv_filter_scorer` is
        installed automatically — that default reads ``[models.filter]``
        + ``[prompts.filter]`` from *config* and drives the real
        score-gating contract from US-014/US-015 on the production
        ``InfluxService`` path.  Tests can pass a deterministic batch
        scorer here to exercise the score-gating behaviour without
        mocking HTTP.
    """
    app = FastAPI(title="Influx Admin API", lifespan=lifespan)
    app.include_router(router)
    install_exception_handlers(app)

    coordinator = Coordinator()
    # Tracks in-flight run/backfill tasks — both HTTP-triggered and
    # scheduler-fired — so that shutdown can await them within
    # ``schedule.shutdown_grace_seconds`` (US-008).
    active_tasks: set[asyncio.Task[Any]] = set()
    probe_loop = ProbeLoop(config, interval=30.0)

    # Production default item provider — drives arXiv + RSS fetch with
    # shared FetchCache for per-fire dedup (R-8 mitigation, AC-09-D).
    # When no per-item ``arxiv_scorer`` override is supplied, the batched
    # LLM filter default is installed so the production ``InfluxService``
    # / ``serve`` path drives the score-gated extraction + enrichment
    # behaviour from US-014/US-015.
    if arxiv_scorer is None and arxiv_filter_scorer is None:
        arxiv_filter_scorer = make_default_arxiv_filter_scorer(config)

    # HTTP-triggered runs (POST /runs, POST /backfills) keep using a
    # single shared cache held on ``app.state``; each request brackets
    # its own ``begin_fire``/``end_fire`` scope on it.
    fetch_cache = FetchCache()
    item_provider: Any = make_item_provider(
        config,
        fetch_cache=fetch_cache,
        arxiv_scorer=arxiv_scorer,
        arxiv_filter_scorer=arxiv_filter_scorer,
    )

    # Scheduled ticks use a per-tick factory so cron tick N+1 starts
    # with a fresh dedup scope even if tick N is still running.  Without
    # this, a slow profile in tick N would either block tick N+1
    # entirely or leak its fetched data into tick N+1's cache (review
    # finding).  Same-profile non-overlap is enforced by the coordinator.
    def _scheduled_tick_provider_factory() -> tuple[Any, FetchCache]:
        cache = FetchCache()
        provider = make_item_provider(
            config,
            fetch_cache=cache,
            arxiv_scorer=arxiv_scorer,
            arxiv_filter_scorer=arxiv_filter_scorer,
        )
        return provider, cache

    scheduler = InfluxScheduler(
        config,
        coordinator,
        active_tasks=active_tasks,
        probe_loop=probe_loop,
        item_provider=item_provider,
        fetch_cache=fetch_cache,
        item_provider_factory=_scheduled_tick_provider_factory,
    )

    run_ledger = RunLedger(Path(config.storage.state_dir))
    run_ledger.abandon_active(reason="Influx process restarted")

    app.state.config = config
    app.state.coordinator = coordinator
    app.state.scheduler = scheduler
    app.state.probe_loop = probe_loop
    app.state.active_tasks = active_tasks
    app.state.item_provider = item_provider
    app.state.fetch_cache = fetch_cache
    app.state.run_ledger = run_ledger

    return app


class InfluxService:
    """Top-level service that owns the ASGI app and all background tasks.

    Exposes a start/stop lifecycle contract that the ``serve`` CLI
    handler drives.
    """

    def __init__(self, config: AppConfig, *, with_lifespan: bool = False) -> None:
        self._config = config
        self._app = create_app(
            config,
            lifespan=self.lifespan if with_lifespan else None,
        )
        self._started = False

    @property
    def app(self) -> FastAPI:
        """The underlying FastAPI/ASGI application."""
        return self._app

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def scheduler(self) -> InfluxScheduler:
        return self._app.state.scheduler  # type: ignore[no-any-return]

    @property
    def probe_loop(self) -> ProbeLoop:
        return self._app.state.probe_loop  # type: ignore[no-any-return]

    @property
    def coordinator(self) -> Coordinator:
        return self._app.state.coordinator  # type: ignore[no-any-return]

    async def start(self) -> None:
        """Start the probe loop and scheduler.

        Must be called from within a running event loop (e.g. inside
        uvicorn's lifespan or an ``async with`` block).
        """
        if self._started:
            return
        logger.info(
            "Starting Influx service profiles=%s schedule=%r timezone=%s",
            [profile.name for profile in self._config.profiles],
            self._config.schedule.cron,
            self._config.schedule.timezone,
        )
        tracer = get_tracer(force_rebuild=True)
        logger.info("OTEL telemetry %s", "enabled" if tracer.enabled else "disabled")
        await self.probe_loop.start()
        self.scheduler.start()
        self._started = True
        logger.info("Influx service started")

    async def stop(self) -> None:
        """Stop the scheduler and probe loop cleanly.

        In-flight runs — both scheduler-fired and HTTP-triggered — are
        allowed to complete within ``schedule.shutdown_grace_seconds``;
        anything still outstanding after that bound is cancelled.
        """
        if not self._started:
            return
        logger.info("Stopping Influx service")
        grace = float(self._config.schedule.shutdown_grace_seconds)

        # Prevent new scheduler fires without cancelling in-flight
        # scheduler work.  ``scheduler.stop(wait=False)`` is deferred
        # until after the grace wait so scheduled fires get the same
        # bounded completion window as HTTP-triggered work.
        self.scheduler.pause()

        active_tasks: set[asyncio.Task[Any]] = self._app.state.active_tasks
        pending = [t for t in active_tasks if not t.done()]
        if pending:
            logger.info(
                "Waiting up to %.1fs for %d in-flight task(s) to finish",
                grace,
                len(pending),
            )
            _, still_running = await asyncio.wait(pending, timeout=grace)
            if still_running:
                logger.warning(
                    "Cancelling %d task(s) that exceeded shutdown grace",
                    len(still_running),
                )
                for task in still_running:
                    task.cancel()
                # Do not await after cancel(): the total shutdown wait is
                # sourced entirely from schedule.shutdown_grace_seconds.
                # cancel() has requested cancellation; tasks that ignore
                # it are left to drain in the background so stop() never
                # blocks past the configured grace window.

        # Now fully shut down the scheduler — no in-flight work remains.
        self.scheduler.stop(wait=False)

        await self.probe_loop.stop()
        self._started = False
        logger.info("Influx service stopped")

    @asynccontextmanager
    async def lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        """FastAPI lifespan context manager.

        Starts the service on enter and stops it on exit, so that
        uvicorn's signal handling triggers a clean shutdown.
        """
        await self.start()
        try:
            yield
        finally:
            await self.stop()


def post_run_webhook_hook(
    result: ProfileRunResult,
    config: AppConfig,
    *,
    kind: RunKind,
) -> None:
    """Post-run webhook hook — POSTs digest for non-backfill runs.

    No-op when ``kind`` is ``RunKind.BACKFILL`` (FR-NOT-4).
    Failures inside the sender are logged but do NOT propagate.
    The end-to-end assertion that this hook fires automatically after
    each completed run is covered by US-019.
    """
    if kind == RunKind.BACKFILL:
        return

    profile_cfg = next(
        (p for p in config.profiles if p.name == result.profile),
        None,
    )
    # Fall back to the pydantic ``ProfileThresholds`` field default so the
    # only place this tunable's default lives is config-parsing code
    # (AC-X-1).  In practice ``profile_cfg`` is always present because the
    # caller posts run results for known profiles.
    threshold = (
        profile_cfg.thresholds.notify_immediate
        if profile_cfg
        else ProfileThresholds().notify_immediate
    )

    digest = build_digest(result, notify_immediate_threshold=threshold)
    send_digest(
        digest,
        webhook_url=config.notifications.webhook_url,
        timeout_seconds=config.notifications.timeout_seconds,
        allow_private_ips=config.security.allow_private_ips,
    )
