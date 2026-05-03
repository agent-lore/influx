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
from influx.cascade import Acquired, Cascade
from influx.coordinator import RunKind
from influx.errors import ExtractionError, NetworkError
from influx.extraction.article import extract_article
from influx.filter import FilterScorerError
from influx.http_client import guarded_fetch as _guarded_fetch
from influx.http_client import guarded_post_json_fetch
from influx.renderer import render
from influx.schemas import FilterResponse
from influx.slugs import slugify_feed_name
from influx.storage import download_archive
from influx.telemetry import (
    current_run_id,
    get_tracer,
    record_source_acquisition_error,
)
from influx.urls import normalise_url, url_hash

if TYPE_CHECKING:
    from influx.config import AppConfig, RssSourceEntry
    from influx.sources import FetchCache

__all__ = [
    "RssFeedItem",
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
) -> list[RssFeedItem]:
    """Parse RSS/Atom feed content and return per-item records.

    Each item inherits the feed's configured ``source_tag`` verbatim
    (FR-SRC-4).  The parser does not infer or override ``source_tag``.

    Parameters
    ----------
    content:
        Raw feed XML (bytes or str).
    feed_entry:
        The ``RssSourceEntry`` from the profile config providing
        ``name``, ``url``, and ``source_tag``.

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
        )

    archive_path = archive_result.rel_posix_path
    archive_missing = not archive_result.ok

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

    candidates = [
        {"id": _rss_filter_id(item), "title": item.title, "abstract": item.summary}
        for item in items
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

    headers: dict[str, str] = {**provider.extra_headers}
    if provider.api_key_env:
        api_key = os.environ.get(provider.api_key_env, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    url = f"{provider.base_url.rstrip('/')}/chat/completions"
    attempts = slot.max_retries + 1
    last_error: Exception | None = None
    for _attempt in range(attempts):
        try:
            response = guarded_post_json_fetch(
                url,
                body,
                headers=headers,
                allow_private_ips=config.security.allow_private_ips,
                max_response_bytes=config.storage.max_download_bytes,
                timeout_seconds=slot.request_timeout,
            )
            if response.status_code >= 400:
                last_error = FilterScorerError(f"HTTP {response.status_code}")
                continue
            envelope = json.loads(response.body.decode("utf-8"))
            content = envelope["choices"][0]["message"]["content"]
            parsed = FilterResponse.model_validate(json.loads(content))
            return {result.id: result for result in parsed.results}
        except Exception as exc:
            last_error = exc
            continue
    raise FilterScorerError(f"RSS filter failed after {attempts} attempts") from (
        last_error
    )


def _rss_filter_id(item: RssFeedItem) -> str:
    return url_hash(item.url)


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
    ) -> Iterable[dict[str, Any]]:
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
            results: list[dict[str, Any]] = []
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
                    results.append(
                        build_rss_note_item(
                            item=item,
                            profile_name=profile,
                            config=config,
                            score=scored.score,
                            confidence=1.0,
                            reason=scored.reason,
                            filter_tags=scored.tags,
                        )
                    )
                _log.info(
                    "rss feed completed profile=%s feed=%r accepted_so_far=%d",
                    profile,
                    feed_entry.name,
                    len(results),
                )
            fetch_span.set_attribute("influx.item_count", len(results))
            _log.info(
                "rss source completed profile=%s feeds=%d accepted=%d",
                profile,
                len(profile_cfg.sources.rss),
                len(results),
            )

        return results

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
            result = _guarded_fetch(
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

    return parse_feed(body, feed_entry)
