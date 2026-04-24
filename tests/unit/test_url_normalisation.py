"""Tests for URL normalisation helpers (FR-MCP-4, FR-MCP-5)."""

from __future__ import annotations

from influx.urls import arxiv_canonical_url, normalise_url


class TestNormaliseUrl:
    """URL normalisation behaviour per FR-MCP-4."""

    def test_ac04h_arxiv_with_tracking_param(self) -> None:
        """AC-04-H: arXiv URL with utm_source normalises to canonical form."""
        raw = "https://arXiv.org:443/abs/2601.12345?utm_source=twitter"
        assert normalise_url(raw) == "https://arxiv.org/abs/2601.12345"

    def test_ac04h_fragment_preserved(self) -> None:
        """AC-04-H: URL with #foo fragment preserves the fragment."""
        raw = "https://example.com/page#foo"
        assert normalise_url(raw) == "https://example.com/page#foo"

    def test_mixed_case_host_lowercased(self) -> None:
        result = normalise_url("https://ArXiV.ORG/abs/1234")
        assert result == "https://arxiv.org/abs/1234"

    def test_default_port_80_on_http_stripped(self) -> None:
        result = normalise_url("http://example.com:80/path")
        assert result == "http://example.com/path"

    def test_default_port_443_on_https_stripped(self) -> None:
        result = normalise_url("https://example.com:443/path")
        assert result == "https://example.com/path"

    def test_non_default_port_preserved(self) -> None:
        result = normalise_url("https://example.com:8443/path")
        assert result == "https://example.com:8443/path"

    def test_multiple_tracking_params_stripped(self) -> None:
        raw = "https://example.com/page?utm_source=x&utm_medium=y&keep=1&fbclid=abc"
        result = normalise_url(raw)
        assert result == "https://example.com/page?keep=1"

    def test_unrelated_params_preserved(self) -> None:
        raw = "https://example.com/search?q=hello&page=2"
        result = normalise_url(raw)
        # Both params should survive
        assert "q=hello" in result
        assert "page=2" in result

    def test_trailing_slash_removed(self) -> None:
        result = normalise_url("https://example.com/path/")
        assert result == "https://example.com/path"

    def test_fragment_preserved_when_query_stripped(self) -> None:
        raw = "https://example.com/page?utm_source=x#section"
        result = normalise_url(raw)
        assert result == "https://example.com/page#section"

    def test_all_tracking_params_stripped(self) -> None:
        """All documented tracking params are stripped."""
        params = (
            "utm_source=a&utm_medium=b&utm_campaign=c"
            "&utm_term=d&utm_content=e&fbclid=f"
            "&gclid=g&mc_cid=h&mc_eid=i&ref=j"
        )
        raw = f"https://example.com/page?{params}"
        result = normalise_url(raw)
        assert result == "https://example.com/page"

    def test_root_path_normalises_cleanly(self) -> None:
        result = normalise_url("https://example.com/")
        assert result == "https://example.com"


class TestArxivCanonicalUrl:
    """arXiv canonical URL construction per FR-MCP-5."""

    def test_simple_id(self) -> None:
        assert arxiv_canonical_url("2601.12345") == "https://arxiv.org/abs/2601.12345"

    def test_versioned_id(self) -> None:
        assert arxiv_canonical_url("2601.12345v2") == "https://arxiv.org/abs/2601.12345v2"

    def test_old_format_id(self) -> None:
        assert arxiv_canonical_url("hep-ph/0601001") == "https://arxiv.org/abs/hep-ph/0601001"
