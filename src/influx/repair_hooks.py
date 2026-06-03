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

from influx.archive_policy import (
    registry_from_config as _archive_policy_registry_from_config,
)
from influx.config import AppConfig
from influx.enrich import tier3_extract as _tier3_extract
from influx.errors import ExtractionError, LCMAError, NetworkError
from influx.extraction.article import extract_article
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
from influx.urls import url_hash

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
_FEED_SLUG_TAG_PREFIX = "feed-slug:"
# Modern arxiv ids encode the publication YYMM in their first four digits
# (e.g. ``2605.10178`` → 2026-05), matching acquisition's published_year/month.
_ARXIV_ID_YYMM_RE = re.compile(r"^(?P<yy>\d{2})(?P<mm>\d{2})\.")
# Leading ``YYYY-MM`` of an ISO-8601 ``created_at`` timestamp.
_ISO_YEAR_MONTH_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})")
_NOTE_PATH_RE = re.compile(
    r"(?:papers|articles)/(?P<source>[^/]+)/(?P<year>\d{4})/(?P<month>\d{2})"
)
_NOTE_PATH_SOURCE_RE = re.compile(r"(?:^|/)(?:papers|articles)/(?P<source>[^/]+)/")
_RSS_NOTE_ID_PREFIX = "rss-"
_ARXIV_NOTE_ID_PREFIX = "arxiv-"
_ARXIV_HOSTNAMES: frozenset[str] = frozenset({"arxiv.org", "www.arxiv.org"})


def _find_tag(tags: list[str], prefix: str) -> str | None:
    """Return the suffix of the first tag starting with *prefix*, or None."""
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def _note_tags(note: dict[str, object]) -> list[str]:
    """Read and cast the note's tag list defensively (Lithos returns ``Any``)."""
    raw = note.get("tags", [])
    return list(raw) if isinstance(raw, list) else []


def _note_source_tag(note: dict[str, object]) -> str:
    """Return the note's ``source:*`` tag suffix or an empty string."""
    return _find_tag(_note_tags(note), _SOURCE_TAG_PREFIX) or ""


# Public alias: cross-module callers (e.g. ``influx.audit_invalid_source``,
# #162) should import :func:`note_source_tag` rather than the leading-
# underscore variant so a future refactor inside this module does not
# break them.  The bare function above is retained because it is
# referenced from many internal helpers in this module and renaming
# them all is needless churn.
note_source_tag = _note_source_tag


def _is_rss_source(source: str) -> bool:
    """Return whether *source* names an RSS feed source tag.

    Production RSS notes carry ``source:rss-<feed-slug>``; the bare
    ``source:rss`` sentinel is accepted to keep the dispatcher robust
    to historical or hand-edited notes.
    """
    return source == "rss" or source.startswith(_RSS_NOTE_ID_PREFIX)


# ── Source metadata invariant (#150) ────────────────────────────────


def _infer_source_from_url(source_url: str) -> str | None:
    """Infer a ``source:*`` suffix from an arxiv URL.

    Returns ``"arxiv"`` for any URL whose host is in
    :data:`_ARXIV_HOSTNAMES`.  Returns ``None`` for everything else —
    RSS feeds carry a ``feed-slug:`` derived value that can't be
    reconstructed from the article URL alone.
    """
    if not source_url:
        return None
    try:
        from urllib.parse import urlparse

        host = urlparse(source_url).hostname or ""
    except ValueError:
        return None
    if host.lower() in _ARXIV_HOSTNAMES:
        return "arxiv"
    return None


