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
from dataclasses import dataclass
from pathlib import Path

import trafilatura

from influx.config import AppConfig
from influx.enrich import tier3_extract as _tier3_extract
from influx.errors import ExtractionError, LCMAError, NetworkError
from influx.extraction.html import _clean_html_fragments, _strip_tags
from influx.extraction.pdf import extract_pdf
from influx.extraction.pipeline import extract_arxiv_text
from influx.notes import parse_archive_path, parse_note
from influx.repair import (
    ArchiveDownloadHook,
    ExtractionOutcome,
    ReExtractArchiveHook,
    ReExtractionResult,
    SweepHooks,
    TextExtractionHook,
    Tier2EnrichHook,
    Tier3ExtractHook,
)
from influx.schemas import Tier3Extraction
from influx.storage import download_archive

__all__ = ["DefaultSweepHooks", "make_default_sweep_hooks"]

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


# ── Archive download metadata recovery ──────────────────────────────

_ARXIV_ID_TAG_PREFIX = "arxiv-id:"
_SOURCE_TAG_PREFIX = "source:"
_NOTE_PATH_RE = re.compile(r"papers/(?P<source>[^/]+)/(?P<year>\d{4})/(?P<month>\d{2})")


def _find_tag(tags: list[str], prefix: str) -> str | None:
    """Return the suffix of the first tag starting with *prefix*, or None."""
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def _parse_year_month_from_note_path(note_path: str) -> tuple[int, int] | None:
    """Pull ``(year, month)`` from a Lithos note path like ``papers/arxiv/2026/04``."""
    m = _NOTE_PATH_RE.search(note_path)
    if not m:
        return None
    try:
        return int(m.group("year")), int(m.group("month"))
    except ValueError:
        return None


def _classify_download_kind(error: str) -> str:
    """Return the ``kind`` discriminator from an ``ArchiveResult.error`` string.

    ``download_archive`` packs ``"<kind>: <message>"`` for ``NetworkError``
    cases and ``"HTTP <code> for ..."`` / ``"write: ..."`` for the other
    failure paths.  We surface a stable ``stage`` value to the sweep so
    :func:`influx.repair.classify_failure` can decide counted vs transient.
    """
    if error.startswith("HTTP "):
        return "http"
    head, _, _rest = error.partition(":")
    head = head.strip()
    return head or "archive_failed"


def _resolve_arxiv_download_args(
    note: dict[str, object],
    config: AppConfig,
) -> dict[str, object]:
    """Build kwargs for :func:`download_archive` from an arxiv note's state.

    Raises :class:`ExtractionError` (stage ``"resolve"``) when the note is
    missing fields needed to retry — the sweep treats this as transient
    so an operator hand-fix lands the next pass.
    """
    raw_tags = note.get("tags", [])
    tags: list[str] = list(raw_tags) if isinstance(raw_tags, list) else []
    arxiv_id = _find_tag(tags, _ARXIV_ID_TAG_PREFIX)
    if not arxiv_id:
        raise ExtractionError(
            "Cannot retry archive download: no arxiv-id tag on note",
            stage="resolve",
            detail=f"note id={note.get('id', '?')}",
        )
    note_path = str(note.get("path", ""))
    ym = _parse_year_month_from_note_path(note_path)
    if ym is None:
        raise ExtractionError(
            "Cannot retry archive download: note path missing year/month",
            stage="resolve",
            detail=f"path={note_path!r}",
        )
    year, month = ym
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    return {
        "url": pdf_url,
        "archive_root": Path(config.storage.archive_dir),
        "source": "arxiv",
        "item_id": arxiv_id,
        "published_year": year,
        "published_month": month,
        "ext": ".pdf",
        "allow_private_ips": config.security.allow_private_ips,
        "max_download_bytes": config.storage.max_download_bytes,
        "timeout_seconds": config.storage.download_timeout_seconds,
        "expected_content_type": "pdf",
    }


# ── Hook factories ──────────────────────────────────────────────────


def _make_archive_download_hook(config: AppConfig) -> ArchiveDownloadHook:
    """Create the production ``archive_download`` hook (FR-REP-1).

    The hook re-runs :func:`influx.storage.download_archive` for a note
    tagged ``influx:archive-missing`` and returns the relative POSIX
    path on success.  On failure it raises :class:`ExtractionError` so
    the sweep's existing ``(ExtractionError, LithosError)`` branch
    bumps the per-note ``archive_attempts`` counter (only for
    counted-class kinds — currently ``"oversize"``) and flips
    ``influx:archive-terminal`` once the cap is reached.

    Currently scoped to ``source:arxiv`` notes.  Other sources raise an
    ``ExtractionError(stage="unsupported_source")`` which classifies as
    transient — the note re-enters the sweep next pass and is fixed
    automatically once a per-source resolver is added.
    """

    def hook(note: dict[str, object]) -> str:
        raw_tags = note.get("tags", [])
        tags: list[str] = list(raw_tags) if isinstance(raw_tags, list) else []
        source = _find_tag(tags, _SOURCE_TAG_PREFIX) or ""
        if source != "arxiv":
            raise ExtractionError(
                f"archive_download retry: source {source!r} not supported",
                stage="unsupported_source",
                detail=f"note id={note.get('id', '?')}",
            )

        kwargs = _resolve_arxiv_download_args(note, config)
        result = download_archive(**kwargs)  # type: ignore[arg-type]
        if result.ok and result.rel_posix_path:
            _log.info(
                "archive_download retry succeeded for %s path=%s",
                note.get("id", "?"),
                result.rel_posix_path,
            )
            return result.rel_posix_path

        # The sweep classifies counted vs transient via
        # ``influx.repair.classify_failure`` based on the ``stage``
        # attribute below; surface the discriminator from
        # ``ArchiveResult.error`` verbatim so e.g. ``"oversize"`` lines
        # up with ``influx.repair._COUNTED_STAGES`` and bumps the cap.
        stage = _classify_download_kind(result.error) or "archive_failed"
        raise ExtractionError(
            f"archive_download retry failed: {result.error}",
            url=str(kwargs.get("url", "")),
            stage=stage,
            detail=result.error,
        )

    return hook


