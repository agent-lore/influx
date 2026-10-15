"""Pure metric functions over eval results.

Nothing here touches the network or the filesystem: every function takes
:class:`~evals.cases.CaseResult` lists (plus the cases they came from) and
returns frozen summary records that :mod:`evals.report` renders.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from evals.cases import CaseResult, FilterCase

_ARXIV_REF_RE = re.compile(r"arXiv:\d{4}\.\d{4,5}(v\d+)?", re.IGNORECASE)

# A candidate the scorer left out of ``results`` was scored below
# ``filter.min_score_in_results`` (or dropped); treat it as this sentinel so
# threshold decisions can still be compared per candidate.
DROPPED = 0


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Validity, latency and spend for one slot of one run."""

    slot: str
    model: str
    cases: int
    ok: int
    errors: dict[str, int]
    retried: int
    latency_p50: float
    latency_p95: float
    latency_mean: float
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float | None
    cached: int
    salvaged: int = 0
    """Valid results whose JSON had to be cut out of surrounding text
    (``parse_json_content`` salvage) — a model-quirk rate worth watching."""

    @property
    def validity_rate(self) -> float:
        return self.ok / self.cases if self.cases else 0.0

    @property
    def cost_per_case(self) -> float | None:
        if self.cost_usd is None or not self.cases:
            return None
        return self.cost_usd / self.cases


def summarize_run(results: Sequence[CaseResult]) -> RunSummary:
    if not results:
        raise ValueError("no results to summarize")
    slot = results[0].slot
    model = results[0].model
    # Cached replays carry the latency measured when the call was made.
    latencies = [r.latency_seconds for r in results]
    costs = [r.usage.cost_usd for r in results]
    known_costs = [c for c in costs if c is not None]
    return RunSummary(
        slot=slot,
        model=model,
        cases=len(results),
        ok=sum(1 for r in results if r.ok),
        errors=dict(Counter(r.error for r in results if r.error)),
        retried=sum(1 for r in results if r.attempts > 1),
        latency_p50=_percentile(latencies, 50),
        latency_p95=_percentile(latencies, 95),
        latency_mean=statistics.fmean(latencies) if latencies else 0.0,
        prompt_tokens=sum(r.usage.prompt_tokens for r in results),
        completion_tokens=sum(r.usage.completion_tokens for r in results),
        reasoning_tokens=sum(r.usage.reasoning_tokens for r in results),
        cost_usd=sum(known_costs) if len(known_costs) == len(costs) else None,
        cached=sum(1 for r in results if r.meta.get("from_cache")),
        salvaged=sum(1 for r in results if r.meta.get("salvaged")),
    )


# ── Filter: decision agreement against a baseline run ─────────────────


def filter_scores(result: CaseResult) -> dict[str, int]:
    """``candidate id → score`` from one validated filter response."""
    if not result.ok or result.output is None:
        return {}
    return {row["id"]: int(row["score"]) for row in result.output.get("results", [])}


@dataclass(frozen=True, slots=True)
class ThresholdAgreement:
    """2x2 confusion of pass/fail at one threshold, baseline vs candidate."""

    threshold: int
    both_pass: int
    baseline_only: int
    candidate_only: int
    neither: int

    @property
    def total(self) -> int:
        return self.both_pass + self.baseline_only + self.candidate_only + self.neither

    @property
    def agreement(self) -> float:
        return (self.both_pass + self.neither) / self.total if self.total else 0.0

    @property
    def kappa(self) -> float:
        """Cohen's kappa for the pass/fail decision (1 = perfect, 0 = chance)."""
        n = self.total
        if not n:
            return 0.0
        observed = self.agreement
        base_pass = (self.both_pass + self.baseline_only) / n
        cand_pass = (self.both_pass + self.candidate_only) / n
        expected = base_pass * cand_pass + (1 - base_pass) * (1 - cand_pass)
        if expected >= 1.0:
            return 1.0
        return (observed - expected) / (1 - expected)


@dataclass(frozen=True, slots=True)
class FilterComparison:
    """How a candidate run's scores line up with the baseline's, per candidate."""

    candidates: int
    comparable_cases: int
    both_scored: int
    exact: int
    within_one: int
    mean_abs_error: float
    baseline_mean: float
    candidate_mean: float
    thresholds: tuple[ThresholdAgreement, ...]
    disagreements: tuple[tuple[str, str, int, int], ...] = field(default=())
    """``(case_id, candidate_id, baseline_score, candidate_score)`` where the
    relevance decision flipped — the rows worth reading and judging."""


