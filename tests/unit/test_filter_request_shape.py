"""The production filter scorer must send exactly what the eval harness sends.

PR #309 review finding 1: the scorer built its request without the strict
``FilterResponse`` schema while the eval runner supplied it, so strict-mode
eval results did not describe production.  These tests pin the two paths
to each other.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from evals.cases import CandidateRecord, FilterCase
from evals.runner import ModelSpec, Runner

from influx.config import AppConfig, load_config
from influx.feedback import format_negative_examples
from influx.filter import make_default_batch_scorer
from influx.http_client import FetchResult
from influx.schemas import FilterResponse
from influx.source import Candidate


def _strict_config(influx_config_env: Any) -> AppConfig:
    config = load_config(check_api_keys=False)
    config.models["filter"] = config.models["filter"].model_copy(
        update={"json_schema_strict": True}
    )
    return config


async def test_production_scorer_sends_strict_schema_when_slot_opts_in(
    influx_config_env: Any,
) -> None:
    config = _strict_config(influx_config_env)
    scorer = make_default_batch_scorer(config)
    assert scorer is not None
    captured: dict[str, Any] = {}

    def fake_call(**kwargs: Any) -> FilterResponse:
        captured.update(kwargs["body"])
        return FilterResponse(results=[])

    with patch("influx.filter._call_filter_model_with_retry", side_effect=fake_call):
        await scorer([Candidate("x", "T", "A", "https://e.test")], "ai-robotics", "P")

    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["name"] == "FilterResponse"
    assert captured["response_format"]["json_schema"]["strict"] is True


async def test_production_and_eval_requests_are_identical(
    influx_config_env: Any,
) -> None:
    config = _strict_config(influx_config_env)
    profile = next(p for p in config.profiles if p.name == "ai-robotics")
    candidates = [Candidate("x", "Title X", "Abs X", ""), Candidate("y", "Y", "B", "")]

    # Production: the scheduler renders the prompt, the scorer posts it.
    prompt = (config.prompts.filter.text or "").format(
        profile_description=profile.description,
        negative_examples=format_negative_examples([]),
        min_score_in_results=config.filter.min_score_in_results,
    )
    scorer = make_default_batch_scorer(config)
    assert scorer is not None
    production: dict[str, Any] = {}

    def fake_call(**kwargs: Any) -> FilterResponse:
        production.update(kwargs["body"])
        return FilterResponse(results=[])

    with patch("influx.filter._call_filter_model_with_retry", side_effect=fake_call):
        await scorer(candidates, "ai-robotics", prompt)

    # Eval: the runner renders from the case and posts through its poster.
    harness: dict[str, Any] = {}

    def poster(url: str, body: dict[str, Any], **_: Any) -> FetchResult:
        harness.update(body)
        content = json.dumps({"results": []})
        return FetchResult(
            body=json.dumps({"choices": [{"message": {"content": content}}]}).encode(),
            status_code=200,
            content_type="application/json",
            final_url=url,
        )

    slot = config.models["filter"]
    spec = ModelSpec("t", slot.model, slot.provider)
    runner = Runner(config, spec, poster=poster)  # type: ignore[arg-type]
    case = FilterCase(
        "c",
        "ai-robotics",
        tuple(CandidateRecord(c.item_id, c.title, c.abstract) for c in candidates),
    )
    runner.run_filter([case])

    assert harness == production


@pytest.mark.parametrize("strict", [True, False])
def test_runner_follows_slot_strictness(influx_config_env: Any, strict: bool) -> None:
    config = load_config(check_api_keys=False)
    config.models["filter"] = config.models["filter"].model_copy(
        update={"json_schema_strict": strict}
    )
    seen: dict[str, Any] = {}

    def poster(url: str, body: dict[str, Any], **_: Any) -> FetchResult:
        seen.update(body)
        return FetchResult(
            body=json.dumps(
                {"choices": [{"message": {"content": json.dumps({"results": []})}}]}
            ).encode(),
            status_code=200,
            content_type="application/json",
            final_url=url,
        )

    slot = config.models["filter"]
    Runner(config, ModelSpec("t", slot.model, slot.provider), poster=poster).run_filter(  # type: ignore[arg-type]
        [FilterCase("c", "ai-robotics", (CandidateRecord("x", "T", "A"),))]
    )

    assert seen["response_format"]["type"] == (
        "json_schema" if strict else "json_object"
    )
