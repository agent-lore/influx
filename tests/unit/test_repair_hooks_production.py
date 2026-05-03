"""Tests for the production-default repair hooks (US-016).

Verifies that ``make_default_sweep_hooks`` creates hooks conforming to
the PRD 06 signatures, that the ``re_extract_archive`` hook returns
the correct ``ReExtractionResult`` variants, and that ``tier2_enrich``
and ``tier3_extract`` mutate the note dict correctly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from influx.config import AppConfig, ExtractionConfig, StorageConfig
from influx.errors import ExtractionError, LCMAError
from influx.repair import (
    ExtractionOutcome,
    ReExtractionResult,
    SweepHooks,
)
from influx.repair_hooks import (
    DefaultSweepHooks,
    _extract_full_text_body,
    _extract_title,
    _insert_full_text_section,
    _insert_tier3_sections,
    _render_tier3_sections,
    make_default_sweep_hooks,
)
from influx.schemas import Tier3Extraction

# ── Helpers ──────────────────────────────────────────────────────────


def _make_config(
    tmp_path: Path,
    *,
    min_html_chars: int = 1000,
    min_web_chars: int = 500,
) -> MagicMock:
    """Build a minimal AppConfig mock with storage and extraction settings."""
    config = MagicMock(spec=AppConfig)
    config.storage = MagicMock(spec=StorageConfig)
    config.storage.archive_dir = str(tmp_path / "archive")
    config.storage.max_download_bytes = 10_000_000
    config.storage.download_timeout_seconds = 30
    config.security = MagicMock()
    config.security.allow_private_ips = False
    config.extraction = MagicMock(spec=ExtractionConfig)
    config.extraction.min_html_chars = min_html_chars
    config.extraction.min_web_chars = min_web_chars
    config.extraction.strip_tags = ["script", "iframe", "object", "embed"]
    return config


def _sample_note_content(
    *,
    archive_path: str | None = None,
    full_text: str | None = None,
    score: int = 9,
) -> str:
    """Build a canonical note content string."""
    archive_body = f"path: {archive_path}\n" if archive_path else ""
    full_text_section = f"\n## Full Text\n{full_text}\n" if full_text else ""
    return (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        "source_url: https://arxiv.org/abs/2601.00001\n"
        "tags:\n"
        "  - profile:ai-robotics\n"
        "  - ingested-by:influx\n"
        "confidence: 0.9\n"
        "---\n"
        "# Test Paper Title\n"
        "\n"
        "## Archive\n"
        f"{archive_body}"
        "\n"
        "## Summary\n"
        "A test paper summary.\n"
        f"{full_text_section}"
        "\n"
        "## Profile Relevance\n"
        "### ai-robotics\n"
        f"Score: {score}/10\n"
        "Relevant.\n"
        "\n"
        "## User Notes\n"
    )


def _make_note_dict(
    *,
    archive_path: str | None = None,
    full_text: str | None = None,
    tags: list[str] | None = None,
    score: int = 9,
) -> dict[str, Any]:
    """Build a note dict."""
    if tags is None:
        tags = [
            "profile:ai-robotics",
            "ingested-by:influx",
            "source:arxiv",
            "text:abstract-only",
            "influx:repair-needed",
        ]
    return {
        "id": "note-001",
        "title": "Test Paper Title",
        "content": _sample_note_content(
            archive_path=archive_path,
            full_text=full_text,
            score=score,
        ),
        "tags": list(tags),
        "version": 1,
    }


# ── make_default_sweep_hooks ─────────────────────────────────────────


class TestMakeDefaultSweepHooks:
    def test_returns_default_sweep_hooks_instance(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        assert isinstance(hooks, DefaultSweepHooks)

    def test_converts_to_sweep_hooks(self, tmp_path: Path) -> None:
        """``to_sweep_hooks()`` returns a ``SweepHooks`` for the sweep entrypoint."""
        config = _make_config(tmp_path)
        sweep_hooks = make_default_sweep_hooks(config).to_sweep_hooks()
        assert isinstance(sweep_hooks, SweepHooks)
        assert sweep_hooks.archive_download is not None
        assert sweep_hooks.re_extract_archive is not None
        assert sweep_hooks.tier2_enrich is not None
        assert sweep_hooks.tier3_extract is not None
        assert sweep_hooks.text_extraction is not None

    def test_archive_download_wired(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        assert callable(hooks.archive_download)

    def test_text_extraction_wired(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        assert callable(hooks.text_extraction)


# ── re_extract_archive hook ──────────────────────────────────────────


class TestReExtractArchivePdf:
    """PDF archive re-extraction."""

    def test_upgrade_on_successful_pdf(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        archive_dir = Path(config.storage.archive_dir)
        archive_dir.mkdir(parents=True)
        pdf_path = "papers/2026/04/test.pdf"
        (archive_dir / "papers" / "2026" / "04").mkdir(parents=True)
        # Create a valid PDF fixture.
        fixture_path = Path("tests/fixtures/extraction/sample.pdf")
        (archive_dir / pdf_path).write_bytes(fixture_path.read_bytes())

        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=pdf_path)
        result = hooks.re_extract_archive(note, pdf_path)

        assert result.outcome is ExtractionOutcome.UPGRADE
        assert result.upgraded_text_tag == "text:pdf"

    def test_terminal_on_blank_pdf(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "papers").mkdir(parents=True)
        pdf_path = "papers/blank.pdf"
        fixture_path = Path("tests/fixtures/extraction/blank.pdf")
        (archive_dir / pdf_path).write_bytes(fixture_path.read_bytes())

        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=pdf_path)
        result = hooks.re_extract_archive(note, pdf_path)

        assert result.outcome is ExtractionOutcome.TERMINAL

    def test_transient_on_file_not_found(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path="papers/missing.pdf")
        result = hooks.re_extract_archive(note, "papers/missing.pdf")

        assert result.outcome is ExtractionOutcome.TRANSIENT


class TestReExtractArchiveHtml:
    """HTML archive re-extraction."""

    def test_upgrade_on_successful_html(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, min_html_chars=10)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "pages").mkdir(parents=True)
        html_path = "pages/article.html"
        fixture_path = Path("tests/fixtures/extraction/good_article.html")
        (archive_dir / html_path).write_bytes(fixture_path.read_bytes())

        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=html_path)
        result = hooks.re_extract_archive(note, html_path)

        assert result.outcome is ExtractionOutcome.UPGRADE
        assert result.upgraded_text_tag == "text:html"

    def test_terminal_on_short_html(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, min_html_chars=100000)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "pages").mkdir(parents=True)
        html_path = "pages/short.html"
        fixture_path = Path("tests/fixtures/extraction/short_article.html")
        (archive_dir / html_path).write_bytes(fixture_path.read_bytes())

        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=html_path)
        result = hooks.re_extract_archive(note, html_path)

        assert result.outcome is ExtractionOutcome.TERMINAL


class TestReExtractArchiveReturnsReExtractionResult:
    """The hook return type matches the PRD 06 protocol."""

    def test_upgrade_is_reextraction_result(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "papers").mkdir(parents=True)
        pdf_path = "papers/test.pdf"
        fixture_path = Path("tests/fixtures/extraction/sample.pdf")
        (archive_dir / pdf_path).write_bytes(fixture_path.read_bytes())

        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=pdf_path)
        result = hooks.re_extract_archive(note, pdf_path)

        assert isinstance(result, ReExtractionResult)

    def test_terminal_is_reextraction_result(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "papers").mkdir(parents=True)
        pdf_path = "papers/blank.pdf"
        fixture_path = Path("tests/fixtures/extraction/blank.pdf")
        (archive_dir / pdf_path).write_bytes(fixture_path.read_bytes())

        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=pdf_path)
        result = hooks.re_extract_archive(note, pdf_path)

        assert isinstance(result, ReExtractionResult)


# ── tier2_enrich hook ────────────────────────────────────────────────


class TestTier2EnrichSuccess:
    """Production tier2_enrich inserts ## Full Text and full-text tag."""

    def test_inserts_full_text_section(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "papers").mkdir(parents=True)
        pdf_path = "papers/test.pdf"
        fixture_path = Path("tests/fixtures/extraction/sample.pdf")
        (archive_dir / pdf_path).write_bytes(fixture_path.read_bytes())

        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=pdf_path)

        hooks.tier2_enrich(note)

        content: str = str(note["content"])
        assert "## Full Text" in content

    def test_adds_full_text_tag(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "papers").mkdir(parents=True)
        pdf_path = "papers/test.pdf"
        fixture_path = Path("tests/fixtures/extraction/sample.pdf")
        (archive_dir / pdf_path).write_bytes(fixture_path.read_bytes())

        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=pdf_path)

        hooks.tier2_enrich(note)

        tags: list[str] = list(note.get("tags", []))
        assert "full-text" in tags

    def test_does_not_duplicate_full_text_tag(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "papers").mkdir(parents=True)
        pdf_path = "papers/test.pdf"
        fixture_path = Path("tests/fixtures/extraction/sample.pdf")
        (archive_dir / pdf_path).write_bytes(fixture_path.read_bytes())

        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=pdf_path)
        note["tags"] = list(note["tags"]) + ["full-text"]

        hooks.tier2_enrich(note)

        tags: list[str] = list(note.get("tags", []))
        assert tags.count("full-text") == 1


