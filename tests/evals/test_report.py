"""Markdown rendering of the label grades."""

from __future__ import annotations

from typing import Any

from evals.metrics import DROPPED, GateConfusion, LabelGrade
from evals.report import render_label_grades, render_label_misses


def _grade(**kw: Any) -> LabelGrade:
    base: dict[str, Any] = {
        "labelled": 2,
        "graded": 2,
        "tier_exact": 0,
        "gates": (
            GateConfusion("ingest", tp=0, fp=1, fn=1, tn=0),
            GateConfusion("full text", tp=0, fp=0, fn=0, tn=2),
            GateConfusion("deep extract", tp=0, fp=0, fn=0, tn=2),
        ),
    }
    base.update(kw)
    return LabelGrade(**base)


def test_undefined_rates_render_as_na() -> None:
    text = render_label_grades([("run", _grade())])

    assert (
        "| run | full text | 2 | n/a | n/a | n/a | 100% | n/a | 0 | 0 | 0 | 2 |" in text
    )


def test_misses_say_which_way_the_model_was_wrong() -> None:
    grade = _grade(
        misses=(
            ("p", "x", "Wanted | but dropped", 2, DROPPED),
            ("p", "y", "Noise", 0, 8),
        )
    )

    text = render_label_misses(grade)

    assert (
        r"| p | x | Wanted \| but dropped | 2 full text | omitted | rejected (FN) |"
        in text
    )
    assert "| p | y | Noise | 0 skip | 8 | ingested (FP) |" in text


def test_no_misses() -> None:
    assert render_label_misses(_grade()) == "No ingest-decision misses."
