"""End-to-end integration test for the production arXiv pipeline (finding #1).

Drives ``scheduler.run_profile`` through the production-default
:func:`influx.sources.arxiv.make_arxiv_item_provider`, with only the
``guarded_fetch`` HTTP layer mocked.  Verifies the
``app.state.item_provider`` wiring in :func:`influx.service.create_app`
actually causes the real HTML extraction stack to execute and surface
in the Lithos write payload (``text:html`` tag, populated
``## Full Text``).

This complements ``tests/integration/test_arxiv_html_to_full_text.py``
(which only exercises ``build_arxiv_note_item``) by proving that
``scheduler.run_profile`` for an arXiv profile actually drives the
extraction stack end-to-end (PRD 07 US-014).
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ArxivSourceConfig,
    ExtractionConfig,
    FeedbackConfig,
    LithosConfig,
    ModelSlotConfig,
    NotificationsConfig,
    ProfileConfig,
    ProfileSources,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ProviderConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.coordinator import RunKind
from influx.http_client import FetchResult
from influx.scheduler import run_profile
from influx.service import create_app
from influx.sources.arxiv import ArxivItem, ArxivScorer, ArxivScoreResult
from tests.contract.test_lithos_client import FakeLithosServer

# ── Fixture data ──────────────────────────────────────────────────

_ARXIV_ID = "2601.99777"
_ARXIV_HTML_URL = f"https://arxiv.org/html/{_ARXIV_ID}"

_ATOM_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/{_ARXIV_ID}v1</id>
    <title>End-To-End Pipeline Paper</title>
    <summary>Paper about end-to-end arxiv ingestion validation.</summary>
    <published>2026-04-25T00:00:00Z</published>
    <category term="cs.RO"/>
  </entry>
</feed>
""".encode()

_HTML_BODY = """\
<!DOCTYPE html>
<html>
<head><title>End-To-End Pipeline Paper</title></head>
<body>
<article>
<h1>End-To-End Pipeline Paper</h1>
<p>This integration fixture exists so that the Influx scheduler can be
exercised end-to-end against a fake arXiv API and a fake Lithos MCP
server. The body must contain enough content for trafilatura to extract
at least one thousand characters so the HTML extraction tier passes the
``extraction.min_html_chars`` gate and produces ``text:html``.</p>

<p>We describe a transformer-based scheduling agent that performs
periodic ingestion runs against multiple research feeds. The agent
fetches candidate items, applies a per-profile relevance filter, and
writes structured notes back to a long-running knowledge store. Each
note carries provenance tags, profile relevance scores, and
extraction-tier markers so downstream agents can reason about the
quality of the underlying text.</p>

<p>The first stage of the pipeline acquires HTML or PDF representations
of the candidate paper. HTML is preferred because it preserves
mathematical content, figure references, and section headings as
machine-readable structure. When HTML is not available, the agent
falls back to PDF extraction, then to abstract-only ingestion.</p>

<p>The second stage scores the candidate against the profile's interest
description, dropping items whose relevance falls below the configured
threshold. The third stage performs deep extraction on the surviving
items: structured claims, datasets, prior work references, and open
questions are pulled into dedicated note sections.</p>

<p>We evaluate the pipeline on a six-week ingestion window across three
profiles, totaling roughly fifteen hundred candidate papers. The
extraction tier produces text:html on eighty-two percent of arXiv
items, text:pdf on twelve percent, and abstract-only on the remaining
six percent. End-to-end note write success exceeds ninety-nine
percent, confirming the resilience of the cascade against transient
HTTP and parse failures.</p>
</article>
</body>
</html>
"""


def _make_config(lithos_url: str) -> AppConfig:
    """Build an AppConfig that has one arXiv-enabled profile."""
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="Robotics papers",
                thresholds=ProfileThresholds(
                    relevance=100,
                    full_text=8,
                    deep_extract=100,
                    notify_immediate=8,
                ),
                sources=ProfileSources(
                    arxiv=ArxivSourceConfig(
                        enabled=True,
                        categories=["cs.RO"],
                        max_results_per_category=10,
                        lookback_days=30,
                    ),
                ),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(),
        feedback=FeedbackConfig(),
    )


@pytest.fixture(scope="module")
def fake_lithos() -> Generator[FakeLithosServer, None, None]:
    """Module-scoped fake Lithos MCP server."""
    server = FakeLithosServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def fake_lithos_url(fake_lithos: FakeLithosServer) -> str:
    return f"http://127.0.0.1:{fake_lithos.port}/sse"


