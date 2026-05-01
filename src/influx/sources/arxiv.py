"""arXiv Atom feed fetcher with client-side date filtering.

Queries ``https://export.arxiv.org/api/query`` for configured categories,
parses the Atom response with stdlib ``xml.etree.ElementTree``, and applies
client-side date filtering against ``profile.sources.arxiv.lookback_days``
(FR-SRC-1, FR-SRC-2).

Retry behaviour:
- HTTP 429 → sleep ``resilience.arxiv_429_backoff_seconds`` then retry
  (FR-RES-2)
- Other transient failures → exponential backoff from
  ``resilience.backoff_base_seconds`` (FR-RES-1)

``build_arxiv_note_item`` (PRD 07 US-014) constructs a complete
``ProfileItem`` dict for the scheduler, running the HTML → PDF →
abstract-only extraction cascade and rendering the canonical note.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from influx.config import (
    AppConfig,
    ArxivSourceConfig,
    ProfileThresholds,
    ResilienceConfig,
    StorageConfig,
)
from influx.coordinator import RunKind
from influx.enrich import tier1_enrich, tier3_extract
from influx.errors import ExtractionError, LCMAError, NetworkError
from influx.extraction.pipeline import extract_arxiv_text
from influx.filter import FilterScorerError
from influx.http_client import guarded_fetch
from influx.notes import ProfileRelevanceEntry, render_note
from influx.schemas import Tier1Enrichment, Tier3Extraction
from influx.storage import download_archive
from influx.telemetry import (
    current_run_id,
    get_tracer,
    record_source_acquisition_error,
)

if TYPE_CHECKING:
    from influx.sources import FetchCache

__all__ = [
    "ArxivFilterScorer",
    "ArxivItem",
    "ArxivScorer",
    "ArxivScoreResult",
    "BackfillRange",
    "build_arxiv_note_item",
    "build_query_url",
    "fetch_arxiv",
    "make_arxiv_item_provider",
    "resolve_backfill_range",
]

_log = logging.getLogger(__name__)

_ARXIV_API_URL = "https://export.arxiv.org/api/query"

_ATOM_NS = "http://www.w3.org/2005/Atom"

# Acceptable XML content-type family for successful arXiv responses.
# Content-type validation is performed locally after status-code handling
# so that 429/5xx responses with non-XML bodies route through the proper
# backoff paths (FR-RES-1/2) instead of being raised as content-type
# errors by the guarded fetch.
_XML_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/xml",
        "application/xml",
        "application/atom+xml",
        "application/rss+xml",
    }
)


@dataclass(frozen=True, slots=True)
class ArxivItem:
    """A single parsed arXiv entry from the Atom feed."""

    arxiv_id: str
    title: str
    abstract: str
    published: datetime
    categories: list[str]


@dataclass(frozen=True, slots=True)
class ArxivScoreResult:
    """One scored candidate emitted by an :data:`ArxivScorer`.

    The scorer drives the score-gated extraction / enrichment
    behaviour required by PRD 07 §5.6 (``build_arxiv_note_item`` gates
    HTML/PDF extraction on ``score >= thresholds.full_text``, Tier 1
    enrichment on ``score >= thresholds.relevance``, and Tier 3
    extraction on ``score >= thresholds.deep_extract``).  Returning
    ``None`` from the scorer means "drop this item entirely".

    ``filter_tags`` carries the LLM filter-result tags attached to this
    candidate (FR-FLT-3 ``FilterResult.tags``).  These are the tags the
    filter prompt itself emits — distinct from the persisted note /
    provenance tags the source builder later attaches — and are what
    rejection-rate logging (FR-OBS-5, US-008) consumes when computing
    per-tag rejection rates.
    """

    score: int
    confidence: float
    reason: str
    filter_tags: tuple[str, ...] = ()


# A scorer maps each fetched arXiv item + the active profile name to a
# concrete ``ArxivScoreResult`` (or ``None`` to drop the item from the
# run).  The seam exists so unit/integration tests can drive the
# score-gated extraction / enrichment paths from US-014/US-015 with a
# deterministic per-item scorer.  Production scoring uses the batched
# LLM-driven :data:`ArxivFilterScorer` instead.
ArxivScorer = Callable[[ArxivItem, str], ArxivScoreResult | None]


# A batch scorer maps the full list of fetched arXiv items + the active
# profile name + the rendered ``filter_prompt`` (composed by
# :func:`influx.scheduler.run_profile`) to a mapping of arXiv id →
# ``ArxivScoreResult``.  Items omitted from the mapping are dropped.
# This is the production-default scoring shape — see
# :func:`influx.filter.make_default_arxiv_filter_scorer`.
ArxivFilterScorer = Callable[
    [list[ArxivItem], str, str],
    Awaitable[dict[str, ArxivScoreResult]],
]


# ── Backfill range ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BackfillRange:
    """Resolved backfill date range with absolute UTC bounds.

    Either constructed directly with ``date_from`` / ``date_to`` (the
    explicit ``--from`` / ``--to`` form) or via :func:`resolve_backfill_range`
    which converts the ``--days N`` form into a concrete window relative
    to *now*.
    """

    date_from: date
    date_to: date

    @property
    def days(self) -> int:
        """Number of days covered by this range (inclusive lower bound)."""
        return max((self.date_to - self.date_from).days, 0)


def resolve_backfill_range(
    run_range: dict[str, str | int] | None,
    *,
    now: datetime | None = None,
) -> BackfillRange | None:
    """Convert a ``run_range`` dict into a concrete :class:`BackfillRange`.

    Returns ``None`` when *run_range* is ``None`` (i.e. scheduled / manual
    runs).  Otherwise resolves either the ``--days N`` form (today minus
    *N* days through today) or the explicit ``--from`` / ``--to`` form.
    """
    if run_range is None:
        return None
    if "days" in run_range:
        days = int(run_range["days"])
        ref = now if now is not None else datetime.now(UTC)
        date_to = ref.date()
        date_from = date_to - timedelta(days=days)
        return BackfillRange(date_from=date_from, date_to=date_to)
    if "from" in run_range and "to" in run_range:
        return BackfillRange(
            date_from=date.fromisoformat(str(run_range["from"])),
            date_to=date.fromisoformat(str(run_range["to"])),
        )
    return None


# ── Query URL construction ─────────────────────────────────────────


def build_query_url(
    *,
    categories: list[str],
    max_results: int,
    backfill_range: BackfillRange | None = None,
) -> str:
    """Build the arXiv API query URL per FR-SRC-1.

    Constructs ``search_query`` as an OR-joined expression
    (``cat:X+OR+cat:Y+...``), ``sortBy=submittedDate``,
    ``sortOrder=descending``, and ``max_results`` from the profile.

    When *backfill_range* is provided, an additional
    ``+AND+submittedDate:[YYYYMMDDHHMM+TO+YYYYMMDDHHMM]`` clause restricts
    results to items submitted within the requested window so that
    ``backfill --days N`` actually fetches historical items rather than
    the current feed window (FR-BF-1).

    Range convention (review finding 2): ``BackfillRange`` is half-open
    ``[date_from, date_to)``.  ``date_to`` is exclusive, so a request
    with ``days=N`` covers exactly N calendar days and an explicit
    ``from=A, to=B`` covers exactly ``(B - A).days`` calendar days.
    Because the arXiv ``submittedDate:[... TO ...]`` clause is itself
    inclusive on both endpoints, the upper bound is emitted as the last
    minute (``2359``) of the day BEFORE ``date_to``.
    """
    cat_expr = "+OR+".join(f"cat:{c}" for c in categories)
    if backfill_range is not None:
        from_stamp = backfill_range.date_from.strftime("%Y%m%d") + "0000"
        last_included = backfill_range.date_to - timedelta(days=1)
        if last_included < backfill_range.date_from:
            # Zero-day window — emit a degenerate equal-bound range so
            # the server returns no items rather than an inverted query.
            to_stamp = backfill_range.date_from.strftime("%Y%m%d") + "0000"
        else:
            to_stamp = last_included.strftime("%Y%m%d") + "2359"
        cat_expr = f"({cat_expr})+AND+submittedDate:[{from_stamp}+TO+{to_stamp}]"
    return (
        f"{_ARXIV_API_URL}"
        f"?search_query={cat_expr}"
        f"&sortBy=submittedDate"
        f"&sortOrder=descending"
        f"&max_results={max_results}"
    )


# ── Atom parsing ───────────────────────────────────────────────────


def _extract_arxiv_id(raw_id: str) -> str:
    """Extract the bare arXiv ID from an Atom ``<id>`` element.

    The ``<id>`` element looks like ``http://arxiv.org/abs/2601.12345v1``.
    We strip the URL prefix and the version suffix to get ``2601.12345``.
    """
    # Strip URL prefix
    bare = raw_id
    for prefix in ("http://arxiv.org/abs/", "https://arxiv.org/abs/"):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break

    # Strip version suffix (e.g. "v1", "v2")
    if "v" in bare:
        base, _, rest = bare.rpartition("v")
        if rest.isdigit() and base:
            bare = base

    return bare


def _parse_atom(body: bytes) -> list[ArxivItem]:
    """Parse an arXiv Atom XML response into :class:`ArxivItem` entries."""
    root = ET.fromstring(body)  # noqa: S314
    items: list[ArxivItem] = []

    for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
        id_el = entry.find(f"{{{_ATOM_NS}}}id")
        title_el = entry.find(f"{{{_ATOM_NS}}}title")
        summary_el = entry.find(f"{{{_ATOM_NS}}}summary")
        published_el = entry.find(f"{{{_ATOM_NS}}}published")

        if id_el is None or id_el.text is None:
            continue
        if title_el is None or title_el.text is None:
            continue
        if summary_el is None or summary_el.text is None:
            continue
        if published_el is None or published_el.text is None:
            continue

        arxiv_id = _extract_arxiv_id(id_el.text.strip())
        title = " ".join(title_el.text.strip().split())
        abstract = summary_el.text.strip()

        pub_text = published_el.text.strip()
        published = datetime.fromisoformat(pub_text.replace("Z", "+00:00"))

        categories: list[str] = []
        for cat_el in entry.findall(f"{{{_ATOM_NS}}}category"):
            term = cat_el.get("term")
            if term:
                categories.append(term)

        items.append(
            ArxivItem(
                arxiv_id=arxiv_id,
                title=title,
                abstract=abstract,
                published=published,
                categories=categories,
            )
        )

    return items


def _filter_by_lookback(
    items: list[ArxivItem],
    lookback_days: int,
    now: datetime | None = None,
) -> list[ArxivItem]:
    """Drop items older than *lookback_days* from *now* (FR-SRC-2)."""
    if now is None:
        now = datetime.now(UTC)
    cutoff = now - timedelta(days=lookback_days)
    return [item for item in items if item.published >= cutoff]


# ── Fetch with retry ──────────────────────────────────────────────


def _sleep(seconds: float) -> None:
    """Sleep wrapper for monkeypatching in tests."""
    time.sleep(seconds)


def fetch_arxiv(
    *,
    arxiv_config: ArxivSourceConfig,
    resilience: ResilienceConfig,
    now: datetime | None = None,
    backfill_range: BackfillRange | None = None,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> list[ArxivItem]:
    """Fetch and filter arXiv items for the given config.

    Parameters
    ----------
    arxiv_config:
        The ``profile.sources.arxiv`` section from the config.
    resilience:
        The ``resilience`` section for retry/backoff settings.
    now:
        Override for the current time (for testing date filtering).
    backfill_range:
        Optional historical date range.  When supplied, the query URL
        is constrained to ``submittedDate`` within the range (FR-BF-1)
        and the standard ``lookback_days`` lower-bound filter is replaced
        by the explicit range bounds.  The pacing budget for backfills
        is enforced by ``ResilienceConfig.arxiv_request_min_interval_seconds``
        (FR-BF-3) and applied by the caller around each fetch.
    max_download_bytes:
        Maximum response body size in bytes for the underlying
        ``guarded_fetch``.  ``None`` resolves to the
        :class:`~influx.config.StorageConfig` field default so the only
        place this tunable lives is config-parsing code (AC-X-1).
    timeout_seconds:
        Connect + read timeout in seconds for the underlying
        ``guarded_fetch``.  ``None`` resolves to the
        :class:`~influx.config.StorageConfig` field default (AC-X-1).

    Returns
    -------
    list[ArxivItem]
        Parsed and date-filtered items, newest first.
    """
    url = build_query_url(
        categories=arxiv_config.categories,
        max_results=arxiv_config.max_results_per_category,
        backfill_range=backfill_range,
    )

    body = _fetch_with_retry(
        url=url,
        resilience=resilience,
        max_download_bytes=max_download_bytes,
        timeout_seconds=timeout_seconds,
    )

    items = _parse_atom(body)
    if backfill_range is not None:
        # Server-side ``submittedDate`` already constrains the window;
        # apply the same bounds client-side as a defense-in-depth check
        # against off-by-one timezone drift (FR-BF-1).  The range is
        # half-open ``[date_from, date_to)`` so that ``days=N`` covers
        # exactly N calendar days (review finding 2).
        from_dt = datetime.combine(
            backfill_range.date_from,
            datetime.min.time(),
            tzinfo=UTC,
        )
        to_dt = datetime.combine(
            backfill_range.date_to,
            datetime.min.time(),
            tzinfo=UTC,
        )
        return [it for it in items if from_dt <= it.published < to_dt]
    return _filter_by_lookback(
        items,
        arxiv_config.lookback_days,
        now=now,
    )


def _fetch_with_retry(
    *,
    url: str,
    resilience: ResilienceConfig,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> bytes:
    """Fetch *url* with 429 backoff and exponential retry (FR-RES-1/2).

    ``max_download_bytes`` and ``timeout_seconds`` default to ``None``;
    when omitted they are resolved from the pydantic
    :class:`~influx.config.StorageConfig` field defaults so the only
    place these tunable defaults live is config-parsing code (AC-X-1).
    """
    if max_download_bytes is None or timeout_seconds is None:
        _storage_defaults = StorageConfig()
        if max_download_bytes is None:
            max_download_bytes = _storage_defaults.max_download_bytes
        if timeout_seconds is None:
            timeout_seconds = _storage_defaults.download_timeout_seconds

    max_retries = resilience.max_retries
    backoff_base = resilience.backoff_base_seconds
    backoff_429 = resilience.arxiv_429_backoff_seconds

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            # Fetch without expected_content_type so status-code handling
            # (429 backoff, 5xx retry) runs first. Non-XML 429/5xx
            # responses would otherwise be raised as content-type errors
            # before reaching the rate-limit branch (FR-RES-2).
            result = guarded_fetch(
                url,
                max_download_bytes=max_download_bytes,
                timeout_seconds=timeout_seconds,
            )
        except NetworkError as exc:
            last_error = exc
            if attempt < max_retries:
                delay = backoff_base * (2**attempt)
                _log.warning(
                    "arXiv fetch attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    exc.kind,
                    delay,
                )
                _sleep(delay)
                continue
            raise

        if result.status_code == 429:
            last_error = NetworkError(
                f"HTTP 429 from arXiv API at {url}",
                url=url,
                kind="rate_limit",
            )
            if attempt < max_retries:
                _log.warning(
                    "arXiv 429 on attempt %d/%d, backing off %ds (FR-RES-2)",
                    attempt + 1,
                    max_retries + 1,
                    backoff_429,
                )
                _sleep(backoff_429)
                continue
            raise last_error

        if result.status_code >= 500:
            last_error = NetworkError(
                f"HTTP {result.status_code} from arXiv API",
                url=url,
                kind="network",
                reason=f"status={result.status_code}",
            )
            if attempt < max_retries:
                delay = backoff_base * (2**attempt)
                _log.warning(
                    "arXiv HTTP %d on attempt %d/%d, retrying in %.1fs (FR-RES-1)",
                    result.status_code,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                _sleep(delay)
                continue
            raise last_error

        if result.status_code >= 400:
            raise NetworkError(
                f"HTTP {result.status_code} from arXiv API",
                url=url,
                kind="network",
                reason=f"status={result.status_code}",
            )

        # Successful response: validate the XML content-type family now.
        mime = result.content_type.split(";")[0].strip().lower()
        if mime not in _XML_CONTENT_TYPES:
            raise NetworkError(
                (f"Content-type {mime!r} does not match expected XML family"),
                url=result.final_url,
                kind="content_type_mismatch",
                reason=(
                    f"Expected one of {', '.join(sorted(_XML_CONTENT_TYPES))}"
                    f"; got {mime!r}"
                ),
            )

        return result.body

    # Should not reach here, but satisfy type checker
    assert last_error is not None  # noqa: S101
    raise last_error


# ── Item builder (PRD 07 US-014) ─────────────────────────────────


def build_arxiv_note_item(
    *,
    item: ArxivItem,
    score: int,
    confidence: float,
    reason: str,
    profile_name: str,
    config: AppConfig,
    thresholds: ProfileThresholds | None = None,
    filter_tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a complete ``ProfileItem`` dict for the scheduler.

    Runs the HTML → PDF → abstract-only extraction cascade when the
    candidate's *score* crosses the ``full_text`` threshold, sets the
    appropriate ``text:*`` tier tag, and renders the canonical note via
    :func:`~influx.notes.render_note`.

    Parameters
    ----------
    item:
        Parsed arXiv entry.
    score:
        LLM-filter score (1–10).
    confidence:
        Filter confidence (0.0–1.0).
    reason:
        Human-readable filter reason.
    profile_name:
        Profile name for the ``profile:*`` tag.
    config:
        Loaded :class:`~influx.config.AppConfig`.
    thresholds:
        Optional explicit thresholds; when ``None`` the first matching
        profile's thresholds are used from *config*.

    Returns
    -------
    dict[str, Any]
        Ready-to-yield ``ProfileItem`` dict (title, source_url,
        content, tags, score, confidence, path, abstract_or_summary).
    """
    profile_cfg = next((p for p in config.profiles if p.name == profile_name), None)
    if thresholds is None:
        thresholds = profile_cfg.thresholds if profile_cfg else ProfileThresholds()

    source_url = f"https://arxiv.org/abs/{item.arxiv_id}"
    cat_tags = [f"cat:{c}" for c in item.categories]

    tags: list[str] = [
        f"profile:{profile_name}",
        f"arxiv-id:{item.arxiv_id}",
        "source:arxiv",
        "ingested-by:influx",
        f"schema:{config.influx.note_schema_version}",
        *cat_tags,
    ]

    repair_needed = False
    archive_path: str | None = None
    pdf_url = f"https://arxiv.org/pdf/{item.arxiv_id}.pdf"
    tracer = get_tracer()
    with tracer.span(
        "influx.archive.download",
        attributes={
            "influx.profile": profile_name,
            "influx.run_id": current_run_id.get() or "",
            "influx.source": "arxiv",
        },
    ):
        archive_result = download_archive(
            url=pdf_url,
            archive_root=Path(config.storage.archive_dir),
            source="arxiv",
            item_id=item.arxiv_id,
            published_year=item.published.year,
            published_month=item.published.month,
            ext=".pdf",
            allow_private_ips=config.security.allow_private_ips,
            max_download_bytes=config.storage.max_download_bytes,
            timeout_seconds=config.storage.download_timeout_seconds,
            expected_content_type="pdf",
        )
    if archive_result.ok:
        archive_path = archive_result.rel_posix_path
    else:
        tags.append("influx:archive-missing")
        repair_needed = True

    # ── Extraction cascade ────────────────────────────────────────
    extracted_text: str | None = None
    text_tag = "text:abstract-only"

    if score >= thresholds.full_text:
        # ── Telemetry: influx.enrich.tier2 span (FR-OBS-4) ──
        _tracer = get_tracer()
        with _tracer.span(
            "influx.enrich.tier2",
            attributes={
                "influx.profile": profile_name,
                "influx.run_id": current_run_id.get() or "",
                "influx.item_count": 1,
            },
        ):
            try:
                result = extract_arxiv_text(item.arxiv_id, config)
                extracted_text = result.text
                text_tag = result.source_tag
            except ExtractionError:
                # Both HTML and PDF failed — abstract-only + repair-needed.
                repair_needed = True

    tags.append(text_tag)

    # full-text tag iff extraction succeeded AND above threshold.
    full_text_for_note: str | None = None
    if extracted_text is not None and score >= thresholds.full_text:
        full_text_for_note = extracted_text
        tags.append("full-text")

    if repair_needed:
        tags.append("influx:repair-needed")

    # ── Tier 1 enrichment (FR-ENR-4) ─────────────────────────────
    tier1_result: Tier1Enrichment | None = None
    tier1_attempted = score >= thresholds.relevance
    if tier1_attempted:
        # ── Telemetry: influx.enrich.tier1 span (FR-OBS-4) ──
        _tracer = get_tracer()
        with _tracer.span(
            "influx.enrich.tier1",
            attributes={
                "influx.profile": profile_name,
                "influx.run_id": current_run_id.get() or "",
                "influx.item_count": 1,
            },
        ):
            profile_summary = profile_cfg.description if profile_cfg else ""
            try:
                tier1_result = tier1_enrich(
                    title=item.title,
                    abstract=item.abstract,
                    profile_summary=profile_summary,
                    config=config,
                )
            except LCMAError:
                _log.warning("Tier 1 enrichment failed for %s", item.arxiv_id)
                repair_needed = True
            except Exception:
                # Defensive: any unexpected failure during Tier 1
                # (e.g. an LLM response shape that bypasses the schema's
                # validators with an AttributeError, per staging incident
                # 2026-05-01) must degrade to a per-paper repair, not
                # take the whole scheduler run down.
                _log.warning(
                    "Tier 1 enrichment crashed unexpectedly for %s",
                    item.arxiv_id,
                    exc_info=True,
                )
                repair_needed = True

    # ── Tier 3 deep extraction (FR-ENR-5) ─────────────────────────
    tier3_result: Tier3Extraction | None = None
    if score >= thresholds.deep_extract and extracted_text is not None:
        # ── Telemetry: influx.enrich.tier3 span (FR-OBS-4) ──
        _tracer = get_tracer()
        with _tracer.span(
            "influx.enrich.tier3",
            attributes={
                "influx.profile": profile_name,
                "influx.run_id": current_run_id.get() or "",
                "influx.item_count": 1,
            },
        ):
            try:
                tier3_result = tier3_extract(
                    title=item.title,
                    full_text=extracted_text,
                    config=config,
                )
            except LCMAError:
                _log.warning("Tier 3 extraction failed for %s", item.arxiv_id)
                repair_needed = True
            except Exception:
                # Defensive: same rationale as the Tier 1 catch — a
                # validator bug or unforeseen response shape must not
                # turn a single bad paper into a run-level abort
                # (staging incident 2026-05-01).
                _log.warning(
                    "Tier 3 extraction crashed unexpectedly for %s",
                    item.arxiv_id,
                    exc_info=True,
                )
                repair_needed = True

    # influx:deep-extracted iff all four Tier 3 sections exist.
    if tier3_result is not None:
        tags.append("influx:deep-extracted")

    # Ensure repair-needed is set exactly once.
    if repair_needed and "influx:repair-needed" not in tags:
        tags.append("influx:repair-needed")

    # ── Render note ───────────────────────────────────────────────
    # When Tier 1 was attempted but failed, suppress the plain-text
    # summary so ## Summary is omitted entirely (AC-07-A / FR-ENR-6).
    summary_text = item.abstract
    if tier1_attempted and tier1_result is None:
        summary_text = ""

    profile_entries = [
        ProfileRelevanceEntry(
            profile_name=profile_name,
            score=score,
            reason=reason,
        ),
    ]

    content = render_note(
        title=item.title,
        source_url=source_url,
        tags=tags,
        confidence=confidence,
        archive_path=archive_path,
        summary=summary_text,
        keywords=[],
        profile_entries=profile_entries,
        full_text=full_text_for_note,
        tier1_enrichment=tier1_result,
        tier3_extraction=tier3_result,
    )

    pub = item.published
    path = f"papers/arxiv/{pub.year}/{pub.month:02d}"

    return {
        "id": f"arxiv-{item.arxiv_id}",
        "title": item.title,
        "source_url": source_url,
        "content": content,
        "tags": tags,
        "filter_tags": list(filter_tags) if filter_tags is not None else [],
        "score": score,
        "confidence": confidence,
        "reason": reason,
        "path": path,
        "abstract_or_summary": item.abstract,
        "contributions": tier1_result.contributions if tier1_result else None,
        "builds_on": list(tier3_result.builds_on) if tier3_result else None,
    }


