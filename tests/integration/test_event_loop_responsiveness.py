"""Issue #124 regression: admin API stays responsive during active Runs.

The reported failure mode: while a Run is in flight, blocking source/
filter I/O on the active Run path starves the event loop, so
operator endpoints like ``GET /status`` and ``GET /runs/recent``
stop responding long enough for ``scripts/influx-report.py`` to
time out.

These tests prove the loop stays responsive by simulating slow
filter and HTTP work and asserting that admin endpoints continue
to answer within a tight latency budget.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from influx.config import (
    AppConfig,
    LithosConfig,
    ModelSlotConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ProviderConfig,
    ScheduleConfig,
)
from influx.coordinator import Coordinator
from influx.http_api import install_exception_handlers, router
from influx.http_client import FetchResult
from influx.probes import ProbeLoop
from influx.run_ledger import RunLedger
from influx.scheduler import InfluxScheduler


def _make_config(lithos_url: str, tmp_path: Path) -> AppConfig:
    config = AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[ProfileConfig(name="ai-robotics")],
        providers={
            "openai": ProviderConfig(
                base_url="https://example.invalid/v1",
                api_key_env="OPENAI_API_KEY",
            )
        },
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="filter"),
            tier1_enrich=PromptEntryConfig(text="enrich"),
            tier3_extract=PromptEntryConfig(text="extract"),
        ),
        models={
            "filter": ModelSlotConfig(
                provider="openai",
                model="gpt-4.1-mini",
                temperature=0.0,
                max_retries=0,
                request_timeout=30,
            ),
        },
    )
    config.storage.state_dir = str(tmp_path / "state")
    return config


def _make_app(fake_lithos_sse_url: str, tmp_path: Path) -> FastAPI:
    config = _make_config(fake_lithos_sse_url, tmp_path)
    app = FastAPI()
    app.include_router(router)
    install_exception_handlers(app)

    coordinator = Coordinator()
    scheduler = InfluxScheduler(config, coordinator)
    probe_loop = ProbeLoop(config, interval=30.0)
    probe_loop.run_once()

    app.state.config = config
    app.state.coordinator = coordinator
    app.state.scheduler = scheduler
    app.state.probe_loop = probe_loop
    app.state.run_ledger = RunLedger(Path(config.storage.state_dir))
    return app


@pytest.mark.asyncio
async def test_status_responsive_during_blocking_filter_work(
    fake_lithos_sse_url: str, tmp_path: Path
) -> None:
    """``GET /status`` returns within a tight budget while filter work blocks.

    Reproduces the staging trigger from issue #124: simulate a slow
    filter call (e.g. a 429 backoff) and prove the admin endpoint
    keeps responding while the slow work runs.

    Without the fix (sync filter call on the event loop), every
    ``/status`` request would block until ``slow_filter_call``
    returned. With ``asyncio.to_thread`` offload, the loop stays
    responsive and each ``/status`` returns in milliseconds.
    """
    app = _make_app(fake_lithos_sse_url, tmp_path)

    # Generous slow-call duration so the discrimination margin between
    # "loop is responsive (ms)" and "loop is blocked (~slow_call_seconds)"
    # is wide enough to survive CI-side CPU contention.
    slow_call_seconds = 3.0
    status_budget_seconds = 1.0

    def slow_filter_call(*_args: object, **_kwargs: object) -> Any:
        # Sync sleep — simulates the blocking ``time.sleep`` inside
        # the filter retry/backoff path.
        time.sleep(slow_call_seconds)
        return FetchResult(
            body=b'{"choices":[{"message":{"content":"{\\"results\\":[]}"}}]}',
            status_code=200,
            content_type="application/json",
            final_url="https://example.invalid/v1/chat/completions",
        )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Patch the local re-export inside filter.py (which is what
        # ``_call_filter_model_with_retry`` actually calls), not the
        # http_client source module — ``from … import …`` creates a
        # module-local binding that is unaffected by patching the source.
        with patch(
            "influx.filter.guarded_post_json_fetch",
            side_effect=slow_filter_call,
        ):
            # Kick off the slow filter call via the async wrapper —
            # this is the exact path the Run takes.
            from influx.filter import _acall_filter_model_with_retry

            slow_task = asyncio.create_task(
                _acall_filter_model_with_retry(
                    config=app.state.config,
                    profile="ai-robotics",
                    url="https://example.invalid/v1/chat/completions",
                    body={},
                    headers={},
                    attempts=1,
                )
            )

            # Yield once so the task starts and gets onto the worker thread.
            await asyncio.sleep(0)

            # Poll /status while the slow call is in flight; each
            # request must come back well under the slow-call duration.
            poll_start = time.monotonic()
            for _ in range(5):
                req_start = time.monotonic()
                resp = await client.get("/status")
                req_elapsed = time.monotonic() - req_start
                assert resp.status_code == 200, resp.text
                assert req_elapsed < status_budget_seconds, (
                    f"GET /status took {req_elapsed:.2f}s while filter "
                    f"work was in flight (budget {status_budget_seconds}s); "
                    "event loop is being starved by sync I/O."
                )
            polls_total = time.monotonic() - poll_start

            # Wait for the slow task to finish so the test cleans up.
            await slow_task

            # Sanity: the polls completed BEFORE the slow call finished —
            # confirms the offload is real.
            assert polls_total < slow_call_seconds, (
                f"5 status polls took {polls_total:.2f}s but the slow "
                f"filter call only takes {slow_call_seconds}s; the polls "
                "must have been blocking on the slow call."
            )


@pytest.mark.asyncio
async def test_live_responsive_during_blocking_filter_work(
    fake_lithos_sse_url: str, tmp_path: Path
) -> None:
    """``GET /live`` is the cheapest probe and must always answer fast."""
    app = _make_app(fake_lithos_sse_url, tmp_path)

    slow_call_seconds = 3.0

    def slow_filter_call(*_args: object, **_kwargs: object) -> Any:
        time.sleep(slow_call_seconds)
        return FetchResult(
            body=b'{"choices":[{"message":{"content":"{\\"results\\":[]}"}}]}',
            status_code=200,
            content_type="application/json",
            final_url="https://example.invalid/v1/chat/completions",
        )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Patch the local re-export inside filter.py (which is what
        # ``_call_filter_model_with_retry`` actually calls), not the
        # http_client source module — ``from … import …`` creates a
        # module-local binding that is unaffected by patching the source.
        with patch(
            "influx.filter.guarded_post_json_fetch",
            side_effect=slow_filter_call,
        ):
            from influx.filter import _acall_filter_model_with_retry

            slow_task = asyncio.create_task(
                _acall_filter_model_with_retry(
                    config=app.state.config,
                    profile="ai-robotics",
                    url="https://example.invalid/v1/chat/completions",
                    body={},
                    headers={},
                    attempts=1,
                )
            )
            await asyncio.sleep(0)

            req_start = time.monotonic()
            resp = await client.get("/live")
            req_elapsed = time.monotonic() - req_start
            assert resp.status_code == 200
            assert req_elapsed < 1.0, (
                f"GET /live took {req_elapsed:.2f}s while filter work "
                "was in flight; event loop is being starved."
            )

            await slow_task


@pytest.mark.asyncio
async def test_admin_endpoints_responsive_during_repair_sweep(
    fake_lithos_sse_url: str, tmp_path: Path
) -> None:
    """Repair sweep stage hooks must not starve the admin event loop.

    Stage 1 of every Run is the repair sweep, which calls per-stage
    sync hooks (``archive_download``, ``re_extract_archive``,
    ``tier2_enrich``, ``tier3_extract``, ``text_extraction``) that do
    blocking HTTP / extraction / model work — exactly the same class
    of bug as the acquire/filter starvation.

    This test drives :func:`influx.repair._process_sweep_note`
    directly with a hook that hangs on a ``threading.Event``, so the
    sweep is provably still in flight when ``/status`` and
    ``/runs/recent`` are polled. If the hook ran inline on the event
    loop instead of via ``asyncio.to_thread``, the loop would be
    blocked for the full hook duration and these polls could not
    interleave — the discriminator is "polls succeed AND sweep_task
    is still in flight."
    """
    import threading

    from influx.repair import SweepHooks, _process_sweep_note

    app = _make_app(fake_lithos_sse_url, tmp_path)
    profile = "ai-robotics"

    hook_started = threading.Event()
    release_hook = threading.Event()

    def slow_archive_download(note: dict[str, Any]) -> str:  # noqa: ARG001
        del note  # unused — hook only signals the test, not the note
        # Signals "I'm running" then blocks until the test releases me.
        # Simulates a slow archive HTTP fetch (the blocking
        # ``download_archive`` call inside the real hook). Using an
        # Event rather than ``time.sleep`` makes the discrimination
        # deterministic: the hook is provably mid-flight while the
        # test polls happen.
        hook_started.set()
        # 10s ceiling so a regression that breaks the offload eventually
        # fails the test instead of hanging forever.
        if not release_hook.wait(timeout=10.0):
            raise TimeoutError("hook never released by test")
        return "blog/2026/05/some-archive-path.html"

    note: dict[str, Any] = {
        "id": "test-note-1",
        "tags": [
            "ingested-by:influx",
            "source:blog",
            "profile:ai-robotics",
            "influx:archive-missing",
            "influx:repair-needed",
            "text:abstract-only",
        ],
        "content": (
            "---\nsource_url: https://example.com/post-1\n"
            "tags:\n  - influx:archive-missing\n"
            "confidence: 1.0\n---\n"
            "# Test note\n## Archive\n\n## Summary\nfallback\n## User Notes\n"
        ),
        "version": 1,
    }

    hooks = SweepHooks(
        archive_download=slow_archive_download,
        # Other stage hooks left as None so only archive_retry runs.
    )

    # Stub out the rewrite path — this test isolates the per-stage
    # hook offload, not the lithos_write retry/conflict semantics.
    async def fake_rewrite(*_args: object, **_kwargs: object) -> None:
        return None

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("influx.repair._rewrite_sweep_note", side_effect=fake_rewrite):
            sweep_task = asyncio.create_task(
                _process_sweep_note(
                    note,
                    profile=profile,
                    client=None,  # type: ignore[arg-type] # rewrite is stubbed
                    config=app.state.config,
                    hooks=hooks,
                )
            )

            # Wait for the hook to actually start running. ``Event.wait``
            # is sync; we use ``asyncio.to_thread`` so the wait itself
            # doesn't block the loop. If the production code routes the
            # hook through ``asyncio.to_thread`` (the fix), the hook
            # starts on a worker thread and ``hook_started`` fires.
            # If the production code calls the hook inline, the hook
            # is on the main loop, the main loop is blocked on
            # ``release_hook.wait``, and ``hook_started`` is never set
            # within the wait window — the assertion below fails.
            wait_seconds = 4.0
            started = await asyncio.to_thread(hook_started.wait, wait_seconds)
            if not started:
                release_hook.set()
                sweep_task.cancel()
                raise AssertionError(
                    f"hook never reported started within {wait_seconds}s — "
                    "event loop is being starved by inline sync hook I/O "
                    "(fix not in place)"
                )

            # Hook is mid-flight on a worker thread. Main loop must be
            # free to service admin requests. The discriminator: each
            # poll succeeds quickly AND ``sweep_task`` remains
            # un-finished throughout — proving the polls interleaved
            # with the in-flight hook.
            status_budget_seconds = 1.0
            for _ in range(5):
                assert not sweep_task.done(), (
                    "sweep_task completed before polls could observe in-flight "
                    "interleaving — the hook ran fully before polling started, "
                    "which means the loop was blocked through the hook."
                )

                req_start = time.monotonic()
                status_resp = await client.get("/status")
                status_elapsed = time.monotonic() - req_start
                assert status_resp.status_code == 200, status_resp.text
                assert status_elapsed < status_budget_seconds, (
                    f"GET /status took {status_elapsed:.2f}s while repair "
                    f"sweep was in flight (budget {status_budget_seconds}s)"
                )

                req_start = time.monotonic()
                recent_resp = await client.get("/runs/recent")
                recent_elapsed = time.monotonic() - req_start
                assert recent_resp.status_code == 200, recent_resp.text
                assert recent_elapsed < status_budget_seconds, (
                    f"GET /runs/recent took {recent_elapsed:.2f}s while "
                    f"repair sweep was in flight (budget {status_budget_seconds}s)"
                )

            # Release the hook so the sweep finishes and we clean up.
            release_hook.set()
            await sweep_task
