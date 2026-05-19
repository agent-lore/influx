"""Unit tests for :mod:`influx.thin_summary` (issue #166).

The module is pure (no IO, no global state) so every test passes
synthetic strings directly.  The goal is to pin the precise contract
between the source adapters and the structural rule so an accidental
broadening / narrowing is loud.
"""

from __future__ import annotations

import pytest

from influx.thin_summary import (
    BOILERPLATE_PATTERNS,
    _normalize_for_equality,
    is_thin_summary,
)

# Sentinel non-thin string used whenever a test wants to isolate one
# rule by feeding the others a known-passing input.  Padded with real
# words so the length / boilerplate / title-equality checks all see
# something distinct.
_NON_THIN_SUMMARY = (
    "A meaningful summary of roughly two hundred characters describing "
    "the article body in enough detail that no structural thin-summary "
    "rule would ever fire on it under default configuration."
)


class TestNormalizeForEquality:
    """``_normalize_for_equality`` strips punctuation, casing, and folds whitespace."""

    def test_lowercases(self) -> None:
        assert _normalize_for_equality("Hello") == "hello"

    def test_collapses_internal_whitespace(self) -> None:
        assert _normalize_for_equality("hello   world\t\n  here") == "hello world here"

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        assert _normalize_for_equality("   hello world   ") == "hello world"

    def test_strips_ascii_punctuation(self) -> None:
        assert _normalize_for_equality("Hello, world!") == "hello world"

    def test_strips_unicode_punctuation(self) -> None:
        # Curly quotes (Unicode Pi / Pf) and an em dash (Pd) all strip.
        assert _normalize_for_equality("“Hello” — world") == "hello world"

    def test_empty_string_round_trips(self) -> None:
        assert _normalize_for_equality("") == ""


class TestLengthRule:
    """The length rule fires first; below ``min_chars`` => thin."""

    def test_below_min_chars_is_thin(self) -> None:
        thin, rule = is_thin_summary(
            summary="short string",  # 12 chars
            title="completely unrelated title",
            min_chars=80,
        )
        assert thin is True
        assert rule == "length"

    def test_above_min_chars_passes_length_rule(self) -> None:
        thin, rule = is_thin_summary(
            summary=_NON_THIN_SUMMARY,
            title="completely unrelated title",
            min_chars=80,
        )
        assert thin is False
        assert rule is None

    def test_at_min_chars_passes(self) -> None:
        # Strict ``<`` boundary: exactly ``min_chars`` characters is OK.
        summary = "a" * 80
        thin, rule = is_thin_summary(
            summary=summary,
            title="completely unrelated title",
            min_chars=80,
        )
        # 80 chars passes the length rule; the rest of the rules also pass.
        assert thin is False
        assert rule is None

    def test_min_chars_zero_disables_length_rule(self) -> None:
        # 5-char summary is well below any positive threshold.  With
        # ``min_chars=0`` the length rule is off; title-equality and
        # boilerplate still get a chance — pass both.
        thin, rule = is_thin_summary(
            summary="short",
            title="completely unrelated title",
            min_chars=0,
        )
        assert thin is False
        assert rule is None

    def test_negative_min_chars_treated_as_zero(self) -> None:
        # Defensive: a misconfigured override should not raise inside
        # the source adapter.  Negative is clamped to zero, so a short
        # non-boilerplate summary passes.
        thin, rule = is_thin_summary(
            summary="short but real",
            title="completely unrelated title",
            min_chars=-5,
        )
        assert thin is False
        assert rule is None

    def test_whitespace_only_summary_counts_as_zero_length(self) -> None:
        # ``len(trimmed)`` is what the rule checks, so a summary that
        # is just whitespace fires the length rule even though the raw
        # string is technically long.
        thin, rule = is_thin_summary(
            summary="   \n\t   ",
            title="completely unrelated title",
            min_chars=10,
        )
        assert thin is True
        assert rule == "length"


class TestTitleEqualityRule:
    """Summary equals title under the normalisation transform."""

    def test_exact_match_fires(self) -> None:
        thin, rule = is_thin_summary(
            summary="The Foo Bar",
            title="The Foo Bar",
            min_chars=0,
        )
        assert thin is True
        assert rule == "title_equality"

    def test_punctuation_difference_still_matches(self) -> None:
        thin, rule = is_thin_summary(
            summary="The Foo: Bar!",
            title="The Foo, Bar",
            min_chars=0,
        )
        assert thin is True
        assert rule == "title_equality"

    def test_casing_difference_still_matches(self) -> None:
        thin, rule = is_thin_summary(
            summary="THE FOO BAR",
            title="the foo bar",
            min_chars=0,
        )
        assert thin is True
        assert rule == "title_equality"

    def test_whitespace_difference_still_matches(self) -> None:
        thin, rule = is_thin_summary(
            summary="The  Foo\tBar",
            title="The Foo Bar",
            min_chars=0,
        )
        assert thin is True
        assert rule == "title_equality"

    def test_extra_content_in_summary_is_not_equality(self) -> None:
        # Same title prefix plus extra real content => not title-equal.
        # min_chars=0 disables length so we isolate the equality rule.
        thin, rule = is_thin_summary(
            summary="The Foo Bar by Author Name on Publisher (2026)",
            title="The Foo Bar",
            min_chars=0,
        )
        # Doesn't match equality and doesn't match boilerplate either.
        assert thin is False
        assert rule is None


