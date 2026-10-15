"""Pure metric functions over eval results.

Nothing here touches the network or the filesystem: every function takes
:class:`~evals.cases.CaseResult` lists (plus the cases they came from) and
returns frozen summary records that :mod:`evals.report` renders.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from evals.cases import CaseResult, FilterCase
from evals.labels import LabelKey

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


def _cohen_kappa(both: int, first_only: int, second_only: int, neither: int) -> float:
    """Cohen's kappa for two raters' yes/no calls (1 = perfect, 0 = chance)."""
    n = both + first_only + second_only + neither
    if not n:
        return 0.0
    observed = (both + neither) / n
    first_yes = (both + first_only) / n
    second_yes = (both + second_only) / n
    expected = first_yes * second_yes + (1 - first_yes) * (1 - second_yes)
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


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
        return _cohen_kappa(
            self.both_pass, self.baseline_only, self.candidate_only, self.neither
        )


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
    ingest decision flipped (see :func:`compare_filter`) — the rows worth
    reading and judging."""


def compare_filter(
    cases: Sequence[FilterCase],
    baseline: Sequence[CaseResult],
    candidate: Sequence[CaseResult],
    *,
    thresholds: Sequence[int] = (7, 8, 9),
    relevance: Mapping[str, int] | None = None,
) -> FilterComparison:
    """Per-candidate agreement at each of *thresholds*, plus the flips.

    A flip is judged at the item's own profile ``relevance`` gate when
    *relevance* maps it, else at ``thresholds[0]``: profiles can gate at
    different scores, so no single number is everyone's ingest decision.
    """
    base_by_id = {r.case_id: r for r in baseline}
    cand_by_id = {r.case_id: r for r in candidate}
    pairs: list[tuple[str, str, int, int]] = []
    flips: list[tuple[str, str, int, int]] = []
    first = thresholds[0] if thresholds else 7
    comparable = 0
    for case in cases:
        b = base_by_id.get(case.case_id)
        c = cand_by_id.get(case.case_id)
        if b is None or c is None or not b.ok or not c.ok:
            continue
        comparable += 1
        gate = (relevance or {}).get(case.profile, first)
        bs, cs = filter_scores(b), filter_scores(c)
        for cand in case.candidates:
            pair = (
                case.case_id,
                cand.id,
                bs.get(cand.id, DROPPED),
                cs.get(cand.id, DROPPED),
            )
            pairs.append(pair)
            if (pair[2] >= gate) != (pair[3] >= gate):
                flips.append(pair)

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
        disagreements=tuple(flips),
    )


# ── Filter: graded against human labels ───────────────────────────────

# Gate names in tier order: a label or prediction of tier N passes gates 1..N.
GATES: tuple[str, ...] = ("ingest", "full text", "deep extract")
DEFAULT_GATE_THRESHOLDS: tuple[int, int, int] = (7, 8, 9)
"""``ProfileThresholds`` defaults, for a labelled profile no longer configured."""


def predicted_tier(score: int, thresholds: tuple[int, int, int]) -> int:
    """The label tier a filter score lands in under a profile's thresholds
    (``relevance``, ``full_text``, ``deep_extract``).

    Gates are cumulative, as in the Cascade: an item below ``relevance`` is
    never ingested, so it gets no full text whatever ``full_text`` says.
    """
    tier = 0
    for threshold in thresholds:
        if score < threshold:
            break
        tier += 1
    return tier


@dataclass(frozen=True, slots=True)
class GateConfusion:
    """Model pass/fail vs the human label at one pipeline gate."""

    gate: str
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float | None:
        """Of what the model let through this gate, the share the labeller would."""
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        """Of what the labeller wants through this gate, the share the model let in."""
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def accuracy(self) -> float | None:
        return (self.tp + self.tn) / self.total if self.total else None

    @property
    def kappa(self) -> float | None:
        """``None`` when undefined: nothing graded, or the labels and the
        model both put every item on the same side of this gate."""
        label_pos, model_pos = self.tp + self.fn, self.tp + self.fp
        if not self.total or (label_pos == model_pos and label_pos in (0, self.total)):
            return None
        return _cohen_kappa(self.tp, self.fn, self.fp, self.tn)


