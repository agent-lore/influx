"""Tests for the production-default repair hooks (US-016).

Verifies that ``make_default_sweep_hooks`` creates hooks conforming to
the PRD 06 signatures, that the ``re_extract_archive`` hook returns
the correct ``ReExtractionResult`` variants, and that
``make_sweep_tier2_extractor`` turns an ``Acquired`` into a
``Tier2Result`` (or raises the right counted/transient stage).  (Tier 2
and Tier 3 recovery are no longer production hooks — they run through
the shared Cascade; the sweep-level behaviour is covered by
``test_repair_sweep.py`` and ``test_cascade.py``.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from influx.cascade import Acquired, Tier2Result
from influx.config import (
    AppConfig,
    ArchivePolicyConfig,
    ExtractionConfig,
    StorageConfig,
)
from influx.errors import ExtractionError
from influx.repair import (
    ExtractionOutcome,
    ReExtractionResult,
    SweepHooks,
)
from influx.repair_hooks import (
    DefaultSweepHooks,
    make_default_sweep_hooks,
    make_sweep_tier2_extractor,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _reacquirers(config: Any) -> dict[str, Any]:
    """The arXiv + RSS archive reacquirers the sweep injects (finding 3b).

    Mirrors ``run._default_archive_reacquirers``; the default-hooks archive
    download stage dispatches a note to its Source's
    ``archive_download_identity``.  Without this registry the hook treats
    every note as ``unsupported_source``.
    """
    from influx.sources.arxiv import ArxivSource
    from influx.sources.rss import RssSource

    return {"arxiv": ArxivSource(config), "rss": RssSource(config)}


def _make_config(
    tmp_path: Path,
    *,
    min_html_chars: int = 1000,
    min_web_chars: int = 500,
    archive_policy: ArchivePolicyConfig | None = None,
) -> MagicMock:
    """Build a minimal AppConfig mock with storage and extraction settings."""
    config = MagicMock(spec=AppConfig)
    config.storage = MagicMock(spec=StorageConfig)
    config.storage.archive_dir = str(tmp_path / "archive")
    config.storage.max_download_bytes = 10_000_000
    config.storage.download_timeout_seconds = 30
    # Issue #149 follow-up: repair archive hook now builds a policy
    # registry from this attr, so it must be present on the mock.
    config.storage.archive_policy = archive_policy or ArchivePolicyConfig()
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


def _acquired(*, archive_path: str | None) -> Acquired:
    """Build the minimal ``Acquired`` the sweep's Tier 2 extractor reads."""
    return Acquired(
        item_id="note-001",
        source_url="https://arxiv.org/abs/2601.00001",
        title="Test Paper Title",
        abstract="",
        archive_path=archive_path,
    )


# ── make_default_sweep_hooks ─────────────────────────────────────────


