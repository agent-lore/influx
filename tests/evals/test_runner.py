"""Runner: production-identical requests, validation, retries, caching."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.cases import CandidateRecord, FilterCase, Tier1Case, Tier3Case
from evals.runner import ModelSpec, Runner

from influx.config import AppConfig
from influx.enrich import render_tier1_prompt
from influx.errors import NetworkError
from influx.http_client import FetchResult
from tests.evals.conftest import chat_response, error_response


class _Poster:
    """Records every request body and replays scripted responses."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, url: str, body: dict[str, Any], **_: Any):
        self.bodies.append(body)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


_SPEC = ModelSpec(
    label="t",
    model="openai/gpt-6-luna",
    provider="test-provider",
    reasoning_effort="none",
)

_TIER1 = {
    "contributions": ["a"],
    "method": "m",
    "result": "r",
    "relevance": "rel",
}


def test_tier1_request_matches_production_shape(eval_config: AppConfig) -> None:
    poster = _Poster([chat_response(_TIER1)])
    runner = Runner(eval_config, _SPEC, poster=poster)  # type: ignore[arg-type]
    case = Tier1Case(case_id="t1", profile="ai-robotics", title="T", abstract="A")

    (result,) = runner.run_tier1([case])

    assert result.ok and result.output == _TIER1
    body = poster.bodies[0]
    assert body["model"] == "openai/gpt-6-luna"
    assert body["reasoning_effort"] == "none"
    assert "temperature" in body
    profile = next(p for p in eval_config.profiles if p.name == "ai-robotics")
    assert body["messages"][0]["content"] == render_tier1_prompt(
        eval_config, title="T", abstract="A", profile_summary=profile.description
    )
    assert body["response_format"] == {"type": "json_object"}
    assert result.usage.cost_usd == pytest.approx(0.0001)


def test_strict_override_sends_json_schema(eval_config: AppConfig) -> None:
    poster = _Poster([chat_response(_TIER1)])
    spec = ModelSpec("t", "m", "test-provider", json_schema_strict=True)
    runner = Runner(eval_config, spec, poster=poster)  # type: ignore[arg-type]

    runner.run_tier1([Tier1Case("t1", "ai-robotics", "T", "A")])

    fmt = poster.bodies[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True


def test_filter_prompt_embeds_candidates_block(eval_config: AppConfig) -> None:
    out = {"results": [{"id": "x", "score": 8, "tags": ["t"], "reason": "r"}]}
    poster = _Poster([chat_response(out)])
    runner = Runner(eval_config, _SPEC, poster=poster)  # type: ignore[arg-type]
    case = FilterCase(
        case_id="f1",
        profile="ai-robotics",
        candidates=(CandidateRecord("x", "Title X", "Abs X"),),
    )

    (result,) = runner.run_filter([case])

    assert result.ok and result.output == out
    content = poster.bodies[0]["messages"][0]["content"]
    assert "## CANDIDATES" in content
    assert json.dumps([{"id": "x", "title": "Title X", "abstract": "Abs X"}]) in content
    assert "Multi-agent systems" in content  # profile description rendered in


def test_validation_failure_is_recorded_not_raised(eval_config: AppConfig) -> None:
    poster = _Poster(
        [
            chat_response(
                {"contributions": [], "method": "m", "result": "r", "relevance": ""}
            )
        ]
    )
    runner = Runner(eval_config, _SPEC, poster=poster)  # type: ignore[arg-type]

    (result,) = runner.run_tier1([Tier1Case("t1", "ai-robotics", "T", "A")])

    assert not result.ok
    assert result.error == "validate"
    assert "contributions" in result.error_detail
    assert result.usage.prompt_tokens == 100


def test_non_json_content_is_a_parse_error_after_production_retries(
    eval_config: AppConfig,
) -> None:
    attempts = eval_config.models["extract"].max_retries + 1
    poster = _Poster([chat_response("not json at all")] * attempts)
    runner = Runner(eval_config, _SPEC, poster=poster)  # type: ignore[arg-type]

    (result,) = runner.run_tier3([Tier3Case("t3", "T", "body")])

    assert result.error == "parse" and result.attempts == attempts
    assert result.raw_content == "not json at all"
    assert len(poster.bodies) == attempts


def test_http_errors_retry_up_to_slot_max_retries(eval_config: AppConfig) -> None:
    retries = eval_config.models["enrich"].max_retries
    poster = _Poster([error_response(500)] * (retries + 1))
    runner = Runner(eval_config, _SPEC, poster=poster)  # type: ignore[arg-type]

    (result,) = runner.run_tier1([Tier1Case("t1", "ai-robotics", "T", "A")])

    assert result.error == "http"
    assert result.attempts == retries + 1
    assert len(poster.bodies) == retries + 1


def test_network_error_then_success(eval_config: AppConfig) -> None:
    poster = _Poster(
        [NetworkError("boom", url="https://x", kind="timeout"), chat_response(_TIER1)]
    )
    runner = Runner(eval_config, _SPEC, poster=poster)  # type: ignore[arg-type]

    (result,) = runner.run_tier1([Tier1Case("t1", "ai-robotics", "T", "A")])

    assert result.ok and result.attempts == 2


def test_cache_replays_identical_request(
    eval_config: AppConfig, tmp_path: Path
) -> None:
    poster = _Poster([chat_response(_TIER1)])
    case = Tier1Case("t1", "ai-robotics", "T", "A")

    first = Runner(eval_config, _SPEC, poster=poster, cache_dir=tmp_path).run_tier1(
        [case]
    )  # type: ignore[arg-type]
    second = Runner(eval_config, _SPEC, poster=poster, cache_dir=tmp_path).run_tier1(
        [case]
    )  # type: ignore[arg-type]

    assert first[0].ok and second[0].ok
    assert second[0].meta["from_cache"] is True
    assert len(poster.bodies) == 1  # no second network call


def test_spec_overrides_only_what_it_sets(eval_config: AppConfig) -> None:
    base = eval_config.models["enrich"]
    spec = ModelSpec(label="x", model="m", provider="p", temperature=0.9)

    slot = spec.apply(base)

    assert (slot.model, slot.provider, slot.temperature) == ("m", "p", 0.9)
    assert slot.json_schema_strict == base.json_schema_strict
    assert slot.reasoning_effort is None


def test_unknown_provider_is_a_clear_error(eval_config: AppConfig) -> None:
    runner = Runner(eval_config, ModelSpec("x", "m", "nope"), poster=_Poster([]))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="provider 'nope'"):
        runner.run_tier1([Tier1Case("t1", "ai-robotics", "T", "A")])


