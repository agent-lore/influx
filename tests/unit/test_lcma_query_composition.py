"""Golden-file tests for compose_retrieve_query (US-001, AC-08-A/B).

The ``GOLDEN_CASES`` table is the authoritative behavioural contract for
``compose_retrieve_query``. New behavioural cases should be added to the
table; class-based tests cover scenarios that are awkward to express
inline (e.g. very long strings).
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
# empties" rule from FR-LCMA-2 step 2.
GOLDEN_CASES: list[tuple[str, str, list[str] | None, str]] = [
    # AC-08-A: 5 canonical cases
    ("AC-08-A-1: title only", "My Paper Title", None, "My Paper Title"),
    (
        "AC-08-A-2: title + 1 contribution",
        "Paper A",
        ["Novel architecture"],
        "Paper A | Novel architecture",
    ),
    (
        "AC-08-A-3: title + 3 contributions (all used)",
        "Paper B",
        ["First", "Second", "Third"],
        "Paper B | First | Second | Third",
    ),
    (
        "AC-08-A-4: title + 5 contributions (only first 3 used)",
        "Paper C",
        ["A", "B", "C", "D", "E"],
        "Paper C | A | B | C",
    ),
    # (case 5 — long-title truncation — covered in TestTruncation below)
    # AC-08-B: whitespace collapse
    ("AC-08-B-1: newlines collapsed", "hello\n\nworld", None, "hello world"),
    ("AC-08-B-2: tabs collapsed", "hello\t\tworld", None, "hello world"),
    ("AC-08-B-3: mixed whitespace", "hello  \n\t  world", None, "hello world"),
    (
        "AC-08-B-4: whitespace inside contributions",
        "Title",
        ["first\n\ncontrib", "second  contrib"],
        "Title | first contrib | second contrib",
    ),
    # FR-LCMA-2 step 2: first up to 3 elements, trim, skip empties.
    (
        "FR-LCMA-2-a: empty string in first slot is skipped",
        "Title",
        ["", "Valid"],
        "Title | Valid",
    ),
    (
        "FR-LCMA-2-b: whitespace-only entries skipped",
        "Title",
        ["   ", "\t\n", "Real"],
        "Title | Real",
    ),
    (
        "FR-LCMA-2-c: empties WITHIN first 3 dropped, not replaced by later entries",
        "Title",
        ["", "A", "", "B", "C", "D"],
        "Title | A",
    ),
    (
        "FR-LCMA-2-d: all-empty contributions",
        "Title",
        ["", "  ", "\n"],
        "Title",
    ),
    (
        "FR-LCMA-2-e: empty contributions list",
        "Title",
        [],
        "Title",
    ),
    (
        "FR-LCMA-2-f: explicit None",
        "My Paper Title",
        None,
        "My Paper Title",
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
