"""Canonical URL normalisation helpers (FR-MCP-4, FR-MCP-5).

Reused by the note renderer and the forthcoming MCP layer (PRD 05) so
that arXiv and other sources agree on a single canonical ``source_url``
shape.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# Query parameters stripped during normalisation.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
    }
)

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

    # Filter tracking params, preserve ordering of remaining params
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {
        k: v for k, v in query_params.items() if k not in _TRACKING_PARAMS
    }
    query = urlencode(filtered, doseq=True) if filtered else ""

    return urlunparse((scheme, netloc, path, "", query, parsed.fragment))


def arxiv_canonical_url(arxiv_id: str) -> str:
    """Return the canonical arXiv URL for a given arXiv ID (FR-MCP-5).

    >>> arxiv_canonical_url("2601.12345")
    'https://arxiv.org/abs/2601.12345'
    """
    return f"https://arxiv.org/abs/{arxiv_id}"
