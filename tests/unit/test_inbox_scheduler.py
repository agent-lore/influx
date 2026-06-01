"""Inbox-tick scheduler registration tests (Inbox v1 slice 1).

The ``influx-inbox-tick`` cron job is registered only when ``[inbox]``
is enabled AND an ``inbox_tick`` orchestrator is wired — independent of
the per-Profile ``influx-tick`` job.
"""

from __future__ import annotations

from influx.config import (
    AppConfig,
    InboxConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.coordinator import Coordinator
from influx.scheduler import InfluxScheduler


def _config(*, inbox: InboxConfig | None = None) -> AppConfig:
    return AppConfig(
        schedule=ScheduleConfig(),
        profiles=[ProfileConfig(name="alpha")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        inbox=inbox or InboxConfig(),
    )


class _StubInboxTick:
    async def execute(self) -> None:  # pragma: no cover — never fired in unit test
        return None


async def test_no_inbox_job_when_disabled() -> None:
    sched = InfluxScheduler(
        _config(inbox=InboxConfig(enabled=False)),
        Coordinator(),
        inbox_tick=_StubInboxTick(),
    )
    sched.start()
    try:
        job_ids = {j.id for j in sched.jobs}
        assert "influx-inbox-tick" not in job_ids
        assert "influx-tick" in job_ids
    finally:
        sched.stop()


async def test_inbox_job_registered_when_enabled() -> None:
    sched = InfluxScheduler(
        _config(inbox=InboxConfig(enabled=True, poll_cron="*/5 * * * *")),
        Coordinator(),
        inbox_tick=_StubInboxTick(),
    )
    sched.start()
    try:
        job_ids = {j.id for j in sched.jobs}
        assert "influx-inbox-tick" in job_ids
    finally:
        sched.stop()


async def test_no_inbox_job_when_enabled_but_no_tick_wired() -> None:
    """Enabled config without an orchestrator wired registers no inbox job."""
    sched = InfluxScheduler(
        _config(inbox=InboxConfig(enabled=True)),
        Coordinator(),
    )
    sched.start()
    try:
        assert "influx-inbox-tick" not in {j.id for j in sched.jobs}
    finally:
        sched.stop()
