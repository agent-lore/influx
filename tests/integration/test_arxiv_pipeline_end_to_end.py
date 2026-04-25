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

import json
import time
from collections.abc import Generator
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ArxivSourceConfig,
    ExtractionConfig,
    FeedbackConfig,
    LithosConfig,
    NotificationsConfig,
    ProfileConfig,
    ProfileSources,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.coordinator import RunKind
from influx.http_client import FetchResult
from influx.scheduler import run_profile
from influx.service import create_app
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
        app = create_app(config)

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
