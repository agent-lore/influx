"""Pure metric functions: run summaries, filter agreement, output shape."""

from __future__ import annotations

import pytest
from evals.cases import CandidateRecord, CaseResult, FilterCase, Usage
from evals.metrics import (
    DROPPED,
    ThresholdAgreement,
    compare_filter,
    filter_scores,
    summarize_run,
    summarize_shape,
)


def _result(
    case_id: str, output: dict | None, *, error: str | None = None, **kw
) -> CaseResult:
    base = {
        "slot": "filter",
        "model": "m",
        "latency_seconds": 1.0,
        "usage": Usage(100, 10, 0, 0.001),
        "meta": {"from_cache": False},
    }
    base.update(kw)
    return CaseResult(case_id=case_id, output=output, error=error, **base)


def _filter_output(scores: dict[str, int]) -> dict:
    return {
        "results": [
            {"id": k, "score": v, "tags": [], "reason": "r"} for k, v in scores.items()
        ]
    }


class TestSummarizeRun:
    def test_counts_validity_latency_and_cost(self) -> None:
        results = [
            _result("a", {"results": []}, latency_seconds=1.0),
            _result("b", {"results": []}, latency_seconds=3.0),
            _result("c", None, error="http", attempts=3, latency_seconds=2.0),
        ]

        s = summarize_run(results)

        assert (s.cases, s.ok, s.errors, s.retried) == (3, 2, {"http": 1}, 1)
        assert s.validity_rate == pytest.approx(2 / 3)
        assert s.latency_p50 == 2.0
        assert s.latency_mean == pytest.approx(2.0)
        assert s.prompt_tokens == 300
        assert s.cost_usd == pytest.approx(0.003)
        assert s.cost_per_case == pytest.approx(0.001)

    def test_unknown_cost_on_any_case_makes_total_unknown(self) -> None:
        results = [
            _result("a", {"results": []}),
            _result("b", {"results": []}, usage=Usage(1, 1, 0, None)),
        ]

        assert summarize_run(results).cost_usd is None

    def test_cached_results_keep_their_measured_latency(self) -> None:
        results = [
            _result("a", {"results": []}, latency_seconds=5.0),
            _result(
                "b", {"results": []}, latency_seconds=3.0, meta={"from_cache": True}
            ),
            _result("c", {"results": []}, latency_seconds=1.0, meta={"salvaged": True}),
        ]

        s = summarize_run(results)

        assert (s.cached, s.salvaged) == (1, 1)
        assert s.latency_mean == 3.0

    def test_empty_results_rejected(self) -> None:
        with pytest.raises(ValueError):
            summarize_run([])


class TestCompareFilter:
    _case = FilterCase(
        case_id="c1",
        profile="p",
        candidates=tuple(CandidateRecord(i, "t", "a") for i in ("x", "y", "z", "w")),
    )

    def test_scores_and_threshold_confusion(self) -> None:
        baseline = [_result("c1", _filter_output({"x": 9, "y": 7, "z": 6}))]
        candidate = [_result("c1", _filter_output({"x": 8, "y": 6, "w": 7}))]

        cmp = compare_filter([self._case], baseline, candidate, thresholds=(7,))

        assert cmp.candidates == 4
        assert cmp.both_scored == 2  # x and y
        assert cmp.exact == 0
        assert cmp.within_one == 2
        assert cmp.mean_abs_error == pytest.approx(1.0)
        (t7,) = cmp.thresholds
        assert t7 == ThresholdAgreement(
            7, both_pass=1, baseline_only=1, candidate_only=1, neither=1
        )
        assert t7.agreement == 0.5
        assert sorted(cmp.disagreements) == [("c1", "w", DROPPED, 7), ("c1", "y", 7, 6)]

    def test_perfect_agreement_gives_kappa_one(self) -> None:
        out = _filter_output({"x": 9, "y": 3})
        cmp = compare_filter([self._case], [_result("c1", out)], [_result("c1", out)])

        assert cmp.thresholds[0].kappa == 1.0
        assert cmp.disagreements == ()

    def test_failed_case_on_either_side_is_skipped(self) -> None:
        baseline = [_result("c1", _filter_output({"x": 9}))]
        candidate = [_result("c1", None, error="validate")]

        cmp = compare_filter([self._case], baseline, candidate)

        assert cmp.comparable_cases == 0
        assert cmp.candidates == 0

    def test_filter_scores_of_failed_result_is_empty(self) -> None:
        assert filter_scores(_result("c1", None, error="http")) == {}


class TestSummarizeShape:
    def test_tier3_shape_and_arxiv_ref_rate(self) -> None:
        results = [
            _result(
                "a",
                {
                    "claims": ["c1", "c2"],
                    "datasets": [],
                    "builds_on": ["Attention (arXiv:1706.03762)", "a book"],
                    "open_questions": ["q"],
                    "potential_connections": [],
                },
                slot="tier3",
            ),
            _result("b", None, error="http", slot="tier3"),
        ]

        s = summarize_shape(results)

        assert s.ok == 1
        assert s.list_means["claims"] == 2.0
        assert s.empty_list_rate["datasets"] == 1.0
        assert s.arxiv_ref_rate == 0.5

    def test_tier1_text_lengths_and_no_arxiv_rate(self) -> None:
        results = [
            _result(
                "a",
                {
                    "contributions": ["x"],
                    "method": "abcd",
                    "result": "ab",
                    "relevance": "",
                },
                slot="tier1",
            )
        ]

        s = summarize_shape(results)

        assert s.text_mean_chars == {"method": 4.0, "result": 2.0, "relevance": 0.0}
        assert s.arxiv_ref_rate is None
