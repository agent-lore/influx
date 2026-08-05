"""Production-default repair hooks (PRD 07 US-016).

Bridges the PRD 06 hook signatures (``ReExtractArchiveHook``,
``TextExtractionHook``) to the lower-level extraction helpers from PRD
07 (``extraction.html``, ``extraction.pdf``).  Tier 2 and Tier 3
recovery are no longer hooks — the sweep runs them through the shared
:class:`~influx.cascade.Cascade` (finding 3, 3a.2 / 3a.3); this module
supplies the sweep's archive-reading :data:`~influx.cascade.Tier2Extractor`
via :func:`make_sweep_tier2_extractor`.

The ``SweepHooks`` test-injection seam is preserved unchanged: these
defaults are only wired in when ``sweep()`` is called without explicit
hooks.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import trafilatura

from influx.archive_policy import (
    ArchivePolicyMode,
    classify_failure_kind,
)
from influx.archive_policy import (
    registry_from_config as _archive_policy_registry_from_config,
)
from influx.cascade import Acquired, TextFlavour, Tier2Extractor, Tier2Result
from influx.config import AppConfig
from influx.errors import ExtractionError, NetworkError
from influx.extraction.article import extract_article
from influx.extraction.html import _clean_html_fragments, _strip_tags
from influx.extraction.pdf import extract_pdf
from influx.extraction.pipeline import extract_arxiv_text
from influx.repair import (
    ArchiveDownloadHook,
    ExtractionOutcome,
    ReExtractArchiveHook,
    ReExtractionResult,
    SweepHooks,
    TextExtractionHook,
)
from influx.source import (
    ARXIV_ID_TAG_PREFIX,
    Source,
    find_note_tag,
    note_source_url,
    note_tags,
)
from influx.storage import ArchiveResult, download_archive

__all__ = [
    "DefaultSweepHooks",
    "make_default_sweep_hooks",
    "make_sweep_tier2_extractor",
]

_log = logging.getLogger(__name__)

# ── Content manipulation helpers ────────────────────────────────────
#
# The section-shape ops (insert Full Text, find the insertion point) are
# owned by :mod:`influx.canonical_note` and imported above.


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


# ── Source metadata / note-id helpers ───────────────────────────────
#
# The archive-download identity reconstruction (arXiv YYMM buckets +
# pdf URL, RSS ``{feed-slug}-{url-hash}`` item_id) now lives with the
# adapter that owns each scheme — ``Source.archive_download_identity`` in
# ``influx.sources.arxiv`` / ``influx.sources.rss`` (finding 3b).  The
# generic note-envelope readers (:func:`find_note_tag`, :func:`note_tags`,
# :func:`note_source_url`) are shared from :mod:`influx.source`.

_SOURCE_TAG_PREFIX = "source:"
_NOTE_PATH_SOURCE_RE = re.compile(r"(?:^|/)(?:papers|articles)/(?P<source>[^/]+)/")
_RSS_NOTE_ID_PREFIX = "rss-"
# Mirrors ``influx.sources.rss._FEED_SLUG_TAG_PREFIX``.  Duplicated
# rather than imported because ``sources.*`` already import from this
# module, so importing back would close a cycle.
_FEED_SLUG_TAG_PREFIX = "feed-slug:"
_ARXIV_NOTE_ID_PREFIX = "arxiv-"
_ARXIV_HOSTNAMES: frozenset[str] = frozenset({"arxiv.org", "www.arxiv.org"})


def _note_source_tag(note: dict[str, object]) -> str:
    """Return the note's ``source:*`` tag suffix or an empty string."""
    return find_note_tag(note_tags(note), _SOURCE_TAG_PREFIX) or ""


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
    to historical or hand-edited notes.  ``source:blog`` is the other
    value :class:`~influx.config.RssSourceEntry` permits for
    ``source_tag`` (``Literal["rss", "blog"]``) and is written verbatim
    by RSS acquisition, so it names the same family.
    """
    return source in ("rss", "blog") or source.startswith(_RSS_NOTE_ID_PREFIX)


def _has_rss_identity(note: dict[str, object]) -> bool:
    """Return whether *note* carries recoverable RSS archive identity.

    :meth:`~influx.sources.rss.RssSource.archive_download_identity`
    rebuilds the retry identity from an ``rss-`` note id, or from a
    ``feed-slug:`` tag plus a doc-level ``source_url``.  Neither
    requires a recognised ``source:*`` tag, so a note can be
    reacquirable even when its source tag is one the dispatcher does
    not name — e.g. a feed whose ``source_tag`` was renamed, or a
    historical value predating the current vocabulary.

    Checking identity rather than the tag alone keeps recoverable notes
    out of the ``unsupported_source`` path, which is now *counted* for
    the archive stage and therefore permanent at the cap.
    """
    if str(note.get("id", "")).startswith(_RSS_NOTE_ID_PREFIX):
        return True
    has_feed_slug = find_note_tag(note_tags(note), _FEED_SLUG_TAG_PREFIX) is not None
    return has_feed_slug and bool(note_source_url(note))


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

    source_url = note_source_url(note) or ""
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
    tags = note_tags(note)
    rebuilt = [t for t in tags if not t.startswith(_SOURCE_TAG_PREFIX)]
    rebuilt.append(f"{_SOURCE_TAG_PREFIX}{inferred}")
    note["tags"] = rebuilt


def _raise_unsupported_source(
    note: dict[str, object], *, stage_label: str, source: str
) -> None:
    """Raise ``ExtractionError(stage="unsupported_source")`` for an unknown source.

    Both paths treat this as permanent, by different mechanisms:

    * text extraction flips ``influx:text-terminal`` directly via
      :func:`influx.repair._terminate_unsupported_text_source`;
    * archive download counts it toward the per-stage cap (see
      ``_STAGE_SCOPED_COUNTED_STAGES`` in ``influx.repair_counters``),
      flipping ``influx:archive-terminal`` at
      ``REPAIR_COUNTED_CAP``.

    The archive path classified this transient until the note churn it
    caused was measured in production: a note whose source has no
    resolver was re-swept and rewritten on every pass indefinitely,
    pinning ``updated_at`` to "now" and dominating retrieval ranking.
    Retrying cannot help — only shipping a resolver can — so it now
    advances the cap.  Once a resolver lands, re-arm affected notes per
    ``docs/operations/runbook.md`` §6.
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