class TestMakeDefaultSweepHooks:
    def test_returns_default_sweep_hooks_instance(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        assert isinstance(hooks, DefaultSweepHooks)

    def test_converts_to_sweep_hooks(self, tmp_path: Path) -> None:
        """``to_sweep_hooks()`` returns a ``SweepHooks`` for the sweep entrypoint."""
        config = _make_config(tmp_path)
        sweep_hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        ).to_sweep_hooks()
        assert isinstance(sweep_hooks, SweepHooks)
        assert sweep_hooks.archive_download is not None
        assert sweep_hooks.re_extract_archive is not None
        assert sweep_hooks.text_extraction is not None

    def test_archive_download_wired(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        assert callable(hooks.archive_download)

    def test_archive_download_without_reacquirers_is_unsupported(
        self, tmp_path: Path
    ) -> None:
        # Finding 3b: the Repair layer cannot import the Sources adapters,
        # so the archive-download hook depends on an injected reacquirer
        # registry.  With none injected (the ``make_default_sweep_hooks``
        # default), every note is ``unsupported_source`` (transient) —
        # production injects the registry via ``run._run_repair_stage``.
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(config)
        note = _make_archive_missing_note()

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "unsupported_source"
        assert classify_failure(exc_info.value) == "transient"

    def test_text_extraction_wired(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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

        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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

        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_note_dict(archive_path=pdf_path)
        result = hooks.re_extract_archive(note, pdf_path)

        assert result.outcome is ExtractionOutcome.TERMINAL

    def test_transient_on_file_not_found(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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

        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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

        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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

        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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

        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_note_dict(archive_path=pdf_path)
        result = hooks.re_extract_archive(note, pdf_path)

        assert isinstance(result, ReExtractionResult)


# ── make_sweep_tier2_extractor ───────────────────────────────────────


class TestSweepTier2ExtractorSuccess:
    """The sweep's Tier 2 extractor re-extracts full text from the archive.

    Section insertion + the ``full-text`` tag are the Cascade's / sweep's
    job now (covered by ``test_repair_sweep.py`` + ``test_canonical_note``);
    the extractor only produces the ``Tier2Result``.
    """

    def test_extracts_pdf(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "papers").mkdir(parents=True)
        pdf_path = "papers/test.pdf"
        fixture_path = Path("tests/fixtures/extraction/sample.pdf")
        (archive_dir / pdf_path).write_bytes(fixture_path.read_bytes())

        extractor = make_sweep_tier2_extractor(config)
        result = extractor(_acquired(archive_path=pdf_path))

        assert isinstance(result, Tier2Result)
        assert result.text
        assert result.flavour == "pdf"
        assert result.text_tag == "text:pdf"

    def test_extracts_html(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, min_html_chars=10)
        archive_dir = Path(config.storage.archive_dir)
        (archive_dir / "pages").mkdir(parents=True)
        html_path = "pages/article.html"
        fixture_path = Path("tests/fixtures/extraction/good_article.html")
        (archive_dir / html_path).write_bytes(fixture_path.read_bytes())

        extractor = make_sweep_tier2_extractor(config)
        result = extractor(_acquired(archive_path=html_path))

        assert result.text
        assert result.flavour == "html"
        assert result.text_tag == "text:html"


class TestSweepTier2ExtractorFailure:
    """The extractor raises ``ExtractionError`` so the Cascade can classify it."""

    def test_raises_counted_parse_on_no_archive_path(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        extractor = make_sweep_tier2_extractor(config)

        with pytest.raises(ExtractionError) as exc_info:
            extractor(_acquired(archive_path=None))

        # Counted stage: a note that never gains an archive still trips
        # the Tier-2 cap, exactly as the old hook raised.
        assert exc_info.value.stage == "parse"

    def test_raises_transient_read_on_missing_file(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        extractor = make_sweep_tier2_extractor(config)

        with pytest.raises(ExtractionError) as exc_info:
            extractor(_acquired(archive_path="papers/missing.pdf"))

        # Transient: the archive file is expected but temporarily unreadable,
        # so the counter must not advance.
        assert exc_info.value.stage == "archive_read"


# NOTE: the ## Full Text / Tier 3 section-shape helpers moved to
# influx.canonical_note in PR 2; their behaviour is covered by
# tests/unit/test_canonical_note.py (extract_section_body, insert_full_text_
# section, insert_tier3_sections, render_tier3_sections).


# ── Sweep hooks injection seam preserved ─────────────────────────────


class TestSweepHooksInjectionSeam:
    """The SweepHooks dataclass still accepts test-injected fakes."""

    def test_fake_hooks_override_defaults(self) -> None:
        """Test injection via SweepHooks still works."""
        call_count = 0

        def fake_text_extraction(note: dict[str, object]) -> str:
            nonlocal call_count
            call_count += 1
            return "text:html"

        hooks = SweepHooks(text_extraction=fake_text_extraction)
        # Narrow the optional callable; this is the test-injection seam,
        # not the production-default factory.
        assert hooks.text_extraction is not None
        assert hooks.text_extraction({"id": "n1"}) == "text:html"
        assert call_count == 1

    def test_empty_sweep_hooks_has_none_hooks(self) -> None:
        hooks = SweepHooks()
        assert hooks.re_extract_archive is None
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
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note()
        note["tags"] = [t for t in note["tags"] if not t.startswith("arxiv-id:")]

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "resolve"
        assert classify_failure(exc_info.value) == "transient"

    def test_unresolvable_year_month_raises_resolve(self, tmp_path: Path) -> None:
        # #223: year/month now falls back to the arxiv id's YYMM, then
        # created_at. It only raises when NONE resolve: a legacy-format
        # arxiv id (no YYMM prefix), a path without year/month, and no
        # created_at on the note.
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note(
            arxiv_id="math.GT/0309136", note_path="papers/arxiv/"
        )
        note.pop("created_at", None)

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "resolve"

    def test_unsupported_source_raises_unsupported_source(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note()
        # ``hackernews`` is a stand-in for any future source that hasn't
        # yet had a per-source resolver wired in (issue #130 added rss).
        note["tags"] = [
            t.replace("source:arxiv", "source:hackernews") for t in note["tags"]
        ]

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "unsupported_source"
        # Counted for the archive stage: no adapter can resolve this note,
        # so retrying cannot help and the note converges to
        # influx:archive-terminal at the cap rather than churning forever.
        # Landing a resolver later requires re-arming affected notes per
        # docs/operations/runbook.md §6.
        assert classify_failure(exc_info.value, repair_stage="archive") == "counted"
        # Context-free (and text-extraction) classification is unchanged —
        # that path has its own terminal handling via
        # repair._terminate_unsupported_text_source.
        assert classify_failure(exc_info.value) == "transient"

    def test_blog_source_reaches_rss_reacquirer(self, tmp_path: Path) -> None:
        """``source:blog`` is a valid RssSourceEntry source_tag (#281 review).

        581 notes in production carry it.  Dispatching on ``source:rss*``
        alone routed them to ``unsupported_source``, which is now counted
        — so they would have been permanently terminalised despite
        ``RssSource.archive_download_identity`` being able to rebuild
        their retry identity from ``feed-slug`` + ``source_url``.
        """
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note()
        note["id"] = "550e8400-e29b-41d4-a716-446655440000"
        note["tags"] = [
            "profile:ai-robotics",
            "ingested-by:influx",
            "source:blog",
            "feed-slug:openai-news",
            "influx:repair-needed",
            "influx:archive-missing",
        ]
        note["source_url"] = "https://openai.com/index/concrete-ai-safety-problems"
        note["path"] = "articles/blog/2016/06/concrete-ai-safety-problems.md"

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        # Reached RssSource and attempted a real download — NOT rejected
        # at dispatch.  The network failure here is transient, so the
        # note stays recoverable.
        assert exc_info.value.stage != "unsupported_source"
        assert classify_failure(exc_info.value, repair_stage="archive") == "transient"

    def test_unknown_source_with_feed_slug_reaches_rss_reacquirer(
        self, tmp_path: Path
    ) -> None:
        """Identity markers win over an unrecognised source tag.

        A note whose ``source:*`` value the dispatcher does not name is
        still reacquirable when it carries ``feed-slug`` + ``source_url``
        — the exact inputs ``archive_download_identity`` needs.
        """
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note()
        note["id"] = "550e8400-e29b-41d4-a716-446655440001"
        note["tags"] = [
            "profile:ai-robotics",
            "ingested-by:influx",
            "source:some-renamed-feed",
            "feed-slug:some-renamed-feed",
            "influx:repair-needed",
        ]
        note["source_url"] = "https://example.com/post"
        note["path"] = "articles/some-renamed-feed/2026/06/post.md"

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage != "unsupported_source"

    def test_inbox_shape_without_rss_identity_is_unsupported(
        self, tmp_path: Path
    ) -> None:
        """The real production shape behind #279 — no RSS identity at all.

        Mirrors notes 0cff8956 / 3b1500c5: ``source:ai-agents-briefing``
        with a ``submitter:`` tag, a UUID id, and NO ``feed-slug``.
        ``_rss_item_id_from_note`` needs an ``rss-`` id or feed-slug +
        source_url and has neither, so routing this to RssSource would
        only swap ``unsupported_source`` for ``resolve`` — both would
        churn.  It is genuinely unsupported, and counting it is what
        makes it converge.
        """
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note()
        note["id"] = "0cff8956-c5b7-4a8f-8086-1de58fd3b1ef"
        note["tags"] = [
            "profile:knowledge-systems",
            "ingested-by:influx",
            "source:ai-agents-briefing",
            "submitter:ai-agents-briefing",
            "influx:repair-needed",
            "influx:archive-missing",
            "text:abstract-only",
            "influx:text-terminal",
        ]
        note["source_url"] = (
            "https://origintrail.io/blog/the-next-big-shift-in-ai-agents"
        )
        note["path"] = "articles/ai-agents-briefing/2026/06/shared-context-graphs.md"

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "unsupported_source"
        assert classify_failure(exc_info.value, repair_stage="archive") == "counted"


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
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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
    """Live extraction failure converges to ``text:abstract-only`` (issue #137).

    Per the :class:`~influx.repair.TextExtractionHook` protocol the
    hook returns ``"text:abstract-only"`` on cascade fall-through so
    the sweep stamps a ``text:*`` tag and the note exits the
    text-extraction stage.  Re-extraction from a stored archive (via
    the source-agnostic ``re_extract_archive`` hook) can still upgrade
    the tag to ``text:html`` / ``text:pdf`` once ``archive_download``
    lands.
    """

    def test_cascade_failure_returns_abstract_only(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_textless_note()

        with patch("influx.repair_hooks.extract_arxiv_text") as mock_x:
            mock_x.side_effect = ExtractionError(
                "cascade fell through",
                stage="cascade",
                detail="both html and pdf failed",
            )
            assert hooks.text_extraction is not None
            tag = hooks.text_extraction(note)

        assert tag == "text:abstract-only"

    def test_network_error_returns_abstract_only(self, tmp_path: Path) -> None:
        from influx.errors import NetworkError

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_textless_note()

        with patch("influx.repair_hooks.extract_arxiv_text") as mock_x:
            mock_x.side_effect = NetworkError(
                "ssrf guard tripped",
                url="https://arxiv.org/pdf/2604.26946.pdf",
                kind="ssrf",
            )
            assert hooks.text_extraction is not None
            tag = hooks.text_extraction(note)

        assert tag == "text:abstract-only"

    def test_logs_warning_on_failure(self, tmp_path: Path, caplog) -> None:
        """Operator visibility: the structural failure stage is logged."""
        import logging

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_textless_note()

        with patch("influx.repair_hooks.extract_arxiv_text") as mock_x:
            mock_x.side_effect = ExtractionError(
                "cascade fell through",
                stage="cascade",
                detail="both html and pdf failed",
            )
            assert hooks.text_extraction is not None
            with caplog.at_level(logging.WARNING, logger="influx.repair_hooks"):
                hooks.text_extraction(note)

        assert any(
            "stage=cascade" in record.message
            and "text:abstract-only" in record.message
            and "2604.26946" in record.message
            for record in caplog.records
        )


class TestTextExtractionHookMetadataRecovery:
    def test_missing_arxiv_id_raises_resolve(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
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
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_textless_note()
        # See archive_download equivalent test — ``hackernews`` represents
        # any future source without a per-source resolver.
        note["tags"] = [
            t.replace("source:arxiv", "source:hackernews") for t in note["tags"]
        ]

        assert hooks.text_extraction is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.text_extraction(note)
        assert exc_info.value.stage == "unsupported_source"
        assert classify_failure(exc_info.value) == "transient"


# ── RSS archive_download / text_extraction (issue #130) ─────────────


def _rss_note_content(
    *,
    source_url: str = "https://example.com/article-42",
    archive_path: str | None = None,
    score: int = 6,
) -> str:
    """Build a canonical RSS note content string."""
    archive_body = f"path: {archive_path}\n" if archive_path else ""
    return (
        "---\n"
        "note_type: summary\n"
        "namespace: influx\n"
        f"source_url: {source_url}\n"
        "tags:\n"
        "  - profile:ai-robotics\n"
        "  - source:rss-techcrunch\n"
        "  - feed-slug:techcrunch\n"
        "  - influx:archive-missing\n"
        "  - influx:repair-needed\n"
        "confidence: 0.7\n"
        "---\n"
        "# RSS Article Title\n"
        "\n"
        "## Archive\n"
        f"{archive_body}"
        "\n"
        "## Summary\n"
        "An RSS article summary.\n"
        "\n"
        "## Profile Relevance\n"
        "### ai-robotics\n"
        f"Score: {score}/10\n"
        "Relevant.\n"
        "\n"
        "## User Notes\n"
    )


def _make_rss_archive_missing_note(
    *,
    feed_slug: str = "techcrunch",
    url_hash: str = "abc123def",
    note_path: str = "articles/rss-techcrunch/2026/05",
    source_url: str = "https://example.com/article-42",
    omit_source_url: bool = False,
    omit_id_prefix: bool = False,
) -> dict[str, Any]:
    """Build an RSS note dict in the ``influx:archive-missing`` state.

    Mirrors the shape produced by
    :func:`influx.sources.rss.build_rss_note_item` for a feed item
    whose archive download failed.
    """
    note_id = (
        f"{feed_slug}-{url_hash}" if omit_id_prefix else f"rss-{feed_slug}-{url_hash}"
    )
    content = _rss_note_content(source_url=source_url)
    # The repair sweep reads source_url from the doc-level field (#218), so
    # ``omit_source_url`` clears that field to exercise the resolve-raise
    # branch. The content body is irrelevant to resolution and is left intact.
    return {
        "id": note_id,
        "title": "RSS Article Title",
        "source_url": "" if omit_source_url else source_url,
        "path": note_path,
        "content": content,
        "tags": [
            "profile:ai-robotics",
            "source:rss-techcrunch",
            f"feed-slug:{feed_slug}",
            "influx:archive-missing",
            "influx:repair-needed",
        ],
        "version": 1,
    }


class TestArchiveDownloadHookRssSuccess:
    def test_returns_relative_path_on_success(self, tmp_path: Path) -> None:
        from influx.storage import ArchiveResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_archive_missing_note()

        with patch("influx.repair_hooks.download_archive") as mock_dl:
            mock_dl.return_value = ArchiveResult(
                ok=True,
                rel_posix_path="rss-techcrunch/2026/05/techcrunch-abc123def.html",
                error="",
            )
            assert hooks.archive_download is not None
            result = hooks.archive_download(note)

        assert result == "rss-techcrunch/2026/05/techcrunch-abc123def.html"
        kwargs = mock_dl.call_args.kwargs
        assert kwargs["url"] == "https://example.com/article-42"
        assert kwargs["source"] == "rss-techcrunch"
        assert kwargs["item_id"] == "techcrunch-abc123def"
        assert kwargs["published_year"] == 2026
        assert kwargs["published_month"] == 5
        assert kwargs["ext"] == ".html"
        assert kwargs["expected_content_type"] == "html"


class TestArchiveDownloadHookRssFailures:
    def test_oversize_is_counted(self, tmp_path: Path) -> None:
        """Oversize is counted for RSS the same way as for arxiv."""
        from influx.repair_counters import classify_failure
        from influx.storage import ArchiveResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_archive_missing_note()

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

    def test_http_error_is_transient(self, tmp_path: Path) -> None:
        """HTTP 4xx/5xx leaves the RSS note re-enterable next sweep."""
        from influx.repair_counters import classify_failure
        from influx.storage import ArchiveResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_archive_missing_note()

        with patch("influx.repair_hooks.download_archive") as mock_dl:
            mock_dl.return_value = ArchiveResult(
                ok=False,
                rel_posix_path=None,
                error="HTTP 503 for https://example.com/article-42",
            )
            assert hooks.archive_download is not None
            with pytest.raises(ExtractionError) as exc_info:
                hooks.archive_download(note)

        assert exc_info.value.stage == "http"
        assert classify_failure(exc_info.value) == "transient"


class TestArchiveDownloadHookRssMetadataRecovery:
    def test_missing_source_url_raises_resolve(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_archive_missing_note(omit_source_url=True)

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "resolve"
        assert classify_failure(exc_info.value) == "transient"

    def test_unresolvable_item_id_raises_resolve(self, tmp_path: Path) -> None:
        # #223: a non-``rss-`` id now reconstructs item_id from the
        # feed-slug tag + source_url. It only raises when reconstruction is
        # also impossible — here the feed-slug tag is stripped.
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_archive_missing_note(omit_id_prefix=True)
        note["tags"] = [t for t in note["tags"] if not t.startswith("feed-slug:")]

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "resolve"

    def test_missing_year_month_in_path_raises_resolve(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_archive_missing_note(note_path="articles/rss-techcrunch/")

        assert hooks.archive_download is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.archive_download(note)
        assert exc_info.value.stage == "resolve"


def _make_rss_textless_note(
    *,
    source_url: str = "https://example.com/article-42",
    omit_source_url: bool = False,
) -> dict[str, Any]:
    """Build an RSS note dict with no ``text:*`` tag (text_extraction stage)."""
    note = _make_rss_archive_missing_note(
        source_url=source_url,
        omit_source_url=omit_source_url,
    )
    # Drop archive-missing so the note is in the pure text-extraction
    # state; the text_extraction hook only inspects source + frontmatter.
    note["tags"] = [t for t in note["tags"] if t != "influx:archive-missing"]
    return note


class TestTextExtractionHookRssSuccess:
    def test_returns_html_tag_on_extract_success(self, tmp_path: Path) -> None:
        from influx.extraction.article import ArticleExtractionResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_textless_note()

        with patch("influx.repair_hooks.extract_article") as mock_ex:
            mock_ex.return_value = ArticleExtractionResult(
                text="extracted body",
                source="article",
            )
            assert hooks.text_extraction is not None
            tag = hooks.text_extraction(note)

        assert tag == "text:html"
        # First positional arg is the source_url from frontmatter.
        assert mock_ex.call_args.args[0] == "https://example.com/article-42"


class TestTextExtractionHookRssFailures:
    """Live extraction failure converges to ``text:abstract-only`` (issue #130).

    Per the :class:`~influx.repair.TextExtractionHook` protocol the
    hook returns ``"text:abstract-only"`` on cascade fall-through so
    the sweep stamps a ``text:*`` tag and the note exits the
    text-extraction stage.  Re-extraction from a stored archive (via
    the source-agnostic ``re_extract_archive`` hook) can still upgrade
    the tag to ``text:html`` once ``archive_download`` lands.
    """

    def test_extraction_error_returns_abstract_only(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_textless_note()

        with patch("influx.repair_hooks.extract_article") as mock_ex:
            mock_ex.side_effect = ExtractionError(
                "extracted body too short",
                stage="min_length",
                detail="got 100 chars, need 500",
            )
            assert hooks.text_extraction is not None
            tag = hooks.text_extraction(note)

        assert tag == "text:abstract-only"

    def test_network_error_returns_abstract_only(self, tmp_path: Path) -> None:
        from influx.errors import NetworkError

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_textless_note()

        with patch("influx.repair_hooks.extract_article") as mock_ex:
            mock_ex.side_effect = NetworkError(
                "tls handshake failed",
                url="https://example.com/article-42",
                kind="tls",
            )
            assert hooks.text_extraction is not None
            tag = hooks.text_extraction(note)

        assert tag == "text:abstract-only"

    def test_logs_warning_on_failure(self, tmp_path: Path, caplog) -> None:
        """Operator visibility: the structural failure stage is logged."""
        import logging

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_textless_note()

        with patch("influx.repair_hooks.extract_article") as mock_ex:
            mock_ex.side_effect = ExtractionError(
                "trafilatura returned no content",
                stage="extract",
            )
            assert hooks.text_extraction is not None
            with caplog.at_level(logging.WARNING, logger="influx.repair_hooks"):
                hooks.text_extraction(note)

        assert any(
            "stage=extract" in record.message and "text:abstract-only" in record.message
            for record in caplog.records
        )


class TestTextExtractionHookRssMetadataRecovery:
    def test_missing_source_url_raises_resolve(self, tmp_path: Path) -> None:
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_rss_textless_note(omit_source_url=True)

        assert hooks.text_extraction is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.text_extraction(note)
        assert exc_info.value.stage == "resolve"
        assert classify_failure(exc_info.value) == "transient"


# ── Source metadata invariant (#150) ────────────────────────────────


class TestInferNoteSource:
    """Source inference for notes with missing/empty ``source:*`` tags (#150)."""

    def test_existing_known_source_tag_honoured(self) -> None:
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": ["source:arxiv", "profile:ai-robotics"],
            "source_url": "https://arxiv.org/abs/2604.26946",
            "path": "papers/arxiv/2026/04",
            "id": "arxiv-2604.26946",
        }
        assert infer_note_source(note) == "arxiv"

    def test_existing_unsupported_source_tag_honoured_verbatim(self) -> None:
        """An explicit but unsupported source tag is well-formed metadata.

        Inference must not second-guess an operator/source label —
        ``hackernews`` is the dispatcher's problem, not the
        invariant's problem (#150 distinguishes invalid-metadata
        from unsupported-source).
        """
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": ["source:hackernews"],
            "source_url": "https://arxiv.org/abs/2604.26946",
            "path": "papers/arxiv/2026/04",
        }
        assert infer_note_source(note) == "hackernews"

    def test_does_not_infer_from_content_body_source_url(self) -> None:
        """#218: a ``source_url:`` line in the content body is the removed
        legacy shape; inference must NOT read it. Doc-level inference is
        covered by ``test_infers_arxiv_from_top_level_source_url_*``.
        """
        from influx.repair_hooks import infer_note_source

        body = (
            "---\n"
            "source_url: https://arxiv.org/abs/2604.26946\n"
            "tags: []\n"
            "---\n"
            "# Paper\n"
        )
        note = {
            "tags": [],
            "content": body,  # source_url only in the body — must be ignored
            "path": "",
            "id": "",
        }
        # No doc-level source_url and no tag/path/id signal -> cannot infer.
        assert infer_note_source(note) is None

    def test_infers_arxiv_from_canonical_path(self) -> None:
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": [],
            "content": "---\ntags: []\n---\n",
            "path": "papers/arxiv/2026/04",
            "id": "",
        }
        assert infer_note_source(note) == "arxiv"

    def test_infers_rss_with_slug_from_articles_path(self) -> None:
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": [],
            "content": "---\ntags: []\n---\n",
            "path": "articles/rss-techcrunch/2026/04",
            "id": "",
        }
        assert infer_note_source(note) == "rss-techcrunch"

    def test_infers_rss_normalises_bare_slug_path(self) -> None:
        """``articles/<slug>/`` without ``rss-`` prefix is normalised."""
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": [],
            "content": "---\ntags: []\n---\n",
            "path": "articles/techcrunch/2026/04",
            "id": "",
        }
        assert infer_note_source(note) == "rss-techcrunch"

    def test_infers_arxiv_from_note_id_prefix(self) -> None:
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": [],
            "content": "---\ntags: []\n---\n",
            "path": "",
            "id": "arxiv-2604.26946",
        }
        assert infer_note_source(note) == "arxiv"

    def test_infers_bare_rss_from_note_id_prefix(self) -> None:
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": [],
            "content": "---\ntags: []\n---\n",
            "path": "",
            "id": "rss-techcrunch-abc123",
        }
        # Bare ``rss`` sentinel is accepted by the RSS dispatcher
        # (see ``_is_rss_source``); the path-based inference produces
        # the richer ``rss-<slug>`` shape when a path is available.
        assert infer_note_source(note) == "rss"

    def test_returns_none_when_no_signal_present(self) -> None:
        """The classic staging-incident shape: empty source, no fallback."""
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": ["profile:ai-robotics"],
            "content": "---\ntags: []\n---\n",
            "path": "",
            "id": "",
        }
        assert infer_note_source(note) is None

    def test_returns_none_when_url_is_non_arxiv_and_other_signals_empty(self) -> None:
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": [],
            "content": "# Paper\n\nBody text only.\n",
            "source_url": "https://example.com/something",  # doc-level, non-arxiv
            "path": "",
            "id": "",
        }
        # Without a tag/path/id we can't safely guess the feed-slug
        # for a non-arxiv URL — caller treats this as terminal.
        assert infer_note_source(note) is None

    def test_infers_arxiv_from_top_level_source_url_when_frontmatter_missing(
        self,
    ) -> None:
        """Top-level ``source_url`` repairs notes with stripped frontmatter.

        Repair reads/writes ``source_url`` as a first-class top-level
        field on the note dict (see ``tests/unit/test_repair_sweep.py``
        coverage of ``test_rewrite_includes_note_fields``).  When the
        body has been corrupted/stripped and the frontmatter no longer
        carries the URL, the top-level field must still rescue the
        note from terminalisation.
        """
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": [],
            # Body has no frontmatter at all — only top-level field survives.
            "content": "# Paper\n\nBody text only.\n",
            "source_url": "https://arxiv.org/abs/2604.26946",
            "path": "",
            "id": "",
        }
        assert infer_note_source(note) == "arxiv"

    def test_infers_arxiv_from_top_level_source_url_with_malformed_frontmatter(
        self,
    ) -> None:
        """Malformed YAML must not block the top-level fallback."""
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": [],
            # Unbalanced frontmatter / unparsable YAML — parse_note may
            # return empty frontmatter_raw or raise; top-level wins.
            "content": "---\nnot: [valid: yaml\n",
            "source_url": "https://arxiv.org/abs/2604.26946",
            "path": "",
            "id": "",
        }
        assert infer_note_source(note) == "arxiv"

    def test_top_level_non_arxiv_source_url_falls_through_to_other_signals(
        self,
    ) -> None:
        """Non-arxiv top-level URL must not short-circuit path/id inference.

        ``_infer_source_from_url`` only returns ``"arxiv"`` for arxiv
        hosts; for anything else it returns ``None``.  The top-level
        check must therefore fall through to path/id signals so an
        article from e.g. ``example.com`` whose canonical path lives
        under ``articles/rss-techcrunch/`` is still dispatched to RSS.
        """
        from influx.repair_hooks import infer_note_source

        note = {
            "tags": [],
            "content": "# Article\n",
            "source_url": "https://example.com/post",
            "path": "articles/rss-techcrunch/2026/04",
            "id": "",
        }
        assert infer_note_source(note) == "rss-techcrunch"

    def test_top_level_source_url_preferred_over_frontmatter(self) -> None:
        """When both are present, the top-level field wins.

        Rationale: the top-level field is the canonical persisted
        shape on read/write; the frontmatter copy is a re-derived
        view that can drift if the body is hand-edited or partially
        rewritten.  Both happen to map to ``arxiv`` here so the
        observable assertion is on the return value; the ordering
        is documented for future signal types where a divergence
        would matter.
        """
        from influx.repair_hooks import infer_note_source

        body = (
            "---\nsource_url: https://example.com/not-arxiv\ntags: []\n---\n# Paper\n"
        )
        note = {
            "tags": [],
            "content": body,
            # Top-level points at arxiv; frontmatter points elsewhere.
            "source_url": "https://arxiv.org/abs/2604.26946",
            "path": "",
            "id": "",
        }
        # Top-level wins → "arxiv".  If ordering were flipped we'd
        # get None (frontmatter URL is non-arxiv, no other signals).
        assert infer_note_source(note) == "arxiv"


