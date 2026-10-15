"""Dataset building from canonical notes and candidate pools."""

from __future__ import annotations

import random
from pathlib import Path

from evals.cases import CandidateRecord, FilterCase
from evals.dataset import (
    DatasetSpec,
    build_tier1_cases,
    build_tier3_cases,
    tier3_case_from_note,
)

_NOTE = """---
title: 'A Paper: With Colon'
source_url: https://arxiv.org/abs/2610.00001
---

# A Paper: With Colon

## Archive
path: arxiv/2026/10/2610.00001.pdf

## Summary
### Contributions
- thing

## Full Text
{body}

## Claims
- claim one
- claim two

## Datasets & Benchmarks

## Builds On
- Attention (arXiv:1706.03762)

## Open Questions
- q

## Profile Relevance
### ai-agents
Score: 9/10
reason

## User Notes
"""


def test_tier3_case_from_note_takes_full_text_and_reference() -> None:
    body = "x" * 100
    case = tier3_case_from_note(_NOTE.format(body=body), min_chars=10, max_chars=1000)

    assert case is not None
    assert case.title == "A Paper: With Colon"
    assert case.full_text == body
    assert case.reference == {
        "claims": ["claim one", "claim two"],
        "datasets": [],
        "builds_on": ["Attention (arXiv:1706.03762)"],
        "open_questions": ["q"],
    }


def test_tier3_case_rejects_text_outside_bounds() -> None:
    assert (
        tier3_case_from_note(_NOTE.format(body="short"), min_chars=10, max_chars=1000)
        is None
    )
    assert (
        tier3_case_from_note(
            _NOTE.format(body="y" * 2000), min_chars=10, max_chars=1000
        )
        is None
    )


def test_build_tier3_cases_is_seeded_and_capped(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"n{i}.md").write_text(
            _NOTE.format(body=f"{i}" * 50), encoding="utf-8"
        )
    (tmp_path / "no-tier3.md").write_text(
        "# T\n\n## Full Text\n" + "z" * 50 + "\n", encoding="utf-8"
    )
    spec = DatasetSpec(
        name="t", tier3_limit=3, tier3_min_chars=10, tier3_max_chars=1000
    )

    first = build_tier3_cases(tmp_path, spec, random.Random(1))
    second = build_tier3_cases(tmp_path, spec, random.Random(1))

    assert len(first) == 3
    assert [c.case_id for c in first] == [c.case_id for c in second]


def test_build_tier1_cases_samples_per_profile() -> None:
    cases = [
        FilterCase(
            "f1", "a", tuple(CandidateRecord(str(i), "t", "abs") for i in range(10))
        ),
        FilterCase("f2", "b", (CandidateRecord("x", "t", "abs"),)),
    ]
    spec = DatasetSpec(name="t", tier1_per_profile=3)

    out = build_tier1_cases(cases, spec, random.Random(0))

    assert [c.profile for c in out].count("a") == 3
    assert [c.profile for c in out].count("b") == 1
    assert all(c.abstract == "abs" for c in out)
