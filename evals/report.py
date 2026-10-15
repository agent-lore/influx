"""Markdown rendering of eval summaries and comparisons."""

from __future__ import annotations

from collections.abc import Sequence

from evals.judge import JudgeSummary
from evals.labels import TIER_NAMES
from evals.metrics import (
    DROPPED,
    FilterComparison,
    JudgeLabelAgreement,
    LabelGrade,
    RunSummary,
    ShapeSummary,
)


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _cell(text: str, width: int = 80) -> str:
    """*text* made safe for one markdown table cell."""
    flat = " ".join(text.split()).replace("|", "\\|")
    return flat if len(flat) <= width else flat[: width - 1] + "…"


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


def render_label_mix(tiers: Sequence[int]) -> str:
    counts = [sum(1 for t in tiers if t == n) for n in range(len(TIER_NAMES))]
    mix = ", ".join(
        f"{n} {name} {c}"
        for n, (name, c) in enumerate(zip(TIER_NAMES, counts, strict=True))
    )
    return f"Label mix: {mix}."


def render_label_grades(grades: Sequence[tuple[str, LabelGrade]]) -> str:
    """Per-run, per-gate precision/recall against the human labels."""
    if not grades:
        return ""
    lines = [
        f"{grades[0][1].labelled} labelled candidates in this dataset; a run's "
        "failed batches are not graded (see validity).",
        "",
        "| run | gate | graded | precision | recall | F1 | accuracy | kappa "
        + "| TP | FP | FN | TN |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run, g in grades:
        for gate in g.gates:
            lines.append(
                f"| {run} | {gate.gate} | {g.graded} | {_pct(gate.precision)} | "
                f"{_pct(gate.recall)} | {_ratio(gate.f1)} | {_pct(gate.accuracy)} | "
                f"{_ratio(gate.kappa)} | {gate.tp} | {gate.fp} | {gate.fn} | "
                f"{gate.tn} |"
            )
    lines += [
        "",
        "| run | graded | exact tier | ingest misses |",
        "|---|---:|---:|---:|",
    ]
    lines += [
        f"| {run} | {g.graded} | {g.tier_exact}/{g.graded} | {len(g.misses)} |"
        for run, g in grades
    ]
    return "\n".join(lines)


def render_label_misses(grade: LabelGrade, *, limit: int = 40) -> str:
    """The items whose ingest decision contradicts the label."""
    if not grade.misses:
        return "No ingest-decision misses."
    lines = [
        "| profile | item | title | label | score | model |",
        "|---|---|---|---|---:|---|",
    ]
    for profile, item_id, title, tier, score in grade.misses[:limit]:
        shown = "omitted" if score == DROPPED else str(score)
        # A miss is an ingest disagreement, so the label alone says which way.
        call = "rejected (FN)" if tier >= 1 else "ingested (FP)"
        lines.append(
            f"| {profile} | {_cell(item_id, 60)} | {_cell(title)} | "
            f"{tier} {TIER_NAMES[tier]} | {shown} | {call} |"
        )
    if len(grade.misses) > limit:
        lines += ["", f"… and {len(grade.misses) - limit} more."]
    return "\n".join(lines)


def render_judge_vs_labels(check: JudgeLabelAgreement) -> str:
    if not check.checked:
        return "Judge vs your labels: no judged flip has a label yet."
    return (
        f"Judge vs your labels on {check.checked} labelled flips: agrees "
        f"{check.agree}, disagrees {check.disagree}, ties {check.ties}."
    )
