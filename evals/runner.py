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
from dataclasses import dataclass, replace
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
from influx.filter import render_filter_user_message
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
    ) -> None:
        self._config = config
        self._spec = spec
        self._cache_dir = cache_dir
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

        cached = self._cached(key)
        if cached is not None:
            return _finish(
                case_id, eval_slot, slot.model, schema, cached, from_cache=True
            )

        attempts = slot.max_retries + 1
        last: dict[str, Any] = {}
        for attempt in range(1, attempts + 1):
            result, latency, net_error = self._post(
                url, headers, body, slot.request_timeout
            )
            last = {
                "attempts": attempt,
                "latency_seconds": latency,
                "status": result.status_code if result else None,
                "body": result.body.decode("utf-8", errors="replace") if result else "",
                "network_error": net_error,
            }
            if result is not None and result.status_code < 400:
                self._store(key, last)
                break
            _log.warning(
                "%s/%s attempt %d/%d failed: status=%s %s",
                eval_slot,
                case_id,
                attempt,
                attempts,
                last["status"],
                (net_error or last["body"])[:200],
            )
        return _finish(case_id, eval_slot, slot.model, schema, last, from_cache=False)

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


def _is_clean_object(content: str) -> bool:
    try:
        return isinstance(json.loads(content), dict)
    except json.JSONDecodeError:
        return False


def _finish(
    case_id: str,
    eval_slot: str,
    model: str,
    schema: type[BaseModel],
    last: dict[str, Any],
    *,
    from_cache: bool,
) -> CaseResult:
    base = CaseResult(
        case_id=case_id,
        slot=eval_slot,
        model=model,
        latency_seconds=float(last.get("latency_seconds") or 0.0),
        usage=Usage(),
        attempts=int(last.get("attempts") or 1),
        meta={"from_cache": from_cache},
    )
    status = last.get("status")
    if status is None:
        return replace(
            base, error="network", error_detail=last.get("network_error", "")
        )
    if status >= 400:
        return replace(
            base, error="http", error_detail=f"HTTP {status}: {last['body'][:300]}"
        )
    try:
        resp = json.loads(last["body"])
        content = resp["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        return replace(base, error="parse", error_detail=f"envelope: {exc}")
    usage = _usage_from(resp)
    raw = content if isinstance(content, str) else ""
    try:
        parsed = parse_json_content(content)
    except JsonContentError as exc:
        return replace(
            base, usage=usage, raw_content=raw, error="parse", error_detail=str(exc)
        )
    salvaged = not _is_clean_object(content)
    if salvaged:
        base = replace(base, meta={**base.meta, "salvaged": True})
    try:
        validated = schema.model_validate(parsed)
    except ValidationError as exc:
        return replace(
            base,
            usage=usage,
            raw_content=content,
            error="validate",
            error_detail=str(exc)[:500],
        )
    return replace(
        base, usage=usage, raw_content=content, output=validated.model_dump()
    )