def test_null_content_is_recorded_as_parse_error_and_cached_replay_agrees(
    eval_config: AppConfig, tmp_path: Path
) -> None:
    envelope = {
        "choices": [{"message": {"content": None}}],
        "usage": {"prompt_tokens": 5},
    }
    null_response = FetchResult(
        body=json.dumps(envelope).encode(),
        status_code=200,
        content_type="application/json",
        final_url="https://example.test",
    )
    attempts = eval_config.models["enrich"].max_retries + 1
    poster = _Poster([null_response] * attempts)
    case = Tier1Case("t1", "ai-robotics", "T", "A")

    (first,) = Runner(eval_config, _SPEC, poster=poster, cache_dir=tmp_path).run_tier1(
        [case]
    )  # type: ignore[arg-type]
    (replay,) = Runner(eval_config, _SPEC, poster=poster, cache_dir=tmp_path).run_tier1(
        [case]
    )  # type: ignore[arg-type]

    assert first.error == "parse" and "NoneType" in first.error_detail
    assert first.attempts == attempts
    assert first.raw_content == "" and first.usage.prompt_tokens == 5 * attempts
    assert replay.error == "parse" and replay.meta["from_cache"] is True
    assert len(poster.bodies) == attempts  # replay made no network call


# ── production retry policy ──────────────────────────────────────────


def test_malformed_200_reply_is_retried_and_all_attempts_are_charged(
    eval_config: AppConfig, tmp_path: Path
) -> None:
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "cost": 0.001}
    poster = _Poster(
        [
            chat_response("We need JSON only. hmm", usage=usage),
            chat_response(_TIER1, usage=usage),
        ]
    )
    runner = Runner(eval_config, _SPEC, poster=poster, cache_dir=tmp_path)  # type: ignore[arg-type]

    (result,) = runner.run_tier1([Tier1Case("t1", "ai-robotics", "T", "A")])

    assert result.ok and result.attempts == 2
    assert len(poster.bodies) == 2
    assert result.usage.prompt_tokens == 200
    assert result.usage.cost_usd == pytest.approx(0.002)
    assert result.meta["attempt_errors"] == ["parse"]
    # Replay from cache: the final outcome, no further network calls.
    (replay,) = Runner(eval_config, _SPEC, poster=poster, cache_dir=tmp_path).run_tier1(  # type: ignore[arg-type]
        [Tier1Case("t1", "ai-robotics", "T", "A")]
    )
    assert replay.ok and replay.attempts == 2 and replay.meta["from_cache"] is True
    assert len(poster.bodies) == 2


def test_filter_retries_validation_failure_like_production(
    eval_config: AppConfig,
) -> None:
    bad = {"results": [{"id": "x", "score": 42, "tags": [], "reason": "r"}]}
    good = {"results": [{"id": "x", "score": 8, "tags": [], "reason": "r"}]}
    poster = _Poster([chat_response(bad), chat_response(good)])
    runner = Runner(eval_config, _SPEC, poster=poster)  # type: ignore[arg-type]
    case = FilterCase("f", "ai-robotics", (CandidateRecord("x", "T", "A"),))

    (result,) = runner.run_filter([case])

    assert result.ok and result.attempts == 2 and result.output == good


def test_tier1_does_not_retry_validation_failure_like_production(
    eval_config: AppConfig,
) -> None:
    bad = {"contributions": [], "method": "m", "result": "r", "relevance": ""}
    poster = _Poster([chat_response(bad), chat_response(_TIER1)])
    runner = Runner(eval_config, _SPEC, poster=poster)  # type: ignore[arg-type]

    (result,) = runner.run_tier1([Tier1Case("t1", "ai-robotics", "T", "A")])

    assert result.error == "validate" and result.attempts == 1
    assert len(poster.bodies) == 1


