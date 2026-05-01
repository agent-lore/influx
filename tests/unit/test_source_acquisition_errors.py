"""End-to-end wiring for swallowed source-fetch failures (issue #20).

Verifies that when a source provider catches ``NetworkError`` and
returns zero items, the failure is surfaced through
``current_source_acquisition_errors`` so the run ledger can mark the
run ``degraded=True`` instead of indistinguishable from a quiet window.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from influx.config import (
    AppConfig,
    ProfileConfig,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
)
from influx.coordinator import RunKind
from influx.errors import NetworkError
from influx.sources.arxiv import make_arxiv_item_provider
from influx.telemetry import (
    current_run_id,
    current_source_acquisition_errors,
)


def _make_minimal_config() -> AppConfig:
    """Smallest AppConfig that ``make_arxiv_item_provider`` accepts."""
    return AppConfig(
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[ProfileConfig(name="ai-robotics")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
    )


class TestArxivProviderRecordsSwallowedNetworkError:
    """When ``fetch_arxiv`` raises ``NetworkError`` the provider returns
    zero items (existing behaviour) and now also records a structured
    error against the current run's contextvar (issue #20).
    """

    async def test_network_error_appends_to_run_context(self) -> None:
        config = _make_minimal_config()

        run_token = current_run_id.set("test-run-degraded")
        errors_token = current_source_acquisition_errors.set([])
        try:
            provider = make_arxiv_item_provider(config)

            with patch(
                "influx.sources.arxiv.fetch_arxiv",
                side_effect=NetworkError(
                    "HTTP 500 from arXiv API",
                    url="https://arxiv.org",
                    kind="http",
                    reason="upstream 500",
                ),
            ):
                result = await provider(
                    "ai-robotics", RunKind.SCHEDULED, None, "prompt"
                )
                # The provider yields an empty iterable on swallow.
                assert list(result) == []

            errors = current_source_acquisition_errors.get() or []
        finally:
            current_run_id.reset(run_token)
            current_source_acquisition_errors.reset(errors_token)

        assert len(errors) == 1
        record = errors[0]
        assert record["source"] == "arxiv"
        assert record["kind"] == "http"
        assert "HTTP 500" in record["detail"]

    async def test_no_record_when_fetch_succeeds(self) -> None:
        """Successful fetches must NOT pollute the degraded-run signal."""
        config = _make_minimal_config()

        run_token = current_run_id.set("test-run-clean")
        errors_token = current_source_acquisition_errors.set([])
        try:
            provider = make_arxiv_item_provider(config)
            with patch("influx.sources.arxiv.fetch_arxiv", return_value=[]):
                result = await provider(
                    "ai-robotics", RunKind.SCHEDULED, None, "prompt"
                )
                assert list(result) == []

            errors = current_source_acquisition_errors.get() or []
        finally:
            current_run_id.reset(run_token)
            current_source_acquisition_errors.reset(errors_token)

        assert errors == []

    async def test_record_outside_run_context_is_ignored(self) -> None:
        """Calling the helper outside a run context (e.g. CLI smoke
        commands) must NOT raise and must NOT leak across into a
        subsequent run's record.
        """
        # No contextvar set → record_source_acquisition_error is a no-op.
        config = _make_minimal_config()
        provider = make_arxiv_item_provider(config)

        with patch(
            "influx.sources.arxiv.fetch_arxiv",
            side_effect=NetworkError("boom", url="x", kind="timeout", reason="t"),
        ):
            result = await provider("ai-robotics", RunKind.SCHEDULED, None, "prompt")
            assert list(result) == []
        # Did not raise.  No assertion on the contextvar state — there
        # isn't one set in this test.


async def test_scheduler_drains_context_into_ledger_complete(
    tmp_path: Any,
) -> None:
    """``run_profile`` reads the contextvar after the run body returns
    and forwards the structured errors to ``ledger.complete``.

    Mocks out the run body so the test stays focused on the
    contextvar-→-ledger linkage that issue #20 introduces; the upstream
    swallow path is covered by
    ``TestArxivProviderRecordsSwallowedNetworkError`` above.
    """
    from unittest.mock import AsyncMock

    from influx.run_ledger import RunLedger
    from influx.scheduler import run_profile
    from influx.telemetry import record_source_acquisition_error

    config = _make_minimal_config()
    ledger = RunLedger(tmp_path / "state")

    async def fake_body(*_args: Any, **_kwargs: Any) -> None:
        # Inside the body — the scheduler has already initialised the
        # contextvar.  Mimic an arxiv NetworkError swallow.
        record_source_acquisition_error(
            source="arxiv", kind="http", detail="HTTP 500 from arXiv API"
        )
        return None

    fake = AsyncMock(side_effect=fake_body)
    with patch("influx.scheduler._run_profile_body", new=fake):
        await run_profile(
            "ai-robotics",
            RunKind.SCHEDULED,
            config=config,
            run_ledger=ledger,
        )

    entry = ledger.recent()[0]
    assert entry["status"] == "completed"
    assert entry["degraded"] is True
    assert entry["source_acquisition_errors"] == [
        {"source": "arxiv", "kind": "http", "detail": "HTTP 500 from arXiv API"}
    ]


async def test_scheduler_writes_clean_ledger_on_no_errors(tmp_path: Any) -> None:
    """A run with no swallowed errors writes ``degraded=false`` and an
    empty list — preserves the dashboard semantics that "degraded" means
    "the source genuinely failed mid-run".
    """
    from unittest.mock import AsyncMock

    from influx.run_ledger import RunLedger
    from influx.scheduler import run_profile

    config = _make_minimal_config()
    ledger = RunLedger(tmp_path / "state")

    with patch(
        "influx.scheduler._run_profile_body",
        new=AsyncMock(return_value=None),
    ):
        await run_profile(
            "ai-robotics",
            RunKind.SCHEDULED,
            config=config,
            run_ledger=ledger,
        )

    entry = ledger.recent()[0]
    assert entry["degraded"] is False
    assert entry["source_acquisition_errors"] == []
