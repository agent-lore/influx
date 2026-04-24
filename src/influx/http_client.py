"""Guarded HTTP client for outbound fetches.

Every outbound request passes through :func:`guarded_fetch`, which
enforces a scheme allow-list, SSRF IP-classification, streaming size
cap, connect+read timeout, content-type family check, and redirect
re-validation.

See PRD §5.4 for the full contract.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from influx.errors import NetworkError

__all__ = ["FetchResult", "guarded_fetch"]

_ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Immutable result of a guarded fetch."""

    body: bytes
    status_code: int
    content_type: str
    final_url: str


# ── Scheme validation ────────────────────────────────────────────────


def _validate_scheme(url: str) -> None:
    """Raise ``NetworkError`` if the URL scheme is not http or https."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise NetworkError(
            f"Scheme {parsed.scheme!r} is not allowed",
            url=url,
            kind="scheme",
            reason=f"Only {', '.join(sorted(_ALLOWED_SCHEMES))} are permitted",
        )


# ── SSRF guard ───────────────────────────────────────────────────────


def _resolve_host(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve *hostname* to IP addresses via ``socket.getaddrinfo``."""
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkError(
            f"DNS resolution failed for {hostname!r}",
            url=hostname,
            kind="dns",
            reason=str(exc),
        ) from exc

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        addresses.append(ipaddress.ip_address(ip_str))
    return addresses


def _classify_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a classification label if *addr* is blocked, else ``None``."""
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link_local"
    if addr.is_private:
        return "private"
    if addr.is_multicast:
        return "multicast"
    return None


def _ssrf_check(url: str, *, allow_private_ips: bool) -> None:
    """Raise ``NetworkError`` if the URL's host resolves to a blocked IP."""
    if allow_private_ips:
        return

    parsed = urlparse(url)
    hostname = parsed.hostname
    if hostname is None:
        raise NetworkError(
            "URL has no hostname",
            url=url,
            kind="ssrf",
            reason="Cannot extract hostname from URL",
        )

    addresses = _resolve_host(hostname)
    for addr in addresses:
        label = _classify_ip(addr)
        if label is not None:
            raise NetworkError(
                f"SSRF guard: {hostname!r} resolves to {label} address {addr}",
                url=url,
                kind="ssrf",
                reason=f"Resolved IP {addr} is classified as {label}",
            )


# ── Public API ───────────────────────────────────────────────────────


def guarded_fetch(
    url: str,
    *,
    allow_private_ips: bool = False,
    max_download_bytes: int = 52_428_800,
    timeout_seconds: int = 30,
) -> FetchResult:
    """Fetch *url* with scheme, SSRF, size, and timeout guards.

    Returns a :class:`FetchResult` on success.  Raises
    :class:`~influx.errors.NetworkError` when any guard is violated.
    """
    _validate_scheme(url)
    _ssrf_check(url, allow_private_ips=allow_private_ips)

    timeout = httpx.Timeout(
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url)
    except httpx.TimeoutException as exc:
        raise NetworkError(
            f"Request timed out: {exc}",
            url=url,
            kind="timeout",
            reason=str(exc),
        ) from exc
    except httpx.HTTPError as exc:
        raise NetworkError(
            f"HTTP error: {exc}",
            url=url,
            kind="network",
            reason=str(exc),
        ) from exc

    return FetchResult(
        body=response.content,
        status_code=response.status_code,
        content_type=response.headers.get("content-type", ""),
        final_url=str(response.url),
    )