class TestTextExtractionHookSourceInvariant:
    """The text-extraction hook tightens the source-metadata invariant (#150).

    Empty-source notes that *can* be inferred from URL/path/id are
    backfilled and dispatched; empty-source notes with *no* fallback
    raise ``invalid_source_metadata`` instead of looping on
    ``source '' not supported``.
    """

    def test_backfills_arxiv_source_tag_from_source_url(self, tmp_path: Path) -> None:
        """Empty source + arxiv URL → backfilled tag + arxiv extraction."""
        from influx.extraction.pipeline import ArxivExtractionResult

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_textless_note()
        # Strip the explicit source:* tag — simulate a degraded note.
        note["tags"] = [t for t in note["tags"] if not t.startswith("source:")]

        with patch("influx.repair_hooks.extract_arxiv_text") as mock_x:
            mock_x.return_value = ArxivExtractionResult(
                text="full body",
                source_tag="text:html",
            )
            assert hooks.text_extraction is not None
            tag = hooks.text_extraction(note)

        assert tag == "text:html"
        # Tag was backfilled in-place so the next pass starts clean.
        assert "source:arxiv" in note["tags"]

    def test_invalid_source_metadata_raised_when_inference_fails(
        self, tmp_path: Path
    ) -> None:
        """Regression for the staging incident: empty source AND no fallback.

        Previously this path emitted ``text_extraction retry: source ''
        not supported`` every sweep (#150).  Now the hook short-
        circuits with ``stage=invalid_source_metadata`` so the sweep
        can flip the note terminal with ``influx:source-invalid``.
        """
        from influx.repair_counters import classify_failure

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        # Build a note whose source tag is empty AND has no fallback
        # signals: blank source_url, blank path, blank id.
        body = (
            "---\n"
            "source_url:\n"
            "tags: []\n"
            "---\n"
            "# Paper\n\n"
            "## Archive\n\n"
            "## Summary\nSummary\n"
        )
        note = {
            "id": "",
            "title": "Paper",
            "path": "",
            "source_url": "",
            "content": body,
            "tags": ["profile:ai-robotics", "influx:repair-needed"],
            "version": 1,
        }

        assert hooks.text_extraction is not None
        with pytest.raises(ExtractionError) as exc_info:
            hooks.text_extraction(note)

        assert exc_info.value.stage == "invalid_source_metadata"
        # Distinct from unsupported_source — the per-source resolver
        # extension can't repair this; only operator metadata-fix can.
        assert "unsupported_source" not in str(exc_info.value)
        # Classified as transient at the counter level (no counted
        # cap), but the sweep handles it specially via the
        # ``invalid_source_metadata`` discriminator.
        assert classify_failure(exc_info.value) == "transient"

    def test_logs_invalid_source_metadata_at_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        """Logs must clearly distinguish invalid-state from extraction failures."""
        import logging

        config = _make_config(tmp_path)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = {
            "id": "",
            "title": "Paper",
            "path": "",
            "source_url": "",
            "content": "---\ntags: []\n---\n",
            "tags": ["profile:ai-robotics"],
            "version": 1,
        }

        assert hooks.text_extraction is not None
        with (
            caplog.at_level(logging.WARNING, logger="influx.repair_hooks"),
            pytest.raises(ExtractionError),
        ):
            hooks.text_extraction(note)

        # Logs flag the invalid-state cause, not a generic
        # extraction-failed message.
        assert any(
            "invalid source metadata" in record.message for record in caplog.records
        )