def _classify_download_kind(result: ArchiveResult) -> str:
    """Return the ``kind`` discriminator for a failed archive download.

    We surface a stable ``stage`` value to the sweep so
    :func:`influx.repair.classify_failure` can decide counted vs
    transient.

    :attr:`~influx.storage.ArchiveResult.failure_kind` is preferred
    because :func:`~influx.storage.download_archive` has already computed
    it *policy-aware*: under ``policy_mode="blocked"`` an HTTP 403
    collapses to ``blocked`` rather than ``http_403``, and re-deriving
    from the error string here would silently lose that.

    Issue #282: this used to flatten every ``"HTTP <code> for ..."``
    error to a bare ``"http"``, discarding the distinction the archive
    layer had just made.  A permanently-403 URL (paywall) was therefore
    indistinguishable from a transient 503, so ``archive_attempts`` never
    advanced and the note was re-swept and rewritten forever.

    The string-derivation fallback covers results built without a
    ``failure_kind`` (older call sites and test fakes); ``""`` with no
    error at all keeps the historical ``"archive_failed"`` sentinel.
    """
    if result.failure_kind:
        return result.failure_kind
    if not result.error:
        return "archive_failed"
    # ``ArchiveResult.policy_mode`` is typed ``str`` (it is ``""`` when no
    # registry was threaded through) rather than the narrower literal.
    policy_mode = cast("ArchivePolicyMode", result.policy_mode or "attempt")
    return classify_failure_kind(error=result.error, policy_mode=policy_mode)


# ── Hook factories ──────────────────────────────────────────────────


