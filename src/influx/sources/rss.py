"""RSS/Atom feed parser and note builder (PRD 09 FR-SRC-4, FR-SRC-5).

Parses RSS 2.0 and Atom feeds, yielding per-item records that carry the
feed's configured ``source_tag`` verbatim.  Each parsed item includes
``title``, ``url``, ``published``, and ``summary``.

The parser does not infer or override ``source_tag`` — it flows through
from the feed configuration unchanged so that downstream archive layout,
note paths, and ``source:*`` tags all agree.

``build_rss_note_item`` constructs a complete ``ProfileItem`` dict for the
scheduler by downloading the article HTML via the guarded HTTP client
(FR-SRC-5 / FR-RES-4) and archiving it on disk.  After archiving, the
builder attempts web article extraction via ``extract_article``; when the
extracted body is shorter than ``extraction.min_web_chars`` (or extraction
fails), the feed item's ``<summary>`` is used instead (FR-ENR-3, AC-09-J).
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import feedparser

from influx import metrics
from influx.archive_policy import (
    registry_from_config as _archive_policy_registry_from_config,
)
from influx.archive_policy import (
    tag_for_failure_kind as _tag_for_archive_failure_kind,
)
from influx.cascade import Acquired, Cascade
from influx.coordinator import RunKind
from influx.errors import ExtractionError, NetworkError
from influx.extraction.article import extract_article
from influx.filter import FilterScorerError, _acall_filter_model_with_retry
from influx.http_client import aguarded_fetch as _aguarded_fetch
from influx.renderer import render
from influx.slugs import slugify_feed_name
from influx.source import BoundScoredCandidate, Candidate, ScoredCandidate
from influx.storage import download_archive
from influx.telemetry import (
    current_run_id,
    get_tracer,
    record_fetched_items,
    record_filter_error,
    record_invalid_url_rejection,
    record_source_acquisition_error,
)
from influx.urls import classify_article_url, normalise_url, url_hash

if TYPE_CHECKING:
    from influx.config import AppConfig, ProfileConfig, RssSourceEntry
    from influx.sources import FetchCache

__all__ = [
    "RssFeedItem",
    "RssSource",
    "build_rss_note_item",
    "make_rss_item_provider",
    "parse_feed",
]

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RssFeedItem:
    """A single parsed item from an RSS/Atom feed."""

    title: str
    url: str
    published: datetime
    summary: str
    source_tag: str
    feed_name: str


def parse_feed(
    content: bytes | str,
    feed_entry: RssSourceEntry,
    *,
    allow_private_ips: bool = False,
    profile: str = "",
) -> list[RssFeedItem]:
    """Parse RSS/Atom feed content and return per-item records.

    Each item inherits the feed's configured ``source_tag`` verbatim
    (FR-SRC-4).  The parser does not infer or override ``source_tag``.

    Issue #131: each entry's ``link`` is validated via
    :func:`influx.urls.classify_article_url` before the item is
    admitted.  Items whose URL is loopback / private / link-local /
    multicast / malformed / disallowed-scheme are dropped pre-acquire
    (logged at WARNING, counted on the run-ledger entry, no
    ``influx:archive-missing`` note produced).  Each rejection also
    bumps the per-run pre-filter ``fetched_total`` so a feed that
    returned items but had every URL rejected is not misclassified as
    ``fetch_stall`` (review concern 2 on PR #133).

    ``allow_private_ips`` is intentionally **not** threaded from
    ``config.security.allow_private_ips`` by the production
    :func:`_fetch_rss_feed` caller — that flag is the fetcher's
    escape hatch for *configured* internal services and must not
    accidentally re-allow third-party feeds to advertise localhost
    links.  It exists here purely for direct test callers (e.g. the
    ``FakeArticleServer`` integration tests in
    ``tests/integration/test_rss_to_lithos.py``).

    Parameters
    ----------
    content:
        Raw feed XML (bytes or str).
    feed_entry:
        The ``RssSourceEntry`` from the profile config providing
        ``name``, ``url``, and ``source_tag``.
    allow_private_ips:
        Test-only opt-in to accept loopback / private / link-local /
        multicast hosts.  Production callers leave this at ``False``.
    profile:
        Profile name used as a metric label on rejection counts.

    Returns
    -------
    list[RssFeedItem]
        Parsed items sorted by published date (newest first).
    """
    feed = feedparser.parse(content)

    items: list[RssFeedItem] = []
    for entry in feed.entries:
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("link", "")).strip()
        summary = str(entry.get("summary", "")).strip()

        if not title or not link:
            _log.debug("Skipping feed entry with missing title or link")
            continue

        validation = classify_article_url(link, allow_private=allow_private_ips)
        if not validation.ok:
            _log.warning(
                "rss item rejected pre-acquire profile=%s feed=%r url=%s "
                "reason=%s title=%r",
                profile,
                feed_entry.name,
                link,
                validation.reason,
                title,
            )
            record_invalid_url_rejection()
            # Issue #131 review concern 2: count the rejected entry
            # toward the run's pre-filter ``fetched_total`` so a feed
            # that returned items with every URL invalid is not
            # misclassified as ``fetch_stall`` (which means "no source
            # returned anything at all").  The ledger uses
            # ``invalid_url_rejections_total`` to fire
            # ``invalid_url_stall`` instead when warranted.
            record_fetched_items(1)
            metrics.rss_items_rejected_invalid_url().add(
                1,
                {
                    "profile": profile,
                    "source": "rss",
                    "reason": validation.reason or "unknown",
                },
            )
            continue

        published = _parse_published(entry)

        items.append(
            RssFeedItem(
                title=title,
                url=link,
                published=published,
                summary=summary,
                source_tag=feed_entry.source_tag,
                feed_name=feed_entry.name,
            )
        )

    items.sort(key=lambda it: it.published, reverse=True)
    return items


def _parse_published(entry: Any) -> datetime:
    """Extract published datetime from a feed entry.

    Feedparser returns time tuples in UTC, so the conversion uses
    :func:`calendar.timegm` (UTC) rather than :func:`time.mktime` (local
    time).  Using ``mktime`` would shift entries authored near midnight
    UTC by the host's UTC offset, sending them to the wrong
    ``{YYYY-MM-DD}`` archive segment / note path bucket.

    Falls back to ``updated_parsed`` and then ``datetime.now(UTC)`` when
    the entry lacks a parseable date.
    """
    for attr in ("published_parsed", "updated_parsed"):
        parsed = entry.get(attr)
        if parsed is not None:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
            except (TypeError, ValueError, OverflowError):
                continue

    return datetime.now(UTC)


# ── Note item builder (PRD 09 US-003) ──────────────────────────────


def build_rss_note_item(
    *,
    item: RssFeedItem,
    profile_name: str,
    config: AppConfig,
    score: int = 0,
    confidence: float = 0.0,
    reason: str = "",
    filter_tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a complete ``ProfileItem`` dict for an RSS feed item.

    Downloads the article HTML via the guarded HTTP client (FR-SRC-5),
    archives it on disk under the PRD 09 archive layout, and renders a
    canonical note with the ``source:*`` tag and note path matching the
    feed's ``source_tag``.

    Parameters
    ----------
    item:
        Parsed RSS/Atom feed item.
    profile_name:
        Profile name for the ``profile:*`` tag.
    config:
        Loaded :class:`~influx.config.AppConfig`.

    Returns
    -------
    dict[str, Any]
        Ready-to-yield ``ProfileItem`` dict.
    """
    feed_slug = slugify_feed_name(item.feed_name)
    hash_val = url_hash(item.url)
    pub = item.published

    # item_id matches PRD 09 FR-ST-1:
    # {feed-slug}-{YYYY-MM-DD}-{url-hash}
    item_id = f"{feed_slug}-{pub.year:04d}-{pub.month:02d}-{pub.day:02d}-{hash_val}"

    archive_root = Path(config.storage.archive_dir)

    # ── Acquire stage (RSS-specific) ──────────────────────────────
    # Download and archive the article HTML (FR-SRC-5 / FR-RES-4).
    # Issue #149: build the per-domain archive policy registry from
    # config; ``download_archive`` short-circuits skip-mode policies
    # and tags blocked / rate-limited failures distinctly so the
    # repair sweep does not tight-loop on doomed paths.
    archive_policy_registry = _archive_policy_registry_from_config(
        config.storage.archive_policy
    )
    tracer = get_tracer()
    with tracer.span(
        "influx.archive.download",
        attributes={
            "influx.profile": profile_name,
            "influx.run_id": current_run_id.get() or "",
            "influx.source": item.source_tag,
        },
    ):
        archive_result = download_archive(
            url=item.url,
            archive_root=archive_root,
            source=item.source_tag,
            item_id=item_id,
            published_year=pub.year,
            published_month=pub.month,
            ext=".html",
            allow_private_ips=config.security.allow_private_ips,
            max_download_bytes=config.storage.max_download_bytes,
            timeout_seconds=config.storage.download_timeout_seconds,
            expected_content_type="html",
            policy_registry=archive_policy_registry,
        )

    archive_path = archive_result.rel_posix_path
    archive_missing = not archive_result.ok
    # Issue #149: extract the policy-driven tag (if any) so the tag
    # composition below can apply ``influx:archive-blocked`` /
    # ``influx:archive-rate-limited`` /
    # ``influx:archive-skipped-by-policy`` alongside
    # ``influx:archive-missing``.  Returns ``None`` for generic
    # failures (http_404, oversize, timeout, …) so the existing
    # ``influx:archive-missing`` shape is preserved.
    archive_policy_tag = (
        _tag_for_archive_failure_kind(
            archive_result.failure_kind  # type: ignore[arg-type]
        )
        if archive_missing
        else None
    )
    if archive_missing:
        metrics.archive_policy_failures().add(
            1,
            {
                "profile": profile_name,
                "source": item.source_tag,
                "kind": archive_result.failure_kind or "unknown",
            },
        )

    # Article text extraction with summary fallback (FR-ENR-3): try web
    # article extraction; fall back to the feed item's ``<summary>``
    # when the extracted body is below ``min_web_chars`` or extraction
    # fails entirely (AC-09-J).  Tier 1 always uses the feed summary
    # as its abstract input; Tier 2/3 use the extracted body if present.
    extracted_text: str | None = None
    summary = item.summary
    try:
        extraction = extract_article(
            item.url,
            min_web_chars=config.extraction.min_web_chars,
            strip_tags=config.extraction.strip_tags,
            allow_private_ips=config.security.allow_private_ips,
            max_download_bytes=config.storage.max_download_bytes,
            timeout_seconds=config.storage.download_timeout_seconds,
        )
        summary = extraction.text
        extracted_text = extraction.text
    except (ExtractionError, NetworkError) as exc:
        _log.debug(
            "Article extraction failed for %s, using feed summary: %s",
            item.url,
            exc,
        )

    source_url = normalise_url(item.url)
    profile_cfg = next((p for p in config.profiles if p.name == profile_name), None)

    acquired = Acquired(
        item_id=item_id,
        source_url=source_url,
        title=item.title,
        # Tier 1 uses the raw feed summary as its abstract input,
        # regardless of extraction success (preserves prior behaviour).
        abstract=item.summary,
        identity_tags=(f"feed-slug:{feed_slug}",),
        archive_path=archive_path,
        archive_missing=archive_missing,
        extracted_text=extracted_text,
        # ``summary-fallback`` records that the feed body was the source
        # of the rendered text, distinguishing it from ``html``-extracted
        # bodies.  Used by future telemetry / Renderer rules.
        text_flavour="html" if extracted_text is not None else "summary-fallback",
    )

    # ── Cascade ───────────────────────────────────────────────────
    # ``ProfileThresholds`` defaults gate every stage off when no
    # profile config is found, mirroring the prior RSS behaviour where
    # an unknown profile produced an unenriched note.
    from influx.config import ProfileThresholds

    cascade = Cascade(
        config=config,
        profile_name=profile_name,
        profile_summary=profile_cfg.description if profile_cfg else "",
        thresholds=profile_cfg.thresholds if profile_cfg else ProfileThresholds(),
        # RSS pre-populates ``Acquired.extracted_text`` at acquire time,
        # so the cascade does not need a Tier-2 extractor seam.
        tier2_extractor=None,
    )
    sections = cascade.enrich(acquired, score)

    # ── Tag composition ──────────────────────────────────────────
    # RSS does not stamp a ``text:*`` provenance tag (canonical
    # convention preserved from PRD 09).  Otherwise the tag-list shape
    # mirrors arXiv's: provenance + identity tags first, archive
    # repair flags next, then cascade-driven tags.
    tags: list[str] = [
        f"profile:{profile_name}",
        f"source:{item.source_tag}",
        f"feed-slug:{feed_slug}",
        "ingested-by:influx",
        f"schema:{config.influx.note_schema_version}",
    ]
    if archive_missing:
        tags.append("influx:archive-missing")
    # Issue #149: domain-policy tag (blocked / rate-limited /
    # skipped-by-policy) — applied alongside ``influx:archive-missing``
    # so existing repair-sweep selectors keep working, but the
    # diagnostic shape is now visible in the tag list.  For ``blocked``
    # and ``missing_by_policy`` (skip) the note is also flipped to
    # ``influx:archive-terminal`` so the repair sweep does NOT
    # tight-loop on a doomed path (this is the staging-log behaviour
    # we are fixing).  ``rate_limited`` retries on cool-down, so it
    # stays in the normal repair-needed loop.
    if archive_policy_tag is not None and archive_policy_tag not in tags:
        tags.append(archive_policy_tag)
    if (
        archive_policy_tag
        in ("influx:archive-blocked", "influx:archive-skipped-by-policy")
        and "influx:archive-terminal" not in tags
    ):
        tags.append("influx:archive-terminal")
    if sections.full_text is not None:
        tags.append("full-text")
    if archive_missing and "influx:repair-needed" not in tags:
        tags.append("influx:repair-needed")
    if sections.tier3 is not None:
        tags.append("influx:deep-extracted")
    for flag in sections.repair_flags:
        if flag not in tags:
            tags.append(flag)
    for flag in sections.terminal_flags:
        if flag not in tags:
            tags.append(flag)

    # Note storage path: articles/{source_tag}/{YYYY}/{MM} (FR-NOTE-2)
    path = f"articles/{item.source_tag}/{pub.year}/{pub.month:02d}"

    # Suppress the plain-text summary when Tier 1 was attempted but
    # failed (AC-07-A / FR-ENR-6).  When Tier 1 was not attempted
    # (e.g. score below threshold), keep the extracted/feed summary so
    # ``## Summary`` renders the fallback body.
    summary_for_note = (
        "" if sections.tier1_attempted and sections.tier1 is None else summary
    )

    content = render(
        title=item.title,
        source_url=source_url,
        tags=tags,
        confidence=confidence,
        archive_path=archive_path,
        summary=summary_for_note,
        profile_name=profile_name,
        score=score,
        reason=reason,
        tier1_enrichment=sections.tier1,
        full_text=sections.full_text,
        tier3_extraction=sections.tier3,
    )

    return {
        "id": f"rss-{feed_slug}-{hash_val}",
        "title": item.title,
        "source": "rss",
        "source_url": source_url,
        "content": content,
        "tags": tags,
        "filter_tags": list(filter_tags) if filter_tags is not None else [],
        "score": score,
        "confidence": confidence,
        "reason": reason,
        "path": path,
        "abstract_or_summary": summary,
        "contributions": sections.tier1.contributions if sections.tier1 else None,
        "builds_on": list(sections.tier3.builds_on) if sections.tier3 else None,
    }


async def _score_rss_items(
    *,
    items: list[RssFeedItem],
    profile: str,
    filter_prompt: str,
    config: AppConfig,
) -> dict[str, Any]:
    """Score RSS items with the configured relevance filter."""
    if not items:
        return {}
    slot = config.models.get("filter")
    if slot is None:
        raise FilterScorerError("models.filter is not configured")
    provider = config.providers.get(slot.provider)
    if provider is None:
        raise FilterScorerError(f"filter provider {slot.provider!r} not configured")

    headers: dict[str, str] = {**provider.extra_headers}
    if provider.api_key_env:
        api_key = os.environ.get(provider.api_key_env, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    url = f"{provider.base_url.rstrip('/')}/chat/completions"
    attempts = slot.max_retries + 1

    all_scores: dict[str, Any] = {}
    batch_size = max(int(config.filter.batch_size), 1)
    for chunk_start in range(0, len(items), batch_size):
        chunk = items[chunk_start : chunk_start + batch_size]
        candidates = [
            {"id": _rss_filter_id(item), "title": item.title, "abstract": item.summary}
            for item in chunk
        ]
        body: dict[str, Any] = {
            "model": slot.model,
            "temperature": slot.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"{filter_prompt}\n\n## CANDIDATES\n"
                        f"{json.dumps(candidates, ensure_ascii=False)}"
                    ),
                }
            ],
        }
        if slot.max_tokens is not None:
            body["max_tokens"] = slot.max_tokens
        if slot.json_mode:
            body["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for _attempt in range(attempts):
            try:
                response = await _acall_filter_model_with_retry(
                    config=config,
                    profile=profile,
                    url=url,
                    body=body,
                    headers=headers,
                    attempts=attempts,
                )
                all_scores.update({result.id: result for result in response.results})
                break
            except Exception as exc:
                last_error = exc
                continue
        else:
            exc_info = None
            if last_error is not None:
                exc_info = (type(last_error), last_error, last_error.__traceback__)
            _log.warning(
                "RSS filter failed for profile %r items %d-%d; skipping chunk",
                profile,
                chunk_start,
                chunk_start + len(chunk) - 1,
                exc_info=exc_info,
            )
            record_filter_error()

    return all_scores


def _rss_filter_id(item: RssFeedItem) -> str:
    return url_hash(item.url)


# ── Source adapter (issue #57) ──────────────────────────────────────


class RssSource:
    """RSS adapter conforming to :class:`influx.source.Source`.

    Fans out across the profile's configured feeds in
    :meth:`fetch_candidates`; per-feed filter scoring stays in the
    legacy provider closure so :func:`_score_rss_items` retains its
    test seam.
    """

    name = "rss"

    def __init__(
        self,
        config: AppConfig,
        *,
        fetch_cache: FetchCache | None = None,
    ) -> None:
        self._config = config
        self._cache = fetch_cache

    async def fetch_candidates(
        self,
        *,
        profile_cfg: ProfileConfig,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
    ) -> list[Candidate]:
        """Fetch every configured feed and flatten into a list of Candidates.

        Each :class:`Candidate.payload` is the original
        :class:`RssFeedItem`, so :meth:`acquire` can reconstruct the
        feed-specific metadata (source_tag, feed_name) without a
        secondary lookup.
        """
        del kind, run_range
        config = self._config
        candidates: list[Candidate] = []
        for feed_entry in profile_cfg.sources.rss:
            items = await _fetch_rss_feed(
                feed_entry,
                self._cache,
                max_download_bytes=config.storage.max_download_bytes,
                timeout_seconds=config.storage.download_timeout_seconds,
                profile=profile_cfg.name,
            )
            metrics.candidates_fetched().add(
                len(items), {"profile": profile_cfg.name, "source": "rss"}
            )
            # #85: per-feed pre-filter count.  Multiple feeds on one
            # profile accumulate.  Sum-of-feeds is the correct
            # ``fetched_total`` for filter_stall discrimination — what
            # matters is whether ANY items reached the filter, not
            # which feed they came from.
            record_fetched_items(len(items))
            for item in items:
                candidates.append(
                    Candidate(
                        item_id=_rss_filter_id(item),
                        title=item.title,
                        abstract=item.summary,
                        source_url=item.url,
                        payload=item,
                    )
                )
        return candidates

    def acquire(
        self,
        scored: ScoredCandidate,
        *,
        profile_cfg: ProfileConfig,
        config: AppConfig,
    ) -> dict[str, Any]:
        """Acquire stage: download archive + run cascade + render note."""
        item = scored.candidate.payload
        if not isinstance(item, RssFeedItem):
            raise TypeError(
                "RssSource.acquire requires Candidate.payload to be RssFeedItem; "
                f"got {type(item).__name__}",
            )
        return build_rss_note_item(
            item=item,
            profile_name=profile_cfg.name,
            config=config,
            score=scored.score,
            confidence=scored.confidence,
            reason=scored.reason,
            filter_tags=scored.filter_tags,
        )


# ── Production-default RSS item provider ────────────────────────────


def make_rss_item_provider(
    config: AppConfig,
    *,
    fetch_cache: FetchCache | None = None,
) -> Any:
    """Build the item provider for RSS feed profiles.

    Fetches each RSS feed configured for the profile, parses items,
    and maps each through :func:`build_rss_note_item`.

    Parameters
    ----------
    config:
        Loaded :class:`~influx.config.AppConfig`.
    fetch_cache:
        Optional shared :class:`~influx.sources.FetchCache` for
        per-fire dedup (R-8).  When two profiles share the same RSS
        feed URL the feed is fetched once and the result shared.
    """
    cache = fetch_cache

    async def provider(
        profile: str,
        kind: RunKind,
        run_range: dict[str, str | int] | None,
        filter_prompt: str,
    ) -> Iterable[BoundScoredCandidate]:
        del kind, run_range

        profile_cfg = next((p for p in config.profiles if p.name == profile), None)
        if profile_cfg is None:
            _log.info("rss source skipped profile=%s reason=unknown_profile", profile)
            return ()

        # ── Telemetry: influx.fetch.rss span (FR-OBS-4) ──
        _tracer = get_tracer()
        with _tracer.span(
            "influx.fetch.rss",
            attributes={
                "influx.profile": profile,
                "influx.run_id": current_run_id.get() or "",
                "influx.source": "rss",
            },
        ) as fetch_span:
            bounds: list[BoundScoredCandidate] = []
            for feed_entry in profile_cfg.sources.rss:
                _log.info(
                    "rss feed fetch started profile=%s feed=%r url=%s source_tag=%s",
                    profile,
                    feed_entry.name,
                    feed_entry.url,
                    feed_entry.source_tag,
                )
                items = await _fetch_rss_feed(
                    feed_entry,
                    cache,
                    max_download_bytes=config.storage.max_download_bytes,
                    timeout_seconds=config.storage.download_timeout_seconds,
                    profile=profile,
                )
                metrics.candidates_fetched().add(
                    len(items), {"profile": profile, "source": "rss"}
                )
                # #85: per-feed pre-filter count for the run-level
                # ``fetched_total`` (filter_stall vs fetch_stall split).
                record_fetched_items(len(items))
                _log.info(
                    "rss feed fetch completed profile=%s feed=%r items=%d",
                    profile,
                    feed_entry.name,
                    len(items),
                )
                try:
                    scores = await _score_rss_items(
                        items=items,
                        profile=profile,
                        filter_prompt=filter_prompt,
                        config=config,
                    )
                except FilterScorerError:
                    _log.warning(
                        "RSS filter failed for feed %r; skipping feed batch",
                        feed_entry.name,
                        exc_info=True,
                    )
                    # #85 review: see arxiv.py for rationale.  Per-feed
                    # failures each tick the counter so a profile with
                    # multiple feeds reflects the true partial-failure
                    # surface.
                    record_filter_error()
                    continue
                _log.info(
                    "rss filter completed profile=%s feed=%r items=%d "
                    "scores_returned=%d",
                    profile,
                    feed_entry.name,
                    len(items),
                    len(scores),
                )
                drop_attrs = {"profile": profile, "decision": "drop"}
                pass_attrs = {"profile": profile, "decision": "pass"}
                for item in items:
                    scored = scores.get(_rss_filter_id(item))
                    if scored is None:
                        metrics.articles_filtered().add(1, drop_attrs)
                        _log.info(
                            "article inspected source=rss profile=%s feed=%r "
                            "published=%s score=none decision=drop "
                            "reason=not_returned_by_filter title=%r url=%s",
                            profile,
                            feed_entry.name,
                            item.published.isoformat(),
                            item.title,
                            item.url,
                        )
                        continue
                    if scored.score < profile_cfg.thresholds.relevance:
                        metrics.articles_filtered().add(1, drop_attrs)
                        _log.info(
                            "article inspected source=rss profile=%s feed=%r "
                            "published=%s score=%d threshold=%d decision=drop "
                            "reason=below_relevance title=%r url=%s",
                            profile,
                            feed_entry.name,
                            item.published.isoformat(),
                            scored.score,
                            profile_cfg.thresholds.relevance,
                            item.title,
                            item.url,
                        )
                        continue
                    metrics.articles_filtered().add(1, pass_attrs)
                    _log.info(
                        "article inspected source=rss profile=%s feed=%r "
                        "published=%s score=%d threshold=%d decision=accept "
                        "title=%r url=%s",
                        profile,
                        feed_entry.name,
                        item.published.isoformat(),
                        scored.score,
                        profile_cfg.thresholds.relevance,
                        item.title,
                        item.url,
                    )
                    # Issue #125: emit a BoundScoredCandidate so the Run
                    # stage can run pre-acquire ``lithos_cache_lookup``
                    # before paying the article fetch / archive / extract
                    # cost.  ``build_rss_note_item`` is the per-item
                    # acquire entrypoint; issue #124's ``asyncio.to_thread``
                    # offload moves into the closure, fired only when the
                    # orchestrator decides this candidate must be acquired.
                    sc = ScoredCandidate(
                        candidate=Candidate(
                            item_id=_rss_filter_id(item),
                            title=item.title,
                            abstract=item.summary or "",
                            source_url=item.url,
                            payload=item,
                        ),
                        score=scored.score,
                        confidence=1.0,
                        reason=scored.reason,
                        filter_tags=scored.tags,
                    )

                    async def _acquire(
                        item: RssFeedItem = item,
                        scored: Any = scored,
                    ) -> dict[str, Any] | None:
                        return await asyncio.to_thread(
                            build_rss_note_item,
                            item=item,
                            profile_name=profile,
                            config=config,
                            score=scored.score,
                            confidence=1.0,
                            reason=scored.reason,
                            filter_tags=scored.tags,
                        )

                    bounds.append(
                        BoundScoredCandidate(
                            scored=sc,
                            acquire=_acquire,
                            source_label=f"rss:{feed_entry.name}",
                        )
                    )
                _log.info(
                    "rss feed completed profile=%s feed=%r accepted_so_far=%d",
                    profile,
                    feed_entry.name,
                    len(bounds),
                )
            fetch_span.set_attribute("influx.item_count", len(bounds))
            _log.info(
                "rss source completed profile=%s feeds=%d accepted=%d",
                profile,
                len(profile_cfg.sources.rss),
                len(bounds),
            )

        return bounds

    return provider


async def _fetch_rss_feed(
    feed_entry: RssSourceEntry,
    cache: FetchCache | None,
    *,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
    profile: str = "",
) -> list[RssFeedItem]:
    """Fetch raw feed bytes, then parse-and-stamp per *feed_entry*.

    The cache is keyed on the feed URL but stores **only** the raw
    response bytes — never the parsed :class:`RssFeedItem` list.  This
    matters because each parsed item embeds the caller's ``source_tag``
    and ``feed_name``: caching the parsed list would let a second profile
    that configured the same URL with different metadata receive items
    stamped with the FIRST profile's metadata, routing them to the wrong
    bucket / archive path (review finding 3).  Re-parsing per call keeps
    the network savings of dedup while preserving the FR-SRC-4 rule that
    each item inherits its feed's configured ``source_tag`` verbatim.
    """
    cache_key = f"rss-bytes:{feed_entry.url}"

    async def _fetch_bytes() -> bytes | None:
        try:
            result = await _aguarded_fetch(
                feed_entry.url,
                max_download_bytes=max_download_bytes,
                timeout_seconds=timeout_seconds,
            )
        except NetworkError as exc:
            _log.warning(
                "RSS feed fetch failed for %r; yielding zero items",
                feed_entry.name,
                exc_info=True,
            )
            # Issue #20: surface to the run ledger so the degraded
            # outcome is distinguishable from a quiet feed.
            record_source_acquisition_error(
                source="rss",
                kind=exc.kind or "unknown",
                detail=f"{feed_entry.name}: {exc}",
            )
            metrics.source_acquisition_errors().add(
                1,
                {
                    "profile": profile,
                    "source": "rss",
                    "kind": exc.kind or "unknown",
                },
            )
            return None
        return result.body

    if cache is not None:
        body = await cache.get_or_fetch(cache_key, _fetch_bytes)
    else:
        body = await _fetch_bytes()

    if body is None:
        return []

    # NB: ``allow_private_ips`` is deliberately NOT threaded from
    # ``config.security.allow_private_ips`` here.  That flag is the
    # fetcher's escape hatch for talking to *configured* internal
    # services; it must NOT be a "trust upstream feeds to advertise
    # localhost links" knob.  Issue #131: a staging config with
    # ``allow_private_ips = true`` would otherwise re-enable the very
    # behaviour this fix removes.  Direct test callers of
    # :func:`parse_feed` may still opt in.
    return parse_feed(body, feed_entry, profile=profile)