@pytest.fixture(autouse=True)
def clear_lithos(fake_lithos: FakeLithosServer) -> None:
    """Clear recorded state before each test."""
    fake_lithos.calls.clear()
    fake_lithos.write_responses.clear()
    fake_lithos.read_responses.clear()
    fake_lithos.cache_lookup_responses.clear()
    fake_lithos.list_responses.clear()


def _atom_fetch_result() -> FetchResult:
    return FetchResult(
        body=_ATOM_FEED,
        status_code=200,
        content_type="application/atom+xml",
        final_url="https://export.arxiv.org/api/query",
    )


def _html_fetch_result() -> FetchResult:
    return FetchResult(
        body=_HTML_BODY.encode("utf-8"),
        status_code=200,
        content_type="text/html; charset=utf-8",
        final_url=_ARXIV_HTML_URL,
    )


# ── Tests ─────────────────────────────────────────────────────────


def _scorer_with_score(score: int) -> ArxivScorer:
    """Build a deterministic scorer that returns the same score for every item.

    Used by the integration tests below to drive the score-gated
    extraction / enrichment paths from US-014/US-015 without standing
    up a real LLM filter.
    """

    def _score(item: ArxivItem, profile: str) -> ArxivScoreResult:
        del item, profile
        return ArxivScoreResult(score=score, confidence=1.0, reason="test-scorer")

    return _score


class TestProductionArxivProviderWiring:
    """``create_app`` wires the real arXiv provider into ``run_profile``."""

    def test_create_app_sets_real_item_provider(
        self,
        fake_lithos_url: str,
    ) -> None:
        """``app.state.item_provider`` is no longer the no-op default."""
        app = create_app(_make_config(fake_lithos_url))

        # No longer ``None`` — the real provider closure is wired in.
        assert app.state.item_provider is not None
        assert callable(app.state.item_provider)


