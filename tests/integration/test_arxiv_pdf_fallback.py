"""Integration test: arXiv PDF fallback path (AC-M2-2, AC-07-E).

Drives the real extraction pipeline through ``build_arxiv_note_item``
where HTML extraction yields 800 chars (below ``min_html_chars``=1000),
causing the pipeline to fall through to PDF extraction.  The PDF path
succeeds against the recorded ``sample.pdf`` fixture.

Only the HTTP layer is mocked via ``guarded_fetch`` — the real extraction
pipeline (tag-stripping → trafilatura → pypdf → render_note) runs end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from influx.config import (
    AppConfig,
    ExtractionConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    ScheduleConfig,
    SecurityConfig,
)
from influx.http_client import FetchResult
from influx.sources.arxiv import ArxivItem, build_arxiv_note_item
from influx.storage import ArchiveResult

# ── Fixture data ──────────────────────────────────────────────────

_ARXIV_ID = "2601.88001"
_HTML_URL = f"https://arxiv.org/html/{_ARXIV_ID}"
_PDF_URL = f"https://arxiv.org/pdf/{_ARXIV_ID}.pdf"


@pytest.fixture(autouse=True)
def _archive_success() -> object:
    with patch(
        "influx.sources.arxiv.download_archive",
        return_value=ArchiveResult(
            ok=True,
            rel_posix_path=f"arxiv/2026/04/{_ARXIV_ID}.pdf",
            error="",
        ),
    ) as patched:
        yield patched


_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "extraction"

# HTML body that trafilatura will extract to ~800 chars (below min_html_chars=1000).
# Deliberately short so extraction falls below the 1000-char threshold.
_SHORT_HTML_BODY = """\
<!DOCTYPE html>
<html>
<head><title>Short Paper</title></head>
<body>
<article>
<h1>Short Paper on Robotic Control</h1>
<p>This paper studies robotic control using reinforcement learning.
We present a policy gradient method that improves sample efficiency
in continuous control tasks. The algorithm combines model-based
planning with model-free optimization for faster convergence.</p>

<p>Our evaluation shows improvements over baselines on the MuJoCo
locomotion suite. Short-horizon model predictions guide the policy
search without requiring accurate long-term dynamics models.</p>
</article>
</body>
</html>
"""


def _make_config(
    *,
    full_text: int = 8,
    relevance: int = 100,
    deep_extract: int = 100,
) -> AppConfig:
    """Minimal config with extraction-focused thresholds.

    Enrichment thresholds are set high by default to isolate this test
    to the extraction path.
    """
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="AI and robotics research",
                thresholds=ProfileThresholds(
                    relevance=relevance,
                    full_text=full_text,
                    deep_extract=deep_extract,
                ),
            ),
        ],
        providers={},
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(),
    )


def _make_item() -> ArxivItem:
    return ArxivItem(
        arxiv_id=_ARXIV_ID,
        title="Robotic Control via Policy Gradients",
        abstract="We study reinforcement learning for robotic control.",
        published=datetime(2026, 1, 20, tzinfo=UTC),
        categories=["cs.RO", "cs.LG"],
    )


def _html_fetch_result() -> FetchResult:
    """HTML response that extracts to ~800 chars (below min_html_chars=1000)."""
    return FetchResult(
        body=_SHORT_HTML_BODY.encode("utf-8"),
        status_code=200,
        content_type="text/html; charset=utf-8",
        final_url=_HTML_URL,
    )


def _pdf_fetch_result() -> FetchResult:
    """PDF response using the recorded sample.pdf fixture."""
    pdf_bytes = (_FIXTURES / "sample.pdf").read_bytes()
    return FetchResult(
        body=pdf_bytes,
        status_code=200,
        content_type="application/pdf",
        final_url=_PDF_URL,
    )


# ── Tests ─────────────────────────────────────────────────────────


class TestArxivPDFFallback:
    """AC-M2-2 / AC-07-E: HTML below min_html_chars falls through to PDF."""

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.html.guarded_fetch")
    def test_text_pdf_tag(
        self, mock_html_fetch: object, mock_pdf_fetch: object
    ) -> None:
        """Note carries text:pdf when HTML is below threshold and PDF succeeds."""
        mock_html_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        mock_pdf_fetch.return_value = _pdf_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "text:pdf" in result["tags"]

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.html.guarded_fetch")
    def test_full_text_tag(
        self, mock_html_fetch: object, mock_pdf_fetch: object
    ) -> None:
        """Note carries full-text tag when PDF fallback succeeds."""
        mock_html_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        mock_pdf_fetch.return_value = _pdf_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "full-text" in result["tags"]

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.html.guarded_fetch")
    def test_no_html_tag(self, mock_html_fetch: object, mock_pdf_fetch: object) -> None:
        """Note does NOT carry text:html — PDF was the successful tier."""
        mock_html_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        mock_pdf_fetch.return_value = _pdf_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "text:html" not in result["tags"]

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.html.guarded_fetch")
    def test_full_text_section_populated(
        self, mock_html_fetch: object, mock_pdf_fetch: object
    ) -> None:
        """## Full Text section is populated from PDF-extracted text."""
        mock_html_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        mock_pdf_fetch.return_value = _pdf_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "## Full Text" in result["content"]
        # The PDF fixture contains recognisable content about extraction testing.
        content_lower = result["content"].lower()
        assert "test pdf document" in content_lower or "extractable" in content_lower

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.html.guarded_fetch")
    def test_html_extraction_below_threshold(
        self, mock_html_fetch: object, mock_pdf_fetch: object
    ) -> None:
        """HTML extraction yields < 1000 chars, triggering PDF fallback (AC-07-E)."""
        mock_html_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        mock_pdf_fetch.return_value = _pdf_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        # Verify PDF was called (proving HTML failed and fell through).
        mock_pdf_fetch.assert_called_once()  # type: ignore[union-attr]
        call_args = mock_pdf_fetch.call_args  # type: ignore[union-attr]
        assert call_args[0][0] == _PDF_URL

    @patch("influx.extraction.pipeline.guarded_fetch")
    @patch("influx.extraction.html.guarded_fetch")
    def test_no_repair_needed(
        self, mock_html_fetch: object, mock_pdf_fetch: object
    ) -> None:
        """Successful PDF fallback does not set influx:repair-needed."""
        mock_html_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        mock_pdf_fetch.return_value = _pdf_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "influx:repair-needed" not in result["tags"]
