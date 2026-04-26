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
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import feedparser

from influx.coordinator import RunKind
from influx.errors import ExtractionError, NetworkError
from influx.extraction.article import extract_article
from influx.http_client import guarded_fetch as _guarded_fetch
from influx.notes import ProfileRelevanceEntry, render_note
from influx.slugs import slugify_feed_name
from influx.storage import download_archive
from influx.telemetry import current_run_id, get_tracer
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

    # ── Article text extraction with summary fallback (FR-ENR-3) ────
    # Attempt web article extraction; fall back to the feed item's
    # <summary> when the extracted body is below min_web_chars or
    # extraction fails entirely (AC-09-J).
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
    except (ExtractionError, NetworkError) as exc:
        _log.debug(
            "Article extraction failed for %s, using feed summary: %s",
            item.url,
            exc,
        )

    tags: list[str] = [
        f"profile:{profile_name}",
        f"source:{item.source_tag}",
        f"feed-slug:{feed_slug}",
        "ingested-by:influx",
        "schema:v1",
    ]

    if not archive_result.ok:
        tags.append("influx:archive-missing")
        tags.append("influx:repair-needed")

    # Note storage path: articles/{source_tag}/{YYYY}/{MM} (FR-NOTE-2)
    path = f"articles/{item.source_tag}/{pub.year}/{pub.month:02d}"

    source_url = normalise_url(item.url)

    profile_entries = [
        ProfileRelevanceEntry(
            profile_name=profile_name,
            score=0,
            reason="",
        ),
    ]

    content = render_note(
        title=item.title,
        source_url=source_url,
        tags=tags,
        confidence=0.0,
        archive_path=archive_path,
        summary=summary,
        keywords=[],
        profile_entries=profile_entries,
    )

    return {
        "id": f"rss-{feed_slug}-{hash_val}",
        "title": item.title,
        "source_url": source_url,
        "content": content,
        "tags": tags,
        "score": 0,
        "confidence": 0.0,
        "reason": "",
        "path": path,
        "abstract_or_summary": summary,
    }


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
        del kind, run_range, filter_prompt

        profile_cfg = next((p for p in config.profiles if p.name == profile), None)
        if profile_cfg is None:
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
                items = await _fetch_rss_feed(
                    feed_entry,
                    cache,
                    max_download_bytes=config.storage.max_download_bytes,
                    timeout_seconds=config.storage.download_timeout_seconds,
                )
                for item in items:
                    results.append(
                        build_rss_note_item(
                            item=item,
                            profile_name=profile,
                            config=config,
                        )
                    )
            fetch_span.set_attribute("influx.item_count", len(results))

        return results

    return provider


async def _fetch_rss_feed(
    feed_entry: RssSourceEntry,
    cache: FetchCache | None,
    *,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
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
        except NetworkError:
            _log.warning(
                "RSS feed fetch failed for %r; yielding zero items",
                feed_entry.name,
                exc_info=True,
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
