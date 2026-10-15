"""Pairwise judge: position-swap consistency and summary counting."""

from __future__ import annotations

from typing import Any

from evals.judge import Judge, Verdict, summarize_verdicts

from influx.config import AppConfig
from tests.evals.conftest import chat_response


class _Poster:
    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, url: str, body: dict[str, Any], **_: Any):
        self.prompts.append(body["messages"][0]["content"])
        return chat_response(self.answers.pop(0))


def _judge(config: AppConfig, answers: list[dict[str, Any]]) -> tuple[Judge, _Poster]:
    poster = _Poster(answers)
    return Judge(
        config, model="judge", provider="test-provider", poster=poster, concurrency=1
    ), poster  # type: ignore[arg-type]


def test_consistent_verdict_across_swapped_positions(eval_config: AppConfig) -> None:
    # Round 1: A (baseline) vs B (candidate) -> B.  Round 2 swapped: A is the
    # candidate now, so "A" again means the candidate won.
    judge, poster = _judge(
        eval_config,
        [
            {"winner": "B", "a_score": 3, "b_score": 5, "rationale": "more specific"},
            {"winner": "A", "a_score": 5, "b_score": 3, "rationale": "more specific"},
        ],
    )

    (verdict,) = judge.judge_outputs(
        "tier1", [("c1", "task", "source", {"k": "base"}, {"k": "cand"})]
    )

    assert verdict.winner == "candidate"
    assert (verdict.baseline_score, verdict.candidate_score) == (3.0, 5.0)
    assert '"base"' in poster.prompts[0].split("## OUTPUT A")[1].split("## OUTPUT B")[0]
    assert '"cand"' in poster.prompts[1].split("## OUTPUT A")[1].split("## OUTPUT B")[0]


def test_position_bias_collapses_to_tie(eval_config: AppConfig) -> None:
    judge, _ = _judge(
        eval_config,
        [
            {"winner": "A", "a_score": 4, "b_score": 4, "rationale": ""},
            {"winner": "A", "a_score": 4, "b_score": 4, "rationale": ""},
        ],
    )

    (verdict,) = judge.judge_outputs("tier3", [("c1", "t", "s", {}, {})])

    assert verdict.winner == "tie"


def test_filter_flip_rows_include_profile_and_threshold(eval_config: AppConfig) -> None:
    judge, poster = _judge(
        eval_config,
        [
            {"winner": "A", "a_score": 4, "b_score": 2, "rationale": "off-profile"},
            {"winner": "B", "a_score": 2, "b_score": 4, "rationale": "off-profile"},
        ],
    )
    rows = [
        (
            "c1",
            "x",
            "robots only",
            7,
            "Title: T",
            {"score": 8, "reason": "r"},
            {"score": 4, "reason": "n"},
        )
    ]

    (verdict,) = judge.judge_filter_flips(rows)

    assert verdict.winner == "baseline" and verdict.item_id == "x"
    assert (
        "robots only" in poster.prompts[0]
        and "Relevance threshold: 7" in poster.prompts[0]
    )


def test_summary_counts_and_errors() -> None:
    verdicts = [
        Verdict("tier1", "a", "", "baseline", 4, 3, ""),
        Verdict("tier1", "b", "", "candidate", 2, 5, ""),
        Verdict("tier1", "c", "", "tie", 3, 3, ""),
        Verdict("tier1", "d", "", "tie", 0, 0, "", error="HTTP 500"),
    ]

    s = summarize_verdicts(verdicts)

    assert (s.judged, s.baseline_wins, s.candidate_wins, s.ties, s.errors) == (
        3,
        1,
        1,
        1,
        1,
    )
    assert s.baseline_mean == 3.0 and s.candidate_mean == 11 / 3


def test_source_over_cap_is_excluded_not_truncated(eval_config: AppConfig) -> None:
    poster = _Poster([])
    judge = Judge(
        eval_config,
        model="judge",
        provider="test-provider",
        poster=poster,  # type: ignore[arg-type]
        concurrency=1,
        source_char_cap=100,
    )

    (verdict,) = judge.judge_outputs("tier3", [("c1", "t", "x" * 101, {}, {})])

    assert verdict.error.startswith("source is 101 chars")
    assert poster.prompts == []  # never sent a clipped source
    assert summarize_verdicts([verdict]).judged == 0
