"""Tests for the guarded HTTP client (src/influx/http_client.py).

US-002: scheme allow-list, SSRF IP-classification guard, and
allow_private_ips bypass.
US-003: streaming size cap and connect + read timeout.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from influx.errors import NetworkError
from influx.http_client import (
    FetchResult,
    guarded_fetch,
)

# ── Scheme allow-list ────────────────────────────────────────────────


class TestSchemeAllowList:
    """The guarded client must reject non-http(s) schemes."""

    @pytest.mark.parametrize("url", [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com",
        "javascript:alert(1)",
    ])
    def test_rejects_disallowed_scheme(self, url: str) -> None:
        with pytest.raises(NetworkError) as exc_info:
            guarded_fetch(url)
        assert exc_info.value.kind == "scheme"
        assert exc_info.value.url == url


# ── SSRF guard ───────────────────────────────────────────────────────

# Helpers: fake getaddrinfo that returns a controlled IP.


def _fake_getaddrinfo(ip: str):
    """Return a factory mimicking socket.getaddrinfo."""

    def _inner(
        host: str,
        port: Any,
        family: int = 0,
        type: int = 0,
        **kw: Any,
    ):
        return [(2, 1, 6, "", (ip, 0))]

    return _inner


_PATCH_GAI = "influx.http_client.socket.getaddrinfo"


class TestSSRFGuardRejectsPrivate:
    """SSRF guard blocks loopback, link-local, private, multicast."""

    @pytest.mark.parametrize("ip,label", [
        ("127.0.0.1", "loopback"),
        ("169.254.169.254", "link_local"),
        ("10.0.0.1", "private"),
        ("224.0.0.1", "multicast"),
    ])
    def test_rejects_ip_class(
        self, ip: str, label: str
    ) -> None:
        url = "http://evil.example.com/path"
        fake = _fake_getaddrinfo(ip)
        with patch(_PATCH_GAI, fake):
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(url, allow_private_ips=False)
            err = exc_info.value
            assert err.kind == "ssrf"
            assert err.url == url
            assert ip in err.reason

    def test_rejects_metadata_endpoint(self) -> None:
        """AC: http://169.254.169.254/... is blocked."""
        ip = "169.254.169.254"
        fake = _fake_getaddrinfo(ip)
        with patch(_PATCH_GAI, fake):
            with pytest.raises(NetworkError) as exc_info:
                url = f"http://{ip}/latest/meta-data/"
                guarded_fetch(url)
            err = exc_info.value
            assert err.kind == "ssrf"
            assert ip in err.url

    def test_rejects_localhost(self) -> None:
        """AC: http://127.0.0.1/... is blocked."""
        ip = "127.0.0.1"
        fake = _fake_getaddrinfo(ip)
        with patch(_PATCH_GAI, fake):
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(f"http://{ip}/something")
            err = exc_info.value
            assert err.kind == "ssrf"
            assert ip in err.url


class TestSSRFGuardAllowPrivateIps:
    """When allow_private_ips=True, the SSRF guard is bypassed."""

    @respx.mock
    @pytest.mark.parametrize("ip", [
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.1",
        "224.0.0.1",
    ])
    def test_allows_when_flag_true(self, ip: str) -> None:
        url = f"http://{ip}/test"
        respx.get(url).mock(
            return_value=httpx.Response(200, text="ok"),
        )
        result = guarded_fetch(url, allow_private_ips=True)
        assert result.status_code == 200
        assert result.body == b"ok"

    @respx.mock
    def test_allows_localhost_ac02a(self) -> None:
        """AC-02-A: request to http://127.0.0.1/... succeeds."""
        url = "http://127.0.0.1/test"
        respx.get(url).mock(
            return_value=httpx.Response(200, text="hello"),
        )
        result = guarded_fetch(url, allow_private_ips=True)
        assert result.status_code == 200
        assert result.body == b"hello"


# ── FetchResult structure ────────────────────────────────────────────


