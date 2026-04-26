"""Per-profile execution coordinator.

Provides a single non-overlap authority that scheduled fires,
``POST /runs``, and ``POST /backfills`` all consult.  Each profile
has an independent lock, so cross-profile parallelism is allowed
while same-profile overlap is rejected.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from enum import Enum


class RunKind(Enum):
    """Discriminator for how a run was initiated."""

    SCHEDULED = "scheduled"
    MANUAL = "manual"
    BACKFILL = "backfill"


class ProfileBusyError(Exception):
    """Raised when a profile lock cannot be acquired (already held)."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        super().__init__(f"Profile {profile!r} is already running")


class Coordinator:
    """Per-profile in-memory lock manager.

    Uses ``asyncio.Lock`` per profile so it is safe to call from both
    async HTTP handlers and scheduler fires (which should be dispatched
    onto the event loop).
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, profile: str) -> asyncio.Lock:
        if profile not in self._locks:
            self._locks[profile] = asyncio.Lock()
        return self._locks[profile]

    def is_busy(self, profile: str) -> bool:
        """Return ``True`` if the profile lock is currently held."""
        lock = self._locks.get(profile)
        return lock is not None and lock.locked()

    async def try_acquire(self, profile: str) -> bool:
        """Try to acquire the profile lock without blocking.

        Returns ``True`` if the lock was acquired, ``False`` if the
        profile is already busy.  The caller is responsible for calling
        :meth:`release` when work is done.
        """
        lock = self._get_lock(profile)
        return lock.locked() is False and await self._do_acquire(lock)

    async def _do_acquire(self, lock: asyncio.Lock) -> bool:
        """Non-blocking acquire helper.

        ``asyncio.Lock`` doesn't have a ``try_lock`` method, so we
        check ``locked()`` first and then immediately ``acquire()``.
        Since everything runs on the same event loop there is no race.
        """
        if lock.locked():
            return False
        await lock.acquire()
        return True

    def release(self, profile: str) -> None:
        """Release the profile lock.

        Raises ``RuntimeError`` if the lock is not held.
        """
        lock = self._locks.get(profile)
        if lock is None:
            raise RuntimeError(f"No lock exists for profile {profile!r}")
        lock.release()

    @asynccontextmanager
    async def hold(self, profile: str) -> AsyncGenerator[None]:
        """Async context manager: acquire-or-raise, auto-release.

        Raises :class:`ProfileBusyError` if the lock is already held.
        """
        acquired = await self.try_acquire(profile)
        if not acquired:
            raise ProfileBusyError(profile)
        try:
            yield
        finally:
            self.release(profile)