def _infer_source_from_note_path(note_path: str) -> str | None:
    """Infer a ``source:*`` suffix from a Lithos note path.

    ``papers/arxiv/...`` → ``"arxiv"``; ``articles/<feed-slug>/...``
    → ``"rss-<feed-slug>"`` (RSS notes live under
    ``articles/<source_tag>/{YYYY}/{MM}`` per
    :mod:`influx.sources.rss`, so the path's first segment after
    ``articles/`` is already the full source-tag suffix).  Returns
    ``None`` when the path doesn't match either canonical layout.
    """
    if not note_path:
        return None
    m = _NOTE_PATH_SOURCE_RE.search(note_path)
    if not m:
        return None
    candidate = m.group("source")
    if "/" in candidate:  # defensive — shouldn't trigger with the regex above
        return None
    # ``papers/`` carries arxiv exclusively; ``articles/`` carries RSS.
    if note_path.lstrip("/").startswith("papers/") and candidate == "arxiv":
        return "arxiv"
    if note_path.lstrip("/").startswith("articles/"):
        # The configured RSS source_tag may or may not already include
        # the ``rss-`` prefix; the path stores it verbatim.  Normalise to
        # the canonical ``rss-<slug>`` shape if missing.
        if candidate.startswith(_RSS_NOTE_ID_PREFIX) or candidate == "rss":
            return candidate
        return f"{_RSS_NOTE_ID_PREFIX}{candidate}"
    return None


def _infer_source_from_note_id(note_id: str) -> str | None:
    """Infer a ``source:*`` suffix from a Lithos note ``id``.

    Production note ids carry an explicit source-prefix: ``arxiv-<id>``
    for arxiv, ``rss-<feed-slug>-<hash>`` for RSS.  The RSS case
    returns the bare ``"rss"`` sentinel because the feed-slug-hash
    suffix is not safely separable here (it can contain hyphens of its
    own); the dispatcher accepts the bare sentinel via
    :func:`_is_rss_source` so this is still actionable.
    """
    if not note_id:
        return None
    if note_id.startswith(_ARXIV_NOTE_ID_PREFIX):
        return "arxiv"
    if note_id.startswith(_RSS_NOTE_ID_PREFIX):
        return "rss"
    return None


def _resolve_source_from_tag_or_url(
    *, source_tag_suffix: str | None, source_url: str | None
) -> str | None:
    """Resolve a dispatchable ``source:*`` suffix from the signals that
    exist at *ingest* time: an explicit source-tag suffix, then an
    arxiv-inferable URL.

    Single source of truth shared by :func:`infer_note_source` (its tag
    and top-level-``source_url`` rules) and :func:`has_usable_source`,
    so the ingest-time guard and the repair sweep agree by construction
    on what counts as a usable source (#189).  A non-empty
    *source_tag_suffix* wins verbatim — even one naming a source we
    don't support yet is well-formed metadata, not our call to
    second-guess — otherwise fall back to the arxiv-host URL inference.
    Returns ``None`` when neither signal yields a source.
    """
    if source_tag_suffix:
        return source_tag_suffix
    return _infer_source_from_url(source_url or "")


def has_usable_source(*, source_tag_suffix: str | None, source_url: str | None) -> bool:
    """Whether a note has a usable, dispatchable source at ingest time (#189).

    ``True`` iff a non-empty ``source:*`` suffix is present, or the
    source URL is one a suffix can be inferred from (an arxiv host).
    This is the boolean form of :func:`_resolve_source_from_tag_or_url`
    and mirrors the terminal (returns-``None``) branch of
    :func:`infer_note_source`, restricted to the signals that exist
    *before* a note is written — the note ``path`` / ``id`` fallbacks
    only exist post-write, so they are repair-only.

    The source builders use this to **count, never drop** notes written
    with no usable source: exactly the population a future #187-class
    metadata loss would later strip into an ``influx:source-invalid``
    zombie.  Observe-only by design (#189) — a blank ``source:`` tag on
    an item with real content and a working non-arxiv link is still
    legitimate content we must not discard.
    """
    return (
        _resolve_source_from_tag_or_url(
            source_tag_suffix=source_tag_suffix, source_url=source_url
        )
        is not None
    )


