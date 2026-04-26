"""RSS/Atom feed parser and note builder (PRD 09 FR-SRC-4, FR-SRC-5).

Parses RSS 2.0 and Atom feeds, yielding per-item records that carry the
feed's configured ``source_tag`` verbatim.  Each parsed item includes
``title``, ``url``, ``published``, and ``summary``.

The parser does not infer or override ``source_tag`` — it flows through
from the feed configuration unchanged so that downstream archive layout,
note paths, and ``source:*`` tags all agree.

``build_rss_note_item`` constructs a complete ``ProfileItem`` dict for the
scheduler by downloading the article HTML via the guarded HTTP client
(FR-SRC-5 / FR-RES-4) and archiving it on disk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import mktime
from typing import TYPE_CHECKING, Any

import feedparser

from influx.notes import ProfileRelevanceEntry, render_note
from influx.slugs import slugify_feed_name
from influx.storage import download_archive
from influx.urls import normalise_url, url_hash

if TYPE_CHECKING:
    from influx.config import AppConfig, RssSourceEntry

__all__ = [
    "RssFeedItem",
    "build_rss_note_item",
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

    Falls back to ``updated_parsed`` and then ``datetime.now(UTC)`` when
    the entry lacks a parseable date.
    """
    for attr in ("published_parsed", "updated_parsed"):
        parsed = entry.get(attr)
        if parsed is not None:
            try:
                return datetime.fromtimestamp(mktime(parsed), tz=UTC)
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
        summary=item.summary,
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
        "abstract_or_summary": item.summary,
    }
