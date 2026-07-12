"""Unit tests for the request-orchestration collaborator (finding 4).

These drive :class:`influx.run_dispatch.RunDispatcher` directly — a real
``Coordinator`` and a plain ``set`` for the task registry, with
``run_profile`` patched to a recorder. No FastAPI / ASGI stack is needed,
which is the point of extracting the orchestration out of the HTTP router:
the acquire-all-or-rollback loop and background-task tracking are testable
on their own.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from influx.config import (
    AppConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.coordinator import Coordinator, RunKind
from influx.run_dispatch import RunAccepted, RunDispatcher, RunRejectedBusy
from influx.run_ledger import RunLedger


def _make_config(profiles: list[str]) -> AppConfig:
    return AppConfig(
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[ProfileConfig(name=name) for name in profiles],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="test"),
            tier1_enrich=PromptEntryConfig(text="test"),
            tier3_extract=PromptEntryConfig(text="test"),
        ),
    )


def _make_dispatcher(
    coordinator: Coordinator,
    config: AppConfig,
    active_tasks: set[asyncio.Task[object]],
    tmp_path: Path,
    fetch_cache: object = None,
) -> RunDispatcher:
    return RunDispatcher(
        coordinator,
        config,
        run_ledger=RunLedger(tmp_path / "state"),
        active_tasks=active_tasks,
        fetch_cache=fetch_cache,
    )


async def _drain(active_tasks: set[asyncio.Task[object]]) -> None:
    """Let every launched background task run to completion.

    ``return_exceptions=True`` so failure-path tests (where ``run_profile``
    re-raises) can still assert on side effects without the drain re-raising.
    The done-callback already retrieves each task's exception.
    """
    await asyncio.gather(*list(active_tasks), return_exceptions=True)


class _RecordingRunProfile:
    """Records the args each ``run_profile`` call receives."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._raises = raises

    async def __call__(
        self,
        profile: str,
        kind: RunKind,
        run_range: object = None,
        *,
        run_id: object = None,
        **_: object,
    ) -> None:
        self.calls.append(
            {
                "profile": profile,
                "kind": kind,
                "run_range": run_range,
                "run_id": run_id,
            }
        )
        if self._raises:
            raise RuntimeError("run_profile blew up")


class _FakeFetchCache:
    """Counts begin_fire / end_fire so tests can assert they balance."""

    def __init__(self) -> None:
        self.begins = 0
        self.ends = 0

    def begin_fire(self) -> None:
        self.begins += 1

    def end_fire(self) -> None:
        self.ends += 1


async def test_dispatch_one_launches_and_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, RunKind]] = []

    async def record(profile: str, kind: RunKind, run_range=None, **_: object) -> None:  # type: ignore[no-untyped-def]
        calls.append((profile, kind))

    monkeypatch.setattr("influx.run_dispatch.run_profile", record)

    coordinator = Coordinator()
    config = _make_config(["alpha", "beta"])
    active_tasks: set[asyncio.Task[object]] = set()
    dispatcher = _make_dispatcher(coordinator, config, active_tasks, tmp_path)

    outcome = await dispatcher.dispatch_one(
        "alpha",
        RunKind.MANUAL,
        request_id="r1",
        log_context={"request_id": "r1", "kind": "manual", "scope": "alpha"},
    )

    assert outcome == RunAccepted(scope="alpha", profiles=("alpha",))
    # The background task is registered before it completes (shutdown grace).
    assert len(active_tasks) == 1
    await _drain(active_tasks)
    assert calls == [("alpha", RunKind.MANUAL)]
    # The lock is released once the run finishes.
    assert not coordinator.is_busy("alpha")


