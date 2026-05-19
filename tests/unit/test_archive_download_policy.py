"""Tests for ``download_archive`` × domain policy interactions (issue #149).

Covers the cross-product of:

* policy mode (``attempt`` / ``rate_limited`` / ``blocked`` / ``skip``), and
* HTTP outcome (success, 403, 429, 404, 5xx, oversize, timeout).

Each combination asserts:

* whether a network call was made (skip-mode short-circuits),
* the resulting ``ArchiveResult.failure_kind``, and
* the policy-mode attribution on the result.

These tests complement the existing ``test_archive_download.py`` suite —
that one verifies the path-safety + happy / generic-failure contract;
this one focuses on the domain-policy reclassification layer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from influx.archive_policy import (
    ArchivePolicyRegistry,
    build_registry,
    default_registry,
)
from influx.errors import NetworkError
from influx.http_client import FetchResult
from influx.storage import ArchiveResult, download_archive


def _make_fetch_result(
    status_code: int = 200,
    body: bytes = b"%PDF-1.4 ok",
    content_type: str = "application/pdf",
    final_url: str = "https://example.com/x.pdf",
) -> FetchResult:
    return FetchResult(
        body=body,
        status_code=status_code,
        content_type=content_type,
        final_url=final_url,
    )


def _dl(
    tmp_path: Path,
    url: str,
    *,
    registry: ArchivePolicyRegistry | None = None,
    source: str = "rss",
    item_id: str = "feed-2026-01-01-deadbeef",
    ext: str = ".html",
) -> ArchiveResult:
    return download_archive(
        url=url,
        archive_root=tmp_path,
        source=source,
        item_id=item_id,
        published_year=2026,
        published_month=1,
        ext=ext,
        expected_content_type="html" if ext == ".html" else "pdf",
        policy_registry=registry,
    )


# ── Skip-mode: no network call, distinct failure_kind ──────────────────


class TestSkipModePolicy:
    """``skip`` mode short-circuits before the network call."""

    def test_skip_returns_missing_by_policy_and_no_fetch(self, tmp_path: Path) -> None:
        registry = build_registry(
            skip={"blocked.example": "permanent skip"},
            include_defaults=False,
        )
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            result = _dl(
                tmp_path,
                "https://blocked.example/article",
                registry=registry,
            )
        # Network was never called.
        mock_fetch.assert_not_called()
        assert result.ok is False
        assert result.rel_posix_path is None
        assert result.failure_kind == "missing_by_policy"
        assert result.policy_mode == "skip"
        assert result.domain == "blocked.example"
        assert "missing_by_policy" in result.error

    def test_skip_does_not_create_archive_file(self, tmp_path: Path) -> None:
        registry = build_registry(
            skip={"blocked.example": ""},
            include_defaults=False,
        )
        with patch("influx.storage.guarded_fetch"):
            _dl(tmp_path, "https://blocked.example/article", registry=registry)
        # No archive directory should be created for a skipped policy
        # — path construction comes after the skip short-circuit.
        assert not (tmp_path / "rss").exists()


# ── Blocked-mode HTTP 403 reclassification ─────────────────────────────


class TestBlockedModePolicy:
    """``blocked`` mode attempts the fetch and maps 403 to ``blocked``."""

    def test_blocked_http_403_returns_blocked_kind(self, tmp_path: Path) -> None:
        registry = build_registry(
            blocked={"science.example": "test-blocked"},
            include_defaults=False,
        )
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result(status_code=403)
            result = _dl(
                tmp_path,
                "https://www.science.example/abc",
                registry=registry,
            )
        mock_fetch.assert_called_once()
        assert result.ok is False
        assert result.failure_kind == "blocked"
        assert result.policy_mode == "blocked"
        assert result.domain == "www.science.example"

    def test_blocked_does_not_swallow_unrelated_codes(self, tmp_path: Path) -> None:
        registry = build_registry(
            blocked={"science.example": ""},
            include_defaults=False,
        )
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result(status_code=503)
            result = _dl(
                tmp_path,
                "https://www.science.example/abc",
                registry=registry,
            )
        # 5xx is a transient failure, NOT a policy-blocked one.
        assert result.failure_kind == "http_5xx"
        assert result.policy_mode == "blocked"

    def test_blocked_lets_success_through(self, tmp_path: Path) -> None:
        # If the policy is wrong (or the upstream lifts the block),
        # a successful fetch must still produce a normal archive.
        registry = build_registry(
            blocked={"science.example": ""},
            include_defaults=False,
        )
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result(
                body=b"<html>ok</html>",
                content_type="text/html",
            )
            result = _dl(
                tmp_path,
                "https://www.science.example/abc",
                registry=registry,
            )
        assert result.ok is True
        assert result.failure_kind == ""
        assert result.policy_mode == "blocked"


# ── Rate-limited mode HTTP 429 reclassification ────────────────────────


class TestRateLimitedModePolicy:
    """``rate_limited`` mode attempts the fetch and maps 429 to ``rate_limited``."""

    def test_rate_limited_http_429_returns_rate_limited_kind(
        self, tmp_path: Path
    ) -> None:
        registry = build_registry(
            rate_limited={"slow.example": ""},
            include_defaults=False,
        )
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result(status_code=429)
            result = _dl(
                tmp_path,
                "https://slow.example/article",
                registry=registry,
            )
        mock_fetch.assert_called_once()
        assert result.failure_kind == "rate_limited"
        assert result.policy_mode == "rate_limited"

    def test_rate_limited_does_not_swallow_403(self, tmp_path: Path) -> None:
        registry = build_registry(
            rate_limited={"slow.example": ""},
            include_defaults=False,
        )
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result(status_code=403)
            result = _dl(
                tmp_path,
                "https://slow.example/article",
                registry=registry,
            )
        # 403 under a rate-limited policy stays as generic http_403 —
        # only 429 collapses into the policy-driven kind.
        assert result.failure_kind == "http_403"


# ── Attempt-mode (default) generic classification ──────────────────────


class TestAttemptModeClassification:
    """The default ``attempt`` policy preserves HTTP-code attribution."""

    @pytest.mark.parametrize(
        "status_code, expected_kind",
        [
            (403, "http_403"),
            (404, "http_404"),
            (429, "http_429"),
            (451, "http_4xx"),
            (503, "http_5xx"),
        ],
    )
    def test_attempt_classifies_http_codes(
        self, tmp_path: Path, status_code: int, expected_kind: str
    ) -> None:
        # No registry → defaults to ``default_registry`` which has no
        # entry for ``unmapped.test``.
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result(status_code=status_code)
            result = _dl(tmp_path, "https://unmapped.test/x")
        assert result.failure_kind == expected_kind
        assert result.policy_mode == "attempt"

    def test_attempt_network_error_classified(self, tmp_path: Path) -> None:
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.side_effect = NetworkError(
                "Request timed out",
                url="https://unmapped.test/x",
                kind="timeout",
                reason="ReadTimeout",
            )
            result = _dl(tmp_path, "https://unmapped.test/x")
        assert result.failure_kind == "timeout"
        assert result.policy_mode == "attempt"


# ── Default-registry regression coverage ────────────────────────────────


class TestDefaultRegistryRegression:
    """Known-blocked staging domains route through the policy."""

    def test_science_org_403_routes_through_blocked_policy(
        self, tmp_path: Path
    ) -> None:
        # ``science.org`` is in the default blocked list — without
        # passing a registry, the default registry should apply.
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result(status_code=403)
            result = _dl(tmp_path, "https://www.science.org/article/abc")
        assert result.failure_kind == "blocked"
        assert result.policy_mode == "blocked"

    def test_alignmentforum_403_routes_through_blocked_policy(
        self, tmp_path: Path
    ) -> None:
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result(status_code=403)
            result = _dl(tmp_path, "https://www.alignmentforum.org/posts/xyz")
        assert result.failure_kind == "blocked"
        assert result.policy_mode == "blocked"

    def test_arxiv_pdf_does_not_match_default_blocked(self, tmp_path: Path) -> None:
        # Regression: an unrelated domain (arxiv.org) must keep the
        # ``attempt`` no-op policy even when the staging defaults are
        # active.
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result()
            result = _dl(
                tmp_path,
                "https://arxiv.org/pdf/2601.12345.pdf",
                source="arxiv",
                item_id="2601.12345",
                ext=".pdf",
            )
        assert result.ok is True
        assert result.policy_mode == "attempt"
        assert result.domain == "arxiv.org"


class TestSuccessHasEmptyFailureKind:
    """A successful archive carries no failure_kind."""

    def test_success_failure_kind_empty(self, tmp_path: Path) -> None:
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result()
            result = _dl(
                tmp_path,
                "https://arxiv.org/pdf/2601.12345.pdf",
                source="arxiv",
                item_id="2601.12345",
                ext=".pdf",
            )
        assert isinstance(result, ArchiveResult)
        assert result.ok is True
        assert result.failure_kind == ""


# ── default_registry is shared across calls ────────────────────────────


class TestDefaultRegistryIdentity:
    """Calls to ``default_registry()`` return the same singleton."""

    def test_singleton_identity(self) -> None:
        assert default_registry() is default_registry()


# ── Issue #160: URL-shape short-circuit for non-HTML sources ──────────


class TestNonHtmlSourceShortCircuit:
    """``expected_content_type="html"`` short-circuits non-HTML URL shapes.

    Pinning the contract guarantees that RSS/feed pointer URLs and HN
    discussion links never reach the network — so they cannot inflate
    the run's ``archive_failures_total`` (degraded-reasons feeder) with
    ``content_type_mismatch`` failures.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://csdb.dk/rss/upcomingevents.php",
            "https://csdb.dk/rss/latestadditions.php?type=release",
            "https://example.com/feed",
            "https://example.com/atom.xml",
        ],
    )
    def test_xml_feed_urls_short_circuit(self, tmp_path: Path, url: str) -> None:
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            result = _dl(tmp_path, url)
        mock_fetch.assert_not_called()
        assert result.ok is False
        assert result.failure_kind == "non_html_source"
        assert result.rel_posix_path is None
        assert "non_html_source" in result.error

    def test_hn_pointer_short_circuits(self, tmp_path: Path) -> None:
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            result = _dl(tmp_path, "https://news.ycombinator.com/item?id=48081266")
        mock_fetch.assert_not_called()
        assert result.failure_kind == "non_html_source"
        assert result.domain == "news.ycombinator.com"

    def test_html_url_is_not_short_circuited(self, tmp_path: Path) -> None:
        # A regular article URL still goes through the network — the
        # short-circuit only fires on known non-HTML shapes.
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result(
                body=b"<html>ok</html>", content_type="text/html"
            )
            result = _dl(tmp_path, "https://example.com/posts/article")
        mock_fetch.assert_called_once()
        assert result.ok is True
        assert result.failure_kind == ""

    def test_pdf_expected_type_skips_classifier(self, tmp_path: Path) -> None:
        # The short-circuit only runs when the caller expects HTML —
        # arXiv (PDF) callers must never be re-routed even if the URL
        # happens to match an XML/pointer shape.
        with patch("influx.storage.guarded_fetch") as mock_fetch:
            mock_fetch.return_value = _make_fetch_result()
            result = _dl(
                tmp_path,
                "https://example.com/feed",
                source="arxiv",
                item_id="2601.12345",
                ext=".pdf",
            )
        mock_fetch.assert_called_once()
        assert result.ok is True
        assert result.failure_kind == ""