def infer_note_source(note: dict[str, object]) -> str | None:
    """Return a dispatchable ``source:*`` suffix for *note*, or ``None``.

    Resolution order, stopping at the first hit (rules 1–2 share the
    :func:`_resolve_source_from_tag_or_url` primitive with
    :func:`has_usable_source` so ingest and repair never drift, #189):

    1. Any **non-empty** existing ``source:*`` tag is honoured
       verbatim — even if it names a source we don't support yet
       (e.g. ``hackernews``).  That case is "unsupported but
       well-formed metadata" and is handled by the existing
       :func:`_raise_unsupported_source` path; we don't second-guess
       an explicit operator/source label here.
    2. (When the existing tag is empty/absent.)  The top-level
       ``source_url`` field on the note dict pointing at arxiv.
       This is the canonical persisted shape (see
       ``influx.repair._rewrite_note_via_lithos`` and the read-note
       coverage in ``tests/unit/test_repair_sweep.py``); it survives
       even when the note body / frontmatter has been corrupted or
       stripped, so we prefer it over the parsed frontmatter copy.
    3. A ``source_url`` recovered from the note's YAML frontmatter
       pointing at arxiv — the fallback when the top-level field is
       absent (e.g. legacy notes, hand-edited dicts in tests).
    4. The Lithos note ``path`` (``papers/<source>/...`` or
       ``articles/<feed-slug>/...``).
    5. The Lithos note ``id`` prefix (``arxiv-`` / ``rss-``).

    Returns ``None`` only when the source tag is empty AND every
    inference signal is missing/unrecognised — the caller treats
    that as a terminal metadata failure (#150) rather than a
    transient extraction failure.
    """
    # Rules 1–2: existing non-empty tag, then top-level ``source_url``
    # pointing at arxiv.  Shared with :func:`has_usable_source` via the
    # resolver so the ingest guard and the sweep stay in lock-step (#189).
    existing = _note_source_tag(note)
    top_level_url = note.get("source_url")
    top_level_url_str = top_level_url if isinstance(top_level_url, str) else ""
    resolved = _resolve_source_from_tag_or_url(
        source_tag_suffix=existing, source_url=top_level_url_str
    )
    if resolved is not None:
        return resolved

    source_url = _note_source_url(note) or ""
    inferred = _infer_source_from_url(source_url)
    if inferred is not None:
        return inferred

    inferred = _infer_source_from_note_path(str(note.get("path", "")))
    if inferred is not None:
        return inferred

    inferred = _infer_source_from_note_id(str(note.get("id", "")))
    if inferred is not None:
        return inferred

    return None


def _backfill_source_tag(note: dict[str, object], inferred: str) -> None:
    """Replace any (possibly empty/garbled) ``source:*`` tag on *note*.

    Mutates ``note["tags"]`` in place so the rewrite step persists the
    repaired tag.  Idempotent — calling twice with the same value is a
    no-op.  Used after :func:`infer_note_source` returns a non-``None``
    suffix so the next sweep pass never re-enters the inference path
    for the same note.
    """
    tags = _note_tags(note)
    rebuilt = [t for t in tags if not t.startswith(_SOURCE_TAG_PREFIX)]
    rebuilt.append(f"{_SOURCE_TAG_PREFIX}{inferred}")
    note["tags"] = rebuilt


def _raise_unsupported_source(
    note: dict[str, object], *, stage_label: str, source: str
) -> None:
    """Raise ``ExtractionError(stage="unsupported_source")`` for an unknown source.

    The text-extraction path flips this to terminal via
    :func:`influx.repair._terminate_unsupported_text_source`; the
    archive-download path leaves it transient (the note re-enters the
    sweep next pass and is repaired automatically once a per-source
    resolver is added).
    """
    raise ExtractionError(
        f"{stage_label}: source {source!r} not supported",
        stage="unsupported_source",
        detail=f"note id={note.get('id', '?')}",
    )


def _raise_invalid_source_metadata(
    note: dict[str, object], *, stage_label: str
) -> None:
    """Raise ``ExtractionError(stage="invalid_source_metadata")`` (#150).

    Distinct from ``unsupported_source`` (a legitimate future source
    without a resolver yet): this signals a note whose ``source:*``
    tag is empty/garbled AND whose URL/path/id provide no fallback,
    so the same retry will loop forever emitting
    ``source '' not supported``.  The text-extraction path flips this
    to terminal via
    :func:`influx.repair._terminate_invalid_source_metadata`, adding
    a discoverable ``influx:source-invalid`` tag for later cleanup.
    """
    raise ExtractionError(
        (
            f"{stage_label}: note has no recoverable source metadata "
            "(empty/garbled source tag and no arxiv URL / canonical path / id prefix)"
        ),
        stage="invalid_source_metadata",
        detail=(
            f"note id={note.get('id', '?')} "
            f"path={note.get('path', '?')!r} "
            f"existing_source={_note_source_tag(note)!r}"
        ),
    )