async def test_dispatch_one_rejects_when_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def record(*_: object, **__: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("influx.run_dispatch.run_profile", record)

    coordinator = Coordinator()
    config = _make_config(["alpha"])
    active_tasks: set[asyncio.Task[object]] = set()
    dispatcher = _make_dispatcher(coordinator, config, active_tasks, tmp_path)

    assert await coordinator.try_acquire("alpha") is True  # someone else holds it

    outcome = await dispatcher.dispatch_one(
        "alpha",
        RunKind.MANUAL,
        request_id="r1",
        log_context={"request_id": "r1", "kind": "manual", "scope": "alpha"},
    )

    assert outcome == RunRejectedBusy("alpha")
    assert active_tasks == set()
    assert called is False


async def test_dispatch_all_launches_every_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def record(profile: str, kind: RunKind, run_range=None, **_: object) -> None:  # type: ignore[no-untyped-def]
        calls.append(profile)

    monkeypatch.setattr("influx.run_dispatch.run_profile", record)

    coordinator = Coordinator()
    config = _make_config(["alpha", "beta"])
    active_tasks: set[asyncio.Task[object]] = set()
    dispatcher = _make_dispatcher(coordinator, config, active_tasks, tmp_path)

    outcome = await dispatcher.dispatch_all(
        RunKind.MANUAL,
        request_id="r1",
        log_context={"request_id": "r1", "kind": "manual", "scope": "all"},
    )

    assert outcome == RunAccepted(scope="all", profiles=("alpha", "beta"))
    await _drain(active_tasks)
    assert sorted(calls) == ["alpha", "beta"]
    assert not coordinator.is_busy("alpha")
    assert not coordinator.is_busy("beta")


async def test_dispatch_all_rolls_back_on_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def record(*_: object, **__: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("influx.run_dispatch.run_profile", record)

    coordinator = Coordinator()
    config = _make_config(["alpha", "beta"])
    active_tasks: set[asyncio.Task[object]] = set()
    dispatcher = _make_dispatcher(coordinator, config, active_tasks, tmp_path)

    # 'beta' is already held, so the all-or-nothing acquire must roll back the
    # 'alpha' lock it grabbed first and reject naming the busy profile.
    assert await coordinator.try_acquire("beta") is True

    outcome = await dispatcher.dispatch_all(
        RunKind.MANUAL,
        request_id="r1",
        log_context={"request_id": "r1", "kind": "manual", "scope": "all"},
    )

    assert outcome == RunRejectedBusy("beta")
    assert active_tasks == set()
    assert called is False
    # Rollback released the lock acquired before hitting the busy profile.
    assert not coordinator.is_busy("alpha")


async def test_dispatch_one_forwards_run_range_and_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingRunProfile()
    monkeypatch.setattr("influx.run_dispatch.run_profile", recorder)

    coordinator = Coordinator()
    config = _make_config(["alpha"])
    active_tasks: set[asyncio.Task[object]] = set()
    dispatcher = _make_dispatcher(coordinator, config, active_tasks, tmp_path)

    await dispatcher.dispatch_one(
        "alpha",
        RunKind.BACKFILL,
        run_range={"days": 7},
        request_id="r1",
        log_context={"request_id": "r1", "kind": "backfill", "scope": "alpha"},
    )
    await _drain(active_tasks)

    assert recorder.calls == [
        {
            "profile": "alpha",
            "kind": RunKind.BACKFILL,
            "run_range": {"days": 7},
            "run_id": "r1",
        }
    ]


async def test_dispatch_all_forwards_run_range_and_per_profile_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingRunProfile()
    monkeypatch.setattr("influx.run_dispatch.run_profile", recorder)

    coordinator = Coordinator()
    config = _make_config(["alpha", "beta"])
    active_tasks: set[asyncio.Task[object]] = set()
    dispatcher = _make_dispatcher(coordinator, config, active_tasks, tmp_path)

    await dispatcher.dispatch_all(
        RunKind.BACKFILL,
        run_range={"days": 3},
        request_id="r1",
        log_context={"request_id": "r1", "kind": "backfill", "scope": "all"},
    )
    await _drain(active_tasks)

    # Each fanned-out run gets the shared run_range and a per-profile run id.
    by_profile = {c["profile"]: c for c in recorder.calls}
    assert by_profile["alpha"]["run_id"] == "r1:alpha"
    assert by_profile["beta"]["run_id"] == "r1:beta"
    assert by_profile["alpha"]["run_range"] == {"days": 3}
    assert by_profile["beta"]["run_range"] == {"days": 3}


async def test_fetch_cache_begin_end_balanced_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("influx.run_dispatch.run_profile", _RecordingRunProfile())

    coordinator = Coordinator()
    config = _make_config(["alpha"])
    active_tasks: set[asyncio.Task[object]] = set()
    fetch_cache = _FakeFetchCache()
    dispatcher = _make_dispatcher(
        coordinator, config, active_tasks, tmp_path, fetch_cache=fetch_cache
    )

    await dispatcher.dispatch_one(
        "alpha",
        RunKind.MANUAL,
        request_id="r1",
        log_context={"request_id": "r1", "kind": "manual", "scope": "alpha"},
    )
    await _drain(active_tasks)

    assert (fetch_cache.begins, fetch_cache.ends) == (1, 1)


async def test_fetch_cache_begin_end_balanced_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "influx.run_dispatch.run_profile", _RecordingRunProfile(raises=True)
    )

    coordinator = Coordinator()
    config = _make_config(["alpha"])
    active_tasks: set[asyncio.Task[object]] = set()
    fetch_cache = _FakeFetchCache()
    dispatcher = _make_dispatcher(
        coordinator, config, active_tasks, tmp_path, fetch_cache=fetch_cache
    )

    await dispatcher.dispatch_one(
        "alpha",
        RunKind.MANUAL,
        request_id="r1",
        log_context={"request_id": "r1", "kind": "manual", "scope": "alpha"},
    )
    await _drain(active_tasks)

    # end_fire runs in the finally even when the run raises, and the lock is
    # still released.
    assert (fetch_cache.begins, fetch_cache.ends) == (1, 1)
    assert not coordinator.is_busy("alpha")