def compare_filter(
    cases: Sequence[FilterCase],
    baseline: Sequence[CaseResult],
    candidate: Sequence[CaseResult],
    *,
    thresholds: Sequence[int] = (7, 8, 9),
) -> FilterComparison:
    base_by_id = {r.case_id: r for r in baseline}
    cand_by_id = {r.case_id: r for r in candidate}
    pairs: list[tuple[str, str, int, int]] = []
    comparable = 0
    for case in cases:
        b = base_by_id.get(case.case_id)
        c = cand_by_id.get(case.case_id)
        if b is None or c is None or not b.ok or not c.ok:
            continue
        comparable += 1
        bs, cs = filter_scores(b), filter_scores(c)
        for cand in case.candidates:
            pairs.append(
                (
                    case.case_id,
                    cand.id,
                    bs.get(cand.id, DROPPED),
                    cs.get(cand.id, DROPPED),
                )
            )

    both = [(b, c) for _cid, _id, b, c in pairs if b != DROPPED and c != DROPPED]
    agreements = []
    for t in thresholds:
        both_pass = sum(1 for _, _, b, c in pairs if b >= t and c >= t)
        base_only = sum(1 for _, _, b, c in pairs if b >= t and c < t)
        cand_only = sum(1 for _, _, b, c in pairs if b < t and c >= t)
        neither = sum(1 for _, _, b, c in pairs if b < t and c < t)
        agreements.append(
            ThresholdAgreement(t, both_pass, base_only, cand_only, neither)
        )
    first = thresholds[0] if thresholds else 7
    flips = tuple(
        (cid, iid, b, c) for cid, iid, b, c in pairs if (b >= first) != (c >= first)
    )
    return FilterComparison(
        candidates=len(pairs),
        comparable_cases=comparable,
        both_scored=len(both),
        exact=sum(1 for b, c in both if b == c),
        within_one=sum(1 for b, c in both if abs(b - c) <= 1),
        mean_abs_error=statistics.fmean(abs(b - c) for b, c in both) if both else 0.0,
        baseline_mean=statistics.fmean(b for b, _ in both) if both else 0.0,
        candidate_mean=statistics.fmean(c for _, c in both) if both else 0.0,
        thresholds=tuple(agreements),
        disagreements=flips,
    )


# ── Tier 1 / Tier 3: structural shape of the outputs ──────────────────


@dataclass(frozen=True, slots=True)
class ShapeSummary:
    """Mean list lengths / field sizes of validated enrichment outputs."""

    slot: str
    ok: int
    list_means: dict[str, float]
    text_mean_chars: dict[str, float]
    empty_list_rate: dict[str, float]
    arxiv_ref_rate: float | None
    """Share of ``builds_on`` entries carrying an ``arXiv:<id>`` reference
    (Tier 3 only; the prompt asks for them when known)."""


def summarize_shape(results: Sequence[CaseResult]) -> ShapeSummary:
    outputs = [r.output for r in results if r.ok and r.output is not None]
    slot = results[0].slot if results else ""
    list_lengths: dict[str, list[int]] = {}
    text_lengths: dict[str, list[int]] = {}
    for out in outputs:
        for key, value in out.items():
            if isinstance(value, list):
                list_lengths.setdefault(key, []).append(len(value))
            elif isinstance(value, str):
                text_lengths.setdefault(key, []).append(len(value))
    builds_on = [item for out in outputs for item in out.get("builds_on", [])]
    arxiv_rate = (
        sum(1 for item in builds_on if _ARXIV_REF_RE.search(item)) / len(builds_on)
        if builds_on
        else None
    )
    return ShapeSummary(
        slot=slot,
        ok=len(outputs),
        list_means={k: statistics.fmean(v) for k, v in list_lengths.items()},
        text_mean_chars={k: statistics.fmean(v) for k, v in text_lengths.items()},
        empty_list_rate={
            k: sum(1 for n in v if n == 0) / len(v) for k, v in list_lengths.items()
        },
        arxiv_ref_rate=arxiv_rate if slot == "tier3" else None,
    )
