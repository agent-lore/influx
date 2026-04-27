"""Integration test: arXiv HTML success path (AC-M2-1).

Drives the real extraction pipeline (HTML fetch → tag-stripping →
trafilatura → render_note) through ``build_arxiv_note_item`` with only
the HTTP layer mocked via ``guarded_fetch``.  Verifies that a note
whose score >= ``full_text`` threshold and whose HTML extraction
yields >= ``min_html_chars`` carries the ``text:html`` and ``full-text``
tags and has a populated ``## Full Text`` section.
"""

from __future__ import annotations

from datetime import UTC, datetime
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

_ARXIV_ID = "2601.99001"
_HTML_URL = f"https://arxiv.org/html/{_ARXIV_ID}"


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


# Realistic HTML body that trafilatura can extract >= 1000 chars from.
_HTML_BODY = """\
<!DOCTYPE html>
<html>
<head><title>Test Paper for Integration</title></head>
<body>
<article>
<h1>Test Paper for Integration</h1>
<p>This paper presents a comprehensive study of transformer architectures applied
to robotic manipulation tasks. We explore how attention mechanisms can be leveraged
to improve grasp planning in cluttered environments where traditional geometric
approaches fail to generalise across object categories.</p>

<p>Our approach combines visual features extracted from RGB-D sensors with
proprioceptive state information using a cross-attention fusion module. The
architecture processes multi-resolution visual tokens alongside joint-angle
embeddings, allowing the network to attend to task-relevant regions of the
visual field while maintaining awareness of the robot's kinematic configuration.</p>

<p>We evaluate our method on three standard benchmarks: the YCB object set for
single-object grasping, the ACRONYM dataset for cluttered-scene planning, and a
novel multi-robot handover benchmark that we introduce in this work. Results show
that our transformer-based planner achieves a 94.2% grasp success rate on YCB
objects, outperforming the previous state-of-the-art by 3.7 percentage points.</p>

<p>On the ACRONYM benchmark, our method demonstrates particularly strong
performance in highly cluttered scenes (>15 objects), where geometric planners
typically degrade. The attention maps reveal that the network learns to segment
graspable surfaces from occlusion boundaries, effectively solving a perception
and planning problem jointly.</p>

<p>For the multi-robot handover task, we extend the architecture with a
communication channel between robot agents. Each agent maintains its own
attention state but shares key-value pairs with partner agents through a
lightweight message-passing protocol. This approach achieves 89.1% handover
success rate compared to 71.3% for independent planners.</p>

<p>We conduct extensive ablation studies to understand the contribution of each
architectural component. Removing cross-attention reduces grasp success by 8.2%,
confirming that sensor fusion through attention is critical. Reducing the number
of visual tokens from 256 to 64 causes only a 1.4% drop, suggesting that the
architecture is relatively robust to input resolution within this range.</p>

<p>The computational requirements of our approach are modest: inference runs at
45 Hz on an NVIDIA Jetson AGX Orin, well within the control-loop timing budget
of 20ms per planning cycle. Training requires approximately 48 GPU-hours on A100
hardware, which is comparable to existing learning-based grasp planners.</p>

<p>We release our code, pre-trained models, and the multi-robot handover benchmark
as open-source contributions to accelerate research in transformer-based robotic
manipulation. Future work will investigate extending the approach to deformable
object manipulation and contact-rich assembly tasks.</p>
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
        title="Transformer-Based Grasp Planning",
        abstract="We study transformer architectures for robotic manipulation.",
        published=datetime(2026, 1, 15, tzinfo=UTC),
        categories=["cs.RO", "cs.AI"],
    )


def _html_fetch_result() -> FetchResult:
    return FetchResult(
        body=_HTML_BODY.encode("utf-8"),
        status_code=200,
        content_type="text/html; charset=utf-8",
        final_url=_HTML_URL,
    )


# ── Tests ─────────────────────────────────────────────────────────


class TestArxivHTMLToFullText:
    """AC-M2-1: HTML success path produces text:html + full-text + ## Full Text."""

    @patch("influx.extraction.html.guarded_fetch")
    def test_text_html_tag(self, mock_fetch: object) -> None:
        """Note carries text:html when HTML extraction succeeds."""
        mock_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=9,
            confidence=0.9,
            reason="Highly relevant",
            profile_name="ai-robotics",
            config=config,
        )

        assert "text:html" in result["tags"]

    @patch("influx.extraction.html.guarded_fetch")
    def test_full_text_tag(self, mock_fetch: object) -> None:
        """Note carries full-text tag when HTML extraction succeeds."""
        mock_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
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

    @patch("influx.extraction.html.guarded_fetch")
    def test_full_text_section_populated(self, mock_fetch: object) -> None:
        """## Full Text section is populated from the extracted text."""
        mock_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
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
        # The extracted text should contain recognisable content from the
        # HTML fixture — exact wording may differ after trafilatura
        # processing, so check for a distinctive phrase.
        assert "transformer" in result["content"].lower()

    @patch("influx.extraction.html.guarded_fetch")
    def test_extracted_text_meets_min_length(self, mock_fetch: object) -> None:
        """Extracted text is >= min_html_chars (1000)."""
        mock_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        # Find the full-text section body.
        content = result["content"]
        idx = content.index("## Full Text")
        # The section body follows the heading line.
        section_start = content.index("\n", idx) + 1
        # Find the next ## heading or end of content.
        next_heading = content.find("\n## ", section_start)
        section_body = (
            content[section_start:next_heading]
            if next_heading != -1
            else content[section_start:]
        )

        assert len(section_body.strip()) >= 1000

    @patch("influx.extraction.html.guarded_fetch")
    def test_no_html_fragments_in_output(self, mock_fetch: object) -> None:
        """No HTML fragments leak into the rendered note (FR-RES-5)."""
        mock_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        result = build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        # The full-text section should contain no HTML tags.
        content = result["content"]
        idx = content.index("## Full Text")
        section_start = content.index("\n", idx) + 1
        next_heading = content.find("\n## ", section_start)
        section_body = (
            content[section_start:next_heading]
            if next_heading != -1
            else content[section_start:]
        )

        import re

        assert not re.search(r"<[a-z][a-z0-9]*\b[^>]*>", section_body, re.IGNORECASE)

    @patch("influx.extraction.html.guarded_fetch")
    def test_no_repair_needed(self, mock_fetch: object) -> None:
        """Successful extraction does not set influx:repair-needed."""
        mock_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
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

    @patch("influx.extraction.html.guarded_fetch")
    def test_pipeline_calls_correct_url(self, mock_fetch: object) -> None:
        """The HTML extraction fetches the correct arxiv.org/html/{id} URL."""
        mock_fetch.return_value = _html_fetch_result()  # type: ignore[union-attr]
        config = _make_config(full_text=8)

        build_arxiv_note_item(
            item=_make_item(),
            score=8,
            confidence=0.8,
            reason="Relevant",
            profile_name="ai-robotics",
            config=config,
        )

        mock_fetch.assert_called_once()  # type: ignore[union-attr]
        call_args = mock_fetch.call_args  # type: ignore[union-attr]
        assert call_args[0][0] == _HTML_URL
