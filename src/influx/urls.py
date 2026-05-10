"""Canonical URL normalisation and hashing helpers (FR-MCP-4, FR-MCP-5).

Reused by the note renderer and the forthcoming MCP layer (PRD 05) so
that arXiv and other sources agree on a single canonical ``source_url``
shape.  The ``url_hash`` helper provides a deterministic per-URL
disambiguator for archive filenames (PRD 09 FR-ST-1).
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse, urlsplit, urlunparse

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


# ── URL hash for archive filename disambiguation ────────────────────

_URL_HASH_LEN = 10


def url_hash(source_url: str) -> str:
    """Return a 10-char hex SHA-256 digest of the normalised *source_url*.

    Used as the ``{url-hash}`` segment in RSS archive filenames
    (PRD 09 FR-ST-1) to disambiguate items from the same feed published
    on the same date.

    The hash is computed over :func:`normalise_url` output so callers
    operating on equivalent URLs always receive the same hash.
    """
    canonical = normalise_url(source_url)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return digest[:_URL_HASH_LEN]


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


# ── Article URL validation (issue #131) ─────────────────────────────


_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class UrlValidation:
    """Result of :func:`classify_article_url`.

    ``ok`` is ``True`` when the URL passed every check; ``reason`` is
    ``None`` in that case.  When ``ok`` is ``False``, ``reason`` carries
    one of: ``"malformed"``, ``"scheme"``, ``"no_host"``, ``"loopback"``,
    ``"link_local"``, ``"private"``, ``"multicast"``.

    The classification labels mirror :func:`influx.http_client._classify_ip`
    so log messages and dashboards stay consistent across the syntactic
    pre-fetch guard and the DNS-resolved SSRF guard.
    """

    ok: bool
    reason: str | None = None


def classify_article_url(raw: str, *, allow_private: bool = False) -> UrlValidation:
    """Validate an article URL syntactically before expensive acquisition.

    Issue #131: RSS feeds can advertise loopback / private-host article
    links (the staging Sourcegraph incident emitted ``http://localhost:5174/``
    URLs).  This guard rejects such links before
    :func:`~influx.storage.download_archive` /
    :func:`~influx.extraction.article.extract_article` are attempted, so
    the offending item never enters the run's pre-filter population and
    never produces an ``influx:archive-missing`` note.

    Pure-syntactic — does **no** DNS resolution.  Hostname literals that
    parse as IP addresses are classified via :mod:`ipaddress`; non-IP
    hostnames are accepted unless they equal ``"localhost"``.  DNS-based
    rejection of hostnames that resolve to private addresses remains the
    SSRF guard's job at fetch time (defence in depth).

    Parameters
    ----------
    raw:
        The candidate URL string (e.g. an RSS ``entry.link``).
    allow_private:
        When ``True``, loopback / private / link-local / multicast hosts
        pass.  Mirrors the existing ``allow_private_ips`` config flag
        used by the SSRF guard so test fixtures (FakeArticleServer on
        ``127.0.0.1``) keep working unchanged.

    Returns
    -------
    UrlValidation
    """
    if not raw or not raw.strip():
        return UrlValidation(ok=False, reason="malformed")

    try:
        parsed = urlsplit(raw.strip())
    except ValueError:
        return UrlValidation(ok=False, reason="malformed")

    scheme = parsed.scheme.lower()
    if not scheme:
        return UrlValidation(ok=False, reason="malformed")
    if scheme not in _ALLOWED_URL_SCHEMES:
        return UrlValidation(ok=False, reason="scheme")

    try:
        hostname = parsed.hostname
    except ValueError:
        return UrlValidation(ok=False, reason="malformed")
    if not hostname:
        return UrlValidation(ok=False, reason="no_host")

    host_label = _classify_host(hostname)
    if host_label is not None and not allow_private:
        return UrlValidation(ok=False, reason=host_label)

    return UrlValidation(ok=True, reason=None)


def _classify_host(hostname: str) -> str | None:
    """Return a classification label for *hostname*, or ``None`` if public.

    Recognises the literal ``localhost`` (case-insensitive) and any
    hostname that parses as an IPv4 / IPv6 literal.  Non-IP hostnames
    that aren't ``localhost`` are treated as public — DNS-resolved
    classification is the SSRF guard's responsibility at fetch time.
    """
    if hostname.lower() == "localhost":
        return "loopback"
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link_local"
    if addr.is_private:
        return "private"
    if addr.is_multicast:
        return "multicast"
    return None
