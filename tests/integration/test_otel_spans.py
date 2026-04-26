"""In-memory OTEL exporter asserts the full FR-OBS-4 span set (US-007).

Configures the telemetry wrapper to use an in-memory collecting exporter and
exercises representative scenarios that collectively cover every FR-OBS-4 span:

  influx.run, influx.fetch.arxiv, influx.fetch.rss, influx.filter,
  influx.enrich.tier1, influx.enrich.tier2, influx.enrich.tier3,
  influx.lithos.write, influx.lithos.retrieve, influx.archive.download

A complementary test confirms zero exported spans with OTEL disabled (AC-M4-2).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from influx.coordinator import RunKind
from influx.telemetry import InfluxTracer, current_run_id

# ── Helpers ────────────────────────────────────────────────────────────


def _make_collecting_tracer() -> tuple[InfluxTracer, list]:
    """Create an InfluxTracer backed by a collecting exporter."""
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import (
        SimpleSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )

    collected: list[ReadableSpan] = []

    class _CollectingExporter(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            collected.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            pass

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_CollectingExporter()))

    tracer = InfluxTracer(
        enabled=True,
        tracer=provider.get_tracer("influx-integration-test"),
    )
    return tracer, collected


def _full_config() -> Any:
    """Build an AppConfig with arXiv + RSS sources and all-tier thresholds."""
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


# Full FR-OBS-4 span list (PRD §6 / §9).
FR_OBS_4_SPANS = frozenset({
    "influx.run",
    "influx.fetch.arxiv",
    "influx.fetch.rss",
    "influx.filter",
    "influx.enrich.tier1",
    "influx.enrich.tier2",
    "influx.enrich.tier3",
    "influx.lithos.write",
    "influx.lithos.retrieve",
    "influx.archive.download",
})

# Expected attribute keys per span from FR-OBS-4.
EXPECTED_ATTRS: dict[str, set[str]] = {
    "influx.run": {"influx.profile", "influx.run_id", "influx.run_type"},
    "influx.fetch.arxiv": {
        "influx.profile",
        "influx.run_id",
        "influx.source",
        "influx.item_count",
    },
    "influx.fetch.rss": {
        "influx.profile",
        "influx.run_id",
        "influx.source",
        "influx.item_count",
    },
    "influx.filter": {"influx.profile", "influx.run_id", "influx.item_count"},
    "influx.enrich.tier1": {"influx.profile", "influx.run_id", "influx.item_count"},
    "influx.enrich.tier2": {"influx.profile", "influx.run_id", "influx.item_count"},
    "influx.enrich.tier3": {"influx.profile", "influx.run_id", "influx.item_count"},
    "influx.lithos.write": {"influx.profile", "influx.run_id"},
    "influx.lithos.retrieve": {"influx.profile", "influx.run_id"},
    "influx.archive.download": {"influx.profile", "influx.run_id", "influx.source"},
}


def _mock_lithos_client() -> AsyncMock:
    """Build a mock LithosClient sufficient for ``run_profile``."""
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


# ── Scenario A: arXiv pipeline via run_profile ─────────────────────
#
# Covers: influx.run, influx.fetch.arxiv, influx.filter,
#   influx.enrich.tier1, influx.enrich.tier2, influx.enrich.tier3,
#   influx.lithos.write, influx.lithos.retrieve


async def _run_arxiv_scenario(
    tracer: InfluxTracer,
    config: Any,
) -> None:
    """Exercise the arXiv pipeline end-to-end with OTEL enabled."""
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

    with (
        # Tracer patches — all modules see the collecting tracer
        patch("influx.scheduler.get_tracer", return_value=tracer),
        patch("influx.sources.arxiv.get_tracer", return_value=tracer),
        patch("influx.lcma.get_tracer", return_value=tracer),
        # ArXiv mocks
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
        # Scheduler infrastructure
        patch("influx.scheduler.LithosClient", return_value=mock_client),
        patch(
            "influx.scheduler.build_negative_examples_block",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch("influx.scheduler.repair_sweep", new_callable=AsyncMock),
        # lcma_after_write runs normally — produces influx.lithos.retrieve span
        # lcma_resolve_builds_on runs normally — "b1" has no arXiv ref, no-ops
        patch("influx.service.post_run_webhook_hook"),
    ):
        from influx.scheduler import run_profile

        await run_profile(
            "ai-robotics",
            RunKind.SCHEDULED,
            config=config,
            item_provider=arxiv_provider,
        )


# ── Scenario B: RSS provider ──────────────────────────────────────
#
# Covers: influx.fetch.rss, influx.archive.download


async def _run_rss_scenario(
    tracer: InfluxTracer,
    config: Any,
) -> None:
    """Exercise the RSS provider with OTEL enabled."""
    from influx.sources.rss import RssFeedItem, make_rss_item_provider
    from influx.storage import ArchiveResult

    rss_items = [
        RssFeedItem(
            title="Blog Post",
            url="https://example.com/post-1",
            published=datetime(2026, 4, 25, tzinfo=UTC),
            summary="Post summary",
            source_tag="blog",
            feed_name="Test Blog",
        ),
    ]

    rss_provider = make_rss_item_provider(config)

    token = current_run_id.set("test-run-rss-integration")
    try:
        with (
            patch("influx.sources.rss.get_tracer", return_value=tracer),
            patch(
                "influx.sources.rss._fetch_rss_feed",
                new_callable=AsyncMock,
                return_value=rss_items,
            ),
            patch(
                "influx.sources.rss.download_archive",
                return_value=ArchiveResult(
                    ok=True,
                    rel_posix_path="blog/2026/04/test.html",
                    error="",
                ),
            ),
            patch(
                "influx.sources.rss.extract_article",
                return_value=MagicMock(text="Extracted text", source_tag="html"),
            ),
        ):
            result = await rss_provider(
                "ai-robotics", RunKind.SCHEDULED, None, "prompt"
            )
            list(result)
    finally:
        current_run_id.reset(token)


# ── Tests ──────────────────────────────────────────────────────────


class TestFROBS4FullSpanSet:
    """AC-10-B: in-memory exporter asserts all FR-OBS-4 spans + attributes."""

    async def test_all_fr_obs_4_spans_present_with_attributes(self) -> None:
        """Across arXiv + RSS scenarios, all 10 FR-OBS-4 spans appear with
        the documented attribute keys."""
        tracer, collected = _make_collecting_tracer()
        config = _full_config()

        # Scenario A: arXiv pipeline via run_profile
        await _run_arxiv_scenario(tracer, config)

        # Scenario B: RSS provider
        await _run_rss_scenario(tracer, config)

        # ── Union assertion: all 10 FR-OBS-4 span names present ──
        span_names = {s.name for s in collected}
        missing = FR_OBS_4_SPANS - span_names
        assert not missing, (
            f"Missing FR-OBS-4 spans: {sorted(missing)}. "
            f"Got: {sorted(span_names)}"
        )

        # ── Per-span attribute assertions ──
        seen_spans: dict[str, set[str]] = {}
        for span in collected:
            attrs = dict(span.attributes) if span.attributes else {}
            seen_spans.setdefault(span.name, set()).update(attrs.keys())

        for span_name, expected_keys in EXPECTED_ATTRS.items():
            actual_keys = seen_spans.get(span_name, set())
            missing_keys = expected_keys - actual_keys
            assert not missing_keys, (
                f"Span {span_name!r} missing attributes: {sorted(missing_keys)}. "
                f"Got: {sorted(actual_keys)}"
            )

    async def test_specific_attribute_values(self) -> None:
        """Spot-check specific attribute values for representative spans."""
        tracer, collected = _make_collecting_tracer()
        config = _full_config()

        await _run_arxiv_scenario(tracer, config)

        # influx.run carries the correct profile and run_type
        run_spans = [s for s in collected if s.name == "influx.run"]
        assert len(run_spans) == 1
        run_attrs = dict(run_spans[0].attributes or {})
        assert run_attrs["influx.profile"] == "ai-robotics"
        assert run_attrs["influx.run_type"] == "scheduled"
        assert isinstance(run_attrs["influx.run_id"], str)
        assert len(run_attrs["influx.run_id"]) > 0

        # influx.fetch.arxiv carries source=arxiv and item_count
        fetch_spans = [s for s in collected if s.name == "influx.fetch.arxiv"]
        assert len(fetch_spans) == 1
        fetch_attrs = dict(fetch_spans[0].attributes or {})
        assert fetch_attrs["influx.source"] == "arxiv"
        assert fetch_attrs["influx.item_count"] == 1

        # influx.filter carries the item count
        filter_spans = [s for s in collected if s.name == "influx.filter"]
        assert len(filter_spans) == 1
        filter_attrs = dict(filter_spans[0].attributes or {})
        assert filter_attrs["influx.item_count"] == 1

        # influx.enrich.tier1/2/3 each carry item_count=1
        for tier in ("tier1", "tier2", "tier3"):
            tier_spans = [
                s for s in collected if s.name == f"influx.enrich.{tier}"
            ]
            assert len(tier_spans) == 1, f"Expected 1 influx.enrich.{tier} span"
            tier_attrs = dict(tier_spans[0].attributes or {})
            assert tier_attrs["influx.item_count"] == 1


class TestOtelDisabledZeroSpans:
    """AC-M4-2 / AC-10-A: with OTEL disabled, zero spans are exported."""

    async def test_disabled_produces_zero_spans(self) -> None:
        """With OTEL disabled, an equivalent arXiv invocation produces no spans."""
        tracer, collected = _make_collecting_tracer()
        config = _full_config()

        # Build a disabled tracer
        disabled_tracer = InfluxTracer(enabled=False)

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

        with (
            # All modules see the disabled tracer
            patch("influx.scheduler.get_tracer", return_value=disabled_tracer),
            patch("influx.sources.arxiv.get_tracer", return_value=disabled_tracer),
            patch("influx.lcma.get_tracer", return_value=disabled_tracer),
            # ArXiv mocks
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
                return_value=Tier3Extraction(
                    claims=["claim1"], builds_on=["b1"]
                ),
            ),
            # Scheduler infrastructure
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

        # The collecting exporter should have captured zero spans because
        # the disabled tracer never delegates to the OTEL provider.
        assert len(collected) == 0, (
            f"Expected 0 spans with OTEL disabled, got {len(collected)}: "
            f"{[s.name for s in collected]}"
        )
