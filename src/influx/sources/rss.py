"""RSS/Atom feed parser using ``feedparser`` (PRD 09 FR-SRC-4).

Parses RSS 2.0 and Atom feeds, yielding per-item records that carry the
feed's configured ``source_tag`` verbatim.  Each parsed item includes
``title``, ``url``, ``published``, and ``summary``.

The parser does not infer or override ``source_tag`` — it flows through
from the feed configuration unchanged so that downstream archive layout,
note paths, and ``source:*`` tags all agree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import mktime
from typing import TYPE_CHECKING, Any

import feedparser

if TYPE_CHECKING:
    from influx.config import RssSourceEntry

__all__ = [
    "RssFeedItem",
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
