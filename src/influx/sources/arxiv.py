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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from influx.config import (
    AppConfig,
    ArxivSourceConfig,
    ProfileThresholds,
    ResilienceConfig,
)
from influx.coordinator import RunKind
from influx.enrich import tier1_enrich, tier3_extract
from influx.errors import ExtractionError, LCMAError, NetworkError
from influx.extraction.pipeline import extract_arxiv_text
from influx.filter import FilterScorerError
from influx.http_client import guarded_fetch
from influx.notes import ProfileRelevanceEntry, render_note
from influx.schemas import Tier1Enrichment, Tier3Extraction

if TYPE_CHECKING:
    from influx.sources import FetchCache

__all__ = [
    "ArxivFilterScorer",
    "ArxivItem",
    "ArxivScorer",
    "ArxivScoreResult",
    "build_arxiv_note_item",
    "build_query_url",
    "fetch_arxiv",
    "make_arxiv_item_provider",
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
    """

    score: int
    confidence: float
    reason: str


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


# ── Query URL construction ─────────────────────────────────────────


def build_query_url(
    *,
    categories: list[str],
    max_results: int,
) -> str:
    """Build the arXiv API query URL per FR-SRC-1.

    Constructs ``search_query`` as an OR-joined expression
    (``cat:X+OR+cat:Y+...``), ``sortBy=submittedDate``,
    ``sortOrder=descending``, and ``max_results`` from the profile.
    """
    cat_expr = "+OR+".join(f"cat:{c}" for c in categories)
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

    Returns
    -------
    list[ArxivItem]
        Parsed and date-filtered items, newest first.
    """
    url = build_query_url(
        categories=arxiv_config.categories,
        max_results=arxiv_config.max_results_per_category,
    )

    body = _fetch_with_retry(
        url=url,
        resilience=resilience,
    )

    items = _parse_atom(body)
    return _filter_by_lookback(
        items,
        arxiv_config.lookback_days,
        now=now,
    )


def _fetch_with_retry(
    *,
    url: str,
    resilience: ResilienceConfig,
) -> bytes:
    """Fetch *url* with 429 backoff and exponential retry (FR-RES-1/2)."""
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
            result = guarded_fetch(url)
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
        "schema:v1",
        *cat_tags,
    ]

    # ── Extraction cascade ────────────────────────────────────────
    extracted_text: str | None = None
    text_tag = "text:abstract-only"
    repair_needed = False

    if score >= thresholds.full_text:
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

    # ── Tier 3 deep extraction (FR-ENR-5) ─────────────────────────
    tier3_result: Tier3Extraction | None = None
    if score >= thresholds.deep_extract and extracted_text is not None:
        try:
            tier3_result = tier3_extract(
                title=item.title,
                full_text=extracted_text,
                config=config,
            )
        except LCMAError:
            _log.warning("Tier 3 extraction failed for %s", item.arxiv_id)
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
        archive_path=None,
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
        del kind, run_range

        profile_cfg = next((p for p in config.profiles if p.name == profile), None)
        if profile_cfg is None:
            return ()
        if not profile_cfg.sources.arxiv.enabled:
            return ()

        # ── Cached fetch (R-8 dedup) ─────────────────────────────
        arxiv_cfg = profile_cfg.sources.arxiv
        cache_key = "arxiv:" + build_query_url(
            categories=arxiv_cfg.categories,
            max_results=arxiv_cfg.max_results_per_category,
        )
        if cache is not None and cache.has(cache_key):
            items = cache.get(cache_key)
        else:
            try:
                items = fetch_arxiv(
                    arxiv_config=profile_cfg.sources.arxiv,
                    resilience=config.resilience,
                )
            except NetworkError:
                _log.warning(
                    "arxiv fetch failed for profile %r; yielding zero items",
                    profile,
                    exc_info=True,
                )
                return ()
            if cache is not None:
                cache.put(cache_key, items)

        # Batched LLM filter takes precedence as the production default.
        # The per-item ``scorer`` seam stays available for tests that
        # want deterministic, synchronous scoring without an LLM.
        #
        # Distinguish two filter-scorer outcomes:
        #   - returned a (possibly empty) mapping → the filter ran and
        #     intentionally omitted any items missing from the mapping
        #     (typically because they fell below
        #     ``filter.min_score_in_results``).  Drop those items.
        #   - raised ``FilterScorerError`` → the filter could not produce
        #     a scoring decision at all (provider misconfigured, HTTP
        #     failure, parse failure).  Fall every item back to
        #     abstract-only ingestion so the run still produces notes
        #     (PRD 07 §5.6 graceful degradation).
        batch_scores: dict[str, ArxivScoreResult] = {}
        filter_failed = False
        if scorer is None and filter_scorer is not None:
            try:
                batch_scores = await filter_scorer(items, profile, filter_prompt)
            except FilterScorerError:
                _log.warning(
                    "filter_scorer failed for profile %r; "
                    "falling back to abstract-only ingestion for entire batch",
                    profile,
                    exc_info=True,
                )
                filter_failed = True

        results: list[dict[str, Any]] = []
        for arxiv_item in items:
            if scorer is not None:
                score_result: ArxivScoreResult | None = scorer(arxiv_item, profile)
                if score_result is None:
                    continue
            elif filter_scorer is not None:
                if filter_failed:
                    # The filter call hard-failed for the whole batch —
                    # write each item abstract-only (score=0) instead of
                    # dropping the run.
                    score_result = ArxivScoreResult(
                        score=0,
                        confidence=0.0,
                        reason="filter-scorer-failed",
                    )
                elif arxiv_item.arxiv_id not in batch_scores:
                    # Items absent from the LLM filter response are
                    # dropped entirely — the filter explicitly chose not
                    # to score them (typically because they fell below
                    # ``filter.min_score_in_results``).
                    continue
                else:
                    score_result = batch_scores[arxiv_item.arxiv_id]
            else:
                score_result = ArxivScoreResult(
                    score=0,
                    confidence=0.0,
                    reason="no-scorer-configured",
                )

            results.append(
                build_arxiv_note_item(
                    item=arxiv_item,
                    score=score_result.score,
                    confidence=score_result.confidence,
                    reason=score_result.reason,
                    profile_name=profile,
                    config=config,
                )
            )

        return results

    return provider
