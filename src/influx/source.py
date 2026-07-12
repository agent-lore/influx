"""Unified Source seam (CONTEXT.md ``Source``).

Defines the protocol every source adapter (arXiv, RSS, blog) implements
and the candidate / scored-candidate value types that flow through the
Run's Acquire stage.

Per CONTEXT.md the Run's Acquire stage walks::

    Source.fetch_candidates → Filter.score → Source.acquire → Acquired

This module owns the seam.  Source adapters live under
``influx.sources.*``; the Filter that scores candidates lives in
``influx.filter``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from influx.config import AppConfig, ProfileConfig
from influx.coordinator import RunKind

__all__ = [
    "ARXIV_ID_TAG_PREFIX",
    "ArchiveDownloadIdentity",
    "BoundScoredCandidate",
    "Candidate",
    "ScoredCandidate",
    "Source",
    "find_note_tag",
    "note_source_url",
    "note_tags",
    "year_month_from_created_at",
    "year_month_from_note_path",
]


@dataclass(frozen=True, slots=True)
class Candidate:
    """An unscored item returned from :meth:`Source.fetch_candidates`.

    Carries the minimal identity surface every Filter needs (id, title,
    abstract, source URL) plus a ``payload`` slot for the source-native
    metadata the adapter's :meth:`Source.acquire` will consume.

    The ``payload`` is opaque to the Filter — typically the original
    :class:`influx.sources.arxiv.ArxivItem` or
    :class:`influx.sources.rss.RssFeedItem`.
    """

    item_id: str
    title: str
    abstract: str
    source_url: str
    payload: Any = None


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    """A :class:`Candidate` plus the Filter's 1–10 relevance score.

    ``filter_tags`` carries the LLM-filter tags (FR-FLT-3) used by
    rejection-rate logging, distinct from the persisted note tags the
    source builder later attaches.  Items below
    ``thresholds.relevance`` or absent from the filter response are
    dropped by the Filter and never reach this stage.
    """

    candidate: Candidate
    score: int
    confidence: float
    reason: str
    filter_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundScoredCandidate:
    """A :class:`ScoredCandidate` plus a thunk that performs source-specific
    acquire (#125).

    Returned by the unified ``ItemProvider`` so the orchestration layer
    can run ``lithos_cache_lookup`` on candidate identity *before* the
    source adapter pays the full download/archive/extract cost.

    ``acquire`` is a no-arg async callable that captures the source
    adapter, ``profile_cfg``, and ``config`` from the provider scope;
    invoking it runs the per-item acquire (typically wrapping
    :func:`asyncio.to_thread` around blocking work, #124) and returns
    the ready-to-yield ``ProfileItem`` dict.

    ``source_label`` carries the source family used for metric labels and
    log lines — ``"arxiv"`` or ``"rss:<feed_name>"``.
    """

    scored: ScoredCandidate
    acquire: Callable[[], Awaitable[dict[str, Any] | None]]
    source_label: str


@dataclass(frozen=True, slots=True)
class ArchiveDownloadIdentity:
    """A Source's reconstruction of the archive-download identity for a note.

    Produced by :meth:`Source.archive_download_identity` from a
    repair-sweep note the Source authored, and consumed by the sweep's
    archive-download stage — which supplies the config / archive-policy
    args and performs the actual :func:`influx.storage.download_archive`.

    The seam deliberately carries a *value* (the identity the adapter
    owns: how to address its own archives), not the download *mechanism*
    (shared, config-driven, and the same for every source).  Per ADR-0001
    only this archive identity is reconstructed — not a full ``Acquired``
    — because Tier 1 recovery (which would need the un-persisted abstract)
    is out of the sweep.

    ``published_year`` / ``published_month`` name the archive bucket; the
    retry bucket may differ from the original (``read_note`` does not
    preserve the Influx note ``path``) — that is acceptable, it only needs
    to be deterministic, because acquisition failed and no archive lives
    on disk for the original path.
    """

    url: str
    item_id: str
    published_year: int
    published_month: int
    ext: str
    # Structurally matches ``influx.storage.ContentTypeFamily`` (which
    # Foundation cannot import — Storage is Core); a same-member Literal
    # is assignable to it at the ``download_archive`` call site.
    expected_content_type: Literal["html", "pdf", "xml"]


@runtime_checkable
class Source(Protocol):
    """Unified Source seam (CONTEXT.md).

    A Source adapter exposes two stages:

    - :meth:`fetch_candidates` — bulk per-Profile, called once per Run.
      Returns the unscored candidates the Filter will score.
    - :meth:`acquire` — per-item, called by the orchestrator after
      Filter scoring.  Performs download/archive/extract and returns
      the ready-to-yield ``ProfileItem`` dict consumed by the
      scheduler's ``run_profile``.

    The score-gated cascade (Tier 1/2/3 + Renderer) is reached only
    via :meth:`acquire`; sources do not score candidates themselves.

    A Source also owns the *inverse* of the identity it builds at
    acquire time:

    - :meth:`archive_download_identity` — repair-time, called by the
      sweep's archive-download stage.  Reconstructs the download identity
      (URL / item_id / archive bucket / extension) from a note the Source
      authored, so the acquire-time identity scheme and its repair-time
      reconstruction live in one module and cannot drift (finding 3b).
    """

    name: str

    def fetch_candidates(
        self,
        *,
        profile_cfg: ProfileConfig,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
    ) -> Awaitable[list[Candidate]]: ...

    def acquire(
        self,
        scored: ScoredCandidate,
        *,
        profile_cfg: ProfileConfig,
        config: AppConfig,
    ) -> dict[str, Any] | None: ...

    def archive_download_identity(
        self, note: dict[str, object]
    ) -> ArchiveDownloadIdentity | None:
        """Reconstruct the archive-download identity for a repair-sweep note.

        *note* is the ``lithos_read`` envelope for a repair-needed note
        this Source authored.  Returns ``None`` when the note is missing
        the fields this Source needs to rebuild the identity — the sweep
        treats that as a transient "needs operator hand-fix" and re-enters
        the note next pass.
        """
        ...


# ── Repair-sweep note-envelope readers ───────────────────────────────
#
# The repair sweep and each Source's :meth:`Source.archive_download_identity`
# read identity fields back out of the ``lithos_read`` note envelope a
# Source authored.  They live here (Foundation) so the Repair sweep and the
# Sources adapters share one reader set without a Repair<->Sources import.

# Identity tag a Source writes; read by the arXiv adapter's re-acquire and
# by the repair sweep's text-extraction stage.
ARXIV_ID_TAG_PREFIX = "arxiv-id:"

# ``papers|articles/<source>/YYYY/MM`` archive bucket embedded in a note path.
_NOTE_PATH_RE = re.compile(
    r"(?:papers|articles)/(?P<source>[^/]+)/(?P<year>\d{4})/(?P<month>\d{2})"
)
# Leading ``YYYY-MM`` of an ISO-8601 ``created_at`` timestamp.
_ISO_YEAR_MONTH_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})")


def note_tags(note: dict[str, object]) -> list[str]:
    """Read and cast the note's tag list defensively (Lithos returns ``Any``)."""
    raw = note.get("tags", [])
    return list(raw) if isinstance(raw, list) else []


def find_note_tag(tags: list[str], prefix: str) -> str | None:
    """Return the suffix of the first tag starting with *prefix*, or None."""
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def note_source_url(note: dict[str, object]) -> str | None:
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


def year_month_from_note_path(note: dict[str, object]) -> tuple[int, int] | None:
    """Pull the archive ``(year, month)`` bucket from a note path.

    ``read_note`` does not preserve the Influx note ``path``
    (``papers|articles/<source>/YYYY/MM``), so this returns ``None`` for
    most sweep candidates and callers fall back to a source-specific or
    ``created_at``-based derivation.
    """
    m = _NOTE_PATH_RE.search(str(note.get("path") or ""))
    if not m:
        return None
    try:
        return int(m.group("year")), int(m.group("month"))
    except ValueError:
        return None


def year_month_from_created_at(note: dict[str, object]) -> tuple[int, int] | None:
    """Derive ``(year, month)`` from the note's ``created_at`` timestamp.

    Reads the top-level ``created_at``, falling back to ``metadata``, then
    parses the leading ``YYYY-MM``.  The final fallback for the archive
    bucket when neither the note path nor a source-specific scheme resolves.
    """
    created_at = note.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        meta = note.get("metadata")
        if isinstance(meta, dict):
            meta_created = meta.get("created_at")
            created_at = meta_created if isinstance(meta_created, str) else ""
        else:
            created_at = ""
    if not created_at:
        return None
    m = _ISO_YEAR_MONTH_RE.match(created_at)
    if not m:
        return None
    month = int(m.group("month"))
    if not 1 <= month <= 12:
        return None
    return int(m.group("year")), month
