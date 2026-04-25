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

    def test_non_enumerated_utm_prefix_stripped(self) -> None:
        """Any utm_* key is stripped, not just the enumerated five."""
        raw = "https://example.com/path?utm_id=123&keep=1"
        result = normalise_url(raw)
        assert result == "https://example.com/path?keep=1"

    def test_multiple_non_enumerated_utm_params_stripped(self) -> None:
        raw = (
            "https://example.com/path?utm_id=1&utm_brand=foo&utm_creative=bar&keep=yes"
        )
        result = normalise_url(raw)
        assert result == "https://example.com/path?keep=yes"

    def test_percent_encoded_value_preserved_verbatim(self) -> None:
        """Percent-encoded values for unrelated params are not re-encoded."""
        raw = "https://example.com/search?q=hello%20world"
        assert normalise_url(raw) == "https://example.com/search?q=hello%20world"

    def test_plus_in_value_preserved_verbatim(self) -> None:
        """``+`` in unrelated params is not rewritten to ``%20`` or vice versa."""
        raw = "https://example.com/search?q=hello+world"
        assert normalise_url(raw) == "https://example.com/search?q=hello+world"

    def test_repeated_key_order_preserved(self) -> None:
        """Repeated query keys keep their original interleaved order."""
        raw = "https://example.com/search?a=1&b=2&a=3"
        assert normalise_url(raw) == "https://example.com/search?a=1&b=2&a=3"

    def test_param_order_preserved_when_tracking_in_middle(self) -> None:
        raw = "https://example.com/path?a=1&utm_source=x&b=2"
        assert normalise_url(raw) == "https://example.com/path?a=1&b=2"

    def test_value_with_special_chars_preserved(self) -> None:
        """Reserved characters within values are not normalised away."""
        raw = "https://example.com/path?q=a%26b%3Dc&keep=1"
        assert normalise_url(raw) == "https://example.com/path?q=a%26b%3Dc&keep=1"

    def test_blank_value_preserved(self) -> None:
        raw = "https://example.com/path?keep=&utm_source=x"
        assert normalise_url(raw) == "https://example.com/path?keep="

    def test_keyless_segment_preserved(self) -> None:
        """A bare key with no ``=`` is treated as a non-tracking param."""
        raw = "https://example.com/path?flag&utm_source=x"
        assert normalise_url(raw) == "https://example.com/path?flag"


class TestArxivCanonicalUrl:
    """arXiv canonical URL construction per FR-MCP-5."""

    def test_simple_id(self) -> None:
        assert arxiv_canonical_url("2601.12345") == "https://arxiv.org/abs/2601.12345"

    def test_versioned_id(self) -> None:
        assert (
            arxiv_canonical_url("2601.12345v2") == "https://arxiv.org/abs/2601.12345v2"
        )

    def test_old_format_id(self) -> None:
        assert (
            arxiv_canonical_url("hep-ph/0601001")
            == "https://arxiv.org/abs/hep-ph/0601001"
        )