def _make_text_extraction_hook(config: AppConfig) -> TextExtractionHook:
    """Create the production ``text_extraction`` hook (FR-REP-1 stage 2).

    The hook re-runs the source-specific extraction cascade for a note
    that carries no ``text:*`` tag and returns the resulting tag.  On
    cascade fall-through (both HTML and PDF failed) the underlying
    helper raises :class:`ExtractionError`; we surface it to the sweep
    so the per-stage failure logging path fires.

    Currently scoped to ``source:arxiv`` — RSS support follows when
    the multi-source resolver lands (out of scope for issue #24).
    """

    def hook(note: dict[str, object]) -> str:
        raw_tags = note.get("tags", [])
        tags: list[str] = list(raw_tags) if isinstance(raw_tags, list) else []
        source = _find_tag(tags, _SOURCE_TAG_PREFIX) or ""
        if source != "arxiv":
            raise ExtractionError(
                f"text_extraction retry: source {source!r} not supported",
                stage="unsupported_source",
                detail=f"note id={note.get('id', '?')}",
            )

        arxiv_id = _find_tag(tags, _ARXIV_ID_TAG_PREFIX)
        if not arxiv_id:
            raise ExtractionError(
                "Cannot retry text extraction: no arxiv-id tag on note",
                stage="resolve",
                detail=f"note id={note.get('id', '?')}",
            )

        try:
            result = extract_arxiv_text(arxiv_id, config)
        except NetworkError as exc:
            # extract_arxiv_text raises ExtractionError on full cascade
            # fall-through, but a NetworkError can leak from helpers it
            # calls (e.g. SSRF guards on the PDF fetch).  Re-wrap so the
            # sweep's ``(ExtractionError, ...)`` branch handles it.
            raise ExtractionError(
                f"text_extraction retry network failure: {exc.kind}",
                url=getattr(exc, "url", "") or "",
                stage=exc.kind or "network",
                detail=str(exc),
            ) from exc
        _log.info(
            "text_extraction retry succeeded for %s tag=%s",
            note.get("id", "?"),
            result.source_tag,
        )
        return result.source_tag

    return hook


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


@dataclass(frozen=True, slots=True)
class DefaultSweepHooks:
    """Production-default sweep hook wiring with non-optional callables.

    Holds the five production hooks with non-``Optional`` types so
    pyright can statically know they are wired, while the parent
    :class:`~influx.repair.SweepHooks` keeps them ``| None`` to preserve
    the test-injection seam.

    Use :meth:`to_sweep_hooks` to obtain a ``SweepHooks`` instance for
    passing into :func:`influx.repair.sweep`.
    """

    archive_download: ArchiveDownloadHook
    re_extract_archive: ReExtractArchiveHook
    tier2_enrich: Tier2EnrichHook
    tier3_extract: Tier3ExtractHook
    text_extraction: TextExtractionHook

    def to_sweep_hooks(self) -> SweepHooks:
        """Return a :class:`SweepHooks` carrying these production hooks."""
        return SweepHooks(
            archive_download=self.archive_download,
            re_extract_archive=self.re_extract_archive,
            tier2_enrich=self.tier2_enrich,
            tier3_extract=self.tier3_extract,
            text_extraction=self.text_extraction,
        )


def make_default_sweep_hooks(config: AppConfig) -> DefaultSweepHooks:
    """Create production-default sweep hooks for the repair sweep.

    Each hook bridges the PRD 06 hook signature to the lower-level
    fetch / extraction / enrichment helpers (FR-REP-1).

    Returns :class:`DefaultSweepHooks` (typed with non-optional
    callables) so callers and tests do not need to narrow ``Optional``
    attributes before invoking them.  Convert to a ``SweepHooks`` for
    the sweep entrypoint via :meth:`DefaultSweepHooks.to_sweep_hooks`.
    """
    return DefaultSweepHooks(
        archive_download=_make_archive_download_hook(config),
        re_extract_archive=_make_re_extract_archive_hook(config),
        tier2_enrich=_make_tier2_enrich_hook(config),
        tier3_extract=_make_tier3_extract_hook(config),
        text_extraction=_make_text_extraction_hook(config),
    )