class TestFetchResult:
    """guarded_fetch returns an object with required attributes."""

    @respx.mock
    def test_result_attributes(self) -> None:
        url = "http://example.com/page"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=b"<html>hi</html>",
                headers={
                    "content-type": "text/html; charset=utf-8",
                },
            )
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            result = guarded_fetch(url)
        assert isinstance(result, FetchResult)
        assert result.body == b"<html>hi</html>"
        assert result.status_code == 200
        assert "text/html" in result.content_type
        assert result.final_url == url


# ── DNS resolution failure ───────────────────────────────────────────


class TestDNSFailure:
    def test_dns_failure_raises_network_error(self) -> None:
        import socket as _socket

        def _fail(
            host: str,
            port: Any,
            family: int = 0,
            type: int = 0,
            **kw: Any,
        ):
            raise _socket.gaierror("Name or service not known")

        with patch(_PATCH_GAI, _fail):
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch("http://no-such-host.invalid/x")
            assert exc_info.value.kind == "dns"


# ── Streaming size cap (US-003) ──────────────────────────────────────


class TestStreamingSizeCap:
    """The guarded client must abort mid-stream when body exceeds limit."""

    @respx.mock
    def test_oversize_response_raises_network_error(self) -> None:
        """AC-02-B: body exceeding max_download_bytes raises oversize."""
        url = "http://example.com/big"
        # 100 bytes body, limit to 50
        respx.get(url).mock(
            return_value=httpx.Response(200, content=b"x" * 100),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(url, max_download_bytes=50)
            err = exc_info.value
            assert err.kind == "oversize"
            assert err.url == url

    @respx.mock
    def test_oversize_no_body_returned(self) -> None:
        """AC-02-B: partial body is NOT returned to the caller."""
        url = "http://example.com/big2"
        respx.get(url).mock(
            return_value=httpx.Response(200, content=b"y" * 200),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake), pytest.raises(NetworkError):
            guarded_fetch(url, max_download_bytes=100)
            # No FetchResult is returned — the exception is the only outcome

    @respx.mock
    def test_body_at_exact_limit_succeeds(self) -> None:
        """Body exactly at limit should succeed (not exceed)."""
        url = "http://example.com/exact"
        respx.get(url).mock(
            return_value=httpx.Response(200, content=b"z" * 50),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            result = guarded_fetch(url, max_download_bytes=50)
        assert result.body == b"z" * 50

    @respx.mock
    def test_body_under_limit_succeeds(self) -> None:
        """Body under limit returns normally."""
        url = "http://example.com/small"
        respx.get(url).mock(
            return_value=httpx.Response(200, content=b"abc"),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            result = guarded_fetch(url, max_download_bytes=1000)
        assert result.body == b"abc"


# ── Timeout (US-003) ──────────────────────────────────────────────────


class TestTimeout:
    """Connect and read timeouts raise NetworkError with kind='timeout'."""

    def test_connect_timeout_raises_network_error(self) -> None:
        """FR-RES-4: connect timeout raises NetworkError."""
        url = "http://example.com/slow"
        fake = _fake_getaddrinfo("93.184.216.34")
        with (
            patch(_PATCH_GAI, fake),
            patch(
                "influx.http_client.httpx.Client",
            ) as mock_client_cls,
        ):
            ctx = mock_client_cls.return_value.__enter__.return_value
            ctx.stream.side_effect = httpx.ConnectTimeout(
                "timed out"
            )
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(url, timeout_seconds=1)
            err = exc_info.value
            assert err.kind == "timeout"
            assert err.url == url

    def test_read_timeout_raises_network_error(self) -> None:
        """FR-RES-4: read timeout raises NetworkError."""
        url = "http://example.com/stall"
        fake = _fake_getaddrinfo("93.184.216.34")
        with (
            patch(_PATCH_GAI, fake),
            patch(
                "influx.http_client.httpx.Client",
            ) as mock_client_cls,
        ):
            ctx = mock_client_cls.return_value.__enter__.return_value
            ctx.stream.side_effect = httpx.ReadTimeout(
                "read timed out"
            )
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(url, timeout_seconds=1)
            err = exc_info.value
            assert err.kind == "timeout"
            assert err.url == url
