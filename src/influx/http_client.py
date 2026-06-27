"""Guarded HTTP client for outbound fetches.

Every outbound request passes through :func:`guarded_fetch`, which
enforces a scheme allow-list, SSRF IP-classification, streaming size
cap, connect+read timeout, content-type family check, and redirect
re-validation.

See PRD §5.4 for the full contract.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx

from influx.config import NotificationsConfig, StorageConfig
from influx.errors import NetworkError

__all__ = [
    "ContentTypeFamily",
    "FetchResult",
    "aguarded_fetch",
    "aguarded_post_json_fetch",
    "content_type_family",
    "guarded_fetch",
    "guarded_outbound_post",
    "guarded_post_json",
    "guarded_post_json_fetch",
]

# Browser-like headers to avoid publisher 403/429 from anti-bot stubs
# that block the default python-httpx User-Agent (Issue #239).
_DEFAULT_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

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
    headers: dict[str, str] = field(default_factory=dict)


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


def content_type_family(content_type: str) -> ContentTypeFamily | None:
    """Classify a raw ``Content-Type`` header into its family, or ``None``.

    Parses the bare MIME type (drops parameters like ``; charset=utf-8``)
    and maps it through :data:`_CONTENT_TYPE_FAMILIES`.  Used by callers
    that fetch with no ``expected_content_type`` guard and route on the
    actual response type (issue #200 — inbox auto-detect acquisition).
    Returns ``None`` for any MIME not in a known family.
    """
    mime = content_type.split(";")[0].strip().lower()
    for family, allowed in _CONTENT_TYPE_FAMILIES.items():
        if mime in allowed:
            return family
    return None


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
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers=_DEFAULT_BROWSER_HEADERS,
        ) as client:
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
                    response_headers = dict(response.headers)
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

    # The content-type guard only runs on a non-error response
    # (status < 400).  On an HTTP error (>= 400) the status code is the
    # real signal: an error body's content-type is meaningless (arXiv
    # serves its HTTP 429 rate-limit page as text/html even when a PDF
    # was requested).  Checking it here would mask the status as a
    # content_type_mismatch and lose the rate-limit signal — so we skip
    # the check and return the FetchResult for the caller's status
    # handling (#227).
    if expected_content_type is not None and status_code < 400:
        _check_content_type(content_type, expected_content_type, final_url)

    return FetchResult(
        body=body,
        status_code=status_code,
        content_type=content_type,
        final_url=final_url,
        headers=response_headers,
    )


@contextmanager
def guarded_outbound_post(
    url: str,
    *,
    allow_private_ips: bool = False,
    timeout_seconds: int | None = None,
) -> Iterator[httpx.Client]:
    """Yield a guarded :class:`httpx.Client` for outbound POSTs.

    Validates the URL against the scheme allow-list and SSRF
    classifier, builds a connect+read+write+pool timeout from
    ``timeout_seconds`` (falling back to the
    :class:`~influx.config.NotificationsConfig` field default), and
    translates :class:`httpx.TimeoutException` /
    :class:`httpx.HTTPError` raised inside the ``with`` block into
    :class:`~influx.errors.NetworkError`.

    Used by callers that need POST semantics beyond a fire-and-forget
    status — e.g. the webhook dispatcher capturing a bounded body
    snippet for diagnostics — without re-implementing the guard stack.
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
            yield client
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


def guarded_post_json(
    url: str,
    payload: dict[str, object],
    *,
    headers: dict[str, str] | None = None,
    allow_private_ips: bool = False,
    timeout_seconds: int | None = None,
) -> int:
    """POST *payload* as JSON to *url* with scheme and SSRF guards.

    Returns the HTTP status code.  Fire-and-forget: the response body
    is read and discarded by ``httpx``.  Callers that need to inspect
    the body should use :func:`guarded_post_json_fetch` (full body) or
    drive :func:`guarded_outbound_post` directly with their own
    streaming/cap policy.

    Raises :class:`~influx.errors.NetworkError` on guard violations,
    timeouts, or connection failures.  No retry logic — callers handle
    retries if needed (FR-NOT-1).

    ``timeout_seconds`` defaults to ``None``; when omitted it is resolved
    from the pydantic :class:`~influx.config.NotificationsConfig` field
    default so the only place this tunable lives is config-parsing code
    (AC-X-1).  Webhook callers pass the loaded config value explicitly.
    """
    with guarded_outbound_post(
        url,
        allow_private_ips=allow_private_ips,
        timeout_seconds=timeout_seconds,
    ) as client:
        response = client.post(url, json=payload, headers=headers)
        return response.status_code


def guarded_post_json_fetch(
    url: str,
    payload: dict[str, object],
    *,
    headers: dict[str, str] | None = None,
    allow_private_ips: bool = False,
    max_response_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> FetchResult:
    """POST JSON and return the response body under the outbound HTTP guard.

    This is the POST analogue of :func:`guarded_fetch`: it enforces the
    scheme allow-list, SSRF checks, streaming response-size cap, and timeout.
    It intentionally does not enforce a content-type family because model
    provider error responses vary; callers parse or validate the body.
    """
    _validate_scheme(url)
    _ssrf_check(url, allow_private_ips=allow_private_ips)

    if max_response_bytes is None or timeout_seconds is None:
        _storage_defaults = StorageConfig()
        if max_response_bytes is None:
            max_response_bytes = _storage_defaults.max_download_bytes
        if timeout_seconds is None:
            timeout_seconds = _storage_defaults.download_timeout_seconds

    timeout = httpx.Timeout(
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )

    try:
        with (
            httpx.Client(timeout=timeout, follow_redirects=False) as client,
            client.stream(
                "POST",
                url,
                json=payload,
                headers=headers,
            ) as response,
        ):
            chunks: list[bytes] = []
            received = 0
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > max_response_bytes:
                    raise NetworkError(
                        f"Response body exceeds {max_response_bytes} bytes",
                        url=url,
                        kind="oversize",
                        reason=(
                            f"Received {received} bytes, limit is {max_response_bytes}"
                        ),
                    )
                chunks.append(chunk)
            return FetchResult(
                body=b"".join(chunks),
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                final_url=str(response.url),
                headers=dict(response.headers),
            )
    except NetworkError:
        raise
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


# ── Async wrappers (issue #124) ─────────────────────────────────────
#
# The sync ``guarded_fetch`` and ``guarded_post_json_fetch`` functions
# perform blocking I/O via ``httpx.Client``. When called from the
# async event loop (Run path: source fetch, filter scoring, archive
# download, content extraction), they starve the loop and stall the
# admin HTTP API. These thin async wrappers offload the sync call to
# a worker thread so the event loop stays responsive.
#
# The sync helpers are unchanged — all SSRF, redirect, size cap,
# timeout, and content-type guard behaviour is preserved verbatim.


async def aguarded_fetch(
    url: str,
    *,
    allow_private_ips: bool = False,
    max_download_bytes: int | None = None,
    timeout_seconds: int | None = None,
    expected_content_type: ContentTypeFamily | None = None,
) -> FetchResult:
    """Async wrapper: offloads :func:`guarded_fetch` to a worker thread."""
    return await asyncio.to_thread(
        guarded_fetch,
        url,
        allow_private_ips=allow_private_ips,
        max_download_bytes=max_download_bytes,
        timeout_seconds=timeout_seconds,
        expected_content_type=expected_content_type,
    )


async def aguarded_post_json_fetch(
    url: str,
    payload: dict[str, object],
    *,
    headers: dict[str, str] | None = None,
    allow_private_ips: bool = False,
    max_response_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> FetchResult:
    """Async wrapper: offloads :func:`guarded_post_json_fetch` to a worker thread."""
    return await asyncio.to_thread(
        guarded_post_json_fetch,
        url,
        payload,
        headers=headers,
        allow_private_ips=allow_private_ips,
        max_response_bytes=max_response_bytes,
        timeout_seconds=timeout_seconds,
    )
