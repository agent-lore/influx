"""Pure metric functions: run summaries, filter agreement, output shape."""

from __future__ import annotations

import pytest
from evals.cases import CandidateRecord, CaseResult, FilterCase, Usage
from evals.metrics import (
    DROPPED,
    GateConfusion,
    ThresholdAgreement,
    compare_filter,
    filter_scores,
    grade_filter,
    judge_label_agreement,
    predicted_tier,
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

    def test_flips_use_each_profiles_own_relevance_gate(self) -> None:
        cases = [
            FilterCase("c1", "strict", (CandidateRecord("x", "t", "a"),)),
            FilterCase("c2", "lenient", (CandidateRecord("y", "t", "a"),)),
        ]
        # x: 7 vs 6 flips at strict's 7; y: 6 vs 3 flips at lenient's 6.
        baseline = [
            _result("c1", _filter_output({"x": 7})),
            _result("c2", _filter_output({"y": 6})),
        ]
        candidate = [
            _result("c1", _filter_output({"x": 6})),
            _result("c2", _filter_output({"y": 3})),
        ]

        cmp = compare_filter(
            cases,
            baseline,
            candidate,
            thresholds=(6, 7),
            relevance={"strict": 7, "lenient": 6},
        )

        assert sorted(cmp.disagreements) == [("c1", "x", 7, 6), ("c2", "y", 6, 3)]

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


class TestPredictedTier:
    @pytest.mark.parametrize(
        ("score", "tier"), [(DROPPED, 0), (6, 0), (7, 1), (8, 2), (9, 3), (10, 3)]
    )
    def test_score_maps_to_highest_gate_passed(self, score: int, tier: int) -> None:
        assert predicted_tier(score, (7, 8, 9)) == tier

    def test_gates_are_cumulative_like_the_cascade(self) -> None:
        # Below relevance nothing is ingested, whatever full_text says.
        assert predicted_tier(7, (8, 7, 9)) == 0
        assert predicted_tier(8, (8, 7, 9)) == 2


class TestGateConfusion:
    def test_rates(self) -> None:
        g = GateConfusion("ingest", tp=6, fp=2, fn=3, tn=9)

        assert g.total == 20
        assert g.precision == pytest.approx(0.75)
        assert g.recall == pytest.approx(6 / 9)
        assert g.f1 == pytest.approx(2 * 0.75 * (6 / 9) / (0.75 + 6 / 9))
        assert g.accuracy == pytest.approx(0.75)
        # Same formula the baseline-vs-candidate agreement uses.
        assert g.kappa == pytest.approx(ThresholdAgreement(7, 6, 3, 2, 9).kappa)

    def test_undefined_rates_are_none(self) -> None:
        g = GateConfusion("deep extract", tp=0, fp=0, fn=0, tn=5)

        assert (g.precision, g.recall, g.f1, g.kappa) == (None, None, None, None)
        assert g.accuracy == 1.0

    def test_nothing_graded_is_all_none(self) -> None:
        g = GateConfusion("ingest", tp=0, fp=0, fn=0, tn=0)

        assert (g.accuracy, g.kappa) == (None, None)

    def test_constant_labels_but_varying_model_has_a_kappa(self) -> None:
        assert GateConfusion("ingest", tp=0, fp=2, fn=0, tn=3).kappa == 0.0


class TestGradeFilter:
    _cases = (
        FilterCase(
            case_id="c1",
            profile="a",
            candidates=tuple(CandidateRecord(i, f"T{i}", "") for i in "wxyz"),
        ),
        FilterCase(
            case_id="c2",
            profile="b",
            candidates=(CandidateRecord("v", "Tv", ""), CandidateRecord("x", "Tx", "")),
        ),
    )
    _gates = {"a": (7, 8, 9), "b": (5, 6, 7)}

    def test_per_gate_confusion_with_per_profile_thresholds(self) -> None:
        labels = {
            ("a", "w"): 3,
            ("a", "x"): 1,
            ("a", "y"): 0,
            ("a", "z"): 2,
            ("b", "v"): 1,
        }
        results = [
            _result("c1", _filter_output({"w": 9, "x": 8, "y": 7})),  # z dropped
            _result("c2", _filter_output({"v": 5})),  # 5 passes b's ingest gate
        ]

        g = grade_filter(self._cases, results, labels, self._gates)

        assert (g.labelled, g.graded) == (5, 5)
        ingest, full_text, deep = g.gates
        assert ingest == GateConfusion("ingest", tp=3, fp=1, fn=1, tn=0)
        assert full_text == GateConfusion("full text", tp=1, fp=1, fn=1, tn=2)
        assert deep == GateConfusion("deep extract", tp=1, fp=0, fn=0, tn=4)
        assert g.tier_exact == 2  # w (3/3) and v (1/1)
        assert sorted(m[:2] for m in g.misses) == [("a", "y"), ("a", "z")]
        assert ("a", "z", "Tz", 2, DROPPED) in g.misses

    def test_unlabelled_items_and_failed_batches_are_not_graded(self) -> None:
        labels = {("a", "w"): 3, ("b", "v"): 0}
        results = [
            _result("c1", _filter_output({"w": 9})),
            _result("c2", None, error="validate"),
        ]

        g = grade_filter(self._cases, results, labels, self._gates)

        assert (g.labelled, g.graded) == (2, 1)
        assert g.gates[0] == GateConfusion("ingest", tp=1, fp=0, fn=0, tn=0)

    def test_unknown_profile_falls_back_to_default_gates(self) -> None:
        case = FilterCase("c9", "gone", (CandidateRecord("x", "T", ""),))

        g = grade_filter(
            [case], [_result("c9", _filter_output({"x": 7}))], {("gone", "x"): 1}, {}
        )

        assert g.tier_exact == 1

    def test_item_repeated_across_batches_is_graded_once(self) -> None:
        cases = [
            FilterCase("c1", "a", (CandidateRecord("x", "T", ""),)),
            FilterCase("c2", "a", (CandidateRecord("x", "T", ""),)),
        ]
        results = [
            _result("c1", _filter_output({"x": 9})),
            _result("c2", _filter_output({"x": 2})),
        ]

        g = grade_filter(cases, results, {("a", "x"): 3}, self._gates)

        assert (g.labelled, g.graded, g.tier_exact) == (1, 1, 1)

    def test_repeated_item_graded_on_the_first_batch_the_run_scored(self) -> None:
        cases = [
            FilterCase("c1", "a", (CandidateRecord("x", "T", ""),)),
            FilterCase("c2", "a", (CandidateRecord("x", "T", ""),)),
        ]
        results = [
            _result("c1", None, error="http"),
            _result("c2", _filter_output({"x": 9})),
        ]

        g = grade_filter(cases, results, {("a", "x"): 3}, self._gates)

        assert (g.labelled, g.graded, g.tier_exact) == (1, 1, 1)


class TestJudgeLabelAgreement:
    _profiles = {"c1": "a", "c2": "b"}
    _gates = {"a": (7, 8, 9), "b": (8, 9, 10)}

    def test_judge_checked_against_the_model_the_label_sides_with(self) -> None:
        flips = [
            ("c1", "x", 8, 3),  # label 1: baseline right
            ("c1", "y", 3, 8),  # label 1: candidate right
            ("c1", "z", 9, 0),  # label 0: candidate right
            ("c1", "w", 7, 6),  # unlabelled
        ]
        judged = [
            ("c1", "x", "baseline"),
            ("c1", "y", "baseline"),
            ("c1", "z", "tie"),
            ("c1", "w", "candidate"),
        ]
        labels = {("a", "x"): 1, ("a", "y"): 1, ("a", "z"): 0}

        check = judge_label_agreement(
            judged, flips, self._profiles, labels, self._gates
        )

        assert (check.checked, check.agree, check.disagree, check.ties) == (
            3,
            1,
            1,
            1,
        )

    def test_flip_that_is_not_a_flip_at_the_profile_gate_is_skipped(self) -> None:
        # Both scores sit below profile b's ingest gate of 8.
        check = judge_label_agreement(
            [("c2", "x", "baseline")],
            [("c2", "x", 7, 3)],
            self._profiles,
            {("b", "x"): 0},
            self._gates,
        )

        assert check.checked == 0

    def test_item_judged_in_two_batches_counts_once(self) -> None:
        check = judge_label_agreement(
            [("c1", "x", "baseline"), ("c3", "x", "baseline")],
            [("c1", "x", 8, 3), ("c3", "x", 8, 3)],
            {"c1": "a", "c3": "a"},
            {("a", "x"): 1},
            self._gates,
        )

        assert (check.checked, check.agree) == (1, 1)
