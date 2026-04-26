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
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx

from influx.config import NotificationsConfig, StorageConfig
from influx.errors import NetworkError

__all__ = ["ContentTypeFamily", "FetchResult", "guarded_fetch", "guarded_post_json"]

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_MAX_REDIRECTS = 20

ContentTypeFamily = Literal["html", "pdf", "xml"]

_CONTENT_TYPE_FAMILIES: dict[ContentTypeFamily, frozenset[str]] = {
    "html": frozenset({"text/html", "application/xhtml+xml"}),
    "pdf": frozenset({"application/pdf"}),
    "xml": frozenset(
        {
            "text/xml",
            "application/xml",
            "application/atom+xml",
            "application/rss+xml",
        }
    ),
}


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


def _check_content_type(
    content_type: str,
    expected: ContentTypeFamily,
    url: str,
) -> None:
    """Raise ``NetworkError`` if *content_type* doesn't match *expected* family."""
    mime = content_type.split(";")[0].strip().lower()
    allowed = _CONTENT_TYPE_FAMILIES[expected]
    if mime not in allowed:
        raise NetworkError(
            f"Content-type {mime!r} does not match expected family {expected!r}",
            url=url,
            kind="content_type_mismatch",
            reason=(f"Expected one of {', '.join(sorted(allowed))}; got {mime!r}"),
        )


def guarded_fetch(
    url: str,
    *,
    allow_private_ips: bool = False,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
    expected_content_type: ContentTypeFamily | None = None,
) -> FetchResult:
    """Fetch *url* with scheme, SSRF, size, timeout, and content-type guards.

    Every redirect hop is re-validated against the scheme allow-list and
    the SSRF IP classifier (PRD §5.3 R-4).

    Returns a :class:`FetchResult` on success.  Raises
    :class:`~influx.errors.NetworkError` when any guard is violated.

    ``max_download_bytes`` and ``timeout_seconds`` default to ``None``;
    when omitted they are resolved from the pydantic
    :class:`~influx.config.StorageConfig` field defaults so the only
    place these tunables live is config-parsing code (AC-X-1).
    """
    _validate_scheme(url)
    _ssrf_check(url, allow_private_ips=allow_private_ips)

    if max_download_bytes is None or timeout_seconds is None:
        _storage_defaults = StorageConfig()
        if max_download_bytes is None:
            max_download_bytes = _storage_defaults.max_download_bytes
        if timeout_seconds is None:
            timeout_seconds = _storage_defaults.download_timeout_seconds

    timeout = httpx.Timeout(
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )

    current_url = url

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _hop in range(_MAX_REDIRECTS + 1):
                with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        next_url = urljoin(
                            current_url,
                            response.headers["location"],
                        )
                        _validate_scheme(next_url)
                        _ssrf_check(
                            next_url,
                            allow_private_ips=allow_private_ips,
                        )
                        current_url = next_url
                        continue

                    chunks: list[bytes] = []
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > max_download_bytes:
                            raise NetworkError(
                                f"Response body exceeds {max_download_bytes} bytes",
                                url=current_url,
                                kind="oversize",
                                reason=(
                                    f"Received {received}"
                                    " bytes, limit is"
                                    f" {max_download_bytes}"
                                ),
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    status_code = response.status_code
                    content_type = response.headers.get("content-type", "")
                    final_url = str(response.url)
                    break
            else:
                raise NetworkError(
                    f"Too many redirects (>{_MAX_REDIRECTS})",
                    url=url,
                    kind="network",
                    reason=(f"Exceeded {_MAX_REDIRECTS} redirects"),
                )
    except NetworkError:
        raise
    except httpx.TimeoutException as exc:
        raise NetworkError(
            f"Request timed out: {exc}",
            url=current_url,
            kind="timeout",
            reason=str(exc),
        ) from exc
    except httpx.HTTPError as exc:
        raise NetworkError(
            f"HTTP error: {exc}",
            url=current_url,
            kind="network",
            reason=str(exc),
        ) from exc

    if expected_content_type is not None:
        _check_content_type(content_type, expected_content_type, final_url)

    return FetchResult(
        body=body,
        status_code=status_code,
        content_type=content_type,
        final_url=final_url,
    )


def guarded_post_json(
    url: str,
    payload: dict[str, object],
    *,
    allow_private_ips: bool = False,
    timeout_seconds: int | None = None,
) -> int:
    """POST *payload* as JSON to *url* with scheme and SSRF guards.

    Returns the HTTP status code.  Raises
    :class:`~influx.errors.NetworkError` on guard violations, timeouts,
    or connection failures.  No retry logic — callers handle retries if
    needed (FR-NOT-1).

    ``timeout_seconds`` defaults to ``None``; when omitted it is resolved
    from the pydantic :class:`~influx.config.NotificationsConfig` field
    default so the only place this tunable lives is config-parsing code
    (AC-X-1).  Webhook callers pass the loaded config value explicitly.
    """
    _validate_scheme(url)
    _ssrf_check(url, allow_private_ips=allow_private_ips)

    if timeout_seconds is None:
        timeout_seconds = NotificationsConfig().timeout_seconds

    timeout = httpx.Timeout(
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload)
            return response.status_code
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
