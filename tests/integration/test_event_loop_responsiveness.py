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
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
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
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
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