class TestBoilerplateRule:
    """Each :data:`BOILERPLATE_PATTERNS` entry fires on a representative string."""

    @pytest.mark.parametrize(
        ("summary",),
        [
            ("Discussion (47 points)",),
            ("Discussion (1 point)",),
            ("DISCUSSION (47 POINTS)",),  # case-insensitive
        ],
    )
    def test_hn_points_pointer(self, summary: str) -> None:
        thin, rule = is_thin_summary(
            summary=summary,
            title="completely unrelated title",
            min_chars=0,
        )
        assert thin is True
        assert rule == "boilerplate"

    @pytest.mark.parametrize(
        ("summary",),
        [
            ("Comments",),
            ("Comments.",),
            ("Comments ",),
        ],
    )
    def test_comments_pointer(self, summary: str) -> None:
        thin, rule = is_thin_summary(
            summary=summary,
            title="completely unrelated title",
            min_chars=0,
        )
        assert thin is True
        assert rule == "boilerplate"

    @pytest.mark.parametrize(
        ("summary",),
        [
            ("Read the full article at example.com tomorrow",),
            ("Read this full post on Medium",),
            ("Read the entire story at the publisher",),
            ("Read the article via the syndication feed",),
        ],
    )
    def test_generic_teaser_pointer(self, summary: str) -> None:
        thin, rule = is_thin_summary(
            summary=summary,
            title="completely unrelated title",
            min_chars=0,
        )
        assert thin is True
        assert rule == "boilerplate"

    @pytest.mark.parametrize(
        ("summary",),
        [
            ("Continue reading",),
            ("Continue reading at the source",),
            ("CONTINUE READING",),
        ],
    )
    def test_continue_reading_truncation(self, summary: str) -> None:
        thin, rule = is_thin_summary(
            summary=summary,
            title="completely unrelated title",
            min_chars=0,
        )
        assert thin is True
        assert rule == "boilerplate"

    @pytest.mark.parametrize(
        ("summary",),
        [
            ("Read more",),
            ("Read more.",),
            ("Read more at example.com",),
        ],
    )
    def test_read_more_truncation(self, summary: str) -> None:
        thin, rule = is_thin_summary(
            summary=summary,
            title="completely unrelated title",
            min_chars=0,
        )
        assert thin is True
        assert rule == "boilerplate"

    @pytest.mark.parametrize(
        ("summary",),
        [
            ("...",),
            ("…",),
            ("[…]",),
            ("[ ]",),
            (".....",),
        ],
    )
    def test_empty_ish_markers(self, summary: str) -> None:
        thin, rule = is_thin_summary(
            summary=summary,
            title="completely unrelated title",
            min_chars=0,
        )
        assert thin is True
        assert rule == "boilerplate"

    def test_legitimate_body_with_continue_reading_inline_passes(self) -> None:
        # The pattern is anchored at the start of the *trimmed* summary,
        # so an inline "continue reading" inside real body text does not
        # trip the rule.
        summary = (
            "This is a legitimate article body discussing the topic in "
            "depth.  Authors continue reading the literature throughout."
        )
        thin, rule = is_thin_summary(
            summary=summary,
            title="A long descriptive title",
            min_chars=0,
        )
        assert thin is False
        assert rule is None


class TestRulePrecedence:
    """When multiple rules would fire, the first match wins (length → equality → bp)."""

    def test_short_boilerplate_reports_length(self) -> None:
        # "Read more" is also boilerplate but length fires first.
        thin, rule = is_thin_summary(
            summary="Read more",
            title="completely unrelated title",
            min_chars=80,
        )
        assert thin is True
        # Length is evaluated before boilerplate, so the rule label
        # surfaces the cheapest-actionable signal.
        assert rule == "length"

    def test_title_equality_reports_equality_over_boilerplate(self) -> None:
        # A summary that equals the title (after normalisation) AND
        # would also match boilerplate — equality is reported first.
        thin, rule = is_thin_summary(
            summary="Read more",
            title="read more",
            min_chars=0,
        )
        assert thin is True
        assert rule == "title_equality"


class TestNonThinPasses:
    """The default 80-char threshold lets meaningful summaries through."""

    def test_substantive_summary_passes_default_threshold(self) -> None:
        thin, rule = is_thin_summary(
            summary=_NON_THIN_SUMMARY,
            title="A long descriptive title that does not match the summary",
            min_chars=80,
        )
        assert thin is False
        assert rule is None

    def test_realistic_arxiv_abstract_passes(self) -> None:
        # ~ 250-character snippet typical of arXiv abstracts.
        abstract = (
            "We present a novel architecture for self-supervised learning "
            "on graph-structured data that improves downstream node "
            "classification accuracy by 4.7% on benchmark datasets while "
            "reducing training time by an order of magnitude over prior work."
        )
        thin, rule = is_thin_summary(
            summary=abstract,
            title="Self-Supervised Graph Learning",
            min_chars=80,
        )
        assert thin is False
        assert rule is None


class TestBoilerplatePatternsExposed:
    """The pattern list is part of the public contract (operators read it)."""

    def test_each_pattern_has_a_rationale(self) -> None:
        # Module-level constant ships ``(regex, rationale)`` pairs.  An
        # empty rationale would defeat the purpose of the registry.
        for pattern, rationale in BOILERPLATE_PATTERNS:
            assert pattern.pattern, "pattern must be non-empty"
            assert rationale.strip(), f"pattern {pattern.pattern!r} missing rationale"

    def test_pattern_count_is_at_least_six(self) -> None:
        # Six patterns ship today; future additions should never reduce
        # the set without an explicit migration.  This is a low-stakes
        # canary that catches accidental deletions.
        assert len(BOILERPLATE_PATTERNS) >= 6
