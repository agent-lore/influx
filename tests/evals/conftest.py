"""Fixtures for the model-eval harness tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from influx.config import AppConfig, load_config
from influx.http_client import FetchResult


@pytest.fixture()
def eval_config(influx_config_env: Any) -> AppConfig:
    """The realistic v0.7 fixture config, loaded without the API-key probe."""
    return load_config(check_api_keys=False)


def chat_response(
    content: dict[str, Any] | str, *, status: int = 200, usage: dict | None = None
) -> FetchResult:
    """Wrap *content* in a chat-completions envelope as a ``FetchResult``."""
    text = content if isinstance(content, str) else json.dumps(content)
    body: dict[str, Any] = {"choices": [{"message": {"content": text}}]}
    body["usage"] = usage or {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "completion_tokens_details": {"reasoning_tokens": 0},
        "cost": 0.0001,
    }
    return FetchResult(
        body=json.dumps(body).encode("utf-8"),
        status_code=status,
        content_type="application/json",
        final_url="https://example.test/v1/chat/completions",
    )


def error_response(status: int, message: str = "nope") -> FetchResult:
    return FetchResult(
        body=json.dumps({"error": {"message": message}}).encode("utf-8"),
        status_code=status,
        content_type="application/json",
        final_url="https://example.test/v1/chat/completions",
    )
