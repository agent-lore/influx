"""Tests for tier1_enrich and tier3_extract (PRD 07, US-009, US-010).

Tier 1 tests verify:
- Successful enrichment returns a valid ``Tier1Enrichment``
- Validation failure (e.g. ``contributions: []``) raises ``LCMAError``
- Prompt is rendered with all three required variables
- The ``models.enrich`` slot is the one invoked

Tier 3 tests verify:
- Successful extraction returns a valid ``Tier3Extraction``
- ``claims: []`` triggers validation failure
- 600-char claim is truncated to 500
- Prompt is rendered with both required variables
- The ``models.extract`` slot is the one invoked
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from influx.config import AppConfig, load_config
from influx.enrich import _call_json_model, tier1_enrich, tier3_extract
from influx.errors import LCMAError
from influx.http_client import FetchResult
from influx.schemas import Tier1Enrichment, Tier3Extraction


def _valid_tier1_response(**overrides: Any) -> dict[str, Any]:
    """Build a valid Tier1Enrichment JSON dict with optional overrides."""
    base: dict[str, Any] = {
        "contributions": ["Novel attention mechanism for long-context tasks"],
        "method": "Transformer architecture with sliding window attention",
        "result": "15% improvement on SCROLLS benchmark",
        "relevance": "Directly applicable to LLM reasoning research",
    }
    base.update(overrides)
    return base


def _make_chat_response(content_dict: dict[str, Any]) -> dict[str, Any]:
    """Wrap a content dict in an OpenAI chat-completions response envelope."""
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(content_dict),
                }
            }
        ]
    }


class TestTier1EnrichSuccess:
    """tier1_enrich returns a validated Tier1Enrichment on success."""

    def test_returns_tier1_enrichment(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier1_response()

        with patch("influx.enrich._call_json_model", return_value=payload):
            result = tier1_enrich(
                title="Attention Is All You Need",
                abstract="We propose a new architecture...",
                profile_summary="LLM reasoning research",
                config=config,
            )

        assert isinstance(result, Tier1Enrichment)
        assert result.contributions == payload["contributions"]
        assert result.method == payload["method"]
        assert result.result == payload["result"]
        assert result.relevance == payload["relevance"]

    def test_multi_contribution(self, influx_config_env: Any, tmp_path: Any) -> None:
        config = load_config()
        payload = _valid_tier1_response(
            contributions=[
                "Novel attention mechanism",
                "New training recipe",
                "Open-source codebase",
            ]
        )

        with patch("influx.enrich._call_json_model", return_value=payload):
            result = tier1_enrich(
                title="Title",
                abstract="Abstract",
                profile_summary="Summary",
                config=config,
            )

        assert len(result.contributions) == 3

    def test_max_contributions(self, influx_config_env: Any, tmp_path: Any) -> None:
        config = load_config()
        payload = _valid_tier1_response(
            contributions=[f"Contribution {i}" for i in range(6)]
        )

        with patch("influx.enrich._call_json_model", return_value=payload):
            result = tier1_enrich(
                title="Title",
                abstract="Abstract",
                profile_summary="Summary",
                config=config,
            )

        assert len(result.contributions) == 6


class TestTier1EnrichValidationFailure:
    """Validation failure surfaces as LCMAError (FR-ENR-6, AC-07-A)."""

    def test_empty_contributions_raises(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        """AC-07-A: contributions: [] triggers validation failure path."""
        config = load_config()
        payload = _valid_tier1_response(contributions=[])

        with (
            patch("influx.enrich._call_json_model", return_value=payload),
            pytest.raises(LCMAError, match="validation"),
        ):
            tier1_enrich(
                title="Title",
                abstract="Abstract",
                profile_summary="Summary",
                config=config,
            )

    def test_too_many_contributions_raises(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier1_response(contributions=[f"c{i}" for i in range(7)])

        with (
            patch("influx.enrich._call_json_model", return_value=payload),
            pytest.raises(LCMAError, match="validation"),
        ):
            tier1_enrich(
                title="Title",
                abstract="Abstract",
                profile_summary="Summary",
                config=config,
            )

    def test_missing_field_raises(self, influx_config_env: Any, tmp_path: Any) -> None:
        config = load_config()
        payload = {"contributions": ["One"], "method": "M"}
        # Missing 'result' and 'relevance'

        with (
            patch("influx.enrich._call_json_model", return_value=payload),
            pytest.raises(LCMAError, match="validation"),
        ):
            tier1_enrich(
                title="Title",
                abstract="Abstract",
                profile_summary="Summary",
                config=config,
            )

    def test_lcma_error_has_validate_stage(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier1_response(contributions=[])

        with (
            patch("influx.enrich._call_json_model", return_value=payload),
            pytest.raises(LCMAError) as exc_info,
        ):
            tier1_enrich(
                title="T",
                abstract="A",
                profile_summary="P",
                config=config,
            )
        assert exc_info.value.stage == "validate"
        assert exc_info.value.model == "enrich"


class TestTier1EnrichPromptRendering:
    """Prompt is rendered with all three required variables."""

    def test_prompt_rendered_with_all_variables(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier1_response()
        captured_prompt: list[str] = []

        def fake_call(
            cfg: Any, slot: str, prompt: str, **kwargs: Any
        ) -> dict[str, Any]:
            captured_prompt.append(prompt)
            return payload

        with patch("influx.enrich._call_json_model", side_effect=fake_call):
            tier1_enrich(
                title="My Title",
                abstract="My Abstract",
                profile_summary="My Profile",
                config=config,
            )

        assert len(captured_prompt) == 1
        rendered = captured_prompt[0]
        # conftest template: "Enrich: {title} {abstract} {profile_summary}"
        assert "My Title" in rendered
        assert "My Abstract" in rendered
        assert "My Profile" in rendered

    def test_prompt_uses_config_template(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier1_response()
        captured: list[str] = []

        def fake_call(
            cfg: Any, slot: str, prompt: str, **kwargs: Any
        ) -> dict[str, Any]:
            captured.append(prompt)
            return payload

        with patch("influx.enrich._call_json_model", side_effect=fake_call):
            tier1_enrich(
                title="T",
                abstract="A",
                profile_summary="P",
                config=config,
            )

        # Template from conftest: "Enrich: {title} {abstract} {profile_summary}"
        assert captured[0] == "Enrich: T A P"


class TestTier1EnrichModelSlot:
    """The configured ``models.enrich`` slot is the one invoked."""

    def test_calls_enrich_slot(self, influx_config_env: Any, tmp_path: Any) -> None:
        config = load_config()
        payload = _valid_tier1_response()
        captured_slot: list[str] = []

        def fake_call(
            cfg: Any, slot: str, prompt: str, **kwargs: Any
        ) -> dict[str, Any]:
            captured_slot.append(slot)
            return payload

        with patch("influx.enrich._call_json_model", side_effect=fake_call):
            tier1_enrich(
                title="T",
                abstract="A",
                profile_summary="P",
                config=config,
            )

        assert captured_slot == ["enrich"]

    def test_passes_config_to_model_call(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier1_response()
        captured_cfg: list[AppConfig] = []

        def fake_call(
            cfg: Any, slot: str, prompt: str, **kwargs: Any
        ) -> dict[str, Any]:
            captured_cfg.append(cfg)
            return payload

        with patch("influx.enrich._call_json_model", side_effect=fake_call):
            tier1_enrich(
                title="T",
                abstract="A",
                profile_summary="P",
                config=config,
            )

        assert captured_cfg[0] is config


class TestTier1EnrichTransportFailure:
    """Transport failures surface as LCMAError."""

    def test_model_call_failure_propagates(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()

        with (
            patch(
                "influx.enrich._call_json_model",
                side_effect=LCMAError("boom", model="enrich", stage="http"),
            ),
            pytest.raises(LCMAError, match="boom"),
        ):
            tier1_enrich(
                title="T",
                abstract="A",
                profile_summary="P",
                config=config,
            )


# ── Tier 3 extraction tests (US-010) ────────────────────────────────


def _valid_tier3_response(**overrides: Any) -> dict[str, Any]:
    """Build a valid Tier3Extraction JSON dict with optional overrides."""
    base: dict[str, Any] = {
        "claims": ["Achieves state-of-the-art on SCROLLS benchmark"],
        "datasets": ["SCROLLS", "LongBench"],
        "builds_on": ["Transformer architecture (Vaswani et al., 2017)"],
        "open_questions": ["How does it scale beyond 128k tokens?"],
        "potential_connections": ["Related to sparse attention literature"],
    }
    base.update(overrides)
    return base


class TestTier3ExtractSuccess:
    """tier3_extract returns a validated Tier3Extraction on success."""

    def test_returns_tier3_extraction(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier3_response()

        with patch("influx.enrich._call_json_model", return_value=payload):
            result = tier3_extract(
                title="Attention Is All You Need",
                full_text="We propose a new architecture...",
                config=config,
            )

        assert isinstance(result, Tier3Extraction)
        assert result.claims == payload["claims"]
        assert result.datasets == payload["datasets"]
        assert result.builds_on == payload["builds_on"]
        assert result.open_questions == payload["open_questions"]
        assert result.potential_connections == payload["potential_connections"]

    def test_max_claims(self, influx_config_env: Any, tmp_path: Any) -> None:
        config = load_config()
        payload = _valid_tier3_response(claims=[f"Claim {i}" for i in range(10)])

        with patch("influx.enrich._call_json_model", return_value=payload):
            result = tier3_extract(
                title="Title",
                full_text="Full text",
                config=config,
            )

        assert len(result.claims) == 10

    def test_optional_lists_empty(self, influx_config_env: Any, tmp_path: Any) -> None:
        config = load_config()
        payload = _valid_tier3_response(
            datasets=[], builds_on=[], open_questions=[], potential_connections=[]
        )

        with patch("influx.enrich._call_json_model", return_value=payload):
            result = tier3_extract(
                title="Title",
                full_text="Full text",
                config=config,
            )

        assert result.datasets == []
        assert result.builds_on == []
        assert result.open_questions == []
        assert result.potential_connections == []


class TestTier3ExtractTruncation:
    """Oversize string elements are truncated to 500 chars (AC-07-B)."""

    def test_600_char_claim_truncated_to_500(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        long_claim = "x" * 600
        payload = _valid_tier3_response(claims=[long_claim])

        with patch("influx.enrich._call_json_model", return_value=payload):
            result = tier3_extract(
                title="Title",
                full_text="Full text",
                config=config,
            )

        assert len(result.claims[0]) == 500

    def test_500_char_claim_not_truncated(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        exact_claim = "y" * 500
        payload = _valid_tier3_response(claims=[exact_claim])

        with patch("influx.enrich._call_json_model", return_value=payload):
            result = tier3_extract(
                title="Title",
                full_text="Full text",
                config=config,
            )

        assert len(result.claims[0]) == 500
        assert result.claims[0] == exact_claim


class TestTier3ExtractValidationFailure:
    """Validation failure surfaces as LCMAError (FR-ENR-6, AC-07-C)."""

    def test_empty_claims_raises(self, influx_config_env: Any, tmp_path: Any) -> None:
        """AC-07-C: claims: [] triggers validation failure path."""
        config = load_config()
        payload = _valid_tier3_response(claims=[])

        with (
            patch("influx.enrich._call_json_model", return_value=payload),
            pytest.raises(LCMAError, match="validation"),
        ):
            tier3_extract(
                title="Title",
                full_text="Full text",
                config=config,
            )

    def test_too_many_claims_raises(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier3_response(claims=[f"c{i}" for i in range(11)])

        with (
            patch("influx.enrich._call_json_model", return_value=payload),
            pytest.raises(LCMAError, match="validation"),
        ):
            tier3_extract(
                title="Title",
                full_text="Full text",
                config=config,
            )

    def test_lcma_error_has_validate_stage(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier3_response(claims=[])

        with (
            patch("influx.enrich._call_json_model", return_value=payload),
            pytest.raises(LCMAError) as exc_info,
        ):
            tier3_extract(
                title="T",
                full_text="F",
                config=config,
            )
        assert exc_info.value.stage == "validate"
        assert exc_info.value.model == "extract"

    def test_dict_elements_raise_lcma_validate_error(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        """Some extract models occasionally emit ``[{"claim": "...", ...}]``
        instead of plain strings.  The schema-level validator must surface
        this as ``LCMAError(stage="validate")`` so the per-paper failure
        path runs (``influx:repair-needed``) rather than aborting the
        whole scheduler run with a bare ``AttributeError`` (staging
        incident 2026-05-01).
        """
        config = load_config()
        payload = _valid_tier3_response(
            claims=[{"claim": "x", "score": 0.8}],  # type: ignore[list-item]
        )

        with (
            patch("influx.enrich._call_json_model", return_value=payload),
            pytest.raises(LCMAError) as exc_info,
        ):
            tier3_extract(title="T", full_text="F", config=config)
        assert exc_info.value.stage == "validate"
        assert exc_info.value.model == "extract"


class TestTier3ExtractPromptRendering:
    """Prompt is rendered with both required variables."""

    def test_prompt_rendered_with_all_variables(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier3_response()
        captured_prompt: list[str] = []

        def fake_call(
            cfg: Any, slot: str, prompt: str, **kwargs: Any
        ) -> dict[str, Any]:
            captured_prompt.append(prompt)
            return payload

        with patch("influx.enrich._call_json_model", side_effect=fake_call):
            tier3_extract(
                title="My Title",
                full_text="My Full Text",
                config=config,
            )

        assert len(captured_prompt) == 1
        rendered = captured_prompt[0]
        assert "My Title" in rendered
        assert "My Full Text" in rendered

    def test_prompt_uses_config_template(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier3_response()
        captured: list[str] = []

        def fake_call(
            cfg: Any, slot: str, prompt: str, **kwargs: Any
        ) -> dict[str, Any]:
            captured.append(prompt)
            return payload

        with patch("influx.enrich._call_json_model", side_effect=fake_call):
            tier3_extract(
                title="T",
                full_text="F",
                config=config,
            )

        # Template from conftest: "Extract: {title} {full_text}"
        assert captured[0] == "Extract: T F"


class TestTier3ExtractModelSlot:
    """The configured ``models.extract`` slot is the one invoked."""

    def test_calls_extract_slot(self, influx_config_env: Any, tmp_path: Any) -> None:
        config = load_config()
        payload = _valid_tier3_response()
        captured_slot: list[str] = []

        def fake_call(
            cfg: Any, slot: str, prompt: str, **kwargs: Any
        ) -> dict[str, Any]:
            captured_slot.append(slot)
            return payload

        with patch("influx.enrich._call_json_model", side_effect=fake_call):
            tier3_extract(
                title="T",
                full_text="F",
                config=config,
            )

        assert captured_slot == ["extract"]

    def test_passes_config_to_model_call(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        payload = _valid_tier3_response()
        captured_cfg: list[AppConfig] = []

        def fake_call(
            cfg: Any, slot: str, prompt: str, **kwargs: Any
        ) -> dict[str, Any]:
            captured_cfg.append(cfg)
            return payload

        with patch("influx.enrich._call_json_model", side_effect=fake_call):
            tier3_extract(
                title="T",
                full_text="F",
                config=config,
            )

        assert captured_cfg[0] is config


class TestTier3ExtractTransportFailure:
    """Transport failures surface as LCMAError."""

    def test_model_call_failure_propagates(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()

        with (
            patch(
                "influx.enrich._call_json_model",
                side_effect=LCMAError("boom", model="extract", stage="http"),
            ),
            pytest.raises(LCMAError, match="boom"),
        ):
            tier3_extract(
                title="T",
                full_text="F",
                config=config,
            )


# ── OpenAI structured-outputs response_format wiring (issue #16) ────


def _make_post_result(content_dict: dict[str, Any]) -> FetchResult:
    """Build a minimal ``FetchResult`` mimicking an OpenAI chat-completions
    success response."""
    body = json.dumps(_make_chat_response(content_dict)).encode("utf-8")
    return FetchResult(
        body=body,
        status_code=200,
        content_type="application/json",
        final_url="https://api.openai.com/v1/chat/completions",
    )


class TestCallJsonModelResponseFormat:
    """``_call_json_model`` selects the right ``response_format`` shape based
    on the slot's ``json_schema_strict`` flag.
    """

    def _capture_post(
        self, response_dict: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], Any]:
        """Patch ``guarded_post_json_fetch`` and return (captured-bodies, ctx)."""
        captured: list[dict[str, Any]] = []

        def fake_post(
            url: str,
            payload: dict[str, Any],
            **kwargs: Any,
        ) -> FetchResult:
            captured.append(payload)
            return _make_post_result(response_dict)

        return captured, patch(
            "influx.enrich.guarded_post_json_fetch", side_effect=fake_post
        )

    def test_json_object_when_strict_disabled(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        """Default (json_mode=true, json_schema_strict=false) sends the loose
        ``json_object`` response_format that callers shipped with."""
        config = load_config()
        config.models["extract"].json_schema_strict = False

        captured, ctx = self._capture_post(_valid_tier3_response())
        with ctx:
            _call_json_model(config, "extract", "prompt", schema_class=Tier3Extraction)

        assert len(captured) == 1
        body = captured[0]
        assert body["response_format"] == {"type": "json_object"}

    def test_json_schema_strict_when_enabled(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        """When the slot opts in, the body carries the strict json_schema
        response_format pinning the Pydantic class."""
        config = load_config()
        config.models["extract"].json_schema_strict = True

        captured, ctx = self._capture_post(_valid_tier3_response())
        with ctx:
            _call_json_model(config, "extract", "prompt", schema_class=Tier3Extraction)

        body = captured[0]
        rf = body["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["strict"] is True
        assert rf["json_schema"]["name"] == "Tier3Extraction"

        schema = rf["json_schema"]["schema"]
        # OpenAI strict mode requirements.
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {
            "claims",
            "datasets",
            "builds_on",
            "open_questions",
            "potential_connections",
        }
        # Per-element type pinned to string for every list field — the
        # whole point of the change for issue #16.
        for field in ("claims", "datasets", "builds_on", "open_questions"):
            assert schema["properties"][field]["type"] == "array"
            assert schema["properties"][field]["items"]["type"] == "string"

    def test_strict_mode_falls_back_when_no_schema_class(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        """Strict flag without a schema class can't build a json_schema
        body; falls back to plain json_object so the request still flies."""
        config = load_config()
        config.models["extract"].json_schema_strict = True

        captured, ctx = self._capture_post(_valid_tier3_response())
        with ctx:
            _call_json_model(config, "extract", "prompt", schema_class=None)

        assert captured[0]["response_format"] == {"type": "json_object"}


class TestTier3ExtractStrictResponseFormat:
    """End-to-end check that ``tier3_extract`` propagates ``Tier3Extraction``
    into the request body when the slot opts in.
    """

    def test_strict_request_carries_tier3_schema(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        config.models["extract"].json_schema_strict = True

        captured: list[dict[str, Any]] = []

        def fake_post(url: str, payload: dict[str, Any], **kwargs: Any) -> FetchResult:
            captured.append(payload)
            return _make_post_result(_valid_tier3_response())

        with patch("influx.enrich.guarded_post_json_fetch", side_effect=fake_post):
            tier3_extract(title="T", full_text="F", config=config)

        rf = captured[0]["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "Tier3Extraction"

    def test_strict_request_carries_tier1_schema(
        self, influx_config_env: Any, tmp_path: Any
    ) -> None:
        config = load_config()
        config.models["enrich"].json_schema_strict = True

        captured: list[dict[str, Any]] = []

        def fake_post(url: str, payload: dict[str, Any], **kwargs: Any) -> FetchResult:
            captured.append(payload)
            return _make_post_result(_valid_tier1_response())

        with patch("influx.enrich.guarded_post_json_fetch", side_effect=fake_post):
            tier1_enrich(title="T", abstract="A", profile_summary="P", config=config)

        rf = captured[0]["response_format"]
        assert rf["json_schema"]["name"] == "Tier1Enrichment"
