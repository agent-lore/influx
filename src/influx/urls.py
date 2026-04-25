"""Canonical URL normalisation helpers (FR-MCP-4, FR-MCP-5).

Reused by the note renderer and the forthcoming MCP layer (PRD 05) so
that arXiv and other sources agree on a single canonical ``source_url``
shape.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

# Non-``utm_*`` tracking query parameters stripped during normalisation.
# Any key starting with ``utm_`` is also stripped (FR-MCP-4).
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
    }
)


def _is_tracking_param(key: str) -> bool:
    """Return True if *key* is a tracking query parameter (FR-MCP-4)."""
    return key.startswith("utm_") or key in _TRACKING_PARAMS

# Default ports that are stripped when they match the scheme.
_DEFAULT_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
}


def normalise_url(raw: str) -> str:
    """Return a canonical form of *raw*.

    The normaliser:
    - lowercases scheme and host
    - drops default ports (80 for http, 443 for https)
    - strips tracking query parameters (``utm_*``, ``fbclid``, ``gclid``,
      ``mc_cid``, ``mc_eid``, ``ref``)
    - removes a trailing slash on the path
    - preserves the fragment verbatim
    - leaves unrelated query parameters untouched
    """
    parsed = urlparse(raw)

    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    host = host.lower()

    # Resolve port: drop default ports
    port = parsed.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None

    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"

    # Strip trailing slash on path
    path = parsed.path.rstrip("/") if parsed.path != "/" else ""

    # Filter tracking params from the raw query string while preserving the
    # original encoding, segment order, and any repeated keys for unrelated
    # parameters (FR-MCP-4: "leaves unrelated query parameters untouched").
    query = _filter_tracking_params(parsed.query)

    return urlunparse((scheme, netloc, path, "", query, parsed.fragment))


def _filter_tracking_params(raw_query: str) -> str:
    """Strip tracking keys from *raw_query* without touching other params.

    Splits on ``&`` and inspects each segment's key only.  Non-tracking
    segments are kept verbatim, preserving percent-encoding, ordering,
    and repeated keys.
    """
    if not raw_query:
        return ""
    kept: list[str] = []
    for segment in raw_query.split("&"):
        if not segment:
            continue
        key = segment.split("=", 1)[0]
        if _is_tracking_param(key):
            continue
        kept.append(segment)
    return "&".join(kept)


def arxiv_canonical_url(arxiv_id: str) -> str:
    """Return the canonical arXiv URL for a given arXiv ID (FR-MCP-5).

    >>> arxiv_canonical_url("2601.12345")
    'https://arxiv.org/abs/2601.12345'
    """
    return f"https://arxiv.org/abs/{arxiv_id}"
