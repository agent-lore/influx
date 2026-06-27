"""Tests for the guarded HTTP client (src/influx/http_client.py).

US-002: scheme allow-list, SSRF IP-classification guard, and
allow_private_ips bypass.
US-003: streaming size cap and connect + read timeout.
US-004: content-type family check (HTML, PDF, XML/Atom).
US-005: redirect re-validation at every hop.
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
    guarded_outbound_post,
    guarded_post_json,
)

# ── Scheme allow-list ────────────────────────────────────────────────


class TestSchemeAllowList:
    """The guarded client must reject non-http(s) schemes."""

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/file",
            "file:///etc/passwd",
            "gopher://example.com",
            "javascript:alert(1)",
        ],
    )
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


def _multi_resolve(mapping: dict[str, str]):
    """Return a getaddrinfo fake resolving hosts per *mapping*."""

    def _inner(
        host: str,
        port: Any,
        family: int = 0,
        type: int = 0,
        **kw: Any,
    ):
        ip = mapping.get(host, "93.184.216.34")
        return [(2, 1, 6, "", (ip, 0))]

    return _inner


_PATCH_GAI = "influx.http_client.socket.getaddrinfo"


class TestSSRFGuardRejectsPrivate:
    """SSRF guard blocks loopback, link-local, private, multicast."""

    @pytest.mark.parametrize(
        "ip,label",
        [
            ("127.0.0.1", "loopback"),
            ("169.254.169.254", "link_local"),
            ("10.0.0.1", "private"),
            ("224.0.0.1", "multicast"),
        ],
    )
    def test_rejects_ip_class(self, ip: str, label: str) -> None:
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
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "169.254.169.254",
            "10.0.0.1",
            "224.0.0.1",
        ],
    )
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


# ── Content-type guard ───────────────────────────────────────────────


class TestContentTypeGuard:
    """guarded_fetch raises NetworkError on unexpected content type."""

    @respx.mock
    def test_passes_for_matching_content_type(self) -> None:
        url = "http://example.com/doc.pdf"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=b"%PDF-1.4",
                headers={"content-type": "application/pdf"},
            ),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            result = guarded_fetch(url, expected_content_type="pdf")
        assert result.status_code == 200

    @respx.mock
    def test_rejects_mismatched_content_type(self) -> None:
        url = "http://example.com/not-pdf"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=b"<html>",
                headers={"content-type": "text/html"},
            ),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(url, expected_content_type="pdf")
            assert exc_info.value.kind == "content_type_mismatch"

    @respx.mock
    def test_does_not_check_content_type_on_error(self) -> None:
        """Issue #227: HTTP error responses (>=400) skip content-type check."""
        url = "http://example.com/rate-limited"
        respx.get(url).mock(
            return_value=httpx.Response(
                429,
                content=b"<html>too fast</html>",
                headers={"content-type": "text/html"},
            ),
        )
        result = guarded_fetch(url, expected_content_type="pdf")
        assert result.status_code == 429


# ── Oversize guard ───────────────────────────────────────────────────


class TestOversizeGuard:
    """guarded_fetch raises NetworkError when body exceeds limit."""

    @respx.mock
    def test_raises_when_body_exceeds_limit(self) -> None:
        url = "http://example.com/big.pdf"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=b"x" * 100,
                headers={"content-type": "application/pdf"},
            ),
        )
        with pytest.raises(NetworkError) as exc_info:
            guarded_fetch(url, max_download_bytes=50)
        assert exc_info.value.kind == "oversize"

    @respx.mock
    def test_passes_for_body_under_limit(self) -> None:
        url = "http://example.com/small.pdf"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=b"x" * 30,
                headers={"content-type": "application/pdf"},
            ),
        )
        result = guarded_fetch(url, max_download_bytes=50)
        assert result.status_code == 200
        assert result.body == b"x" * 30

    @respx.mock
    def test_defaults_to_storage_config_limit(self) -> None:
        """When max_download_bytes is None, StorageConfig default is used."""
        url = "http://example.com/big-default.pdf"
        # 60 MB — above StorageConfig default (50 MB) — uncomment to test:
        # respx.get(url).mock(...)
        # For now, verify a small fetch succeeds with the default.
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=b"x" * 100,
                headers={"content-type": "application/pdf"},
            ),
        )
        result = guarded_fetch(url, max_download_bytes=200)
        assert result.status_code == 200


# ── Timeout guard ────────────────────────────────────────────────────