class TestRunProfileDrivesExtraction:
    """``run_profile`` for an arXiv profile drives the real extraction stack."""

    def test_lithos_write_carries_text_html_and_full_text(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """End-to-end: arXiv fetch → HTML extract → lithos_write payload."""
        config = _make_config(fake_lithos_url)
        # Inject a scorer that returns score=8 (= ``thresholds.full_text``) so
        # the extraction cascade actually runs; without an injected scorer the
        # provider now returns score=0 and the note would be abstract-only.
        app = create_app(config, arxiv_scorer=_scorer_with_score(8))

        # Mock both guarded_fetch import sites: the arXiv API fetcher in
        # ``sources.arxiv`` and the HTML extractor in ``extraction.html``.
        with (
            patch(
                "influx.sources.arxiv.guarded_fetch",
                return_value=_atom_fetch_result(),
            ),
            patch(
                "influx.extraction.html.guarded_fetch",
                return_value=_html_fetch_result(),
            ),
        ):
            import asyncio

            asyncio.run(
                run_profile(
                    "ai-robotics",
                    RunKind.MANUAL,
                    config=config,
                    item_provider=app.state.item_provider,
                )
            )

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 1, (
            "expected exactly one lithos_write call from arXiv fetch + extraction"
        )

        payload = write_calls[0][1]
        assert "text:html" in payload["tags"], payload["tags"]
        assert "full-text" in payload["tags"], payload["tags"]
        assert "## Full Text" in payload["content"]
        # The full-text body should be substantive — i.e. came from the
        # mocked HTML body, not a placeholder.
        assert "transformer" in payload["content"].lower()


class TestRunProfileHonoursScoreGating:
    """``run_profile`` honours the score-gated extraction/enrichment contract.

    Drives the production-default item provider end-to-end with both a
    below-``full_text`` score and a ``>= deep_extract`` score so the
    score-gated behaviour from US-014/US-015 is exercised through
    ``run_profile`` rather than only through ``build_arxiv_note_item``.
    Demonstrates that the new injectable ``arxiv_scorer`` seam (the
    finding's recommended fix) actually drives the gating logic.
    """

    def test_below_full_text_score_yields_abstract_only(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Score below ``full_text`` → no extraction, no full-text tag."""
        config = _make_config(fake_lithos_url)
        # Score 5 < relevance(100) < full_text(8) — no Tier 1, no extraction.
        app = create_app(config, arxiv_scorer=_scorer_with_score(5))

        with (
            patch(
                "influx.sources.arxiv.guarded_fetch",
                return_value=_atom_fetch_result(),
            ),
            patch(
                "influx.extraction.html.guarded_fetch",
            ) as mock_html,
        ):
            import asyncio

            asyncio.run(
                run_profile(
                    "ai-robotics",
                    RunKind.MANUAL,
                    config=config,
                    item_provider=app.state.item_provider,
                )
            )

            # Below-threshold scores must NOT trigger the HTML extractor.
            assert mock_html.call_count == 0, (
                "HTML extractor must not be called when score < full_text"
            )

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 1
        payload = write_calls[0][1]

        assert "text:abstract-only" in payload["tags"]
        assert "text:html" not in payload["tags"]
        assert "full-text" not in payload["tags"]
        assert "## Full Text" not in payload["content"]
        assert "influx:deep-extracted" not in payload["tags"]

    def test_deep_extract_threshold_triggers_tier3(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Score ≥ ``deep_extract`` → Tier 3 enrichment is invoked."""
        # Use a config with permissive thresholds so deep_extract is reachable.
        config = AppConfig(
            lithos=LithosConfig(url=fake_lithos_url),
            schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
            profiles=[
                ProfileConfig(
                    name="ai-robotics",
                    description="Robotics papers",
                    thresholds=ProfileThresholds(
                        relevance=100,  # disable Tier 1 so we don't need to
                        # mock ``enrich.tier1_enrich``
                        full_text=8,
                        deep_extract=9,
                        notify_immediate=8,
                    ),
                    sources=ProfileSources(
                        arxiv=ArxivSourceConfig(
                            enabled=True,
                            categories=["cs.RO"],
                            max_results_per_category=10,
                            lookback_days=30,
                        ),
                    ),
                ),
            ],
            providers={},
            prompts=PromptsConfig(
                filter=PromptEntryConfig(text="x"),
                tier1_enrich=PromptEntryConfig(text="x"),
                tier3_extract=PromptEntryConfig(text="x"),
            ),
            notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
            security=SecurityConfig(allow_private_ips=True),
            extraction=ExtractionConfig(),
            feedback=FeedbackConfig(),
        )

        # Score 9 >= deep_extract — Tier 3 must be invoked.
        app = create_app(config, arxiv_scorer=_scorer_with_score(9))

        from influx.schemas import Tier3Extraction

        tier3_stub = Tier3Extraction(
            claims=["claim"],
            datasets=[],
            builds_on=[],
            open_questions=[],
            potential_connections=[],
        )

        with (
            patch(
                "influx.sources.arxiv.guarded_fetch",
                return_value=_atom_fetch_result(),
            ),
            patch(
                "influx.extraction.html.guarded_fetch",
                return_value=_html_fetch_result(),
            ),
            patch(
                "influx.sources.arxiv.tier3_extract",
                return_value=tier3_stub,
            ) as mock_tier3,
        ):
            import asyncio

            asyncio.run(
                run_profile(
                    "ai-robotics",
                    RunKind.MANUAL,
                    config=config,
                    item_provider=app.state.item_provider,
                )
            )

            # Score >= deep_extract MUST invoke Tier 3 exactly once.
            assert mock_tier3.call_count == 1, (
                "Tier 3 extraction must be called when score >= deep_extract"
            )

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 1
        payload = write_calls[0][1]

        assert "text:html" in payload["tags"]
        assert "full-text" in payload["tags"]
        assert "influx:deep-extracted" in payload["tags"]
        assert "## Claims" in payload["content"]


class TestRunProfileSkipsWhenSourceDisabled:
    """An arXiv-disabled profile yields zero items and no writes."""

    def test_disabled_arxiv_yields_no_writes(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        config = _make_config(fake_lithos_url)
        # Disable arxiv on the only profile.
        config.profiles[0].sources.arxiv.enabled = False
        app = create_app(config)

        import asyncio

        # Ensure our mocked guarded_fetch is NEVER called when arXiv is
        # disabled — the provider should short-circuit before fetching.
        with (
            patch("influx.sources.arxiv.guarded_fetch") as mock_arxiv_fetch,
            patch("influx.extraction.html.guarded_fetch") as mock_html_fetch,
        ):
            asyncio.run(
                run_profile(
                    "ai-robotics",
                    RunKind.MANUAL,
                    config=config,
                    item_provider=app.state.item_provider,
                )
            )
            assert mock_arxiv_fetch.call_count == 0
            assert mock_html_fetch.call_count == 0

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert write_calls == []


# ── Default LLM filter scorer (finding #1 production-default path) ────


def _make_config_with_filter(lithos_url: str) -> AppConfig:
    """AppConfig with ``[models.filter]`` configured so the default scorer wires.

    Same shape as :func:`_make_config` but adds a ``providers.openai``
    entry and a ``models.filter`` slot so
    :func:`influx.filter.make_default_arxiv_filter_scorer` returns a
    real scorer (instead of falling through to the no-scorer default).
    Thresholds are kept deterministic so the score-gating tests can
    pin behaviour to specific scores.
    """
    return AppConfig(
        lithos=LithosConfig(url=lithos_url),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="Robotics papers",
                thresholds=ProfileThresholds(
                    relevance=100,  # disable Tier 1 (no enrich slot mocked)
                    full_text=8,
                    deep_extract=9,
                    notify_immediate=8,
                ),
                sources=ProfileSources(
                    arxiv=ArxivSourceConfig(
                        enabled=True,
                        categories=["cs.RO"],
                        max_results_per_category=10,
                        lookback_days=30,
                    ),
                ),
            ),
        ],
        providers={
            "openai": ProviderConfig(base_url="https://api.openai.invalid/v1"),
        },
        models={
            "filter": ModelSlotConfig(
                provider="openai",
                model="gpt-test",
                json_mode=True,
            ),
        },
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="Filter rubric"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        notifications=NotificationsConfig(webhook_url="", timeout_seconds=5),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(),
        feedback=FeedbackConfig(),
    )


def _filter_response(arxiv_id: str, score: int) -> dict[str, object]:
    """Build a fake OpenAI-compatible filter response for one item."""
    return {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"results": [{'
                        f'"id": "{arxiv_id}", '
                        f'"score": {score}, '
                        '"tags": ["robotics"], '
                        '"reason": "stub"'
                        "}]}"
                    ),
                },
            },
        ],
    }