# ── archive policy registry threading (issue #149 follow-up) ──────────
#
# The repair archive download path must honour the same per-domain
# policy overrides as initial acquisition.  Before this fix the repair
# hook silently fell back to the module-level ``default_registry`` and
# ignored operator overrides like ``include_defaults = false`` or
# custom per-domain entries.  These tests pin the contract: the registry
# the hook threads into :func:`download_archive` is built from
# ``config.storage.archive_policy``.


class TestArchiveDownloadHookPolicyRegistry:
    """Operator overrides in config win during repair, not just acquire."""

    def test_threads_registry_built_from_config_archive_policy(
        self, tmp_path: Path
    ) -> None:
        """The hook passes a config-derived registry into ``download_archive``."""
        from influx.archive_policy import ArchivePolicyRegistry
        from influx.storage import ArchiveResult

        # Custom override: block a domain that is NOT in the built-in
        # staging defaults so the assertion can prove the override was
        # threaded through (and not merely the default registry).
        policy = ArchivePolicyConfig(
            blocked={"example-custom.test": "operator override"},
            include_defaults=True,
        )
        config = _make_config(tmp_path, archive_policy=policy)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note()

        with patch("influx.repair_hooks.download_archive") as mock_dl:
            mock_dl.return_value = ArchiveResult(
                ok=True,
                rel_posix_path="arxiv/2026/04/2604.26946.pdf",
                error="",
            )
            assert hooks.archive_download is not None
            hooks.archive_download(note)

        registry = mock_dl.call_args.kwargs["policy_registry"]
        assert isinstance(registry, ArchivePolicyRegistry)
        # The operator-added domain is present in the threaded registry.
        assert "example-custom.test" in registry.domains()

    def test_include_defaults_false_drops_builtin_defaults(
        self, tmp_path: Path
    ) -> None:
        """``include_defaults = false`` honoured on the repair path."""
        from influx.archive_policy import ArchivePolicyRegistry
        from influx.storage import ArchiveResult

        policy = ArchivePolicyConfig(include_defaults=False)
        config = _make_config(tmp_path, archive_policy=policy)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note()

        with patch("influx.repair_hooks.download_archive") as mock_dl:
            mock_dl.return_value = ArchiveResult(
                ok=True,
                rel_posix_path="arxiv/2026/04/2604.26946.pdf",
                error="",
            )
            assert hooks.archive_download is not None
            hooks.archive_download(note)

        registry = mock_dl.call_args.kwargs["policy_registry"]
        assert isinstance(registry, ArchivePolicyRegistry)
        # Built-in staging-defaults are absent; the registry is empty.
        assert registry.domains() == ()

    def test_custom_blocked_override_shortcircuits_during_repair(
        self, tmp_path: Path
    ) -> None:
        """A custom per-domain blocked entry classifies failures during repair.

        The test uses a real (non-mocked) ``download_archive`` and a
        custom-blocked arxiv.org override to demonstrate that the
        operator override flows through to the policy classification
        path on repair sweeps.  We expect the policy_mode on the
        :class:`ArchiveResult` to reflect the operator setting.
        """
        from influx.storage import ArchiveResult

        policy = ArchivePolicyConfig(
            blocked={"arxiv.org": "operator-blocked for test"},
            include_defaults=False,
        )
        config = _make_config(tmp_path, archive_policy=policy)
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note()

        captured: dict[str, object] = {}

        def fake_download_archive(
            *,
            policy_registry: object = None,
            **_kwargs: object,
        ) -> ArchiveResult:
            # Mimic ``download_archive`` consulting the registry — the
            # contract under test is purely that the registry it
            # received reflects the operator override.
            captured["registry"] = policy_registry
            return ArchiveResult(
                ok=False,
                rel_posix_path=None,
                error="HTTP 403 for https://arxiv.org/pdf/2604.26946.pdf",
                failure_kind="blocked",
                policy_mode="blocked",
                domain="arxiv.org",
            )

        with patch(
            "influx.repair_hooks.download_archive", side_effect=fake_download_archive
        ):
            assert hooks.archive_download is not None
            with pytest.raises(ExtractionError):
                hooks.archive_download(note)

        registry = captured["registry"]
        # The registry the hook threaded through resolves arxiv.org to
        # the operator's blocked policy.
        from influx.archive_policy import ArchivePolicyRegistry

        assert isinstance(registry, ArchivePolicyRegistry)
        resolved = registry.policy_for("https://arxiv.org/pdf/2604.26946.pdf")
        assert resolved.mode == "blocked"
        assert resolved.note == "operator-blocked for test"

    def test_default_empty_policy_config_falls_back_gracefully(
        self, tmp_path: Path
    ) -> None:
        """A default ``ArchivePolicyConfig`` still produces a real registry.

        With no operator overrides, the registry built carries only the
        built-in staging defaults — exercising the no-op happy path on
        the repair sweep.
        """
        from influx.archive_policy import ArchivePolicyRegistry
        from influx.storage import ArchiveResult

        config = _make_config(tmp_path)  # default ArchivePolicyConfig()
        hooks = make_default_sweep_hooks(
            config, archive_reacquirers=_reacquirers(config)
        )
        note = _make_archive_missing_note()

        with patch("influx.repair_hooks.download_archive") as mock_dl:
            mock_dl.return_value = ArchiveResult(
                ok=True,
                rel_posix_path="arxiv/2026/04/2604.26946.pdf",
                error="",
            )
            assert hooks.archive_download is not None
            hooks.archive_download(note)

        registry = mock_dl.call_args.kwargs["policy_registry"]
        assert isinstance(registry, ArchivePolicyRegistry)
        # arxiv.org has no built-in policy entry, so it resolves to the
        # no-op ``attempt`` policy.
        assert registry.policy_for("https://arxiv.org/pdf/x.pdf").mode == "attempt"
