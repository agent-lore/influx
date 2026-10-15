"""Tests for the shared chat-completions request builder (``influx.llm_request``)."""

from __future__ import annotations

import pytest

from influx.config import ModelSlotConfig, ProviderConfig
from influx.llm_request import (
    JsonContentError,
    build_chat_request,
    parse_json_content,
    provider_endpoint,
)
from influx.schemas import Tier1Enrichment, openai_strict_response_format


def _slot(**overrides: object) -> ModelSlotConfig:
    base: dict[str, object] = {
        "provider": "p",
        "model": "openai/gpt-4.1-mini",
        "temperature": 0.2,
        "json_mode": True,
    }
    base.update(overrides)
    return ModelSlotConfig.model_validate(base)


class TestBuildChatRequest:
    def test_legacy_slot_shape_is_unchanged(self) -> None:
        body = build_chat_request(_slot(max_tokens=2048), "hi")

        assert body == {
            "model": "openai/gpt-4.1-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
        }

    def test_reasoning_none_keeps_temperature(self) -> None:
        body = build_chat_request(_slot(reasoning_effort="none"), "hi")

        assert body["reasoning_effort"] == "none"
        assert body["temperature"] == 0.2

    @pytest.mark.parametrize(
        "effort", ["minimal", "low", "medium", "high", "xhigh", "max"]
    )
    def test_reasoning_on_drops_temperature(self, effort: str) -> None:
        body = build_chat_request(_slot(reasoning_effort=effort), "hi")

        assert body["reasoning_effort"] == effort
        assert "temperature" not in body

    def test_unset_reasoning_sends_no_effort_key(self) -> None:
        body = build_chat_request(_slot(), "hi")

        assert "reasoning_effort" not in body

    def test_strict_schema_used_only_when_slot_opts_in(self) -> None:
        strict = openai_strict_response_format(Tier1Enrichment)

        opted_in = build_chat_request(
            _slot(json_schema_strict=True), "hi", strict_response_format=strict
        )
        opted_out = build_chat_request(_slot(), "hi", strict_response_format=strict)

        assert opted_in["response_format"] is strict
        assert opted_out["response_format"] == {"type": "json_object"}

    def test_strict_slot_without_schema_falls_back_to_json_object(self) -> None:
        body = build_chat_request(_slot(json_schema_strict=True), "hi")

        assert body["response_format"] == {"type": "json_object"}

    def test_no_json_mode_sends_no_response_format(self) -> None:
        body = build_chat_request(_slot(json_mode=False), "hi")

        assert "response_format" not in body

    def test_invalid_effort_is_rejected_by_config(self) -> None:
        with pytest.raises(ValueError, match="reasoning_effort"):
            _slot(reasoning_effort="turbo")


class TestProviderEndpoint:
    def test_bearer_header_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_LLM_KEY", "sk-test")
        provider = ProviderConfig(
            base_url="https://example.test/v1/",
            api_key_env="TEST_LLM_KEY",
            extra_headers={"X-Title": "influx"},
        )

        url, headers = provider_endpoint(provider)

        assert url == "https://example.test/v1/chat/completions"
        assert headers == {"X-Title": "influx", "Authorization": "Bearer sk-test"}

    def test_keyless_provider_sends_no_authorization(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_LLM_KEY", raising=False)
        provider = ProviderConfig(base_url="http://localhost:11434/v1", api_key_env="")

        _url, headers = provider_endpoint(provider)

        assert "Authorization" not in headers


class TestParseJsonContent:
    def test_clean_object_passes_through(self) -> None:
        assert parse_json_content('{"a": 1}') == {"a": 1}

    def test_prose_prefix_is_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        content = 'We need JSON only. Scores: x 7, y 8.\n{"results": [{"id": "x"}]}'

        with caplog.at_level("WARNING", logger="influx.llm_request"):
            parsed = parse_json_content(content, context="slot=filter model=m")

        assert parsed == {"results": [{"id": "x"}]}
        assert "salvaged JSON object" in caplog.text
        assert "prefix=37 chars trailing=0 chars slot=filter model=m" in caplog.text

    def test_trailing_junk_and_duplicate_object_are_dropped(self) -> None:
        content = '{"claims": ["a"]} ciao\n\n{"claims": ["a"]'

        assert parse_json_content(content) == {"claims": ["a"]}

    def test_brace_inside_prose_is_not_fatal(self) -> None:
        content = 'think {not json} then {"ok": true}'

        assert parse_json_content(content) == {"ok": True}

    def test_top_level_array_is_rejected(self) -> None:
        with pytest.raises(JsonContentError):
            parse_json_content('[{"a": 1}]')

    def test_no_object_raises(self) -> None:
        with pytest.raises(JsonContentError):
            parse_json_content("nothing to see here")

    def test_null_content_is_a_content_error_not_a_crash(self) -> None:
        with pytest.raises(JsonContentError, match="NoneType"):
            parse_json_content(None)  # type: ignore[arg-type]
