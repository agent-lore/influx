"""Production-default repair hooks (PRD 07 US-016).

Bridges the PRD 06 hook signatures (``ReExtractArchiveHook``,
``Tier2EnrichHook``, ``Tier3ExtractHook``) to the lower-level
extraction and enrichment helpers from PRD 07 (``extraction.html``,
``extraction.pdf``, ``enrich.tier3_extract``).

The ``SweepHooks`` test-injection seam is preserved unchanged: these
defaults are only wired in when ``sweep()`` is called without explicit
hooks.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import trafilatura

from influx.config import AppConfig
from influx.enrich import tier3_extract as _tier3_extract
from influx.errors import ExtractionError, LCMAError
from influx.extraction.html import _clean_html_fragments, _strip_tags
from influx.extraction.pdf import extract_pdf
from influx.notes import parse_archive_path, parse_note
from influx.repair import (
    ExtractionOutcome,
    ReExtractArchiveHook,
    ReExtractionResult,
    SweepHooks,
    Tier2EnrichHook,
    Tier3ExtractHook,
)
from influx.schemas import Tier3Extraction

__all__ = ["make_default_sweep_hooks"]

_log = logging.getLogger(__name__)

# ── Content manipulation helpers ────────────────────────────────────

_PROFILE_RELEVANCE_RE = re.compile(r"^## Profile Relevance\b", re.MULTILINE)
_USER_NOTES_RE = re.compile(r"^## User Notes\b", re.MULTILINE)
_FULL_TEXT_HEADING_RE = re.compile(r"^## Full Text[ \t]*$", re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^## ", re.MULTILINE)
_TITLE_RE = re.compile(r"^# ([^\r\n]+)", re.MULTILINE)


def _find_insertion_point(content: str) -> int:
    """Find the position to insert new sections before Profile Relevance.

    Falls back to before ``## User Notes`` or end-of-string.
    """
    m = _PROFILE_RELEVANCE_RE.search(content)
    if m:
        return m.start()
    m = _USER_NOTES_RE.search(content)
    if m:
        return m.start()
    return len(content)


def _insert_full_text_section(content: str, full_text: str) -> str:
    """Insert ``## Full Text`` section at the canonical position."""
    pos = _find_insertion_point(content)
    section = f"\n## Full Text\n{full_text}\n"
    return content[:pos] + section + "\n" + content[pos:]


def _render_tier3_sections(tier3: Tier3Extraction) -> str:
    """Render the four Tier 3 sections as markdown."""
    parts: list[str] = []

    parts.append("## Claims")
    for claim in tier3.claims:
        parts.append(f"- {claim}")

    parts.append("\n## Datasets & Benchmarks")
    for ds in tier3.datasets:
        parts.append(f"- {ds}")

    parts.append("\n## Builds On")
    for item in tier3.builds_on:
        parts.append(f"- {item}")

    parts.append("\n## Open Questions")
    for q in tier3.open_questions:
        parts.append(f"- {q}")

    return "\n".join(parts) + "\n"


def _insert_tier3_sections(content: str, tier3: Tier3Extraction) -> str:
    """Insert Tier 3 sections at the canonical position."""
    pos = _find_insertion_point(content)
    section_text = "\n" + _render_tier3_sections(tier3)
    return content[:pos] + section_text + "\n" + content[pos:]


def _extract_full_text_body(content: str) -> str:
    """Extract the ``## Full Text`` section body from note content."""
    start_match = _FULL_TEXT_HEADING_RE.search(content)
    if not start_match:
        return ""
    body_start = start_match.end()
    if body_start < len(content) and content[body_start] == "\n":
        body_start += 1
    next_match = _NEXT_H2_RE.search(content, body_start)
    if next_match:
        return content[body_start : next_match.start()].rstrip()
    return content[body_start:].rstrip()


def _extract_title(content: str) -> str:
    """Extract the ``# Title`` from note content."""
    m = _TITLE_RE.search(content)
    return m.group(1) if m else ""


# ── Archive file reading ────────────────────────────────────────────


def _read_archive_file(config: AppConfig, archive_path: str) -> bytes:
    """Read the stored archive file.

    Raises ``ExtractionError`` if the file cannot be read.
    """
    full_path = Path(config.storage.archive_dir) / archive_path
    try:
        return full_path.read_bytes()
    except OSError as exc:
        raise ExtractionError(
            f"Cannot read archive file: {full_path}",
            url=str(full_path),
            stage="archive_read",
            detail=str(exc),
        ) from exc


def _extract_from_archive(
    file_bytes: bytes,
    archive_path: str,
    config: AppConfig,
) -> tuple[str, str]:
    """Extract text from archived file.

    Returns ``(text, source_tag)`` where source_tag is
    ``"text:html"`` or ``"text:pdf"``.

    Raises ``ExtractionError`` on failure.
    """
    suffix = Path(archive_path).suffix.lower()
    extraction_cfg = config.extraction

    if suffix == ".pdf":
        result = extract_pdf(file_bytes, source_url=archive_path)
        return result.text, "text:pdf"

    # Default to HTML extraction for non-PDF archives.
    html_body = file_bytes.decode("utf-8", errors="replace")
    html_body = _strip_tags(html_body, extraction_cfg.strip_tags)
    extracted = trafilatura.extract(html_body, favor_recall=True)

    if extracted is None:
        raise ExtractionError(
            "trafilatura returned no content from archive",
            url=archive_path,
            stage="extract",
            detail="trafilatura.extract() returned None",
        )

    extracted = _clean_html_fragments(extracted)

    if len(extracted) < extraction_cfg.min_html_chars:
        raise ExtractionError(
            f"Archived HTML too short "
            f"({len(extracted)} < {extraction_cfg.min_html_chars})",
            url=archive_path,
            stage="min_length",
            detail=f"Got {len(extracted)} chars, need {extraction_cfg.min_html_chars}",
        )

    return extracted, "text:html"


