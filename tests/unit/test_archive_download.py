"""Tests for archive download via guarded HTTP client (US-009, FR-ST-4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from influx.errors import NetworkError
from influx.http_client import FetchResult
from influx.storage import ArchivePathError, ArchiveResult, download_archive

_URL = "https://arxiv.org/pdf/2601.12345"


def _make_fetch_result(
    body: bytes = b"%PDF-1.4 sample",
    status_code: int = 200,
    content_type: str = "application/pdf",
    final_url: str = _URL,
) -> FetchResult:
    return FetchResult(
        body=body,
        status_code=status_code,
        content_type=content_type,
        final_url=final_url,
    )


def _dl(
    tmp_path: Path,
    url: str = _URL,
    source: str = "arxiv",
    item_id: str = "2601.12345",
    published_year: int = 2026,
    published_month: int = 1,
    ext: str = ".pdf",
    allow_private_ips: bool = False,
    max_download_bytes: int = 52_428_800,
    timeout_seconds: int = 30,
) -> ArchiveResult:
    """Call download_archive with standard arXiv defaults."""
    return download_archive(
        url=url,
        archive_root=tmp_path,
        source=source,
        item_id=item_id,
        published_year=published_year,
        published_month=published_month,
        ext=ext,
        allow_private_ips=allow_private_ips,
        max_download_bytes=max_download_bytes,
        timeout_seconds=timeout_seconds,
    )


def _ssrf_error(url: str = _URL) -> NetworkError:
    return NetworkError(
        "SSRF guard: 'localhost' resolves to loopback address",
        url=url,
        kind="ssrf",
        reason="Resolved IP 127.0.0.1 is classified as loopback",
    )


def _oversize_error(url: str = _URL) -> NetworkError:
    return NetworkError(
        "Response body exceeds 1000 bytes",
        url=url,
        kind="oversize",
        reason="Received 1500 bytes, limit is 1000",
    )


def _timeout_error(url: str = _URL) -> NetworkError:
    return NetworkError(
        "Request timed out",
        url=url,
        kind="timeout",
        reason="ReadTimeout",
    )


class TestSuccessfulDownload:
    """Happy-path tests: guarded_fetch succeeds."""

    @patch("influx.storage.guarded_fetch")
    def test_writes_pdf_and_returns_success(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        mock_fetch.return_value = _make_fetch_result()  # type: ignore[union-attr]
        result = _dl(tmp_path)

        assert result.ok is True
        assert result.rel_posix_path == "arxiv/2026/01/2601.12345.pdf"
        assert result.error == ""

        fs_path = (
            tmp_path / "arxiv" / "2026" / "01" / "2601.12345.pdf"
        )
        assert fs_path.exists()
        assert fs_path.read_bytes() == b"%PDF-1.4 sample"

    @patch("influx.storage.guarded_fetch")
    def test_creates_parent_directories(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        mock_fetch.return_value = _make_fetch_result()  # type: ignore[union-attr]
        result = _dl(tmp_path)

        assert result.ok is True
        parent = tmp_path / "arxiv" / "2026" / "01"
        assert parent.is_dir()

    @patch("influx.storage.guarded_fetch")
    def test_passes_guard_params_to_fetch(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        mock_fetch.return_value = _make_fetch_result()  # type: ignore[union-attr]
        _dl(
            tmp_path,
            allow_private_ips=True,
            max_download_bytes=1000,
            timeout_seconds=5,
        )
        mock_fetch.assert_called_once_with(  # type: ignore[union-attr]
            _URL,
            allow_private_ips=True,
            max_download_bytes=1000,
            timeout_seconds=5,
            expected_content_type="pdf",
        )


class TestOversizeAbort:
    """AC-X-4 partial: oversize abort returns failure signal."""

    @patch("influx.storage.guarded_fetch")
    def test_oversize_returns_failure_result(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        mock_fetch.side_effect = _oversize_error()  # type: ignore[union-attr]
        result = _dl(tmp_path, max_download_bytes=1000)

        assert result.ok is False
        assert result.rel_posix_path is None
        assert "oversize" in result.error

    @patch("influx.storage.guarded_fetch")
    def test_oversize_does_not_write_file(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        mock_fetch.side_effect = _oversize_error()  # type: ignore[union-attr]
        _dl(tmp_path, max_download_bytes=1000)

        fs_path = (
            tmp_path / "arxiv" / "2026" / "01" / "2601.12345.pdf"
        )
        assert not fs_path.exists()


class TestSSRFRejection:
    """SSRF rejection returns failure signal."""

    @patch("influx.storage.guarded_fetch")
    def test_ssrf_returns_failure_result(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        ssrf_url = "http://localhost/evil.pdf"
        mock_fetch.side_effect = _ssrf_error(ssrf_url)  # type: ignore[union-attr]
        result = _dl(tmp_path, url=ssrf_url)

        assert result.ok is False
        assert result.rel_posix_path is None
        assert "ssrf" in result.error


class TestTimeoutFailure:
    """Timeout returns failure signal."""

    @patch("influx.storage.guarded_fetch")
    def test_timeout_returns_failure_result(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        mock_fetch.side_effect = _timeout_error()  # type: ignore[union-attr]
        result = _dl(tmp_path)

        assert result.ok is False
        assert result.rel_posix_path is None
        assert "timeout" in result.error


class TestHTTPError:
    """HTTP error status returns failure signal."""

    @patch("influx.storage.guarded_fetch")
    def test_network_error_returns_failure_result(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        mock_fetch.side_effect = NetworkError(  # type: ignore[union-attr]
            "HTTP error: 503 Service Unavailable",
            url=_URL,
            kind="network",
            reason="503 Service Unavailable",
        )
        result = _dl(tmp_path)

        assert result.ok is False
        assert result.rel_posix_path is None

    @patch("influx.storage.guarded_fetch")
    def test_http_4xx_status_returns_failure(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        """A non-exception 4xx response also signals failure."""
        mock_fetch.return_value = _make_fetch_result(  # type: ignore[union-attr]
            status_code=404,
        )
        result = _dl(tmp_path)

        assert result.ok is False
        assert result.rel_posix_path is None
        assert "404" in result.error


class TestFailurePathTagSignal:
    """Failure results carry the information callers need for tagging."""

    @patch("influx.storage.guarded_fetch")
    def test_failure_result_drives_empty_archive_and_tags(
        self, mock_fetch: object, tmp_path: Path
    ) -> None:
        """Demonstrate that a failure result provides the signal for
        callers to render an empty Archive body + repair/missing tags."""
        mock_fetch.side_effect = _oversize_error()  # type: ignore[union-attr]
        result = _dl(tmp_path, max_download_bytes=1000)

        assert result.ok is False
        assert result.rel_posix_path is None

        # Caller logic: on failure, archive_path=None and add tags
        failure_tags: list[str] = []
        if not result.ok:
            failure_tags.extend([
                "influx:repair-needed",
                "influx:archive-missing",
            ])
        assert "influx:repair-needed" in failure_tags
        assert "influx:archive-missing" in failure_tags


class TestPathSafetyBeforeDownload:
    """Path-safety errors propagate as ArchivePathError, not caught."""

    def test_traversal_raises_before_download(
        self, tmp_path: Path
    ) -> None:
        """Unsafe IDs are rejected before any network call."""
        with pytest.raises(ArchivePathError):
            _dl(tmp_path, item_id="../../etc/passwd")
