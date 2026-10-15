"""Markdown rendering of eval summaries and comparisons."""

from __future__ import annotations

from collections.abc import Sequence

from evals.judge import JudgeSummary
from evals.metrics import FilterComparison, RunSummary, ShapeSummary


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def render_run_table(summaries: Sequence[RunSummary]) -> str:
    lines = [
        "| slot | model | cases | valid | salvaged | errors | retried | p50 s | p95 s "
        + "| prompt tok | completion tok | reasoning tok | cost | cost/case |",
        "|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        errors = ", ".join(f"{k}={v}" for k, v in sorted(s.errors.items())) or "-"
        lines.append(
            f"| {s.slot} | {s.model} | {s.cases} | {s.validity_rate:.0%} | "
            f"{s.salvaged} | {errors} | "
            f"{s.retried} | {s.latency_p50:.2f} | {s.latency_p95:.2f} | "
            f"{s.prompt_tokens} | "
            f"{s.completion_tokens} | {s.reasoning_tokens} | {_money(s.cost_usd)} | "
            f"{_money(s.cost_per_case)} |"
        )
    return "\n".join(lines)


def render_filter_comparison(cmp: FilterComparison) -> str:
    lines = [
        f"Comparable cases: {cmp.comparable_cases}; candidates compared: "
        f"{cmp.candidates}; scored by both: {cmp.both_scored}.",
        "",
        f"- exact score match: {cmp.exact}/{cmp.both_scored}"
        + (f" ({cmp.exact / cmp.both_scored:.0%})" if cmp.both_scored else ""),
        f"- within ±1: {cmp.within_one}/{cmp.both_scored}"
        + (f" ({cmp.within_one / cmp.both_scored:.0%})" if cmp.both_scored else ""),
        f"- mean absolute error: {cmp.mean_abs_error:.2f}",
        f"- mean score (baseline / candidate): {cmp.baseline_mean:.2f} / "
        f"{cmp.candidate_mean:.2f}",
        "",
        "| threshold | both pass | baseline only | candidate only | neither "
        + "| agreement | kappa |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for t in cmp.thresholds:
        lines.append(
            f"| {t.threshold} | {t.both_pass} | {t.baseline_only} | "
            f"{t.candidate_only} | {t.neither} | {t.agreement:.0%} | {t.kappa:.2f} |"
        )
    return "\n".join(lines)


def render_shape(baseline: ShapeSummary, candidate: ShapeSummary) -> str:
    keys = sorted(set(baseline.list_means) | set(candidate.list_means))
    lines = [
        "| field | baseline mean len | candidate mean len | baseline empty "
        + "| candidate empty |",
        "|---|---:|---:|---:|---:|",
    ]
    for k in keys:
        lines.append(
            f"| {k} | {baseline.list_means.get(k, 0):.1f} | "
            f"{candidate.list_means.get(k, 0):.1f} | "
            f"{baseline.empty_list_rate.get(k, 0):.0%} | "
            f"{candidate.empty_list_rate.get(k, 0):.0%} |"
        )
    text_keys = sorted(set(baseline.text_mean_chars) | set(candidate.text_mean_chars))
    if text_keys:
        lines += [
            "",
            "| text field | baseline mean chars | candidate mean chars |",
            "|---|---:|---:|",
        ]
        for k in text_keys:
            lines.append(
                f"| {k} | {baseline.text_mean_chars.get(k, 0):.0f} | "
                f"{candidate.text_mean_chars.get(k, 0):.0f} |"
            )
    if baseline.arxiv_ref_rate is not None or candidate.arxiv_ref_rate is not None:
        b = baseline.arxiv_ref_rate or 0.0
        c = candidate.arxiv_ref_rate or 0.0
        lines += [
            "",
            f"builds_on entries carrying an arXiv id: baseline {b:.0%}, "
            f"candidate {c:.0%}",
        ]
    return "\n".join(lines)


def render_judge(summary: JudgeSummary) -> str:
    return (
        f"Judged {summary.judged} pairs (errors: {summary.errors}): "
        f"baseline wins {summary.baseline_wins}, candidate wins "
        f"{summary.candidate_wins}, ties {summary.ties}.  Mean judge score "
        f"baseline {summary.baseline_mean:.2f} / candidate "
        f"{summary.candidate_mean:.2f} (1-5)."
    )