# ── Hook factories ──────────────────────────────────────────────────


def _make_re_extract_archive_hook(
    config: AppConfig,
) -> ReExtractArchiveHook:
    """Create the production ``re_extract_archive`` hook.

    Reads the stored archive artifact and attempts extraction.
    Returns UPGRADE on success, TERMINAL when the stored content
    is not extractable, TRANSIENT on file-read failure.
    """

    def hook(
        note: dict[str, object],
        archive_path: str,
    ) -> ReExtractionResult:
        try:
            file_bytes = _read_archive_file(config, archive_path)
        except ExtractionError:
            # Can't read the file — transient failure.
            _log.info("re_extract_archive: cannot read archive %s", archive_path)
            return ReExtractionResult(outcome=ExtractionOutcome.TRANSIENT)

        try:
            _text, source_tag = _extract_from_archive(file_bytes, archive_path, config)
            return ReExtractionResult(
                outcome=ExtractionOutcome.UPGRADE,
                upgraded_text_tag=source_tag,
            )
        except ExtractionError:
            # Content is not extractable from this archive — terminal.
            _log.info("re_extract_archive: extraction failed for %s", archive_path)
            return ReExtractionResult(outcome=ExtractionOutcome.TERMINAL)

    return hook


def _make_tier2_enrich_hook(config: AppConfig) -> Tier2EnrichHook:
    """Create the production ``tier2_enrich`` hook.

    Reads the stored archive, extracts text, inserts ``## Full Text``
    into the note content, and adds the ``full-text`` tag.
    """

    def hook(note: dict[str, object]) -> None:
        content: str = str(note.get("content", ""))
        raw_tags = note.get("tags", [])
        tags: list[str] = list(raw_tags) if isinstance(raw_tags, list) else []

        # Find archive path from note content.
        try:
            parsed = parse_note(content)
            archive_path = parse_archive_path(parsed)
        except Exception as exc:
            raise ExtractionError(
                "Cannot parse archive path from note",
                stage="parse",
                detail=str(exc),
            ) from exc

        if archive_path is None:
            raise ExtractionError(
                "No archive path found in note",
                stage="parse",
                detail="## Archive section missing or empty",
            )

        # Read and extract from archive.
        file_bytes = _read_archive_file(config, archive_path)
        extracted_text, _source_tag = _extract_from_archive(
            file_bytes, archive_path, config
        )

        # Insert ## Full Text section into content.
        note["content"] = _insert_full_text_section(content, extracted_text)

        # Add full-text tag.
        if "full-text" not in tags:
            tags.append("full-text")
            note["tags"] = tags

    return hook


def _make_tier3_extract_hook(config: AppConfig) -> Tier3ExtractHook:
    """Create the production ``tier3_extract`` hook.

    Reads the ``## Full Text`` body from the note, calls
    ``enrich.tier3_extract``, inserts the four Tier 3 sections,
    and adds the ``influx:deep-extracted`` tag.
    """

    def hook(note: dict[str, object]) -> None:
        content: str = str(note.get("content", ""))
        raw_tags = note.get("tags", [])
        tags: list[str] = list(raw_tags) if isinstance(raw_tags, list) else []

        # Extract full text from note content.
        full_text = _extract_full_text_body(content)
        if not full_text:
            raise ExtractionError(
                "No ## Full Text section found in note",
                stage="parse",
                detail="Cannot run Tier 3 extraction without full text",
            )

        title = _extract_title(content)

        # Call the Tier 3 extraction model.
        try:
            tier3_result = _tier3_extract(
                title=title,
                full_text=full_text,
                config=config,
            )
        except LCMAError:
            raise  # Propagate — the sweep treats LCMAError as stage failure.

        # Insert Tier 3 sections into content.
        note["content"] = _insert_tier3_sections(content, tier3_result)

        # Add influx:deep-extracted tag.
        if "influx:deep-extracted" not in tags:
            tags.append("influx:deep-extracted")
            note["tags"] = tags

    return hook


# ── Public factory ──────────────────────────────────────────────────


def make_default_sweep_hooks(config: AppConfig) -> SweepHooks:
    """Create production-default ``SweepHooks`` for the repair sweep.

    Each hook bridges the PRD 06 hook signature to the lower-level
    extraction and enrichment helpers from PRD 07.  The
    ``archive_download`` hook is left ``None`` (PRD 04 responsibility).
    """
    return SweepHooks(
        re_extract_archive=_make_re_extract_archive_hook(config),
        tier2_enrich=_make_tier2_enrich_hook(config),
        tier3_extract=_make_tier3_extract_hook(config),
    )
