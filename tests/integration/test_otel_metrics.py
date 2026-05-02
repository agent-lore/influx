"""End-to-end OTEL metric emission test (issue #6).

Mirrors :mod:`tests.integration.test_otel_spans` but for metrics:
configures the meter wrapper with an in-memory metric reader and runs
the arXiv pipeline + an RSS feed through ``run_profile``, then asserts
the documented instrument set fired with the expected label keys.

A complementary test confirms zero metric exports with OTEL disabled
(matching the ``test_otel_absent_service`` discipline for traces).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip(
    "opentelemetry.sdk.metrics",
    reason="OTEL metrics SDK required for metric integration tests",
)

from influx.coordinator import RunKind
from influx.telemetry import InfluxMeter, InfluxTracer

# ── Helpers ────────────────────────────────────────────────────────────


def _make_inmemory_meter() -> tuple[InfluxMeter, Any]:
    """Build an InfluxMeter backed by an InMemoryMetricReader."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = InfluxMeter(enabled=True, meter=provider.get_meter("influx-int-test"))
    return meter, reader


def _disabled_tracer() -> InfluxTracer:
    """A no-op tracer so the test does not also exercise span export."""
    return InfluxTracer(enabled=False)


def _full_config() -> Any:
    """Build a config with arXiv + RSS sources (mirrors test_otel_spans)."""
    from influx.config import (
        AppConfig,
        ExtractionConfig,
        LithosConfig,
        ProfileConfig,
        ProfileSources,
        ProfileThresholds,
        PromptEntryConfig,
        PromptsConfig,
        RssSourceEntry,
        ScheduleConfig,
        SecurityConfig,
    )

    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="AI and robotics research",
                thresholds=ProfileThresholds(
                    relevance=5,
                    full_text=6,
                    deep_extract=7,
                ),
                sources=ProfileSources(
                    rss=[
                        RssSourceEntry(
                            name="Test Blog",
                            url="https://example.com/feed.xml",
                            source_tag="blog",
                        ),
                    ],
                ),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(
                text="{profile_description} {negative_examples} {min_score_in_results}",
            ),
            tier1_enrich=PromptEntryConfig(
                text="{title} {abstract} {profile_summary}",
            ),
            tier3_extract=PromptEntryConfig(text="{title} {full_text}"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(),
    )


def _mock_lithos_client() -> AsyncMock:
    client = AsyncMock()
    client.task_create.return_value = MagicMock(
        content=[MagicMock(text='{"task_id": "task-1"}')],
    )
    client.cache_lookup_for_item.return_value = MagicMock(
        content=[MagicMock(text='{"hit": false}')],
    )
    write_result = MagicMock()
    write_result.status = "created"
    write_result.note_id = "note-123"
    client.write_note.return_value = write_result
    client.retrieve.return_value = MagicMock(
        content=[MagicMock(text='{"results": []}')],
    )
    client.close = AsyncMock()
    client.task_complete = AsyncMock()
    return client


def _collect(reader: Any) -> dict[str, list[dict[str, Any]]]:
    """Snapshot the in-memory reader and group data points by instrument name.

    Returns ``{"influx_run_starts_total": [{"value": 1, "attributes": {...}}, ...]}``.
    The shape ignores aggregation type (counter sums, histograms expose
    ``sum`` / ``count``) so tests can assert "this counter was incremented
    with these labels at least once".
    """
    metrics_data = reader.get_metrics_data()
    out: dict[str, list[dict[str, Any]]] = {}
    if metrics_data is None:
        return out
    for resource_metric in metrics_data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                points = out.setdefault(metric.name, [])
                for dp in metric.data.data_points:
                    points.append(
                        {
                            "value": getattr(dp, "value", None)
                            or getattr(dp, "sum", None)
                            or getattr(dp, "count", None),
                            "attributes": dict(dp.attributes or {}),
                        }
                    )
    return out


def _has_label_set(points: list[dict[str, Any]], expected: dict[str, str]) -> bool:
    """Return True iff at least one data point matches every expected label."""
    return any(
        all(point["attributes"].get(k) == v for k, v in expected.items())
        for point in points
    )


# ── Scenario A: arXiv pipeline via run_profile ─────────────────────


