"""Golden-file tests for compose_retrieve_query (US-001, AC-08-A/B).

The ``GOLDEN_CASES`` table is the authoritative behavioural contract for
``compose_retrieve_query``. New behavioural cases should be added to the
table; class-based tests cover scenarios that are awkward to express
inline (e.g. very long strings).

Each disjunct is wrapped in double quotes so Tantivy parses it as a
phrase query rather than as field-qualified terms. This neutralises
reserved characters in academic titles like ``Topic: Subtitle`` (#80).
"""

from __future__ import annotations

import pytest

from influx.lcma import compose_retrieve_query

# ── Golden table ───────────────────────────────────────────────────
#
# Each row: (case_id, title, contributions, expected_query)
#
# Cases AC-08-A-* mirror the five canonical AC-08-A scenarios from
# US-001. Cases AC-08-B-* exercise whitespace collapsing. Cases
# FR-LCMA-2-* exercise the "first up to 3 list elements, then skip
# empties" rule from FR-LCMA-2 step 2. Cases ISSUE-80-* exercise the
# Tantivy phrase-quoting added to fix #80.
GOLDEN_CASES: list[tuple[str, str, list[str] | None, str]] = [
    # AC-08-A: 5 canonical cases
    ("AC-08-A-1: title only", "My Paper Title", None, '"My Paper Title"'),
    (
        "AC-08-A-2: title + 1 contribution",
        "Paper A",
        ["Novel architecture"],
        '"Paper A" | "Novel architecture"',
    ),
    (
        "AC-08-A-3: title + 3 contributions (all used)",
        "Paper B",
        ["First", "Second", "Third"],
        '"Paper B" | "First" | "Second" | "Third"',
    ),
    (
        "AC-08-A-4: title + 5 contributions (only first 3 used)",
        "Paper C",
        ["A", "B", "C", "D", "E"],
        '"Paper C" | "A" | "B" | "C"',
    ),
    # (case 5 — long-title truncation — covered in TestTruncation below)
    # AC-08-B: whitespace collapse
    ("AC-08-B-1: newlines collapsed", "hello\n\nworld", None, '"hello world"'),
    ("AC-08-B-2: tabs collapsed", "hello\t\tworld", None, '"hello world"'),
    ("AC-08-B-3: mixed whitespace", "hello  \n\t  world", None, '"hello world"'),
    (
        "AC-08-B-4: whitespace inside contributions",
        "Title",
        ["first\n\ncontrib", "second  contrib"],
        '"Title" | "first contrib" | "second contrib"',
    ),
    # FR-LCMA-2 step 2: first up to 3 elements, trim, skip empties.
    (
        "FR-LCMA-2-a: empty string in first slot is skipped",
        "Title",
        ["", "Valid"],
        '"Title" | "Valid"',
    ),
    (
        "FR-LCMA-2-b: whitespace-only entries skipped",
        "Title",
        ["   ", "\t\n", "Real"],
        '"Title" | "Real"',
    ),
    (
        "FR-LCMA-2-c: empties WITHIN first 3 dropped, not replaced by later entries",
        "Title",
        ["", "A", "", "B", "C", "D"],
        '"Title" | "A"',
    ),
    (
        "FR-LCMA-2-d: all-empty contributions",
        "Title",
        ["", "  ", "\n"],
        '"Title"',
    ),
    (
        "FR-LCMA-2-e: empty contributions list",
        "Title",
        [],
        '"Title"',
    ),
    (
        "FR-LCMA-2-f: explicit None",
        "My Paper Title",
        None,
        '"My Paper Title"',
    ),
    # Issue #80: Tantivy phrase-quoting neutralises reserved chars
    # (`:`, `(`, `)`, `'`) inside titles and contributions. Real-world
    # arXiv titles drawn from the 2026-05-04 staging burst.
    (
        "ISSUE-80-a: colon in title (Topic: Subtitle)",
        "When LLMs Stop Following Steps: A Diagnostic Study",
        None,
        '"When LLMs Stop Following Steps: A Diagnostic Study"',
    ),
    (
        "ISSUE-80-b: colon in title with contributions",
        "RunAgent: Interpreting Natural-Language Plans",
        ["Multi-agent platform", "Constraint-guided execution"],
        (
            '"RunAgent: Interpreting Natural-Language Plans"'
            ' | "Multi-agent platform"'
            ' | "Constraint-guided execution"'
        ),
    ),
    (
        "ISSUE-80-c: single quote (apostrophe) in title",
        "What's in a Token: Tokenizer Effects on LLM Reasoning",
        None,
        '"What\'s in a Token: Tokenizer Effects on LLM Reasoning"',
    ),
    (
        "ISSUE-80-d: parentheses in title",
        "BlenderRAG (with code synthesis)",
        None,
        '"BlenderRAG (with code synthesis)"',
    ),
    (
        "ISSUE-80-e: double quotes inside content are escaped",
        'A "Quoted" Phrase Title',
        None,
        '"A \\"Quoted\\" Phrase Title"',
    ),
    (
        "ISSUE-80-f: backslash inside content is escaped",
        "Path\\with\\backslash Title",
        None,
        '"Path\\\\with\\\\backslash Title"',
    ),
    (
        "ISSUE-80-g: colon in contribution",
        "Title",
        ["Contribution: with colon"],
        '"Title" | "Contribution: with colon"',
    ),
]


