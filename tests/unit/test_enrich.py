"""Tests for tier1_enrich — real LLM-backed enrichment (PRD 07, US-009).

Replaces the PRD 04 stub tests with tests that verify:
- Successful enrichment returns a valid ``Tier1Enrichment``
- Validation failure (e.g. ``contributions: []``) raises ``LCMAError``
- Prompt is rendered with all three required variables
- The ``models.enrich`` slot is the one invoked
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from influx.config import AppConfig, load_config
from influx.enrich import tier1_enrich
from influx.errors import LCMAError
from influx.schemas import Tier1Enrichment


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

        def fake_call(cfg: Any, slot: str, prompt: str) -> dict[str, Any]:
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

        def fake_call(cfg: Any, slot: str, prompt: str) -> dict[str, Any]:
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

        def fake_call(cfg: Any, slot: str, prompt: str) -> dict[str, Any]:
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

        def fake_call(cfg: Any, slot: str, prompt: str) -> dict[str, Any]:
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
