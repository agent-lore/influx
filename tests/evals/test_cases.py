"""JSONL round-trips for eval cases and results."""

from __future__ import annotations

from pathlib import Path

from evals.cases import (
    CandidateRecord,
    CaseResult,
    FilterCase,
    Tier1Case,
    Tier3Case,
    Usage,
    load_dataset,
    read_results,
    write_jsonl,
)


def test_dataset_round_trip(tmp_path: Path) -> None:
    filter_case = FilterCase(
        case_id="f1",
        profile="ai-robotics",
        candidates=(CandidateRecord("2610.00001", "T", "A"),),
        source="arxiv",
    )
    tier1_case = Tier1Case(case_id="t1", profile="ai-robotics", title="T", abstract="A")
    tier3_case = Tier3Case(
        case_id="t3", title="T", full_text="body", reference={"claims": ["c"]}
    )
    write_jsonl(tmp_path / "filter.jsonl", [filter_case])
    write_jsonl(tmp_path / "tier1.jsonl", [tier1_case])
    write_jsonl(tmp_path / "tier3.jsonl", [tier3_case])

    filters, tier1s, tier3s = load_dataset(tmp_path)

    assert filters == [filter_case]
    assert tier1s == [tier1_case]
    assert tier3s == [tier3_case]


def test_missing_slot_file_yields_empty_list(tmp_path: Path) -> None:
    filters, tier1s, tier3s = load_dataset(tmp_path)

    assert (filters, tier1s, tier3s) == ([], [], [])


def test_results_round_trip_preserves_usage_and_ok(tmp_path: Path) -> None:
    ok = CaseResult(
        case_id="a",
        slot="tier1",
        model="m",
        latency_seconds=0.5,
        usage=Usage(10, 5, 0, 0.001),
        output={"contributions": ["x"]},
        meta={"from_cache": False},
    )
    failed = CaseResult(
        case_id="b",
        slot="tier1",
        model="m",
        latency_seconds=0.1,
        usage=Usage(),
        error="http",
    )
    write_jsonl(tmp_path / "r.jsonl", [ok, failed])

    back = read_results(tmp_path / "r.jsonl")

    assert back == [ok, failed]
    assert back[0].ok and not back[1].ok