def _parse_year_month_from_note_path(note_path: str) -> tuple[int, int] | None:
    """Pull ``(year, month)`` from a Lithos note path like ``papers/arxiv/2026/04``."""
    m = _NOTE_PATH_RE.search(note_path)
    if not m:
        return None
    try:
        return int(m.group("year")), int(m.group("month"))
    except ValueError:
        return None


def _year_month_from_arxiv_id(arxiv_id: str) -> tuple[int, int] | None:
    """Derive ``(year, month)`` from a modern arxiv id's ``YYMM`` prefix."""
    m = _ARXIV_ID_YYMM_RE.match(arxiv_id)
    if not m:
        return None
    month = int(m.group("mm"))
    if not 1 <= month <= 12:
        return None
    return 2000 + int(m.group("yy")), month


def _year_month_from_iso(timestamp: str) -> tuple[int, int] | None:
    """Derive ``(year, month)`` from the leading ``YYYY-MM`` of an ISO timestamp."""
    m = _ISO_YEAR_MONTH_RE.match(timestamp)
    if not m:
        return None
    month = int(m.group("month"))
    if not 1 <= month <= 12:
        return None
    return int(m.group("year")), month


def _year_month_from_note(
    note: dict[str, object], *, arxiv_id: str | None = None
) -> tuple[int, int] | None:
    """Resolve the archive ``(year, month)`` bucket for a note.

    ``read_note`` does not preserve the Influx note ``path``
    (``papers|articles/<source>/YYYY/MM``), so the path-based parse fails
    for every sweep candidate. Fall back to the publication date encoded in
    the arxiv id (``YYMM``), then to the note's ``created_at``. The retry
    archive path may differ from the original bucket — that is acceptable
    (see :func:`_rss_item_id_from_note`); it only needs to be deterministic.
    """
    ym = _parse_year_month_from_note_path(str(note.get("path") or ""))
    if ym is not None:
        return ym
    if arxiv_id:
        ym = _year_month_from_arxiv_id(arxiv_id)
        if ym is not None:
            return ym
    created_at = note.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        meta = note.get("metadata")
        if isinstance(meta, dict):
            meta_created = meta.get("created_at")
            created_at = meta_created if isinstance(meta_created, str) else ""
        else:
            created_at = ""
    return _year_month_from_iso(created_at) if created_at else None


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


def _note_source_url(note: dict[str, object]) -> str | None:
    """Return the note's doc-level ``source_url`` (top-level or ``metadata``).

    The repair sweep operates on the ``lithos_read`` envelope, where
    ``source_url`` is a structured doc-level field — hoisted to the top
    level by the #187 read-envelope normalisation, with ``metadata`` as
    the fallback location.  Mirrors
    :func:`influx.lithos_client._doc_source_url`.

    This deliberately does NOT parse a ``source_url:`` line out of the
    content body.  That was the pre-fix legacy shape (empty doc-level
    metadata, populated content-body frontmatter); those notes have been
    cleaned up and the writer no longer produces them, so reading the
    content body would only reintroduce a dependency on a removed shape.
    Returns ``None`` when no doc-level source_url is present — callers
    treat that as a transient "needs operator hand-fix" signal.
    """
    direct = note.get("source_url")
    if isinstance(direct, str) and direct:
        return direct
    meta = note.get("metadata")
    if isinstance(meta, dict):
        nested = meta.get("source_url")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _rss_item_id_from_note(note: dict[str, object]) -> str | None:
    """Recover an archive ``item_id`` from an RSS note's ``id`` field.

    RSS notes are written with ``id = "rss-<feed-slug>-<url-hash>"``
    (see ``influx.sources.rss.build_rss_note_item``).  The leading
    ``rss-`` prefix is dropped to mirror arxiv's
    ``id = "arxiv-<arxiv-id>" -> item_id = "<arxiv-id>"`` convention.

    The retry archive path will differ from what initial acquisition
    would have produced (the original embedded a ``YYYY-MM-DD``
    component that is not recoverable from the persisted note); this is
    fine because acquisition failed and no archive currently lives on
    disk for the original path.

    ``read_note`` (the repair sweep's only note source) returns a Lithos
    UUID as ``id``, not the Influx ``rss-<feed-slug>-<url-hash>`` form, so
    the prefix strip yields nothing. In that case reconstruct the exact
    same ``{feed-slug}-{url-hash}`` item_id from the ``feed-slug:`` tag and
    ``source_url`` — acquisition builds the note id as
    ``rss-{feed_slug}-{url_hash(url)}`` (``influx.sources.rss``), and
    ``url_hash`` normalises internally, so this matches byte-for-byte.
    """
    note_id = str(note.get("id", ""))
    if note_id.startswith(_RSS_NOTE_ID_PREFIX):
        item_id = note_id[len(_RSS_NOTE_ID_PREFIX) :]
        if item_id:
            return item_id
    feed_slug = _find_tag(_note_tags(note), _FEED_SLUG_TAG_PREFIX)
    source_url = _note_source_url(note)
    if feed_slug and source_url:
        return f"{feed_slug}-{url_hash(source_url)}"
    return None