# ── Production-default item provider (PRD 07 finding #1) ──────────────


def make_arxiv_item_provider(
    config: AppConfig,
    *,
    scorer: ArxivScorer | None = None,
    filter_scorer: ArxivFilterScorer | None = None,
    fetch_cache: FetchCache | None = None,
) -> Any:
    """Build the production-default ``item_provider`` for arXiv profiles.

    Returns an async callable that conforms to
    :data:`~influx.scheduler.ItemProvider`: it iterates each profile's
    enabled arXiv source, fetches items via :func:`fetch_arxiv`, and
    maps each result through :func:`build_arxiv_note_item` so the
    scheduler's ``run_profile`` drives the real HTML → PDF →
    abstract-only extraction stack and the Tier 1 / Tier 3 enrichment
    callers end-to-end.

    Score-gating seams
    ------------------
    *filter_scorer* is the production-default batched scoring seam: it
    receives the fetched item list + profile + the rendered
    ``filter_prompt`` and returns a mapping of arXiv id → score.  The
    production default is :func:`influx.filter.make_default_arxiv_filter_scorer`,
    which wraps the configured ``[models.filter]`` slot.  When supplied,
    items missing from the returned mapping are dropped from the run.

    *scorer* is a per-item synchronous seam used by unit/integration
    tests that want deterministic scoring without standing up a real
    LLM.  When set it takes precedence over *filter_scorer*.

    When NEITHER scorer is configured the provider falls back to
    score ``0`` (abstract-only, no extraction or enrichment) so a
    misconfigured deployment still produces notes — it does NOT
    fabricate a score equal to ``thresholds.full_text``.

    Parameters
    ----------
    fetch_cache:
        Optional shared :class:`~influx.sources.FetchCache` for
        per-fire dedup (R-8).  When two profiles build the same
        arXiv query URL the fetch is executed once and the result
        shared.
    """
    cache = fetch_cache

    async def provider(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> Iterable[dict[str, Any]]:
        profile_cfg = next((p for p in config.profiles if p.name == profile), None)
        if profile_cfg is None:
            _log.info("arxiv source skipped profile=%s reason=unknown_profile", profile)
            return ()
        if not profile_cfg.sources.arxiv.enabled:
            _log.info("arxiv source skipped profile=%s reason=disabled", profile)
            return ()

        # ── Cached fetch (R-8 dedup) ─────────────────────────────
        # Backfill runs build a date-bounded query so the requested
        # historical window is honoured (FR-BF-1).  The cache key
        # includes the bounded URL so that two profiles requesting the
        # same window dedup, but distinct windows do not collide.
        arxiv_cfg = profile_cfg.sources.arxiv
        thresholds = profile_cfg.thresholds
        backfill_range = (
            resolve_backfill_range(run_range) if kind == RunKind.BACKFILL else None
        )
        cache_key = "arxiv:" + build_query_url(
            categories=arxiv_cfg.categories,
            max_results=arxiv_cfg.max_results_per_category,
            backfill_range=backfill_range,
        )

        async def _do_fetch() -> list[ArxivItem]:
            # FR-BF-3 / review finding 2: multi-day backfills MUST split
            # into per-day windows so the run actually realizes the
            # ``days × len(categories) × max_results_per_category``
            # contract.  A single OR-joined query with ``max_results=N``
            # returns at most N items total across all days/categories,
            # so a 7-day backfill against ``cs.AI`` with
            # ``max_results_per_category=10`` would only fetch 10 items
            # — not 70.  Splitting per day also means
            # ``arxiv_request_min_interval_seconds`` is applied between
            # each request (not just one sleep up front), giving the
            # required ~30s-per-day-of-backfill pacing budget.
            #
            # Scheduled / manual runs make a single fetch per category
            # set, so pacing is irrelevant there.
            if kind != RunKind.BACKFILL or backfill_range is None:
                _log.info(
                    "arxiv fetch started profile=%s kind=%s categories=%s "
                    "max_results=%d lookback_days=%d",
                    profile,
                    kind.value,
                    arxiv_cfg.categories,
                    arxiv_cfg.max_results_per_category,
                    arxiv_cfg.lookback_days,
                )
                return fetch_arxiv(
                    arxiv_config=profile_cfg.sources.arxiv,
                    resilience=config.resilience,
                    backfill_range=backfill_range,
                    max_download_bytes=config.storage.max_download_bytes,
                    timeout_seconds=config.storage.download_timeout_seconds,
                )

            # Per-day fetch with pacing.  ``per_day_max`` widens
            # ``max_results`` so the OR-joined query can return up to
            # ``len(categories) * max_results_per_category`` items for
            # one day, matching the estimator contract (Q-3 / FR-BF-6).
            n_categories = max(len(arxiv_cfg.categories), 1)
            per_day_max = arxiv_cfg.max_results_per_category * n_categories
            per_day_arxiv_cfg = ArxivSourceConfig(
                enabled=arxiv_cfg.enabled,
                categories=list(arxiv_cfg.categories),
                max_results_per_category=per_day_max,
                lookback_days=arxiv_cfg.lookback_days,
            )
            pacing = float(config.resilience.arxiv_request_min_interval_seconds)
            collected: list[ArxivItem] = []
            seen_ids: set[str] = set()
            current = backfill_range.date_from
            while current < backfill_range.date_to:
                day_range = BackfillRange(
                    date_from=current,
                    date_to=current + timedelta(days=1),
                )
                _log.info(
                    "arxiv backfill day fetch started profile=%s day=%s "
                    "categories=%s max_results=%d",
                    profile,
                    current.isoformat(),
                    arxiv_cfg.categories,
                    per_day_max,
                )
                # Pace BEFORE each request so the very first fetch also
                # respects the spacing budget on a fresh fire.
                _sleep(pacing)
                try:
                    day_items = fetch_arxiv(
                        arxiv_config=per_day_arxiv_cfg,
                        resilience=config.resilience,
                        backfill_range=day_range,
                        max_download_bytes=config.storage.max_download_bytes,
                        timeout_seconds=config.storage.download_timeout_seconds,
                    )
                except NetworkError:
                    _log.warning(
                        "arxiv fetch failed for day %s; continuing backfill",
                        current.isoformat(),
                        exc_info=True,
                    )
                    day_items = []
                for it in day_items:
                    if it.arxiv_id not in seen_ids:
                        seen_ids.add(it.arxiv_id)
                        collected.append(it)
                _log.info(
                    "arxiv backfill day fetch completed profile=%s day=%s items=%d "
                    "collected=%d",
                    profile,
                    current.isoformat(),
                    len(day_items),
                    len(collected),
                )
                current = current + timedelta(days=1)
            return collected

        # ── Telemetry: influx.fetch.arxiv span (FR-OBS-4) ──
        _tracer = get_tracer()
        with _tracer.span(
            "influx.fetch.arxiv",
            attributes={
                "influx.profile": profile,
                "influx.run_id": current_run_id.get() or "",
                "influx.source": "arxiv",
            },
        ) as fetch_span:
            try:
                if cache is not None:
                    items = await cache.get_or_fetch(cache_key, _do_fetch)
                else:
                    items = await _do_fetch()
            except NetworkError as exc:
                _log.warning(
                    "arxiv fetch failed for profile %r; yielding zero items",
                    profile,
                    exc_info=True,
                )
                # Surface to the run ledger so a degraded run is no longer
                # indistinguishable from a quiet window (issue #20).
                record_source_acquisition_error(
                    source="arxiv",
                    kind=exc.kind or "unknown",
                    detail=str(exc),
                )
                return ()
            fetch_span.set_attribute("influx.item_count", len(items))
            _log.info(
                "arxiv fetch completed profile=%s kind=%s items=%d",
                profile,
                kind.value,
                len(items),
            )

        # Batched LLM filter takes precedence as the production default.
        # The per-item ``scorer`` seam stays available for tests that
        # want deterministic, synchronous scoring without an LLM.
        #
        # A failed filter batch is skipped per FR-FLT-6. Items returned
        # by the filter but below the profile relevance threshold are
        # also discarded per FR-FLT-7.
        batch_scores: dict[str, ArxivScoreResult] = {}
        filter_failed = False
        if scorer is None and filter_scorer is not None:
            # ── Telemetry: influx.filter span (FR-OBS-4) ──
            _tracer = get_tracer()
            with _tracer.span(
                "influx.filter",
                attributes={
                    "influx.profile": profile,
                    "influx.run_id": current_run_id.get() or "",
                    "influx.item_count": len(items),
                },
            ):
                # ``filter.batch_size`` caps how many candidates are sent
                # to the LLM filter in a single request so the configured
                # tunable actually shapes runtime behaviour (AC-X-1).
                batch_size = max(int(config.filter.batch_size), 1)
                for chunk_start in range(0, len(items), batch_size):
                    chunk = items[chunk_start : chunk_start + batch_size]
                    _log.info(
                        "arxiv filter batch started profile=%s batch_start=%d "
                        "batch_size=%d",
                        profile,
                        chunk_start,
                        len(chunk),
                    )
                    try:
                        chunk_scores = await filter_scorer(
                            chunk, profile, filter_prompt
                        )
                    except FilterScorerError:
                        _log.warning(
                            "filter_scorer failed for profile %r; skipping batch",
                            profile,
                            exc_info=True,
                        )
                        filter_failed = True
                        break
                    batch_scores.update(chunk_scores)
                    _log.info(
                        "arxiv filter batch completed profile=%s batch_start=%d "
                        "batch_size=%d scores_returned=%d",
                        profile,
                        chunk_start,
                        len(chunk),
                        len(chunk_scores),
                    )

        results: list[dict[str, Any]] = []
        for arxiv_item in items:
            if scorer is not None:
                score_result: ArxivScoreResult | None = scorer(arxiv_item, profile)
                if score_result is None:
                    _log.info(
                        "article inspected source=arxiv profile=%s arxiv_id=%s "
                        "published=%s score=none decision=drop reason=scorer_none "
                        "title=%r",
                        profile,
                        arxiv_item.arxiv_id,
                        arxiv_item.published.isoformat(),
                        arxiv_item.title,
                    )
                    continue
            elif filter_scorer is not None:
                if filter_failed:
                    _log.info(
                        "article inspected source=arxiv profile=%s arxiv_id=%s "
                        "published=%s score=none decision=drop reason=filter_failed "
                        "title=%r",
                        profile,
                        arxiv_item.arxiv_id,
                        arxiv_item.published.isoformat(),
                        arxiv_item.title,
                    )
                    continue
                elif arxiv_item.arxiv_id not in batch_scores:
                    # Items absent from the LLM filter response are
                    # dropped entirely — the filter explicitly chose not
                    # to score them (typically because they fell below
                    # ``filter.min_score_in_results``).
                    _log.info(
                        "article inspected source=arxiv profile=%s arxiv_id=%s "
                        "published=%s score=none decision=drop "
                        "reason=not_returned_by_filter title=%r",
                        profile,
                        arxiv_item.arxiv_id,
                        arxiv_item.published.isoformat(),
                        arxiv_item.title,
                    )
                    continue
                else:
                    score_result = batch_scores[arxiv_item.arxiv_id]
            else:
                _log.info(
                    "article inspected source=arxiv profile=%s arxiv_id=%s "
                    "published=%s score=none decision=drop reason=no_scorer "
                    "title=%r",
                    profile,
                    arxiv_item.arxiv_id,
                    arxiv_item.published.isoformat(),
                    arxiv_item.title,
                )
                continue

            if score_result.score < thresholds.relevance:
                _log.info(
                    "article inspected source=arxiv profile=%s arxiv_id=%s "
                    "published=%s score=%d threshold=%d decision=drop "
                    "reason=below_relevance title=%r",
                    profile,
                    arxiv_item.arxiv_id,
                    arxiv_item.published.isoformat(),
                    score_result.score,
                    thresholds.relevance,
                    arxiv_item.title,
                )
                continue

            _log.info(
                "article inspected source=arxiv profile=%s arxiv_id=%s "
                "published=%s score=%d threshold=%d decision=accept title=%r",
                profile,
                arxiv_item.arxiv_id,
                arxiv_item.published.isoformat(),
                score_result.score,
                thresholds.relevance,
                arxiv_item.title,
            )
            results.append(
                build_arxiv_note_item(
                    item=arxiv_item,
                    score=score_result.score,
                    confidence=score_result.confidence,
                    reason=score_result.reason,
                    profile_name=profile,
                    config=config,
                    filter_tags=score_result.filter_tags,
                )
            )

        _log.info(
            "arxiv source completed profile=%s fetched=%d accepted=%d",
            profile,
            len(items),
            len(results),
        )
        return results

    return provider
