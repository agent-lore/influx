"""Golden-file tests for compose_retrieve_query (US-001, AC-08-A/B)."""

from __future__ import annotations

from influx.lcma import compose_retrieve_query


class TestTitleOnly:
    """AC-08-A case 1: title only, no contributions."""

    def test_title_only(self) -> None:
        result = compose_retrieve_query("My Paper Title")
        assert result == "My Paper Title"

    def test_title_only_none_contributions(self) -> None:
        result = compose_retrieve_query("My Paper Title", contributions=None)
        assert result == "My Paper Title"


class TestTitlePlusContributions:
    """AC-08-A cases 2-4: title + varying contribution counts."""

    def test_title_plus_one_contribution(self) -> None:
        """Case 2: title + 1 contribution."""
        result = compose_retrieve_query(
            "Paper A",
            contributions=["Novel architecture"],
        )
        assert result == "Paper A | Novel architecture"

    def test_title_plus_three_contributions(self) -> None:
        """Case 3: title + 3 contributions (all used)."""
        result = compose_retrieve_query(
            "Paper B",
            contributions=["First", "Second", "Third"],
        )
        assert result == "Paper B | First | Second | Third"

    def test_title_plus_five_contributions_only_first_three(self) -> None:
        """Case 4: title + 5 contributions — only first 3 used."""
        result = compose_retrieve_query(
            "Paper C",
            contributions=["A", "B", "C", "D", "E"],
        )
        assert result == "Paper C | A | B | C"


class TestTruncation:
    """AC-08-A case 5: 600-char title truncated to 500."""

    def test_long_title_truncated_to_500(self) -> None:
        long_title = "x" * 600
        result = compose_retrieve_query(long_title)
        assert len(result) == 500
        assert result == "x" * 500

    def test_long_composed_truncated_to_500(self) -> None:
        title = "t" * 400
        contrib = "c" * 200
        result = compose_retrieve_query(title, contributions=[contrib])
        assert len(result) == 500
        expected_full = f"{'t' * 400} | {'c' * 200}"
        assert result == expected_full[:500]


class TestWhitespaceCollapse:
    """AC-08-B: internal whitespace collapsed to single space."""

    def test_newlines_collapsed(self) -> None:
        result = compose_retrieve_query("hello\n\nworld")
        assert result == "hello world"

    def test_tabs_collapsed(self) -> None:
        result = compose_retrieve_query("hello\t\tworld")
        assert result == "hello world"

    def test_mixed_whitespace(self) -> None:
        result = compose_retrieve_query("hello  \n\t  world")
        assert result == "hello world"

    def test_whitespace_in_contributions(self) -> None:
        result = compose_retrieve_query(
            "Title",
            contributions=["first\n\ncontrib", "second  contrib"],
        )
        assert result == "Title | first contrib | second contrib"


class TestEmptyContributionsSkipped:
    """FR-LCMA-2 step 2: empty-after-trimming contributions are skipped."""

    def test_empty_string_skipped(self) -> None:
        result = compose_retrieve_query(
            "Title",
            contributions=["", "Valid"],
        )
        assert result == "Title | Valid"

    def test_whitespace_only_skipped(self) -> None:
        result = compose_retrieve_query(
            "Title",
            contributions=["   ", "\t\n", "Real"],
        )
        assert result == "Title | Real"

    def test_mixed_empty_and_valid(self) -> None:
        """Empty contributions don't count toward the 3-item cap."""
        result = compose_retrieve_query(
            "Title",
            contributions=["", "A", "", "B", "C", "D"],
        )
        assert result == "Title | A | B | C"

    def test_all_empty_contributions(self) -> None:
        result = compose_retrieve_query(
            "Title",
            contributions=["", "  ", "\n"],
        )
        assert result == "Title"

    def test_empty_list(self) -> None:
        result = compose_retrieve_query("Title", contributions=[])
        assert result == "Title"