def test_filter_4xx_is_fatal_but_tier1_4xx_is_retried(eval_config: AppConfig) -> None:
    poster = _Poster([error_response(400), chat_response({"results": []})])
    (filtered,) = Runner(eval_config, _SPEC, poster=poster).run_filter(  # type: ignore[arg-type]
        [FilterCase("f", "ai-robotics", (CandidateRecord("x", "T", "A"),))]
    )
    assert filtered.error == "http" and filtered.attempts == 1

    poster = _Poster([error_response(400), chat_response(_TIER1)])
    (enriched,) = Runner(eval_config, _SPEC, poster=poster).run_tier1(  # type: ignore[arg-type]
        [Tier1Case("t1", "ai-robotics", "T", "A")]
    )
    assert enriched.ok and enriched.attempts == 2


def test_old_cache_entry_with_retryable_failure_is_continued_live(
    eval_config: AppConfig, tmp_path: Path
) -> None:
    # First run with the pre-retry runner shape: a single failed attempt
    # stored flat.  Simulate by running once with retries exhausted at 1.
    one_shot = eval_config.model_copy(deep=True)
    one_shot.models["enrich"] = one_shot.models["enrich"].model_copy(
        update={"max_retries": 0}
    )
    poster = _Poster([chat_response("not json")])
    Runner(one_shot, _SPEC, poster=poster, cache_dir=tmp_path).run_tier1(  # type: ignore[arg-type]
        [Tier1Case("t1", "ai-robotics", "T", "A")]
    )
    # Now the slot allows retries: the cached parse failure is retried live.
    poster = _Poster([chat_response(_TIER1)])
    (result,) = Runner(eval_config, _SPEC, poster=poster, cache_dir=tmp_path).run_tier1(  # type: ignore[arg-type]
        [Tier1Case("t1", "ai-robotics", "T", "A")]
    )

    assert result.ok and result.attempts == 2
    assert result.meta["from_cache"] is False
    assert len(poster.bodies) == 1


# ── production backoff before retries ─────────────────────────────────


def _rate_limited(retry_after: str) -> FetchResult:
    return FetchResult(
        body=b'{"error": {"message": "slow down"}}',
        status_code=429,
        content_type="application/json",
        final_url="https://example.test",
        headers={"Retry-After": retry_after},
    )


def test_filter_waits_retry_after_before_retrying_429(eval_config: AppConfig) -> None:
    good = {"results": [{"id": "x", "score": 8, "tags": [], "reason": "r"}]}
    poster = _Poster([_rate_limited("0.05"), chat_response(good)])
    slept: list[float] = []
    runner = Runner(eval_config, _SPEC, poster=poster, sleeper=slept.append)  # type: ignore[arg-type]

    (result,) = runner.run_filter(
        [FilterCase("f", "ai-robotics", (CandidateRecord("x", "T", "A"),))]
    )

    assert result.ok and result.attempts == 2
    assert slept == [pytest.approx(0.05)]
    assert result.meta["backoff_seconds"] == pytest.approx(0.05)


def test_filter_backs_off_exponentially_on_5xx_and_network(
    eval_config: AppConfig,
) -> None:
    base = eval_config.resilience.backoff_base_seconds
    good = {"results": []}
    poster = _Poster(
        [
            error_response(503),
            NetworkError("boom", url="https://x", kind="timeout"),
            chat_response(good),
        ]
    )
    slept: list[float] = []
    runner = Runner(eval_config, _SPEC, poster=poster, sleeper=slept.append)  # type: ignore[arg-type]

    (result,) = runner.run_filter(
        [FilterCase("f", "ai-robotics", (CandidateRecord("x", "T", "A"),))]
    )

    assert result.ok and result.attempts == 3
    assert slept == [float(base), float(base * 2)]


def test_filter_does_not_sleep_before_retrying_a_parse_failure(
    eval_config: AppConfig,
) -> None:
    poster = _Poster([chat_response("prose only"), chat_response({"results": []})])
    slept: list[float] = []
    runner = Runner(eval_config, _SPEC, poster=poster, sleeper=slept.append)  # type: ignore[arg-type]

    (result,) = runner.run_filter(
        [FilterCase("f", "ai-robotics", (CandidateRecord("x", "T", "A"),))]
    )

    assert result.ok and result.attempts == 2
    assert slept == []


def test_tier1_retries_immediately_like_production(eval_config: AppConfig) -> None:
    poster = _Poster([_rate_limited("5"), chat_response(_TIER1)])
    slept: list[float] = []
    runner = Runner(eval_config, _SPEC, poster=poster, sleeper=slept.append)  # type: ignore[arg-type]

    (result,) = runner.run_tier1([Tier1Case("t1", "ai-robotics", "T", "A")])

    assert result.ok and result.attempts == 2
    assert slept == []