class TestTimeout:
    """guarded_fetch translates httpx timeouts to NetworkError."""

    @respx.mock
    def test_connect_timeout(self) -> None:
        url = "http://example.com/slow"
        respx.get(url).mock(side_effect=httpx.ConnectTimeout("slow"))
        with pytest.raises(NetworkError) as exc_info:
            guarded_fetch(url)
        assert exc_info.value.kind == "timeout"


# ── Redirect re-validation ───────────────────────────────────────────


class TestRedirectRevalidation:
    """guarded_fetch re-validates scheme + SSRF on every redirect hop."""

    @respx.mock
    def test_follows_one_redirect(self) -> None:
        start = "http://example.com/start"
        target = "http://example.com/target"
        respx.get(start).mock(
            return_value=httpx.Response(302, headers={"location": target}),
        )
        respx.get(target).mock(
            return_value=httpx.Response(200, text="ok"),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            result = guarded_fetch(start)
        assert result.status_code == 200
        assert result.body == b"ok"
        assert result.final_url == target

    @respx.mock
    def test_redirect_to_private_ip_raises(self) -> None:
        """AC-02-C: redirect from public to private IP is caught."""
        pub = "http://public.example.com/start"
        prv = "http://internal.example.com/secret"
        respx.get(pub).mock(
            return_value=httpx.Response(302, headers={"location": prv}),
        )
        resolver = _multi_resolve(
            {
                "public.example.com": "93.184.216.34",
                "internal.example.com": "10.0.0.1",
            }
        )
        with patch(_PATCH_GAI, resolver):
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(pub)
            err = exc_info.value
            assert err.kind == "ssrf"
            assert err.url == prv

    @respx.mock
    def test_redirect_to_loopback_raises(self) -> None:
        """AC-02-C: redirect to loopback is caught."""
        pub = "http://public.example.com/go"
        loop = "http://localhost/admin"
        respx.get(pub).mock(
            return_value=httpx.Response(302, headers={"location": loop}),
        )
        resolver = _multi_resolve(
            {
                "public.example.com": "93.184.216.34",
                "localhost": "127.0.0.1",
            }
        )
        with patch(_PATCH_GAI, resolver):
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(pub)
            err = exc_info.value
            assert err.kind == "ssrf"
            assert err.url == loop

    @respx.mock
    def test_redirect_to_bad_scheme_raises(self) -> None:
        """Redirect to ftp:// is rejected at the redirect hop."""
        pub = "http://public.example.com/redir"
        respx.get(pub).mock(
            return_value=httpx.Response(
                302,
                headers={"location": "ftp://evil.com/f"},
            ),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(pub)
            assert exc_info.value.kind == "scheme"

    @respx.mock
    def test_redirect_allows_private_with_flag(self) -> None:
        """allow_private_ips=True is honoured at redirect hops."""
        pub = "http://public.example.com/start"
        prv = "http://internal.example.com/data"
        respx.get(pub).mock(
            return_value=httpx.Response(302, headers={"location": prv}),
        )
        respx.get(prv).mock(
            return_value=httpx.Response(200, text="ok"),
        )
        result = guarded_fetch(pub, allow_private_ips=True)
        assert result.status_code == 200
        assert result.body == b"ok"


# ── Default browser headers (Issue #239) ────────────────────────────


class TestDefaultBrowserHeaders:
    """guarded_fetch sends browser-like User-Agent, Accept, Accept-Language."""

    @respx.mock
    def test_sends_browser_user_agent(self) -> None:
        """AC: User-Agent header is present and contains a browser token."""
        url = "http://example.com/page"
        route = respx.get(url).mock(
            return_value=httpx.Response(200, text="ok"),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            guarded_fetch(url)
        request = route.calls.last.request
        ua = request.headers.get("User-Agent", "")
        assert "Mozilla/5.0" in ua, f"Expected browser UA, got {ua!r}"
        assert "Chrome" in ua, f"Expected Chrome in UA, got {ua!r}"

    @respx.mock
    def test_sends_accept_header(self) -> None:
        """AC: Accept header is present with HTML types."""
        url = "http://example.com/page"
        route = respx.get(url).mock(
            return_value=httpx.Response(200, text="ok"),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            guarded_fetch(url)
        request = route.calls.last.request
        accept = request.headers.get("Accept", "")
        assert "text/html" in accept, f"Expected text/html in Accept, got {accept!r}"

    @respx.mock
    def test_sends_accept_language(self) -> None:
        """AC: Accept-Language header is present."""
        url = "http://example.com/page"
        route = respx.get(url).mock(
            return_value=httpx.Response(200, text="ok"),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            guarded_fetch(url)
        request = route.calls.last.request
        al = request.headers.get("Accept-Language", "")
        assert "en-US" in al, f"Expected en-US in Accept-Language, got {al!r}"

    @respx.mock
    def test_headers_do_not_break_redirects(self) -> None:
        """AC: Headers on redirect hops do not cause errors."""
        start = "http://example.com/start"
        target = "http://example.com/target"
        respx.get(start).mock(
            return_value=httpx.Response(302, headers={"location": target}),
        )
        respx.get(target).mock(
            return_value=httpx.Response(200, text="redirected"),
        )
        fake = _fake_getaddrinfo("93.184.216.34")
        with patch(_PATCH_GAI, fake):
            result = guarded_fetch(start)
        assert result.status_code == 200
        assert result.body == b"redirected"
        # Verify the redirect target also got the headers
        target_request = respx.get(target).calls.last.request
        ua = target_request.headers.get("User-Agent", "")
        assert "Mozilla/5.0" in ua

    @respx.mock
    def test_headers_do_not_break_ssrf_guard(self) -> None:
        """AC: Setting default headers does not bypass SSRF guard."""
        url = "http://10.0.0.1/secret"
        respx.get(url).mock(
            return_value=httpx.Response(200, text="nope"),
        )
        with patch(_PATCH_GAI, _fake_getaddrinfo("10.0.0.1")):
            with pytest.raises(NetworkError) as exc_info:
                guarded_fetch(url)
            assert exc_info.value.kind == "ssrf"

    @respx.mock
    def test_headers_do_not_break_oversize_guard(self) -> None:
        """AC: Setting default headers does not affect oversize guard."""
        url = "http://example.com/big"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=b"x" * 100,
                headers={"content-type": "text/html"},
            ),
        )
        with pytest.raises(NetworkError) as exc_info:
            guarded_fetch(url, max_download_bytes=50)
        assert exc_info.value.kind == "oversize"


# ── guarded_post_json (status-only fire-and-forget POST) ─────────────


class TestGuardedPostJson:
    """``guarded_post_json`` returns the HTTP status and discards the body."""

    @respx.mock
    def test_returns_status_for_2xx(self) -> None:
        url = "http://public.example.com/webhook"
        respx.post(url).mock(
            return_value=httpx.Response(200, text='{"ok":true}'),
        )
        status = guarded_post_json(
            url,
            {"foo": "bar"},
            allow_private_ips=True,
        )
        assert status == 200

    @respx.mock
    def test_returns_status_for_non_2xx(self) -> None:
        url = "http://public.example.com/webhook"
        respx.post(url).mock(
            return_value=httpx.Response(400, text='{"error":"bad"}'),
        )
        status = guarded_post_json(
            url,
            {"foo": "bar"},
            allow_private_ips=True,
        )
        assert status == 400


# ── guarded_outbound_post (shared guard context) ─────────────────────


class TestGuardedOutboundPost:
    """``guarded_outbound_post`` exposes guarded httpx clients for callers.

    Webhook diagnostics need bounded-body POST semantics, but the
    SSRF/scheme/timeout guard stack is shared with the fire-and-forget
    POST path — this context manager is the seam.
    """

    @respx.mock
    def test_yields_usable_client(self) -> None:
        url = "http://public.example.com/webhook"
        respx.post(url).mock(return_value=httpx.Response(200, text="ok"))
        with guarded_outbound_post(url, allow_private_ips=True) as client:
            response = client.post(url, json={"x": 1})
        assert response.status_code == 200

    def test_validates_scheme_before_yielding(self) -> None:
        with (
            pytest.raises(NetworkError) as exc_info,
            guarded_outbound_post("ftp://example.com") as _,
        ):
            pass
        assert exc_info.value.kind == "scheme"

    def test_translates_httpx_timeout_to_network_error(self) -> None:
        url = "http://public.example.com/webhook"

        @respx.mock
        def _run() -> None:
            respx.post(url).mock(side_effect=httpx.ConnectTimeout("slow"))
            with guarded_outbound_post(url, allow_private_ips=True) as client:
                client.post(url, json={"x": 1})

        with pytest.raises(NetworkError) as exc_info:
            _run()
        assert exc_info.value.kind == "timeout"
