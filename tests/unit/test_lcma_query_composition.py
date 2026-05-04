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
    """AC-08-A case 5: composed query truncated to 500 characters.

    Phrase-quoting adds two quote characters per disjunct (#80), so the
    pre-truncation length is wrapped-length, not raw input length. The
    truncation contract remains a simple slice of the final composed
    string with no word re-wrap.
    """

    def test_long_title_truncated_to_500(self) -> None:
        long_title = "x" * 600
        result = compose_retrieve_query(long_title)
        assert len(result) == 500
        # Wrapped form is `"xxx...xxx"` — leading quote then x's,
        # truncated mid-phrase at 500.
        assert result == '"' + "x" * 499

    def test_long_composed_truncated_to_500(self) -> None:
        title = "t" * 400
        contrib = "c" * 200
        result = compose_retrieve_query(title, contributions=[contrib])
        assert len(result) == 500
        expected_full = f'"{"t" * 400}" | "{"c" * 200}"'
        assert result == expected_full[:500]