def _reacquirer_for_note(
    note: dict[str, object],
    source: str,
    reacquirers: Mapping[str, Source],
) -> Source | None:
    """Pick the Source that owns *note*'s archive-identity scheme.

    Resolution is by source-tag family first (``"arxiv"``; ``"rss"`` for
    any of ``rss`` / ``blog`` / ``rss-<feed>``), then by durable RSS
    identity markers on the note itself — an ``rss-`` id, or a
    ``feed-slug:`` tag plus a doc-level ``source_url``.

    The identity fallback matters because the archive stage now *counts*
    ``unsupported_source`` toward its cap, making it permanent at the
    cap.  Dispatching on the tag alone would terminalise notes that
    :meth:`~influx.source.Source.archive_download_identity` could in
    fact resolve — ``source:blog`` is a currently-valid
    :class:`~influx.config.RssSourceEntry` ``source_tag`` and was the
    concrete instance of this.

    Returns ``None`` only when no registered adapter can own the note
    (e.g. inbox — GH #248), which the hook raises as
    ``unsupported_source``.
    """
    if source == "arxiv":
        return reacquirers.get("arxiv")
    if _is_rss_source(source) or _has_rss_identity(note):
        return reacquirers.get("rss")
    return None


def _make_archive_download_hook(
    config: AppConfig,
    archive_reacquirers: Mapping[str, Source],
) -> ArchiveDownloadHook:
    """Create the production ``archive_download`` hook (FR-REP-1).

    The hook re-runs :func:`influx.storage.download_archive` for a note
    tagged ``influx:archive-missing`` and returns the relative POSIX
    path on success.  On failure it raises :class:`ExtractionError` so
    the sweep's existing ``(ExtractionError, LithosError)`` branch
    bumps the per-note ``archive_attempts`` counter (only for
    counted-class kinds — ``"oversize"`` globally, plus
    ``unsupported_source`` and the permanent HTTP statuses for this
    stage) and flips ``influx:archive-terminal`` once the cap is
    reached.

    *archive_reacquirers* maps a canonical source family (``"arxiv"`` /
    ``"rss"``) to the :class:`~influx.source.Source` that owns that
    family's archive-identity scheme.  The hook dispatches each note to
    its Source's
    :meth:`~influx.source.Source.archive_download_identity` (finding 3b)
    — so the acquire-time identity scheme and its repair-time
    reconstruction live in one module and cannot drift.  A note whose
    source has no registered reacquirer (e.g. inbox — GH #248) raises
    ``ExtractionError(stage="unsupported_source")``, which is *counted*
    for the archive stage: the note reaches ``influx:archive-terminal``
    at the cap and leaves the sweep instead of re-entering it forever.
    A note whose
    reacquirer cannot rebuild the identity (missing fields) raises
    ``ExtractionError(stage="resolve")`` (also transient) awaiting an
    operator hand-fix.

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
        source = _note_source_tag(note)
        reacquirer = _reacquirer_for_note(note, source, archive_reacquirers)
        if reacquirer is None:
            _raise_unsupported_source(
                note, stage_label="archive_download retry", source=source
            )
            raise AssertionError("unreachable")  # pragma: no cover

        identity = reacquirer.archive_download_identity(note)
        if identity is None:
            raise ExtractionError(
                "Cannot retry archive download: source "
                f"{source!r} could not rebuild archive identity from note",
                stage="resolve",
                detail=f"note id={note.get('id', '?')}",
            )

        result = download_archive(
            policy_registry=policy_registry,
            url=identity.url,
            archive_root=Path(config.storage.archive_dir),
            source=source,
            item_id=identity.item_id,
            published_year=identity.published_year,
            published_month=identity.published_month,
            ext=identity.ext,
            allow_private_ips=config.security.allow_private_ips,
            max_download_bytes=config.storage.max_download_bytes,
            timeout_seconds=config.storage.download_timeout_seconds,
            expected_content_type=identity.expected_content_type,
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
        # attribute below; surface the archive layer's own failure kind
        # so e.g. ``"oversize"`` lines up with
        # ``influx.repair_counters._COUNTED_STAGES`` and ``"http_403"``
        # with the archive entry of ``_STAGE_SCOPED_COUNTED_STAGES``.
        stage = _classify_download_kind(result)
        raise ExtractionError(
            f"archive_download retry failed: {result.error}",
            url=identity.url,
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
    arxiv_id = find_note_tag(note_tags(note), ARXIV_ID_TAG_PREFIX)
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
    source_url = note_source_url(note)
    if not source_url:
        raise ExtractionError(
            "Cannot retry text extraction: no doc-level source_url on note",
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


def make_sweep_tier2_extractor(config: AppConfig) -> Tier2Extractor:
    """Create the sweep's Tier 2 extractor for :class:`~influx.cascade.Cascade`.

    Re-extracts full text from the note's already-downloaded archive
    file — the source-agnostic recovery path.  The Cascade owns the
    counter lifecycle and section rendering; this extractor only turns
    an :class:`~influx.cascade.Acquired` into a
    :class:`~influx.cascade.Tier2Result`.

    A missing archive path raises a counted ``parse`` failure (exactly
    as the old ``tier2_enrich`` hook did), so the Tier-2 cap still trips
    for a note that never gains an archive.  Archive-read and extraction
    failures raise transient stages (``archive_read`` / ``extract`` /
    ``min_length``) that keep ``influx:repair-needed`` without advancing
    the counter.
    """

    def extractor(acquired: Acquired) -> Tier2Result:
        if not acquired.archive_path:
            raise ExtractionError(
                "No archive path found in note",
                stage="parse",
                detail="## Archive section missing or empty",
            )

        file_bytes = _read_archive_file(config, acquired.archive_path)
        text, text_tag = _extract_from_archive(
            file_bytes, acquired.archive_path, config
        )
        flavour: TextFlavour = "pdf" if text_tag == "text:pdf" else "html"
        return Tier2Result(text=text, flavour=flavour, text_tag=text_tag)

    return extractor


# ── Public factory ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DefaultSweepHooks:
    """Production-default sweep hook wiring with non-optional callables.

    Holds the three production hooks with non-``Optional`` types so
    pyright can statically know they are wired, while the parent
    :class:`~influx.repair.SweepHooks` keeps them ``| None`` to preserve
    the test-injection seam.  (Tier 2 and Tier 3 recovery are not hooks
    — the sweep runs them through the shared Cascade; finding 3,
    3a.2 / 3a.3.)

    Use :meth:`to_sweep_hooks` to obtain a ``SweepHooks`` instance for
    passing into :func:`influx.repair.sweep`.
    """

    archive_download: ArchiveDownloadHook
    re_extract_archive: ReExtractArchiveHook
    text_extraction: TextExtractionHook

    def to_sweep_hooks(self) -> SweepHooks:
        """Return a :class:`SweepHooks` carrying these production hooks."""
        return SweepHooks(
            archive_download=self.archive_download,
            re_extract_archive=self.re_extract_archive,
            text_extraction=self.text_extraction,
        )


def make_default_sweep_hooks(
    config: AppConfig,
    *,
    archive_reacquirers: Mapping[str, Source] | None = None,
) -> DefaultSweepHooks:
    """Create production-default sweep hooks for the repair sweep.

    Each hook bridges the PRD 06 hook signature to the lower-level
    fetch / extraction / enrichment helpers (FR-REP-1).

    *archive_reacquirers* maps a canonical source family (``"arxiv"`` /
    ``"rss"``) to the :class:`~influx.source.Source` that reconstructs
    that family's archive-download identity from a persisted note
    (finding 3b).  The Repair layer cannot import the Sources adapters
    (it would close a module cycle), so the composition root
    (``run._run_repair_stage``) injects them.  When ``None`` (tests that
    do not exercise archive re-download, or drive the sweep with their
    own hooks) the archive-download hook treats every note as
    ``unsupported_source`` — a transient no-op.

    Returns :class:`DefaultSweepHooks` (typed with non-optional
    callables) so callers and tests do not need to narrow ``Optional``
    attributes before invoking them.  Convert to a ``SweepHooks`` for
    the sweep entrypoint via :meth:`DefaultSweepHooks.to_sweep_hooks`.
    """
    return DefaultSweepHooks(
        archive_download=_make_archive_download_hook(config, archive_reacquirers or {}),
        re_extract_archive=_make_re_extract_archive_hook(config),
        text_extraction=_make_text_extraction_hook(config),
    )
