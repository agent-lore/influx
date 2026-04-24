"""App factory, lifecycle, and local-admin bind guard.

Composes the ASGI app, scheduler, coordinator, and probe loop into a
single startable/stoppable service.  The bind guard refuses non-loopback
bind hosts unless ``security.allow_remote_admin = true`` (AC-03-D).

Environment variables:
    ``INFLUX_ADMIN_BIND_HOST`` — bind host (default ``127.0.0.1``)
    ``INFLUX_ADMIN_PORT``      — bind port (default ``8080``)
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket

from fastapi import FastAPI

from influx.config import AppConfig
from influx.coordinator import Coordinator
from influx.errors import ConfigError
from influx.http_api import router
from influx.probes import ProbeLoop
from influx.scheduler import InfluxScheduler

__all__ = [
    "InfluxService",
    "create_app",
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
        return all(
            ipaddress.ip_address(info[4][0]).is_loopback for info in infos
        )
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


def create_app(config: AppConfig) -> FastAPI:
    """Build and return the FastAPI app with all dependencies on ``app.state``.

    Does NOT start the scheduler or probe loop — call
    :meth:`InfluxService.start` for that.
    """
    app = FastAPI(title="Influx Admin API")
    app.include_router(router)

    coordinator = Coordinator()
    scheduler = InfluxScheduler(config, coordinator)
    probe_loop = ProbeLoop(config, interval=30.0)

    app.state.config = config
    app.state.coordinator = coordinator
    app.state.scheduler = scheduler
    app.state.probe_loop = probe_loop

    return app


class InfluxService:
    """Top-level service that owns the ASGI app and all background tasks.

    Exposes a start/stop lifecycle contract that the ``serve`` CLI
    handler drives.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._app = create_app(config)
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
        logger.info("Starting Influx service")
        await self.probe_loop.start()
        self.scheduler.start()
        self._started = True
        logger.info("Influx service started")

    async def stop(self) -> None:
        """Stop the scheduler and probe loop cleanly.

        In-flight runs are allowed to complete within
        ``schedule.shutdown_grace_seconds``.
        """
        if not self._started:
            return
        logger.info("Stopping Influx service")
        self.scheduler.stop(wait=True)
        await self.probe_loop.stop()
        self._started = False
        logger.info("Influx service stopped")
