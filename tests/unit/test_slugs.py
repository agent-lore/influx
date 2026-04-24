"""Tests for influx.slugs — FR-ST-2 feed-name slugification."""

from __future__ import annotations

import pytest

from influx.slugs import is_valid_slug, slugify_feed_name

# ---------------------------------------------------------------------------
# slugify_feed_name — representative free-form inputs
# ---------------------------------------------------------------------------


class TestSlugifyFeedName:
    """Cover uppercase, spaces, accents, special chars, truncation, empty."""

    def test_lowercase_and_spaces(self) -> None:
        assert slugify_feed_name("My Cool Feed") == "my-cool-feed"

    def test_uppercase_letters(self) -> None:
        assert slugify_feed_name("ALLCAPS") == "allcaps"

    def test_mixed_case_with_numbers(self) -> None:
        assert slugify_feed_name("Feed 42 News") == "feed-42-news"

    def test_accented_characters(self) -> None:
        assert slugify_feed_name("Café Résumé") == "cafe-resume"

    def test_special_characters_replaced(self) -> None:
        result = slugify_feed_name("hello_world! @#$ test")
        assert result == "hello-world-test"

    def test_leading_trailing_hyphens_stripped(self) -> None:
        assert slugify_feed_name("---hello---") == "hello"

    def test_consecutive_separators_collapsed(self) -> None:
        assert slugify_feed_name("a   b---c") == "a-b-c"

    def test_truncation_at_40_chars(self) -> None:
        long_name = "a" * 50
        result = slugify_feed_name(long_name)
        assert len(result) <= 40
        assert result == "a" * 40

    def test_truncation_removes_trailing_hyphen(self) -> None:
        # 39 a's + space + b -> "aaa...a-b" but at 40 chars the hyphen
        # could land at position 40; ensure no trailing hyphen.
        name = "a" * 39 + " b"
        result = slugify_feed_name(name)
        assert len(result) <= 40
        assert not result.endswith("-")

    def test_empty_string(self) -> None:
        assert slugify_feed_name("") == ""

    def test_whitespace_only(self) -> None:
        assert slugify_feed_name("   ") == ""

    def test_all_special_chars(self) -> None:
        assert slugify_feed_name("@#$%^&*") == ""

    def test_result_matches_frst2_pattern(self) -> None:
        """Non-empty results must always satisfy FR-ST-2."""
        inputs = [
            "My Blog",
            "feed-123",
            "  Ünïcödë  Stuff ",
            "a" * 100,
            "Hello World 2024",
        ]
        for name in inputs:
            result = slugify_feed_name(name)
            assert result, f"expected non-empty slug for {name!r}"
            assert is_valid_slug(result), (
                f"slug {result!r} from {name!r} doesn't match FR-ST-2"
            )

    def test_already_valid_slug_unchanged(self) -> None:
        assert slugify_feed_name("my-feed") == "my-feed"

    def test_numeric_only(self) -> None:
        assert slugify_feed_name("12345") == "12345"


# ---------------------------------------------------------------------------
# is_valid_slug — pattern and length checks
# ---------------------------------------------------------------------------


class TestIsValidSlug:
    """Verify FR-ST-2 regex and length enforcement."""

    @pytest.mark.parametrize(
        "slug",
        [
            "abc",
            "a1",
            "feed-name",
            "my-cool-feed-123",
            "a" * 40,
        ],
    )
    def test_valid_slugs(self, slug: str) -> None:
        assert is_valid_slug(slug) is True

    @pytest.mark.parametrize(
        ("slug", "reason"),
        [
            ("", "empty"),
            ("-abc", "leading hyphen"),
            ("abc-", "trailing hyphen"),
            ("a--b", "consecutive hyphens"),
            ("ABC", "uppercase"),
            ("hello world", "contains space"),
            ("a" * 41, "exceeds 40 chars"),
            ("hello_world", "contains underscore"),
        ],
    )
    def test_invalid_slugs(self, slug: str, reason: str) -> None:
        assert is_valid_slug(slug) is False, f"should reject: {reason}"
