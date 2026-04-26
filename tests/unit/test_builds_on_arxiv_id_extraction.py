"""Unit tests for extract_arxiv_ref (US-002, AC-08-C/D)."""

from __future__ import annotations

from influx.lcma import extract_arxiv_ref


class TestPrefixWithParenthesisedArxivId:
    """AC-08-C: prefix + parenthesised arXiv ID."""

    def test_foonet_example(self) -> None:
        result = extract_arxiv_ref("FooNet (arXiv:2412.12345)")
        assert result == ("FooNet", "2412.12345")

    def test_multi_word_prefix(self) -> None:
        result = extract_arxiv_ref("Deep Residual Learning (arXiv:1512.03385)")
        assert result == ("Deep Residual Learning", "1512.03385")

    def test_prefix_with_trailing_whitespace_trimmed(self) -> None:
        result = extract_arxiv_ref("FooNet   (arXiv:2412.12345)")
        assert result is not None
        prior_title, arxiv_id = result
        assert prior_title == "FooNet"
        assert arxiv_id == "2412.12345"


class TestBareArxivId:
    """AC-08-D: bare arXiv ID with no prefix text."""

    def test_bare_arxiv_id(self) -> None:
        result = extract_arxiv_ref("arXiv:2412.12345")
        assert result == ("2412.12345", "2412.12345")

    def test_bare_arxiv_id_with_version(self) -> None:
        result = extract_arxiv_ref("arXiv:2412.12345v2")
        assert result == ("2412.12345v2", "2412.12345v2")


class TestNoMatch:
    """Items with no arXiv ID return None."""

    def test_no_arxiv_id(self) -> None:
        result = extract_arxiv_ref("Some prior work without an ID")
        assert result is None

    def test_empty_string(self) -> None:
        result = extract_arxiv_ref("")
        assert result is None

    def test_partial_arxiv_prefix(self) -> None:
        result = extract_arxiv_ref("See arXiv for details")
        assert result is None


class TestEdgeCases:
    """Additional coverage for edge cases."""

    def test_freestanding_arxiv_id_with_prefix(self) -> None:
        """arXiv ID not in parentheses but with prefix text."""
        result = extract_arxiv_ref("Based on arXiv:2301.00001")
        assert result is not None
        prior_title, arxiv_id = result
        assert prior_title == "Based on"
        assert arxiv_id == "2301.00001"

    def test_five_digit_id(self) -> None:
        result = extract_arxiv_ref("BarNet (arXiv:2301.10001)")
        assert result == ("BarNet", "2301.10001")
