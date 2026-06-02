"""Unit tests for local-PDF acquisition (Inbox v2, docs/plans/inbox.md §16).

Covers :func:`influx.sources.inbox.acquire_inbox_pdf`: SHA-256 content
identity, the synthetic ``inbox-pdf:sha256:<hex>`` source URL, the archive
copy under ``inbox-pdf/YYYY/MM/<sha>.pdf``, extraction via ``extract_pdf``,
and the summary-hint fallback when extraction fails.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

from influx.config import (
    AppConfig,
    ExtractionConfig,
    LithosConfig,
    ProfileConfig,
    ProfileThresholds,
    PromptEntryConfig,
    PromptsConfig,
    SecurityConfig,
    StorageConfig,
)
from influx.errors import ExtractionError
from influx.lithos_client import _extract_slug_suffix
from influx.sources.inbox import acquire_inbox_pdf

_PDF_BYTES = b"%PDF-1.4 a sufficiently long local pdf body for extraction tests"
_LONG_BODY = (
    "This is a substantial extracted PDF body, long enough to clear the "
    "default thin-summary thresholds without any trouble at all whatsoever."
)


def _make_config(archive_dir: str) -> AppConfig:
    return AppConfig(
        lithos=LithosConfig(url="http://localhost:0/sse"),
        storage=StorageConfig(archive_dir=archive_dir),
        profiles=[
            ProfileConfig(
                name="ai-robotics",
                description="AI and robotics research",
                thresholds=ProfileThresholds(relevance=7),
            )
        ],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="x"),
            tier1_enrich=PromptEntryConfig(text="x"),
            tier3_extract=PromptEntryConfig(text="x"),
        ),
        security=SecurityConfig(allow_private_ips=True),
        extraction=ExtractionConfig(min_summary_chars=80),
    )


class _Extraction:
    def __init__(self, text: str) -> None:
        self.text = text


def _write_pdf(tmp_path: Path, name: str = "paper.pdf") -> Path:
    p = tmp_path / name
    p.write_bytes(_PDF_BYTES)
    return p


def test_acquire_pdf_sha256_identity_and_synthetic_url(tmp_path: Path) -> None:
    config = _make_config(str(tmp_path / "archive"))
    pdf = _write_pdf(tmp_path)
    sha = hashlib.sha256(_PDF_BYTES).hexdigest()

    with patch(
        "influx.sources.inbox.extract_pdf", return_value=_Extraction(_LONG_BODY)
    ) as mock_pdf:
        acquired = acquire_inbox_pdf(pdf, config=config)

    assert acquired.source_url == f"inbox-pdf:sha256:{sha}"
    assert acquired.url_hash == sha
    assert acquired.text_flavour == "pdf"
    assert acquired.extracted_text == _LONG_BODY
    # extract_pdf received the in-memory bytes (no disk re-read).
    assert mock_pdf.call_args.args[0] == _PDF_BYTES


def test_acquire_pdf_archives_under_inbox_pdf_subtree(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    config = _make_config(str(archive))
    pdf = _write_pdf(tmp_path)
    sha = hashlib.sha256(_PDF_BYTES).hexdigest()

    with patch(
        "influx.sources.inbox.extract_pdf", return_value=_Extraction(_LONG_BODY)
    ):
        acquired = acquire_inbox_pdf(pdf, config=config)

    assert acquired.archive_path == f"inbox-pdf/{_year_month()}/{sha}.pdf"
    assert acquired.archive_missing is False
    assert acquired.archive_path is not None
    copied = archive / acquired.archive_path
    assert copied.read_bytes() == _PDF_BYTES


def test_acquire_pdf_same_bytes_two_names_one_identity(tmp_path: Path) -> None:
    """Content-hash identity: the same bytes under two filenames are one note."""
    config = _make_config(str(tmp_path / "archive"))
    a = _write_pdf(tmp_path, "first.pdf")
    b = _write_pdf(tmp_path, "second.pdf")

    with patch(
        "influx.sources.inbox.extract_pdf", return_value=_Extraction(_LONG_BODY)
    ):
        acq_a = acquire_inbox_pdf(a, config=config)
        acq_b = acquire_inbox_pdf(b, config=config)

    assert acq_a.source_url == acq_b.source_url
    assert acq_a.archive_path == acq_b.archive_path


def test_acquire_pdf_falls_back_to_summary_hint_on_extraction_failure(
    tmp_path: Path,
) -> None:
    config = _make_config(str(tmp_path / "archive"))
    pdf = _write_pdf(tmp_path)

    with patch(
        "influx.sources.inbox.extract_pdf",
        side_effect=ExtractionError("boom", url="x", stage="read", detail="d"),
    ):
        acquired = acquire_inbox_pdf(pdf, config=config, summary_hint="a hint")

    assert acquired.extracted_text is None
    assert acquired.summary == "a hint"
    assert acquired.text_flavour == "summary-fallback"
    # The archive copy still succeeded — bytes were read before extraction.
    assert acquired.archive_missing is False


def test_synthetic_url_slug_suffix() -> None:
    """A hostless inbox-pdf URL gets the [inbox-pdf] suffix, not an empty []."""
    assert _extract_slug_suffix("inbox-pdf:sha256:deadbeef") == " [inbox-pdf]"


def _year_month() -> str:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return f"{now.year}/{now.month:02d}"