class _FakeFilterResponse:
    """Minimal stand-in for ``httpx.Response`` returned by the filter call."""

    def __init__(self, body: dict[str, object]) -> None:
        self._body = body
        self.status_code = 200
        self.text = ""

    def json(self) -> dict[str, object]:
        return self._body


class TestDefaultFilterScorerEndToEnd:
    """Default LLM filter scorer drives score gating without a test override.

    These tests do NOT pass ``arxiv_scorer`` or ``arxiv_filter_scorer``
    to :func:`create_app` — they exercise the production-default scorer
    that ``create_app`` installs from ``[models.filter]``, mocking only
    the underlying ``httpx.post`` so the filter LLM call is
    deterministic.  This proves the finding's recommended fix: the
    shipped service / serve path now drives score-gated extraction +
    enrichment behaviour from US-014/US-015.
    """

    def test_default_scorer_below_full_text_yields_abstract_only(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Default LLM filter returns score < full_text → abstract-only note."""
        config = _make_config_with_filter(fake_lithos_url)

        # No scorer override — create_app installs the default LLM filter.
        app = create_app(config)
        assert app.state.item_provider is not None

        with (
            patch(
                "influx.sources.arxiv.guarded_fetch",
                return_value=_atom_fetch_result(),
            ),
            patch(
                "influx.extraction.html.guarded_fetch",
            ) as mock_html,
            patch(
                "influx.filter.httpx.post",
                return_value=_FakeFilterResponse(_filter_response(_ARXIV_ID, 5)),
            ) as mock_filter_post,
        ):
            import asyncio

            asyncio.run(
                run_profile(
                    "ai-robotics",
                    RunKind.MANUAL,
                    config=config,
                    item_provider=app.state.item_provider,
                )
            )

            # Default scorer was invoked exactly once (batched).
            assert mock_filter_post.call_count == 1
            # Below-full_text scores must NOT trigger HTML extraction.
            assert mock_html.call_count == 0

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 1
        payload = write_calls[0][1]

        assert "text:abstract-only" in payload["tags"]
        assert "text:html" not in payload["tags"]
        assert "full-text" not in payload["tags"]
        assert "## Full Text" not in payload["content"]
        assert "influx:deep-extracted" not in payload["tags"]
        # Score from the LLM filter response is propagated through.
        assert payload["confidence"] == 1.0

    def test_default_scorer_at_deep_extract_triggers_tier3(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Default LLM filter returns score ≥ deep_extract → Tier 3 invoked."""
        config = _make_config_with_filter(fake_lithos_url)

        # No scorer override — create_app installs the default LLM filter.
        app = create_app(config)

        from influx.schemas import Tier3Extraction

        tier3_stub = Tier3Extraction(
            claims=["claim"],
            datasets=[],
            builds_on=[],
            open_questions=[],
            potential_connections=[],
        )

        with (
            patch(
                "influx.sources.arxiv.guarded_fetch",
                return_value=_atom_fetch_result(),
            ),
            patch(
                "influx.extraction.html.guarded_fetch",
                return_value=_html_fetch_result(),
            ),
            patch(
                "influx.filter.httpx.post",
                return_value=_FakeFilterResponse(_filter_response(_ARXIV_ID, 9)),
            ) as mock_filter_post,
            patch(
                "influx.sources.arxiv.tier3_extract",
                return_value=tier3_stub,
            ) as mock_tier3,
        ):
            import asyncio

            asyncio.run(
                run_profile(
                    "ai-robotics",
                    RunKind.MANUAL,
                    config=config,
                    item_provider=app.state.item_provider,
                )
            )

            # Default scorer was invoked once.
            assert mock_filter_post.call_count == 1
            # Score ≥ deep_extract MUST invoke Tier 3 exactly once.
            assert mock_tier3.call_count == 1

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 1
        payload = write_calls[0][1]

        assert "text:html" in payload["tags"]
        assert "full-text" in payload["tags"]
        assert "influx:deep-extracted" in payload["tags"]
        assert "## Claims" in payload["content"]

    def test_default_scorer_http_failure_yields_abstract_only(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """Default LLM filter HTTP failure → note still written abstract-only.

        Regression test for the finding: when the default batch scorer
        hard-fails (HTTP error, parse error, missing provider) the
        provider must fall every item back to abstract-only ingestion
        rather than dropping the entire batch (PRD 07 §5.6 graceful
        degradation).
        """
        import httpx

        config = _make_config_with_filter(fake_lithos_url)

        app = create_app(config)
        assert app.state.item_provider is not None

        with (
            patch(
                "influx.sources.arxiv.guarded_fetch",
                return_value=_atom_fetch_result(),
            ),
            patch(
                "influx.extraction.html.guarded_fetch",
            ) as mock_html,
            patch(
                "influx.filter.httpx.post",
                side_effect=httpx.ConnectError("simulated transport failure"),
            ) as mock_filter_post,
        ):
            import asyncio

            asyncio.run(
                run_profile(
                    "ai-robotics",
                    RunKind.MANUAL,
                    config=config,
                    item_provider=app.state.item_provider,
                )
            )

            # Default scorer was attempted once and failed.
            assert mock_filter_post.call_count == 1
            # Abstract-only fallback must NOT trigger HTML extraction.
            assert mock_html.call_count == 0

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        # The note is still written even though the filter failed —
        # that is the bug this regression test pins down.
        assert len(write_calls) == 1, (
            "filter scorer hard-failure must fall back to abstract-only "
            "ingestion, not silently drop the batch"
        )
        payload = write_calls[0][1]

        assert "text:abstract-only" in payload["tags"]
        assert "text:html" not in payload["tags"]
        assert "full-text" not in payload["tags"]
        assert "## Full Text" not in payload["content"]
        assert "influx:deep-extracted" not in payload["tags"]

    def test_default_scorer_missing_provider_yields_abstract_only(
        self,
        fake_lithos: FakeLithosServer,
        fake_lithos_url: str,
    ) -> None:
        """models.filter slot points at a missing provider → abstract-only note.

        Misconfiguration where ``[models.filter]`` is configured but the
        referenced provider is not declared in ``[providers]`` must not
        silently drop the batch — every item should fall back to
        abstract-only ingestion (PRD 07 §5.6).
        """
        config = _make_config_with_filter(fake_lithos_url)
        # Drop the provider that ``[models.filter]`` references so the
        # scorer's provider lookup fails at call time.
        config.providers = {}

        app = create_app(config)
        assert app.state.item_provider is not None

        with (
            patch(
                "influx.sources.arxiv.guarded_fetch",
                return_value=_atom_fetch_result(),
            ),
            patch(
                "influx.extraction.html.guarded_fetch",
            ) as mock_html,
            patch("influx.filter.httpx.post") as mock_filter_post,
        ):
            import asyncio

            asyncio.run(
                run_profile(
                    "ai-robotics",
                    RunKind.MANUAL,
                    config=config,
                    item_provider=app.state.item_provider,
                )
            )

            # Provider lookup fails before httpx is called.
            assert mock_filter_post.call_count == 0
            # No HTML extraction either (score=0 fallback < full_text).
            assert mock_html.call_count == 0

        write_calls = [c for c in fake_lithos.calls if c[0] == "lithos_write"]
        assert len(write_calls) == 1, (
            "missing filter provider must fall back to abstract-only, "
            "not silently drop the batch"
        )
        payload = write_calls[0][1]

        assert "text:abstract-only" in payload["tags"]
        assert "text:html" not in payload["tags"]
        assert "full-text" not in payload["tags"]