@pytest.mark.parametrize(
    ("case_id", "title", "contributions", "expected"),
    GOLDEN_CASES,
    ids=[row[0] for row in GOLDEN_CASES],
)
def test_compose_retrieve_query_golden(
    case_id: str,
    title: str,
    contributions: list[str] | None,
    expected: str,
) -> None:
    """Golden-table assertion for compose_retrieve_query."""
    del case_id  # surfaced via parametrize ids
    assert compose_retrieve_query(title, contributions) == expected


class TestTruncation:
    """Composed query bounded to 500 characters AND syntactically balanced.

    Phrase-quoting adds two quote characters per disjunct (#80), and a
    naive tail-slice can split a phrase mid-string and produce an
    unmatched opening ``"`` — exactly the parse-failure class the patch
    set out to eliminate (review on PR #84). Truncation is therefore
    applied to the *raw* title content before wrapping, and any
    contribution whose wrapped form would not fit whole is dropped
    rather than sliced.
    """

    def test_long_title_truncated_to_500_with_balanced_quotes(self) -> None:
        long_title = "x" * 600
        result = compose_retrieve_query(long_title)
        assert len(result) == 500
        # Surviving form is `"xxx...xxx"` — both quotes intact, 498 x's
        # between them.
        assert result == '"' + "x" * 498 + '"'
        assert result.startswith('"')
        assert result.endswith('"')
        assert result.count('"') == 2

    def test_title_at_exact_budget_unchanged(self) -> None:
        # A 498-char title wraps to exactly 500 — unchanged.
        title = "y" * 498
        result = compose_retrieve_query(title)
        assert len(result) == 500
        assert result == '"' + "y" * 498 + '"'

    def test_title_one_over_budget_loses_one_char(self) -> None:
        # A 499-char title wraps to 501 — drop one inner char.
        title = "z" * 499
        result = compose_retrieve_query(title)
        assert len(result) == 500
        assert result == '"' + "z" * 498 + '"'

    def test_title_truncation_does_not_leave_dangling_backslash(self) -> None:
        # A title that is all backslashes escapes to twice its length;
        # truncating the escaped form mid-`\\` would leave a dangling
        # backslash. The fitter must shave it off.
        title = "\\" * 300  # escapes to 600 backslashes
        result = compose_retrieve_query(title)
        assert len(result) <= 500
        assert result.startswith('"')
        assert result.endswith('"')
        # Inner content is even-length pairs of backslashes only.
        inner = result[1:-1]
        assert inner.count("\\") % 2 == 0

    def test_long_composed_drops_contribution_that_will_not_fit(self) -> None:
        # Title wrapped is 402 chars; budget after title is 500 - 402 - 3 = 95
        # for the next contribution. A 200-char contribution wraps to 202
        # chars and must therefore be dropped entirely (not sliced).
        title = "t" * 400
        contrib = "c" * 200
        result = compose_retrieve_query(title, contributions=[contrib])
        assert len(result) <= 500
        assert result == '"' + "t" * 400 + '"'
        assert result.startswith('"')
        assert result.endswith('"')
        assert result.count('"') == 2

    def test_partial_fit_keeps_fitting_contributions_drops_rest(self) -> None:
        # Title 200, contrib1 100, contrib2 200 — first contribution
        # fits (`"<200>"` + ` | ` + `"<100>"` = 202 + 3 + 102 = 307),
        # second would push to 307 + 3 + 202 = 512 > 500, so contrib2
        # is dropped, contrib1 is kept whole.
        title = "t" * 200
        contrib_short = "a" * 100
        contrib_long = "b" * 200
        result = compose_retrieve_query(
            title, contributions=[contrib_short, contrib_long]
        )
        assert len(result) <= 500
        assert result == f'"{"t" * 200}" | "{"a" * 100}"'
        # All quotes balanced.
        assert result.count('"') % 2 == 0

    def test_result_always_has_balanced_quotes(self) -> None:
        # Stress: many titles of varying sizes around the boundary,
        # each must produce a balanced query. Backslash counts are
        # tracked separately via inner-content parity.
        for n in (1, 100, 250, 497, 498, 499, 500, 501, 600, 1000):
            result = compose_retrieve_query("x" * n)
            assert len(result) <= 500
            assert result.startswith('"')
            assert result.endswith('"')
            # Even number of unescaped quotes — for a pure-x title the
            # only quotes are the two surrounding ones.
            assert result.count('"') == 2