def _resolve_rss_download_args(
    note: dict[str, object],
    config: AppConfig,
) -> dict[str, object]:
    """Build kwargs for :func:`download_archive` from an RSS note's state.

    Raises :class:`ExtractionError` (stage ``"resolve"``) when the note
    is missing fields needed to retry — the sweep treats this as
    transient so an operator hand-fix lands the next pass.
    """
    source = _note_source_tag(note)
    source_url = _note_source_url(note)
    if not source_url:
        raise ExtractionError(
            "Cannot retry archive download: no source_url in frontmatter",
            stage="resolve",
            detail=f"note id={note.get('id', '?')}",
        )
    item_id = _rss_item_id_from_note(note)
    if not item_id:
        raise ExtractionError(
            "Cannot retry archive download: cannot recover RSS item_id "
            "(no 'rss-' id, and no feed-slug tag + source_url to reconstruct)",
            stage="resolve",
            detail=f"note id={note.get('id', '?')}",
        )
    ym = _year_month_from_note(note)
    if ym is None:
        raise ExtractionError(
            "Cannot retry archive download: no year/month from path or created_at",
            stage="resolve",
            detail=f"note id={note.get('id', '?')}",
        )
    year, month = ym
    return {
        "url": source_url,
        "archive_root": Path(config.storage.archive_dir),
        "source": source,
        "item_id": item_id,
        "published_year": year,
        "published_month": month,
        "ext": ".html",
        "allow_private_ips": config.security.allow_private_ips,
        "max_download_bytes": config.storage.max_download_bytes,
        "timeout_seconds": config.storage.download_timeout_seconds,
        "expected_content_type": "html",
    }


def _resolve_archive_download_args(
    note: dict[str, object],
    config: AppConfig,
) -> dict[str, object]:
    """Dispatch :func:`download_archive` kwarg construction by note source.

    Currently supports ``arxiv`` and ``rss-*`` (plus the bare ``rss``
    sentinel).  Other sources raise ``unsupported_source`` so the sweep
    keeps existing transient-retry behavior until a per-source
    resolver lands.
    """
    source = _note_source_tag(note)
    if source == "arxiv":
        return _resolve_arxiv_download_args(note, config)
    if _is_rss_source(source):
        return _resolve_rss_download_args(note, config)
    _raise_unsupported_source(note, stage_label="archive_download retry", source=source)
    raise AssertionError("unreachable")  # pragma: no cover


