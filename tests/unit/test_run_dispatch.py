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
) -> RunDispatcher:
    return RunDispatcher(
        coordinator,
        config,
        run_ledger=RunLedger(tmp_path / "state"),
        active_tasks=active_tasks,
    )


async def _drain(active_tasks: set[asyncio.Task[object]]) -> None:
    """Let every launched background task run to completion."""
    await asyncio.gather(*list(active_tasks))


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
