"""Tests for the per-profile execution coordinator (US-001)."""

from __future__ import annotations

import asyncio

import pytest

from influx.coordinator import Coordinator, ProfileBusyError, RunKind

# ── RunKind enum ─────────────────────────────────────────────────────


class TestRunKind:
    def test_enum_values(self) -> None:
        assert RunKind.SCHEDULED.value == "scheduled"
        assert RunKind.MANUAL.value == "manual"
        assert RunKind.BACKFILL.value == "backfill"


# ── Same-profile overlap → reject ────────────────────────────────────


class TestSameProfileOverlap:
    @pytest.mark.asyncio
    async def test_try_acquire_same_profile_twice_rejected(self) -> None:
        """Attempting to acquire a lock for a profile that is already held
        is rejected — the HTTP layer can translate this to 409 Conflict."""
        coord = Coordinator()
        assert await coord.try_acquire("alpha") is True
        assert await coord.try_acquire("alpha") is False
        coord.release("alpha")

    @pytest.mark.asyncio
    async def test_hold_same_profile_twice_raises_profile_busy(self) -> None:
        """The context-manager form raises ``ProfileBusyError``."""
        coord = Coordinator()
        async with coord.hold("alpha"):
            with pytest.raises(ProfileBusyError, match="alpha"):
                async with coord.hold("alpha"):
                    pass  # pragma: no cover

    @pytest.mark.asyncio
    async def test_profile_busy_error_carries_profile_name(self) -> None:
        coord = Coordinator()
        async with coord.hold("beta"):
            try:
                async with coord.hold("beta"):
                    pass  # pragma: no cover
            except ProfileBusyError as exc:
                assert exc.profile == "beta"

    @pytest.mark.asyncio
    async def test_is_busy_reflects_lock_state(self) -> None:
        coord = Coordinator()
        assert coord.is_busy("alpha") is False
        assert await coord.try_acquire("alpha") is True
        assert coord.is_busy("alpha") is True
        coord.release("alpha")
        assert coord.is_busy("alpha") is False


# ── Cross-profile overlap → allow ────────────────────────────────────


class TestCrossProfileOverlap:
    @pytest.mark.asyncio
    async def test_two_different_profiles_can_hold_concurrently(self) -> None:
        """Two different profiles MAY hold their locks concurrently."""
        coord = Coordinator()
        assert await coord.try_acquire("alpha") is True
        assert await coord.try_acquire("beta") is True

        assert coord.is_busy("alpha") is True
        assert coord.is_busy("beta") is True

        coord.release("alpha")
        coord.release("beta")

    @pytest.mark.asyncio
    async def test_hold_context_manager_cross_profile(self) -> None:
        """Nested context managers for different profiles both succeed."""
        coord = Coordinator()
        async with coord.hold("alpha"), coord.hold("beta"):
            assert coord.is_busy("alpha") is True
            assert coord.is_busy("beta") is True
        assert coord.is_busy("alpha") is False
        assert coord.is_busy("beta") is False


# ── Lock lifecycle ───────────────────────────────────────────────────


class TestLockLifecycle:
    @pytest.mark.asyncio
    async def test_release_without_acquire_raises(self) -> None:
        coord = Coordinator()
        # Force creation of a lock object without acquiring
        coord._get_lock("alpha")
        with pytest.raises(RuntimeError):
            coord.release("alpha")

    @pytest.mark.asyncio
    async def test_release_unknown_profile_raises(self) -> None:
        coord = Coordinator()
        with pytest.raises(RuntimeError):
            coord.release("nonexistent")

    @pytest.mark.asyncio
    async def test_reacquire_after_release(self) -> None:
        """After release, the same profile can be acquired again."""
        coord = Coordinator()
        assert await coord.try_acquire("alpha") is True
        coord.release("alpha")
        assert await coord.try_acquire("alpha") is True
        coord.release("alpha")

    @pytest.mark.asyncio
    async def test_hold_releases_on_exception(self) -> None:
        """The context manager releases the lock even on exception."""
        coord = Coordinator()
        with pytest.raises(ValueError, match="boom"):
            async with coord.hold("alpha"):
                raise ValueError("boom")
        assert coord.is_busy("alpha") is False

    @pytest.mark.asyncio
    async def test_concurrent_tasks_same_profile_serialise(self) -> None:
        """Two async tasks competing for the same profile lock serialise."""
        coord = Coordinator()
        results: list[str] = []

        async def worker(name: str) -> None:
            acquired = await coord.try_acquire("shared")
            if acquired:
                results.append(f"{name}-acquired")
                await asyncio.sleep(0)  # yield
                coord.release("shared")
                results.append(f"{name}-released")
            else:
                results.append(f"{name}-rejected")

        await asyncio.gather(worker("A"), worker("B"))

        # One must succeed, one must be rejected
        acquired_count = sum(1 for r in results if r.endswith("-acquired"))
        rejected_count = sum(1 for r in results if r.endswith("-rejected"))
        assert acquired_count == 1
        assert rejected_count == 1