class TestTier2EnrichFailure:
    """tier2_enrich raises ExtractionError on failure."""

    def test_raises_on_missing_archive(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path="papers/missing.pdf")

        with pytest.raises(ExtractionError):
            hooks.tier2_enrich(note)

    def test_raises_on_no_archive_path(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(archive_path=None)

        with pytest.raises(ExtractionError):
            hooks.tier2_enrich(note)


# ── tier3_extract hook ───────────────────────────────────────────────


class TestTier3ExtractSuccess:
    """Production tier3_extract inserts Tier 3 sections and tag."""

    def test_inserts_tier3_sections(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(full_text="This is the full extracted text.")

        with patch("influx.repair_hooks._tier3_extract") as mock_t3:
            mock_t3.return_value = Tier3Extraction(
                claims=["Claim A", "Claim B"],
                datasets=["Dataset 1"],
                builds_on=["Ref 1"],
                open_questions=["Question 1"],
                potential_connections=["Connection 1"],
            )
            hooks.tier3_extract(note)

        content: str = str(note["content"])
        assert "## Claims" in content
        assert "- Claim A" in content
        assert "- Claim B" in content
        assert "## Datasets & Benchmarks" in content
        assert "- Dataset 1" in content
        assert "## Builds On" in content
        assert "- Ref 1" in content
        assert "## Open Questions" in content
        assert "- Question 1" in content

    def test_adds_deep_extracted_tag(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(full_text="Full text here.")

        with patch("influx.repair_hooks._tier3_extract") as mock_t3:
            mock_t3.return_value = Tier3Extraction(
                claims=["Claim"],
            )
            hooks.tier3_extract(note)

        tags: list[str] = list(note.get("tags", []))
        assert "influx:deep-extracted" in tags

    def test_does_not_duplicate_deep_extracted_tag(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(full_text="Full text here.")
        note["tags"] = list(note["tags"]) + ["influx:deep-extracted"]

        with patch("influx.repair_hooks._tier3_extract") as mock_t3:
            mock_t3.return_value = Tier3Extraction(claims=["Claim"])
            hooks.tier3_extract(note)

        tags: list[str] = list(note.get("tags", []))
        assert tags.count("influx:deep-extracted") == 1

    def test_tier3_sections_before_profile_relevance(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(full_text="Full text content.")

        with patch("influx.repair_hooks._tier3_extract") as mock_t3:
            mock_t3.return_value = Tier3Extraction(
                claims=["Claim"],
                datasets=["DS"],
                builds_on=["Ref"],
                open_questions=["Q"],
            )
            hooks.tier3_extract(note)

        content: str = str(note["content"])
        claims_pos = content.find("## Claims")
        profile_pos = content.find("## Profile Relevance")
        user_notes_pos = content.find("## User Notes")
        assert claims_pos < profile_pos
        assert claims_pos < user_notes_pos


class TestTier3ExtractFailure:
    """tier3_extract raises on failure."""

    def test_raises_on_no_full_text(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(full_text=None)

        with pytest.raises(ExtractionError, match="No ## Full Text"):
            hooks.tier3_extract(note)

    def test_propagates_lcma_error(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_note_dict(full_text="Full text content here.")

        with patch("influx.repair_hooks._tier3_extract") as mock_t3:
            mock_t3.side_effect = LCMAError("model failed", model="extract")
            with pytest.raises(LCMAError):
                hooks.tier3_extract(note)


# ── Content manipulation helpers ─────────────────────────────────────


class TestExtractTitle:
    def test_extracts_title(self) -> None:
        content = "---\nfm\n---\n# My Paper Title\n\nbody"
        assert _extract_title(content) == "My Paper Title"

    def test_returns_empty_on_no_title(self) -> None:
        assert _extract_title("no title here") == ""


class TestExtractFullTextBody:
    def test_extracts_body(self) -> None:
        content = (
            "## Summary\nsum\n\n"
            "## Full Text\nThe full text body.\n\n"
            "## Profile Relevance\n"
        )
        assert _extract_full_text_body(content) == "The full text body."

    def test_returns_empty_on_no_section(self) -> None:
        content = "## Summary\nsum\n\n## Profile Relevance\n"
        assert _extract_full_text_body(content) == ""


class TestInsertFullTextSection:
    def test_inserts_before_profile_relevance(self) -> None:
        content = "## Summary\nsum\n\n## Profile Relevance\npr\n"
        result = _insert_full_text_section(content, "Extracted text.")
        assert "## Full Text" in result
        ft_pos = result.find("## Full Text")
        pr_pos = result.find("## Profile Relevance")
        assert ft_pos < pr_pos


class TestInsertTier3Sections:
    def test_inserts_before_profile_relevance(self) -> None:
        content = "## Full Text\ntext\n\n## Profile Relevance\npr\n"
        tier3 = Tier3Extraction(
            claims=["C1"],
            datasets=["D1"],
            builds_on=["B1"],
            open_questions=["Q1"],
        )
        result = _insert_tier3_sections(content, tier3)
        assert "## Claims" in result
        claims_pos = result.find("## Claims")
        pr_pos = result.find("## Profile Relevance")
        assert claims_pos < pr_pos


class TestRenderTier3Sections:
    def test_renders_all_four_sections(self) -> None:
        tier3 = Tier3Extraction(
            claims=["C1", "C2"],
            datasets=["D1"],
            builds_on=["B1"],
            open_questions=["Q1"],
        )
        rendered = _render_tier3_sections(tier3)
        assert "## Claims" in rendered
        assert "- C1" in rendered
        assert "- C2" in rendered
        assert "## Datasets & Benchmarks" in rendered
        assert "- D1" in rendered
        assert "## Builds On" in rendered
        assert "- B1" in rendered
        assert "## Open Questions" in rendered
        assert "- Q1" in rendered

    def test_empty_optional_lists(self) -> None:
        tier3 = Tier3Extraction(claims=["C1"])
        rendered = _render_tier3_sections(tier3)
        assert "## Claims" in rendered
        assert "## Datasets & Benchmarks" in rendered
        assert "## Builds On" in rendered
        assert "## Open Questions" in rendered


# ── Sweep hooks injection seam preserved ─────────────────────────────


class TestSweepHooksInjectionSeam:
    """The SweepHooks dataclass still accepts test-injected fakes."""

    def test_fake_hooks_override_defaults(self) -> None:
        """Test injection via SweepHooks still works."""
        call_count = 0

        def fake_tier3(note: dict[str, object]) -> None:
            nonlocal call_count
            call_count += 1

        hooks = SweepHooks(tier3_extract=fake_tier3)
        # Narrow the optional callable; this is the test-injection seam,
        # not the production-default factory.
        assert hooks.tier3_extract is not None
        hooks.tier3_extract({"id": "n1"})
        assert call_count == 1

    def test_empty_sweep_hooks_has_none_hooks(self) -> None:
        hooks = SweepHooks()
        assert hooks.re_extract_archive is None
        assert hooks.tier2_enrich is None
        assert hooks.tier3_extract is None
        assert hooks.archive_download is None
        assert hooks.text_extraction is None


# ── archive_download hook (issue #23, FR-REP-1 stage 1) ───────────────


def _make_archive_missing_note(
    *,
    arxiv_id: str = "2604.26946",
    note_path: str = "papers/arxiv/2026/04",
    extra_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Build a note dict in the ``influx:archive-missing`` state."""
    tags = [
        "profile:ai-robotics",
        "ingested-by:influx",
        "source:arxiv",
        f"arxiv-id:{arxiv_id}",
        "text:abstract-only",
        "influx:repair-needed",
        "influx:archive-missing",
    ]
    if extra_tags:
        tags.extend(extra_tags)
    return {
        "id": f"arxiv-{arxiv_id}",
        "title": "Test Paper",
        "source_url": f"https://arxiv.org/abs/{arxiv_id}",
        "path": note_path,
        "content": _sample_note_content(),
        "tags": tags,
        "version": 1,
    }


class TestArchiveDownloadHookSuccess:
    def test_returns_relative_path_on_success(self, tmp_path: Path) -> None:
        from influx.storage import ArchiveResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_archive_missing_note()

        with patch("influx.repair_hooks.download_archive") as mock_dl:
            mock_dl.return_value = ArchiveResult(
                ok=True,
                rel_posix_path="arxiv/2026/04/2604.26946.pdf",
                error="",
            )
            assert hooks.archive_download is not None
            result = hooks.archive_download(note)

        assert result == "arxiv/2026/04/2604.26946.pdf"
        # Verify the download was invoked with the recovered metadata.
        kwargs = mock_dl.call_args.kwargs
        assert kwargs["url"] == "https://arxiv.org/pdf/2604.26946.pdf"
        assert kwargs["source"] == "arxiv"
        assert kwargs["item_id"] == "2604.26946"
        assert kwargs["published_year"] == 2026
        assert kwargs["published_month"] == 4
        assert kwargs["ext"] == ".pdf"
        assert kwargs["expected_content_type"] == "pdf"


class TestArchiveDownloadHookFailures:
    def test_oversize_raises_extraction_error_with_oversize_stage(
        self, tmp_path: Path
    ) -> None:
        """Oversize is a counted failure — the stage must round-trip."""
        from influx.repair_counters import classify_failure
        from influx.storage import ArchiveResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_archive_missing_note()

        with patch("influx.repair_hooks.download_archive") as mock_dl:
            mock_dl.return_value = ArchiveResult(
                ok=False,
                rel_posix_path=None,
                error="oversize: response body 12000000 bytes exceeds limit",
            )
            assert hooks.archive_download is not None
            with pytest.raises(ExtractionError) as exc_info:
                hooks.archive_download(note)

        assert exc_info.value.stage == "oversize"
        assert classify_failure(exc_info.value) == "counted"

    def test_http_error_raises_transient(self, tmp_path: Path) -> None:
        """HTTP 4xx/5xx is currently transient — the note retries next sweep."""
        from influx.repair_counters import classify_failure
        from influx.storage import ArchiveResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_archive_missing_note()

        with patch("influx.repair_hooks.download_archive") as mock_dl:
            mock_dl.return_value = ArchiveResult(
                ok=False,
                rel_posix_path=None,
                error="HTTP 503 for https://arxiv.org/pdf/2604.26946.pdf",
            )
            assert hooks.archive_download is not None
            with pytest.raises(ExtractionError) as exc_info:
                hooks.archive_download(note)

        assert exc_info.value.stage == "http"
        assert classify_failure(exc_info.value) == "transient"

    def test_timeout_is_transient(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure
        from influx.storage import ArchiveResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_archive_missing_note()

        with patch("influx.repair_hooks.download_archive") as mock_dl:
            mock_dl.return_value = ArchiveResult(
                ok=False,
                rel_posix_path=None,
                error="timeout: read timed out after 30s",
            )
            assert hooks.archive_download is not None
            with pytest.raises(ExtractionError) as exc_info:
                hooks.archive_download(note)

        assert exc_info.value.stage == "timeout"
        assert classify_failure(exc_info.value) == "transient"


class TestArchiveDownloadHookMetadataRecovery:
    def test_missing_arxiv_id_tag_raises_resolve(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_archive_missing_note()
        note["tags"] = [t for t in note["tags"] if not t.startswith("arxiv-id:")]

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "resolve"
        assert classify_failure(exc_info.value) == "transient"

    def test_missing_year_month_in_path_raises_resolve(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_archive_missing_note(note_path="papers/arxiv/")

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "resolve"

    def test_unsupported_source_raises_unsupported_source(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_archive_missing_note()
        note["tags"] = [t.replace("source:arxiv", "source:rss") for t in note["tags"]]

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "unsupported_source"
        # Still transient — RSS support can land later without a forced cap.
        assert classify_failure(exc_info.value) == "transient"


# ── text_extraction hook (issue #24, FR-REP-1 stage 2) ───────────────


def _make_textless_note(
    *,
    arxiv_id: str = "2604.26946",
    archive_path: str | None = None,
) -> dict[str, Any]:
    """Build a note dict with no ``text:*`` tag at all."""
    tags = [
        "profile:ai-robotics",
        "ingested-by:influx",
        "source:arxiv",
        f"arxiv-id:{arxiv_id}",
        "influx:repair-needed",
    ]
    return {
        "id": f"arxiv-{arxiv_id}",
        "title": "Test Paper",
        "source_url": f"https://arxiv.org/abs/{arxiv_id}",
        "path": "papers/arxiv/2026/04",
        "content": _sample_note_content(archive_path=archive_path),
        "tags": tags,
        "version": 1,
    }


class TestTextExtractionHookSuccess:
    def test_returns_html_tag_on_html_cascade_hit(self, tmp_path: Path) -> None:
        from influx.extraction.pipeline import ArxivExtractionResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_textless_note()

        with patch("influx.repair_hooks.extract_arxiv_text") as mock_x:
            mock_x.return_value = ArxivExtractionResult(
                text="full body",
                source_tag="text:html",
            )
            assert hooks.text_extraction is not None
            tag = hooks.text_extraction(note)

        assert tag == "text:html"
        assert mock_x.call_args.args[0] == "2604.26946"

    def test_returns_pdf_tag_when_html_falls_through(self, tmp_path: Path) -> None:
        from influx.extraction.pipeline import ArxivExtractionResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_textless_note()

        with patch("influx.repair_hooks.extract_arxiv_text") as mock_x:
            mock_x.return_value = ArxivExtractionResult(
                text="pdf body",
                source_tag="text:pdf",
            )
            assert hooks.text_extraction is not None
            tag = hooks.text_extraction(note)

        assert tag == "text:pdf"


class TestTextExtractionHookFailures:
    def test_cascade_failure_propagates_extraction_error(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_textless_note()

        with patch("influx.repair_hooks.extract_arxiv_text") as mock_x:
            mock_x.side_effect = ExtractionError(
                "cascade fell through",
                stage="cascade",
                detail="both html and pdf failed",
            )
            assert hooks.text_extraction is not None
            with pytest.raises(ExtractionError) as exc_info:
                hooks.text_extraction(note)

        assert exc_info.value.stage == "cascade"
        # ``cascade`` is not in _COUNTED_STAGES, so it stays transient —
        # the note re-enters the sweep next pass.
        assert classify_failure(exc_info.value) == "transient"

    def test_network_error_rewrapped_as_extraction_error(self, tmp_path: Path) -> None:
        from influx.errors import NetworkError

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_textless_note()

        with patch("influx.repair_hooks.extract_arxiv_text") as mock_x:
            mock_x.side_effect = NetworkError(
                "ssrf guard tripped",
                url="https://arxiv.org/pdf/2604.26946.pdf",
                kind="ssrf",
            )
            assert hooks.text_extraction is not None
            with pytest.raises(ExtractionError) as exc_info:
                hooks.text_extraction(note)

        assert exc_info.value.stage == "ssrf"


class TestTextExtractionHookMetadataRecovery:
    def test_missing_arxiv_id_raises_resolve(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_textless_note()
        note["tags"] = [t for t in note["tags"] if not t.startswith("arxiv-id:")]

        assert hooks.text_extraction is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.text_extraction(note)
        assert exc_info.value.stage == "resolve"
        assert classify_failure(exc_info.value) == "transient"

    def test_unsupported_source_raises_unsupported_source(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_textless_note()
        note["tags"] = [t.replace("source:arxiv", "source:rss") for t in note["tags"]]

        assert hooks.text_extraction is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.text_extraction(note)
        assert exc_info.value.stage == "unsupported_source"
        assert classify_failure(exc_info.value) == "transient"
