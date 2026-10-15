"""Pairwise LLM-as-judge for outputs that have no single right answer.

Tier-1 summaries and Tier-3 extractions cannot be scored against a key, so
a stronger judge model compares the baseline and candidate outputs for the
same source text.  Each pair is judged twice with the positions swapped; a
verdict only counts when both orderings agree, otherwise it is a tie.
Filter score flips (one model passes the relevance threshold, the other
does not) are judged the same way against the profile description.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

from influx.config import AppConfig, ModelSlotConfig
from influx.errors import NetworkError
from influx.http_client import FetchResult, guarded_post_json_fetch
from influx.llm_request import build_chat_request, provider_endpoint

_log = logging.getLogger(__name__)

_JUDGE_MAX_TOKENS = 1024
_SOURCE_CHAR_CAP = 40_000

_PAIRWISE_PROMPT = (
    "You are judging two structured extractions of the same source for an "
    "AI-research ingestion pipeline.  Judge ONLY against the source text: "
    "reward faithfulness (no invented facts), specificity (concrete over "
    "generic), and coverage of what matters; penalise padding and "
    "hallucination.\n\n"
    "## TASK\n{task}\n\n"
    "## SOURCE\n{source}\n\n"
    "## OUTPUT A\n{a}\n\n"
    "## OUTPUT B\n{b}\n\n"
    'Return JSON: {{"winner": "A" | "B" | "tie", "a_score": 1-5, '
    '"b_score": 1-5, "rationale": "one or two sentences"}}'
)

_FILTER_TASK = (
    "Two relevance filters scored the same candidate against this interest "
    "profile (1-10, higher = more relevant; items scoring below the threshold "
    "are not ingested).  Decide which score and reason better reflect the "
    "profile.\n\nInterest profile:\n{profile}\nRelevance threshold: {threshold}"
)


@dataclass(frozen=True, slots=True)
class Verdict:
    """Outcome of judging one case.

    ``winner`` is ``baseline``, ``candidate`` or ``tie``.
    """

    slot: str
    case_id: str
    item_id: str
    winner: str
    baseline_score: float
    candidate_score: float
    rationale: str
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class JudgeSummary:
    slot: str
    judged: int
    baseline_wins: int
    candidate_wins: int
    ties: int
    errors: int
    baseline_mean: float
    candidate_mean: float


def summarize_verdicts(verdicts: Sequence[Verdict]) -> JudgeSummary:
    clean = [v for v in verdicts if not v.error]
    slot = verdicts[0].slot if verdicts else ""
    return JudgeSummary(
        slot=slot,
        judged=len(clean),
        baseline_wins=sum(1 for v in clean if v.winner == "baseline"),
        candidate_wins=sum(1 for v in clean if v.winner == "candidate"),
        ties=sum(1 for v in clean if v.winner == "tie"),
        errors=len(verdicts) - len(clean),
        baseline_mean=(
            sum(v.baseline_score for v in clean) / len(clean) if clean else 0.0
        ),
        candidate_mean=(
            sum(v.candidate_score for v in clean) / len(clean) if clean else 0.0
        ),
    )


Poster = Callable[..., FetchResult]


class Judge:
    """One judge model bound to a config's provider table."""

    def __init__(
        self,
        config: AppConfig,
        *,
        model: str,
        provider: str,
        reasoning_effort: str | None = None,
        poster: Poster | None = None,
        concurrency: int = 4,
    ) -> None:
        if provider not in config.providers:
            raise ValueError(f"judge provider {provider!r} is not configured")
        self._config = config
        self._provider = config.providers[provider]
        self._slot = ModelSlotConfig(
            provider=provider,
            model=model,
            temperature=0.0,
            max_tokens=_JUDGE_MAX_TOKENS,
            request_timeout=120,
            max_retries=1,
            json_mode=True,
            reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
        )
        # Resolved at call time so tests can monkeypatch the module global.
        self._poster = poster or guarded_post_json_fetch
        self._concurrency = max(1, concurrency)

    def _ask(self, prompt: str) -> dict[str, Any]:
        url, headers = provider_endpoint(self._provider)
        body = build_chat_request(self._slot, prompt)
        last_error = ""
        for attempt in range(self._slot.max_retries + 1):
            try:
                result = self._poster(
                    url,
                    body,
                    headers=headers,
                    allow_private_ips=self._config.security.allow_private_ips,
                    max_response_bytes=self._config.storage.max_download_bytes,
                    timeout_seconds=self._slot.request_timeout,
                )
            except NetworkError as exc:
                last_error = str(exc)
                continue
            if result.status_code >= 400:
                last_error = f"HTTP {result.status_code}"
                time.sleep(2**attempt)
                continue
            try:
                resp = json.loads(result.body.decode("utf-8"))
                return json.loads(resp["choices"][0]["message"]["content"])
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                last_error = f"parse: {exc}"
        raise RuntimeError(last_error or "judge call failed")

    def _pairwise(
        self, task: str, source: str, a: str, b: str
    ) -> tuple[str, float, float, str]:
        """Judge (a, b) in both orders.

        Returns ``(winner as A/B/tie, a_score, b_score, rationale)``.
        """
        source = source[:_SOURCE_CHAR_CAP]
        first = self._ask(_PAIRWISE_PROMPT.format(task=task, source=source, a=a, b=b))
        second = self._ask(_PAIRWISE_PROMPT.format(task=task, source=source, a=b, b=a))
        w1 = str(first.get("winner", "tie")).upper()
        w2 = str(second.get("winner", "tie")).upper()
        # Map the swapped ordering back: a win for "B" in round two is a win for A.
        w2_mapped = {"A": "B", "B": "A"}.get(w2, "tie")
        winner = w1 if w1 == w2_mapped and w1 in ("A", "B") else "tie"
        a_score = (_num(first.get("a_score")) + _num(second.get("b_score"))) / 2
        b_score = (_num(first.get("b_score")) + _num(second.get("a_score"))) / 2
        rationale = str(first.get("rationale", ""))
        return winner, a_score, b_score, rationale

    def judge_outputs(
        self,
        slot: str,
        items: Sequence[tuple[str, str, str, dict[str, Any], dict[str, Any]]],
    ) -> list[Verdict]:
        """Judge ``(case_id, task, source, baseline_output, candidate_output)`` rows."""

        def one(row: tuple[str, str, str, dict[str, Any], dict[str, Any]]) -> Verdict:
            case_id, task, source, base_out, cand_out = row
            try:
                winner, a, b, why = self._pairwise(
                    task,
                    source,
                    json.dumps(base_out, ensure_ascii=False, indent=1),
                    json.dumps(cand_out, ensure_ascii=False, indent=1),
                )
            except Exception as exc:  # noqa: BLE001 — recorded, not raised
                _log.warning("judge failed for %s/%s: %s", slot, case_id, exc)
                return Verdict(slot, case_id, "", "tie", 0.0, 0.0, "", error=str(exc))
            return Verdict(slot, case_id, "", _map_winner(winner), a, b, why)

        return self._run(one, items)

    def judge_filter_flips(
        self,
        rows: Sequence[tuple[str, str, str, int, str, dict[str, Any], dict[str, Any]]],
    ) -> list[Verdict]:
        """Judge ``(case_id, item_id, profile_description, threshold, source,
        baseline_row, candidate_row)`` where each row is ``{score, reason}``."""

        def one(
            row: tuple[str, str, str, int, str, dict[str, Any], dict[str, Any]],
        ) -> Verdict:
            case_id, item_id, profile, threshold, source, base_row, cand_row = row
            task = _FILTER_TASK.format(profile=profile, threshold=threshold)
            try:
                winner, a, b, why = self._pairwise(
                    task, source, json.dumps(base_row), json.dumps(cand_row)
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("judge failed for filter/%s/%s: %s", case_id, item_id, exc)
                return Verdict(
                    "filter", case_id, item_id, "tie", 0.0, 0.0, "", error=str(exc)
                )
            return Verdict("filter", case_id, item_id, _map_winner(winner), a, b, why)

        return self._run(one, rows)

    def _run(self, fn: Callable[[Any], Verdict], rows: Sequence[Any]) -> list[Verdict]:
        if not rows:
            return []
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            return list(pool.map(fn, rows))


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _map_winner(winner: str) -> str:
    return {"A": "baseline", "B": "candidate"}.get(winner, "tie")
