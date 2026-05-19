"""URL-derived source-kind classification (issue #160).

Influx archives RSS-discovered articles by sending the entry URL through
the guarded HTTP client with ``expected_content_type="html"``.  In
production this catches a class of upstream feeds that advertise
``<link>`` URLs which point at non-article resources:

* **XML/feed pointers** — e.g. ``https://csdb.dk/rss/upcomingevents.php``.
  The entry link is itself another RSS endpoint; the archive fetch
  succeeds but the response is ``application/xml`` and fails the HTML
  content-type guard, producing a ``content_type_mismatch``.
* **Discussion pointers** — e.g. ``https://news.ycombinator.com/item?id=...``.
  Some clients receive a ``text/plain`` response from these endpoints,
  again failing the HTML content-type guard.

Both shapes look identical to genuine archive failures
(``influx:archive-missing`` → ``archive_acquisition`` degraded reason)
even though they represent the source-kind never being HTML to begin
with.  This module supplies a *cheap, purely-syntactic* classifier so
the archive layer can short-circuit these URLs with a dedicated
``non_html_source`` failure kind rather than tight-looping on the HTML
acquisition path.

The classifier deliberately does **no** network IO and no DNS
resolution — it inspects the URL shape only.  A URL whose host/path
pattern doesn't match a known non-HTML signature falls through to
``"html"`` (the default), so behaviour is preserved for every URL that
isn't on the known-bad list.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

__all__ = [
    "SourceKind",
    "classify_source_kind",
]


# ── Source-kind taxonomy ──────────────────────────────────────────────

# Stable discriminator for the URL's likely content shape.  Only the
# non-``"html"`` values short-circuit the HTML archive path; ``"html"``
# is the no-op default.
SourceKind = Literal["html", "xml", "pointer"]


# ── Pattern rules ─────────────────────────────────────────────────────

# Path patterns that indicate the URL points at an XML/RSS/Atom feed
# rather than an HTML article.  Matched against the lowercased path:
#
# * trailing ``.xml`` / ``.rss`` / ``.atom`` file extensions,
# * a segment named ``rss`` / ``atom`` / ``feed`` (with or without a
#   trailing extension like ``.php`` / ``.cgi`` / ``.aspx``),
# * a trailing path segment ``rss`` / ``atom`` / ``feed`` (no extension).
#
# Anchored conservatively so an article path like ``/posts/feedback``
# does not accidentally match ``feed``.
_XML_PATH_RE = re.compile(
    r"(?:^|/)(?:rss|atom|feed)(?:\.(?:php|cgi|aspx|xml|html?))?(?:/|$)"
    r"|\.(?:xml|rss|atom)$"
)


# Hostname → URL-path predicate that classifies the URL as a pointer
# (discussion / aggregator page) rather than an article body.  Today
# this only matches Hacker News ``/item?id=...`` pointers, but the
# table shape lets new entries land without restructuring callers.
_POINTER_HOST_PATHS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("news.ycombinator.com", re.compile(r"^/item(?:$|/|\?)")),
)


# ── Classifier ────────────────────────────────────────────────────────


def classify_source_kind(url: str) -> SourceKind:
    """Return the URL's likely source kind based on host/path shape.

    Purely syntactic — no network IO, no DNS resolution.  Used by
    :func:`influx.storage.download_archive` to short-circuit the HTML
    archive acquisition path for URLs that are known to point at
    non-HTML resources.

    Returns
    -------
    SourceKind
        ``"xml"`` for URLs whose path indicates an RSS/Atom/feed
        endpoint, ``"pointer"`` for known discussion/aggregator pointer
        URLs (currently Hacker News ``/item`` links), and ``"html"`` as
        the default for every other URL.
    """
    if not url:
        return "html"
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return "html"

    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()

    for pointer_host, path_re in _POINTER_HOST_PATHS:
        if (
            host == pointer_host or host.endswith("." + pointer_host)
        ) and path_re.match(path):
            return "pointer"

    if path and _XML_PATH_RE.search(path):
        return "xml"

    return "html"