def _resolve_arxiv_download_args(
    note: dict[str, object],
    config: AppConfig,
) -> dict[str, object]:
    """Build kwargs for :func:`download_archive` from an arxiv note's state.

    Raises :class:`ExtractionError` (stage ``"resolve"``) when the note is
    missing fields needed to retry — the sweep treats this as transient
    so an operator hand-fix lands the next pass.
    """
    arxiv_id = _find_tag(_note_tags(note), _ARXIV_ID_TAG_PREFIX)
    if not arxiv_id:
        raise ExtractionError(
            "Cannot retry archive download: no arxiv-id tag on note",
            stage="resolve",
            detail=f"note id={note.get('id', '?')}",
        )
    ym = _year_month_from_note(note, arxiv_id=arxiv_id)
    if ym is None:
        raise ExtractionError(
            "Cannot retry archive download: no year/month from path, "
            "arxiv id, or created_at",
            stage="resolve",
            detail=f"note id={note.get('id', '?')}",
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

    Supports ``source:arxiv`` and ``source:rss-*`` notes via
    :func:`_resolve_archive_download_args`.  Other sources raise an
    ``ExtractionError(stage="unsupported_source")`` which classifies as
    transient — the note re-enters the sweep next pass and is fixed
    automatically once a per-source resolver is added.

    Issue #149 follow-up: the per-domain archive policy registry is
    built once from ``config.storage.archive_policy`` and threaded into
    :func:`download_archive` so operator overrides (``blocked`` /
    ``rate_limited`` / ``skip`` and ``include_defaults = false``) apply
    identically during repair sweeps and initial acquisition.  Without
    this the repair path silently fell back to
    :func:`~influx.archive_policy.default_registry`, ignoring per-run
    config and re-attempting doomed domains.
    """
    policy_registry = _archive_policy_registry_from_config(
        config.storage.archive_policy
    )

    def hook(note: dict[str, object]) -> str:
        kwargs = _resolve_archive_download_args(note, config)
        result = download_archive(
            policy_registry=policy_registry,
            **kwargs,  # type: ignore[arg-type]
        )
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


def _run_arxiv_text_extraction(note: dict[str, object], config: AppConfig) -> str:
    """Run the arxiv text-extraction cascade and return the resulting tag.

    Returns ``"text:html"`` / ``"text:pdf"`` from the cascade's success
    paths.  On cascade fall-through (``ExtractionError`` from
    :func:`extract_arxiv_text`) or a ``NetworkError`` leaking from
    helpers it calls (e.g. SSRF / TLS / timeout on the PDF fetch),
    returns ``"text:abstract-only"`` per the
    :class:`~influx.repair.TextExtractionHook` protocol so the sweep
    stamps a ``text:*`` tag and the note converges out of the
    text-extraction stage.  The note can still be upgraded to
    ``text:html`` / ``text:pdf`` later via
    :func:`_make_re_extract_archive_hook` once
    :func:`_make_archive_download_hook` lands an archive on disk.  A
    WARN is emitted so operators can see the structural reason behind
    the convergence.

    Only ``ExtractionError(stage="resolve")`` is raised — that signals
    a missing ``arxiv-id:`` tag which only an operator fix can repair,
    and it should keep surfacing through the sweep's failure-logging
    path until corrected.
    """
    arxiv_id = _find_tag(_note_tags(note), _ARXIV_ID_TAG_PREFIX)
    if not arxiv_id:
        raise ExtractionError(
            "Cannot retry text extraction: no arxiv-id tag on note",
            stage="resolve",
            detail=f"note id={note.get('id', '?')}",
        )

    try:
        result = extract_arxiv_text(arxiv_id, config)
    except (ExtractionError, NetworkError) as exc:
        stage = getattr(exc, "stage", None) or getattr(exc, "kind", None) or "extract"
        _log.warning(
            "arxiv text_extraction retry: live extraction failed for %s "
            "(stage=%s arxiv_id=%s); converging to text:abstract-only",
            note.get("id", "?"),
            stage,
            arxiv_id,
        )
        return "text:abstract-only"
    return result.source_tag


def _run_rss_text_extraction(note: dict[str, object], config: AppConfig) -> str:
    """Run RSS web-article extraction for a degraded RSS note.

    Reads ``source_url`` from frontmatter and re-runs
    :func:`influx.extraction.article.extract_article` with the same
    config knobs as initial RSS acquisition.  Returns ``"text:html"``
    on success.

    On any extraction failure (HTTP, network, min_length, parse, …)
    returns ``"text:abstract-only"`` per the
    :class:`~influx.repair.TextExtractionHook` protocol, so the sweep
    stamps a ``text:*`` tag and the note converges out of the
    text-extraction stage.  The note can still be upgraded to
    ``text:html`` later via :func:`_make_re_extract_archive_hook`
    once :func:`_make_archive_download_hook` lands an archive on disk.
    A WARN is emitted so operators can see the structural reason
    behind the convergence.

    Only ``ExtractionError(stage="resolve")`` is raised — that signals
    a missing ``source_url`` frontmatter field which only an operator
    fix can repair, and it should keep surfacing through the sweep's
    failure-logging path until corrected.
    """
    source_url = _note_source_url(note)
    if not source_url:
        raise ExtractionError(
            "Cannot retry text extraction: no source_url in frontmatter",
            stage="resolve",
            detail=f"note id={note.get('id', '?')}",
        )

    extraction_cfg = config.extraction
    storage_cfg = config.storage
    try:
        extract_article(
            source_url,
            min_web_chars=extraction_cfg.min_web_chars,
            strip_tags=list(extraction_cfg.strip_tags),
            allow_private_ips=config.security.allow_private_ips,
            max_download_bytes=storage_cfg.max_download_bytes,
            timeout_seconds=storage_cfg.download_timeout_seconds,
        )
    except (ExtractionError, NetworkError) as exc:
        stage = getattr(exc, "stage", None) or getattr(exc, "kind", None) or "extract"
        _log.warning(
            "rss text_extraction retry: live extraction failed for %s "
            "(stage=%s url=%s); converging to text:abstract-only",
            note.get("id", "?"),
            stage,
            source_url,
        )
        return "text:abstract-only"
    return "text:html"


def _make_text_extraction_hook(config: AppConfig) -> TextExtractionHook:
    """Create the production ``text_extraction`` hook (FR-REP-1 stage 2).

    The hook re-runs the source-specific extraction cascade for a note
    that carries no ``text:*`` tag and returns the resulting tag.  On
    cascade fall-through the underlying helper raises
    :class:`ExtractionError`; we surface it to the sweep so the
    per-stage failure logging path fires.

    Supports ``source:arxiv`` (cascade via
    :func:`extract_arxiv_text`) and ``source:rss-*`` (web article
    extraction via :func:`extract_article`).

    Source-metadata invariant (#150)
    --------------------------------
    Before dispatching, the hook normalises the note's source tag via
    :func:`infer_note_source`:

    * If the existing ``source:*`` suffix is already dispatchable
      (``arxiv`` / ``rss-*``), it is used as-is.
    * If it is empty or garbled but can be inferred from
      ``source_url`` / note path / note id, the tag is **backfilled
      in-place** so subsequent sweeps don't re-trigger inference, and
      dispatch proceeds with the inferred source.
    * If inference fails entirely, the hook raises
      ``ExtractionError(stage="invalid_source_metadata")`` — distinct
      from ``unsupported_source`` (a legitimate future source) — so
      the sweep can flip the note to terminal via
      :func:`influx.repair._terminate_invalid_source_metadata` and
      stop the staging-incident retry loop.

    Other (genuinely unsupported but well-formed) sources raise
    ``ExtractionError(stage="unsupported_source")`` so the sweep
    terminalises the note via
    :func:`influx.repair._terminate_unsupported_text_source`.
    """

    def hook(note: dict[str, object]) -> str:
        existing_source = _note_source_tag(note)
        source = infer_note_source(note)
        if source is None:
            _log.warning(
                "text_extraction retry: invalid source metadata for %s "
                "(existing_source=%r, path=%r); marking terminal",
                note.get("id", "?"),
                existing_source,
                note.get("path", ""),
            )
            _raise_invalid_source_metadata(note, stage_label="text_extraction retry")
            raise AssertionError("unreachable")  # pragma: no cover

        # Backfill the tag when inference produced a different (or
        # newly-populated) value, so the next pass starts from a
        # clean state and we never re-emit the diagnostic for the
        # same note.
        if source != existing_source:
            _log.info(
                "text_extraction retry: backfilled source tag for %s (was %r, now %r)",
                note.get("id", "?"),
                existing_source,
                source,
            )
            _backfill_source_tag(note, source)

        if source == "arxiv":
            tag = _run_arxiv_text_extraction(note, config)
        elif _is_rss_source(source):
            tag = _run_rss_text_extraction(note, config)
        else:
            _raise_unsupported_source(
                note, stage_label="text_extraction retry", source=source
            )
            raise AssertionError("unreachable")  # pragma: no cover
        _log.info(
            "text_extraction retry succeeded for %s tag=%s",
            note.get("id", "?"),
            tag,
        )
        return tag

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
        tags: list[str] = _note_tags(note)

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
        tags: list[str] = _note_tags(note)

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
