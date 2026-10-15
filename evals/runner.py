"""Send eval cases to a model and record validated output, latency and cost.

The runner builds each request through the same code production uses
(``influx.filter.render_filter_user_message``, ``influx.enrich.render_*``
and ``influx.llm_request.build_chat_request``) with the slot overridden
by a :class:`ModelSpec`, so the only thing that differs between an eval
request and a production request is the model being asked.

Responses are cached on disk keyed by the full request body, so re-running
a comparison after changing only the metrics is free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from evals.cases import (
    CaseResult,
    FilterCase,
    Tier1Case,
    Tier3Case,
    Usage,
)
from influx.config import AppConfig, ModelSlotConfig, ProviderConfig
from influx.enrich import render_tier1_prompt, render_tier3_prompt
from influx.errors import NetworkError
from influx.feedback import format_negative_examples
from influx.filter import render_filter_user_message, retry_delay_seconds
from influx.http_client import FetchResult, guarded_post_json_fetch
from influx.llm_request import (
    JsonContentError,
    build_chat_request,
    parse_json_content,
    provider_endpoint,
)
from influx.schemas import (
    FilterResponse,
    Tier1Enrichment,
    Tier3Extraction,
    openai_strict_response_format,
)
from influx.source import Candidate

_log = logging.getLogger(__name__)

# Production slot name and output schema for each eval slot.
_SLOT_BINDINGS: dict[str, tuple[str, type[BaseModel]]] = {
    "filter": ("filter", FilterResponse),
    "tier1": ("enrich", Tier1Enrichment),
    "tier3": ("extract", Tier3Extraction),
}

Poster = Callable[..., FetchResult]
Sleeper = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """The model under test and the slot settings that differ from config.

    ``label`` names the run directory; everything else overrides the
    corresponding ``[models.<slot>]`` field for all three slots.  ``None``
    keeps the value from the loaded config.
    """

    label: str
    model: str
    provider: str
    reasoning_effort: str | None = None
    temperature: float | None = None
    json_schema_strict: bool | None = None
    max_tokens: int | None = None
    request_timeout: int | None = None

    def apply(self, slot: ModelSlotConfig) -> ModelSlotConfig:
        updates: dict[str, Any] = {"model": self.model, "provider": self.provider}
        for name in (
            "reasoning_effort",
            "temperature",
            "json_schema_strict",
            "max_tokens",
            "request_timeout",
        ):
            value = getattr(self, name)
            if value is not None:
                updates[name] = value
        return slot.model_copy(update=updates)


def _request_key(body: dict[str, Any], url: str) -> str:
    digest = hashlib.sha256(
        json.dumps({"url": url, "body": body}, sort_keys=True).encode("utf-8")
    )
    return digest.hexdigest()


def _is_openrouter(provider: ProviderConfig) -> bool:
    return "openrouter.ai" in provider.base_url


def _usage_from(resp: dict[str, Any]) -> Usage:
    usage = resp.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    cost = usage.get("cost")
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        cost_usd=float(cost) if cost is not None else None,
    )


class Runner:
    """Executes cases for one :class:`ModelSpec` against one config."""

    def __init__(
        self,
        config: AppConfig,
        spec: ModelSpec,
        *,
        cache_dir: Path | None = None,
        poster: Poster | None = None,
        concurrency: int = 4,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self._config = config
        self._spec = spec
        self._cache_dir = cache_dir
        self._sleeper = sleeper
        # Resolved at call time so tests can monkeypatch the module global.
        self._poster = poster or guarded_post_json_fetch
        self._concurrency = max(1, concurrency)
        self._profiles = {p.name: p for p in config.profiles}

    # ── prompt rendering (production-identical) ───────────────────────

    def _filter_prompt(self, case: FilterCase) -> str:
        profile_cfg = self._profiles.get(case.profile)
        description = profile_cfg.description if profile_cfg else case.profile
        template = self._config.prompts.filter.text or ""
        rendered = template.format(
            profile_description=description,
            negative_examples=format_negative_examples([]),
            min_score_in_results=self._config.filter.min_score_in_results,
        )
        candidates = [
            Candidate(item_id=c.id, title=c.title, abstract=c.abstract, source_url="")
            for c in case.candidates
        ]
        return render_filter_user_message(rendered, candidates)

    def _tier1_prompt(self, case: Tier1Case) -> str:
        profile_cfg = self._profiles.get(case.profile)
        summary = profile_cfg.description if profile_cfg else case.profile
        return render_tier1_prompt(
            self._config,
            title=case.title,
            abstract=case.abstract,
            profile_summary=summary,
        )

    def _tier3_prompt(self, case: Tier3Case) -> str:
        return render_tier3_prompt(
            self._config, title=case.title, full_text=case.full_text
        )

    # ── execution ─────────────────────────────────────────────────────

    def _slot_and_provider(
        self, eval_slot: str
    ) -> tuple[ModelSlotConfig, ProviderConfig]:
        slot_name, _schema = _SLOT_BINDINGS[eval_slot]
        base = self._config.models.get(slot_name)
        if base is None:
            raise ValueError(f"[models.{slot_name}] is not configured")
        slot = self._spec.apply(base)
        provider = self._config.providers.get(slot.provider)
        if provider is None:
            raise ValueError(f"provider {slot.provider!r} is not configured")
        return slot, provider

    def _cached(self, key: str) -> dict[str, Any] | None:
        if self._cache_dir is None:
            return None
        path = self._cache_dir / f"{key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _store(self, key: str, payload: dict[str, Any]) -> None:
        if self._cache_dir is None:
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        (self._cache_dir / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _post(
        self, url: str, headers: dict[str, str], body: dict[str, Any], timeout: int
    ) -> tuple[FetchResult | None, float, str]:
        started = time.perf_counter()
        try:
            result = self._poster(
                url,
                body,
                headers=headers,
                allow_private_ips=self._config.security.allow_private_ips,
                max_response_bytes=self._config.storage.max_download_bytes,
                timeout_seconds=timeout,
            )
        except NetworkError as exc:
            return None, time.perf_counter() - started, str(exc)
        return result, time.perf_counter() - started, ""

    def run_one(self, eval_slot: str, case_id: str, prompt: str) -> CaseResult:
        """Send one case, retrying exactly as the production caller would.

        The filter scorer retries network errors, 429 / 5xx, malformed JSON
        *and* schema-validation failures (other 4xx are fatal); the Tier-1 /
        Tier-3 caller retries network errors, any HTTP error and malformed
        JSON but not validation failures.  Latency, tokens and cost are
        summed over every attempt so a model that needs a retry is charged
        for it, and only the final outcome is cached — a cached run that
        ended in a retryable failure before its attempts were used up is
        continued live rather than replayed.
        """
        slot, provider = self._slot_and_provider(eval_slot)
        _slot_name, schema = _SLOT_BINDINGS[eval_slot]
        url, headers = provider_endpoint(provider)
        body = build_chat_request(
            slot, prompt, strict_response_format=openai_strict_response_format(schema)
        )
        if _is_openrouter(provider):
            # OpenRouter returns the exact upstream cost when asked.
            body = {**body, "usage": {"include": True}}
        key = _request_key(body, url)
        policy = _RETRY_POLICIES[eval_slot]
        max_attempts = slot.max_retries + 1

        attempts: list[dict[str, Any]] = list(self._cached_attempts(key))
        from_cache = bool(attempts)
        backoff_total = 0.0
        while True:
            outcome = _evaluate(attempts, schema) if attempts else None
            if outcome is not None and (
                outcome.error is None
                or not policy.retries(outcome.error, outcome.status)
                or len(attempts) >= max_attempts
            ):
                break
            from_cache = False
            if outcome is not None and policy.backoff:
                # Wait exactly as the production filter scorer would before
                # retrying a 429 (Retry-After), 5xx or network failure.
                delay = retry_delay_seconds(
                    status=outcome.status,
                    headers=attempts[-1].get("headers") or {},
                    config=self._config,
                    attempt=len(attempts) - 1,
                )
                if delay > 0 and outcome.error in ("http", "network"):
                    self._sleeper(delay)
                    backoff_total += delay
            result, latency, net_error = self._post(
                url, headers, body, slot.request_timeout
            )
            attempts.append(
                {
                    "latency_seconds": latency,
                    "status": result.status_code if result else None,
                    "headers": dict(result.headers) if result else {},
                    "body": (
                        result.body.decode("utf-8", errors="replace") if result else ""
                    ),
                    "network_error": net_error,
                }
            )
            if attempts[-1]["status"] is None or attempts[-1]["status"] >= 400:
                _log.warning(
                    "%s/%s attempt %d/%d failed: status=%s %s",
                    eval_slot,
                    case_id,
                    len(attempts),
                    max_attempts,
                    attempts[-1]["status"],
                    (net_error or attempts[-1]["body"])[:200],
                )
        self._store(key, {"attempts": attempts})
        assert outcome is not None
        return _finish(
            case_id,
            eval_slot,
            slot.model,
            outcome,
            from_cache=from_cache,
            backoff_seconds=backoff_total,
        )

    def _cached_attempts(self, key: str) -> list[dict[str, Any]]:
        cached = self._cached(key)
        if cached is None:
            return []
        if isinstance(cached.get("attempts"), list):
            return list(cached["attempts"])
        # Pre-retry cache shape: one attempt stored flat (its ``attempts``
        # key, when present, is the attempt *number*).
        return [cached]

    def run_filter(self, cases: Sequence[FilterCase]) -> list[CaseResult]:
        return self._run_many(
            "filter", [(c.case_id, self._filter_prompt(c)) for c in cases]
        )

    def run_tier1(self, cases: Sequence[Tier1Case]) -> list[CaseResult]:
        return self._run_many(
            "tier1", [(c.case_id, self._tier1_prompt(c)) for c in cases]
        )

    def run_tier3(self, cases: Sequence[Tier3Case]) -> list[CaseResult]:
        return self._run_many(
            "tier3", [(c.case_id, self._tier3_prompt(c)) for c in cases]
        )

    def _run_many(
        self, eval_slot: str, prompts: Sequence[tuple[str, str]]
    ) -> list[CaseResult]:
        if not prompts:
            return []
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            return list(
                pool.map(lambda p: self.run_one(eval_slot, p[0], p[1]), prompts)
            )


def _is_clean_object(content: Any) -> bool:
    if not isinstance(content, str):
        return False
    try:
        return isinstance(json.loads(content), dict)
    except json.JSONDecodeError:
        return False


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Which failure kinds a slot's production caller retries."""

    network: bool = True
    parse: bool = True
    validate: bool = False
    http_4xx: bool = False  # 429 and 5xx are always retried
    backoff: bool = False
    """Wait (Retry-After / exponential) before retrying 429, 5xx and network
    failures, as the production filter scorer does.  The Tier-1 / Tier-3
    caller retries immediately, so its slots leave this off."""

    def retries(self, error: str, status: int | None) -> bool:
        if error == "network":
            return self.network
        if error == "parse":
            return self.parse
        if error == "validate":
            return self.validate
        if error == "http":
            if status is None:
                return self.network
            if status == 429 or status >= 500:
                return True
            return self.http_4xx
        return False