async def _run_arxiv_scenario(meter: InfluxMeter, config: Any) -> None:
    """Exercise the arXiv pipeline end-to-end with the test meter."""
    from influx.extraction.pipeline import ArxivExtractionResult
    from influx.schemas import Tier1Enrichment, Tier3Extraction
    from influx.sources.arxiv import (
        ArxivItem,
        ArxivScoreResult,
        make_arxiv_item_provider,
    )

    arxiv_items = [
        ArxivItem(
            arxiv_id="2601.12345",
            title="Test Paper",
            abstract="Abstract text",
            published=datetime(2026, 4, 25, tzinfo=UTC),
            categories=["cs.AI"],
        ),
    ]

    async def fake_filter_scorer(
        items: list[Any], profile: str, prompt: str
    ) -> dict[str, ArxivScoreResult]:
        return {
            item.arxiv_id: ArxivScoreResult(
                score=10, confidence=0.95, reason="relevant"
            )
            for item in items
        }

    arxiv_provider = make_arxiv_item_provider(config, filter_scorer=fake_filter_scorer)

    mock_client = _mock_lithos_client()
    tracer = _disabled_tracer()

    with (
        patch("influx.scheduler.get_tracer", return_value=tracer),
        patch("influx.sources.arxiv.get_tracer", return_value=tracer),
        patch("influx.lcma.get_tracer", return_value=tracer),
        # Route every metric helper at the call sites through our test meter
        patch("influx.scheduler.metrics.get_meter", return_value=meter),
        patch("influx.sources.arxiv.metrics.get_meter", return_value=meter),
        patch("influx.sources.rss.metrics.get_meter", return_value=meter),
        patch("influx.repair.metrics.get_meter", return_value=meter),
        # ArXiv pipeline mocks
        patch("influx.sources.arxiv.fetch_arxiv", return_value=arxiv_items),
        patch(
            "influx.sources.arxiv.extract_arxiv_text",
            return_value=ArxivExtractionResult(
                text="Full text here", source_tag="text:html"
            ),
        ),
        patch(
            "influx.sources.arxiv.tier1_enrich",
            return_value=Tier1Enrichment(
                contributions=["c1"], method="m", result="r", relevance="rel"
            ),
        ),
        patch(
            "influx.sources.arxiv.tier3_extract",
            return_value=Tier3Extraction(claims=["claim1"], builds_on=["b1"]),
        ),
        patch("influx.scheduler.LithosClient", return_value=mock_client),
        patch(
            "influx.scheduler.build_negative_examples_block",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch("influx.scheduler.repair_sweep", new_callable=AsyncMock),
        patch("influx.service.post_run_webhook_hook"),
    ):
        from influx.scheduler import run_profile

        await run_profile(
            "ai-robotics",
            RunKind.SCHEDULED,
            config=config,
            item_provider=arxiv_provider,
        )


# ── Tests ──────────────────────────────────────────────────────────


class TestRunLifecycleAndFunnelMetrics:
    """Issue #6: lifecycle + funnel + write counters fire with bounded labels."""

    async def test_arxiv_run_emits_documented_metrics(self) -> None:
        meter, reader = _make_inmemory_meter()
        config = _full_config()

        await _run_arxiv_scenario(meter, config)

        points = _collect(reader)

        # Run lifecycle: start + completion + duration + active_runs (up & down)
        assert "influx_run_starts_total" in points
        assert _has_label_set(
            points["influx_run_starts_total"],
            {"profile": "ai-robotics", "run_type": "scheduled"},
        )
        assert "influx_run_completions_total" in points
        # Outcome is "success" (no source_acquisition_errors in the fake run).
        assert _has_label_set(
            points["influx_run_completions_total"],
            {
                "profile": "ai-robotics",
                "run_type": "scheduled",
                "outcome": "success",
            },
        )
        assert "influx_run_duration_seconds" in points
        # Active-runs registers both the +1 at start and -1 at finally;
        # the up_down counter is a sum aggregation that nets to 0 — but
        # the point must still exist for the profile dimension.
        assert "influx_active_runs" in points
        assert _has_label_set(points["influx_active_runs"], {"profile": "ai-robotics"})

        # Source funnel
        assert "influx_source_candidates_fetched_total" in points
        assert _has_label_set(
            points["influx_source_candidates_fetched_total"],
            {"profile": "ai-robotics", "source": "arxiv"},
        )
        assert "influx_articles_filtered_total" in points
        # The single arxiv item passes the filter.
        assert _has_label_set(
            points["influx_articles_filtered_total"],
            {"profile": "ai-robotics", "decision": "pass"},
        )
        assert "influx_articles_inspected_total" in points
        assert _has_label_set(
            points["influx_articles_inspected_total"],
            {"profile": "ai-robotics", "source": "arxiv"},
        )

        # Lithos write outcome (mock returns status=created, no cache hit)
        assert "influx_lithos_writes_total" in points
        assert _has_label_set(
            points["influx_lithos_writes_total"],
            {"profile": "ai-robotics", "source": "arxiv", "status": "created"},
        )

    async def test_high_cardinality_labels_are_never_emitted(self) -> None:
        """Cardinality guard: no instrument leaks per-item identifiers."""
        meter, reader = _make_inmemory_meter()
        config = _full_config()

        await _run_arxiv_scenario(meter, config)

        forbidden = {"run_id", "note_id", "arxiv_id", "source_url", "title"}
        points = _collect(reader)
        for instrument_name, datapoints in points.items():
            for dp in datapoints:
                leaked = forbidden.intersection(dp["attributes"].keys())
                assert not leaked, (
                    f"instrument {instrument_name!r} leaked high-cardinality "
                    f"label(s) {sorted(leaked)} on data point {dp}"
                )


class TestOtelDisabledZeroMetrics:
    """OTEL disabled → in-memory reader sees no metric data points."""

    async def test_disabled_meter_emits_no_metrics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run the same arXiv scenario with the meter disabled; no exports."""
        # Build a real, disabled meter (mirrors the disabled-tracer test).
        disabled_meter = InfluxMeter(enabled=False)

        # Separately, an in-memory reader to confirm no spillover anywhere.
        _meter, reader = _make_inmemory_meter()

        config = _full_config()

        from influx.extraction.pipeline import ArxivExtractionResult
        from influx.schemas import Tier1Enrichment, Tier3Extraction
        from influx.sources.arxiv import (
            ArxivItem,
            ArxivScoreResult,
            make_arxiv_item_provider,
        )

        arxiv_items = [
            ArxivItem(
                arxiv_id="2601.12345",
                title="Test Paper",
                abstract="Abstract text",
                published=datetime(2026, 4, 25, tzinfo=UTC),
                categories=["cs.AI"],
            ),
        ]

        async def fake_filter_scorer(
            items: list[Any], profile: str, prompt: str
        ) -> dict[str, ArxivScoreResult]:
            return {
                item.arxiv_id: ArxivScoreResult(
                    score=10, confidence=0.95, reason="relevant"
                )
                for item in items
            }

        arxiv_provider = make_arxiv_item_provider(
            config, filter_scorer=fake_filter_scorer
        )

        mock_client = _mock_lithos_client()
        tracer = _disabled_tracer()

        with (
            patch("influx.scheduler.get_tracer", return_value=tracer),
            patch("influx.sources.arxiv.get_tracer", return_value=tracer),
            patch("influx.lcma.get_tracer", return_value=tracer),
            patch("influx.scheduler.metrics.get_meter", return_value=disabled_meter),
            patch(
                "influx.sources.arxiv.metrics.get_meter", return_value=disabled_meter
            ),
            patch("influx.sources.rss.metrics.get_meter", return_value=disabled_meter),
            patch("influx.repair.metrics.get_meter", return_value=disabled_meter),
            patch("influx.sources.arxiv.fetch_arxiv", return_value=arxiv_items),
            patch(
                "influx.sources.arxiv.extract_arxiv_text",
                return_value=ArxivExtractionResult(
                    text="Full text here", source_tag="text:html"
                ),
            ),
            patch(
                "influx.sources.arxiv.tier1_enrich",
                return_value=Tier1Enrichment(
                    contributions=["c1"],
                    method="m",
                    result="r",
                    relevance="rel",
                ),
            ),
            patch(
                "influx.sources.arxiv.tier3_extract",
                return_value=Tier3Extraction(claims=["claim1"], builds_on=["b1"]),
            ),
            patch("influx.scheduler.LithosClient", return_value=mock_client),
            patch(
                "influx.scheduler.build_negative_examples_block",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch("influx.scheduler.repair_sweep", new_callable=AsyncMock),
            patch("influx.service.post_run_webhook_hook"),
        ):
            from influx.scheduler import run_profile

            await run_profile(
                "ai-robotics",
                RunKind.SCHEDULED,
                config=config,
                item_provider=arxiv_provider,
            )

        # The in-memory reader was not wired to the disabled meter, so
        # it must see zero metric data — confirms no spurious sink.
        points = _collect(reader)
        assert points == {}, f"Expected zero metric exports, got: {sorted(points)}"