@dataclass(frozen=True, slots=True)
class LabelGrade:
    """One run's filter decisions graded against the human labels."""

    labelled: int
    """Labelled candidates present in the dataset."""
    graded: int
    """Of those, the ones in a batch the run scored (failed batches are not
    graded; validity is reported separately)."""
    tier_exact: int
    gates: tuple[GateConfusion, ...]
    misses: tuple[tuple[str, str, str, int, int], ...] = field(default=())
    """``(profile, item_id, title, label_tier, score)`` where the run's ingest
    decision contradicts the label — the rows worth reading."""


def grade_filter(
    cases: Sequence[FilterCase],
    results: Sequence[CaseResult],
    labels: Mapping[LabelKey, int],
    thresholds: Mapping[str, tuple[int, int, int]],
) -> LabelGrade:
    """Grade a filter run against ``(profile, item_id) → tier`` labels.

    An item the scorer left out of ``results`` counts as :data:`DROPPED`,
    exactly as the pipeline treats it.  An item that appears in several
    batches is graded on the first batch the run scored.
    """
    by_case = {r.case_id: r for r in results}
    present: set[LabelKey] = set()
    graded: set[LabelKey] = set()
    rows: list[tuple[str, str, str, int, int, int]] = []
    for case in cases:
        gates = thresholds.get(case.profile, DEFAULT_GATE_THRESHOLDS)
        result = by_case.get(case.case_id)
        scored = result is not None and result.ok
        scores = filter_scores(result) if result is not None else {}
        for cand in case.candidates:
            key = (case.profile, cand.id)
            if key not in labels:
                continue
            present.add(key)
            if not scored or key in graded:
                continue
            graded.add(key)
            score = scores.get(cand.id, DROPPED)
            rows.append(
                (
                    case.profile,
                    cand.id,
                    cand.title,
                    labels[key],
                    score,
                    predicted_tier(score, gates),
                )
            )
    confusions = tuple(
        GateConfusion(
            gate=name,
            tp=sum(1 for *_, t, _s, p in rows if t >= n and p >= n),
            fp=sum(1 for *_, t, _s, p in rows if t < n <= p),
            fn=sum(1 for *_, t, _s, p in rows if p < n <= t),
            tn=sum(1 for *_, t, _s, p in rows if t < n and p < n),
        )
        for n, name in enumerate(GATES, start=1)
    )
    return LabelGrade(
        labelled=len(present),
        graded=len(rows),
        tier_exact=sum(1 for *_, t, _s, p in rows if t == p),
        gates=confusions,
        misses=tuple(
            (prof, iid, title, t, s)
            for prof, iid, title, t, s, p in rows
            if (t >= 1) != (p >= 1)
        ),
    )


@dataclass(frozen=True, slots=True)
class JudgeLabelAgreement:
    """How often the judge sided with the model the human labels side with,
    on filter flips that have a label."""

    checked: int
    agree: int
    disagree: int
    ties: int


def judge_label_agreement(
    judged: Sequence[tuple[str, str, str]],
    flips: Sequence[tuple[str, str, int, int]],
    case_profiles: Mapping[str, str],
    labels: Mapping[LabelKey, int],
    thresholds: Mapping[str, tuple[int, int, int]],
) -> JudgeLabelAgreement:
    """Check judge verdicts ``(case_id, item_id, winner)`` against the labels.

    *flips* are :attr:`FilterComparison.disagreements`.  A flip is checked
    only when it is labelled and the two scores land on opposite sides of
    the item's own profile ingest gate, so exactly one model is right.
    """
    scores = {(cid, iid): (b, c) for cid, iid, b, c in flips}
    checked: set[LabelKey] = set()
    agree = disagree = ties = 0
    for case_id, item_id, winner in judged:
        profile = case_profiles.get(case_id)
        pair = scores.get((case_id, item_id))
        if profile is None or pair is None or (profile, item_id) in checked:
            continue
        tier = labels.get((profile, item_id))
        if tier is None:
            continue
        gate = thresholds.get(profile, DEFAULT_GATE_THRESHOLDS)[0]
        base_pass, cand_pass = pair[0] >= gate, pair[1] >= gate
        if base_pass == cand_pass:
            continue
        checked.add((profile, item_id))
        right = "baseline" if base_pass == (tier >= 1) else "candidate"
        if winner == "tie":
            ties += 1
        elif winner == right:
            agree += 1
        else:
            disagree += 1
    return JudgeLabelAgreement(
        checked=agree + disagree + ties, agree=agree, disagree=disagree, ties=ties
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