# Mirrors ``influx.filter._call_filter_model_with_retry`` and
# ``influx.enrich._call_json_model`` respectively.
_RETRY_POLICIES: dict[str, RetryPolicy] = {
    "filter": RetryPolicy(parse=True, validate=True, http_4xx=False, backoff=True),
    "tier1": RetryPolicy(parse=True, validate=False, http_4xx=True),
    "tier3": RetryPolicy(parse=True, validate=False, http_4xx=True),
}


@dataclass(frozen=True, slots=True)
class _Outcome:
    """The last attempt's classification plus totals over all attempts."""

    error: str | None
    error_detail: str
    status: int | None
    usage: Usage
    latency_seconds: float
    attempts: int
    raw_content: str = ""
    output: dict[str, Any] | None = None
    salvaged: bool = False
    attempt_errors: tuple[str, ...] = ()


def _classify(
    attempt: dict[str, Any], schema: type[BaseModel]
) -> tuple[str | None, str, Usage, str, dict[str, Any] | None, bool]:
    """Classify one attempt: (error, detail, usage, raw_content, output, salvaged)."""
    status = attempt.get("status")
    if status is None:
        return "network", attempt.get("network_error", ""), Usage(), "", None, False
    if status >= 400:
        return (
            "http",
            f"HTTP {status}: {attempt['body'][:300]}",
            Usage(),
            "",
            None,
            False,
        )
    try:
        resp = json.loads(attempt["body"])
        content = resp["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        return "parse", f"envelope: {exc}", Usage(), "", None, False
    usage = _usage_from(resp)
    raw = content if isinstance(content, str) else ""
    try:
        parsed = parse_json_content(content)
    except JsonContentError as exc:
        return "parse", str(exc), usage, raw, None, False
    try:
        validated = schema.model_validate(parsed)
    except ValidationError as exc:
        return "validate", str(exc)[:500], usage, raw, None, False
    return None, "", usage, raw, validated.model_dump(), not _is_clean_object(content)


def _evaluate(attempts: Sequence[dict[str, Any]], schema: type[BaseModel]) -> _Outcome:
    totals = Usage()
    errors: list[str] = []
    last: tuple[str | None, str, Usage, str, dict[str, Any] | None, bool] = (
        "network",
        "",
        Usage(),
        "",
        None,
        False,
    )
    for attempt in attempts:
        last = _classify(attempt, schema)
        error, _detail, usage, _raw, _out, _salv = last
        totals = Usage(
            prompt_tokens=totals.prompt_tokens + usage.prompt_tokens,
            completion_tokens=totals.completion_tokens + usage.completion_tokens,
            reasoning_tokens=totals.reasoning_tokens + usage.reasoning_tokens,
            cost_usd=_add_cost(
                totals.cost_usd, usage.cost_usd, first=attempt is attempts[0]
            ),
        )
        if error:
            errors.append(error)
    error, detail, _usage, raw, output, salvaged = last
    return _Outcome(
        error=error,
        error_detail=detail,
        status=attempts[-1].get("status"),
        usage=totals,
        latency_seconds=sum(float(a.get("latency_seconds") or 0.0) for a in attempts),
        attempts=len(attempts),
        raw_content=raw,
        output=output,
        salvaged=salvaged,
        attempt_errors=tuple(errors),
    )


def _add_cost(total: float | None, cost: float | None, *, first: bool) -> float | None:
    """Sum costs; a failed attempt reports no cost and is treated as free."""
    if first:
        return cost
    if total is None:
        return cost
    return total + (cost or 0.0)


def _finish(
    case_id: str,
    eval_slot: str,
    model: str,
    outcome: _Outcome,
    *,
    from_cache: bool,
    backoff_seconds: float = 0.0,
) -> CaseResult:
    meta: dict[str, Any] = {"from_cache": from_cache}
    if backoff_seconds:
        meta["backoff_seconds"] = backoff_seconds
    if outcome.salvaged:
        meta["salvaged"] = True
    if outcome.attempt_errors:
        meta["attempt_errors"] = list(outcome.attempt_errors)
    return CaseResult(
        case_id=case_id,
        slot=eval_slot,
        model=model,
        latency_seconds=outcome.latency_seconds,
        usage=outcome.usage,
        output=outcome.output,
        error=outcome.error,
        error_detail=outcome.error_detail,
        raw_content=outcome.raw_content,
        attempts=outcome.attempts,
        meta=meta,
    )
