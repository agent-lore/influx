"""Wire-shape of one OpenAI-compatible ``/chat/completions`` request.

Every LLM caller in Influx — the filter scorer, the Tier-1 / Tier-3
enrichment callers and the ``validate-config`` JSON-mode probe — used to
assemble its own request body and endpoint.  Keeping that in one place
means a model-slot option such as ``reasoning_effort`` reaches every
caller at once, and the model-eval harness (``evals/``) can send
byte-identical requests to the ones production sends.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from influx.config import ModelSlotConfig, ProviderConfig

__all__ = ["build_chat_request", "parse_json_content", "provider_endpoint"]

_log = logging.getLogger(__name__)

# How many ``{`` positions to try when the content does not start with the
# object.  Prose preambles are short; this bounds the work on garbage.
_SALVAGE_MAX_STARTS = 20


class JsonContentError(ValueError):
    """The model's message content held no JSON object."""


def parse_json_content(content: str, *, context: str = "") -> dict[str, Any]:
    """Return the JSON object in a model's message content.

    *context* (e.g. ``"slot=extract model=openai/gpt-6-luna"``) is echoed in
    the salvage WARNING so the rate can be read per slot from the logs.

    The fast path is the whole content being one object.  Reasoning models
    at ``reasoning_effort: "none"`` sometimes think aloud in the content
    before the object, or emit a complete object followed by stray tokens
    and a second copy (observed with GPT-6 Luna on long Tier-3 outputs).
    Both are salvaged by decoding the first complete object found; a
    WARNING records how much was discarded so the rate stays visible.
    Raises :class:`JsonContentError` when no object can be decoded.
    """
    if not isinstance(content, str):
        # ``message.content`` is null on a refusal / empty completion.
        raise JsonContentError(
            f"model content is {type(content).__name__}, not a string"
        )
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        value = None
    else:
        if isinstance(value, dict):
            return value
        # Well-formed JSON of the wrong shape (an array, a string) is a
        # contract violation, not noise around an object: do not dig into it.
        raise JsonContentError(
            f"model content is JSON but not an object ({type(value).__name__})"
        )

    decoder = json.JSONDecoder()
    start = content.find("{")
    tried = 0
    while start != -1 and tried < _SALVAGE_MAX_STARTS:
        try:
            value, end = decoder.raw_decode(content, start)
        except json.JSONDecodeError:
            value, end = None, start
        if isinstance(value, dict):
            _log.warning(
                "salvaged JSON object from model content: prefix=%d chars "
                "trailing=%d chars %s",
                start,
                len(content) - end,
                context,
            )
            return value
        tried += 1
        start = content.find("{", start + 1)
    raise JsonContentError("model content holds no JSON object")


def provider_endpoint(provider: ProviderConfig) -> tuple[str, dict[str, str]]:
    """Return the ``/chat/completions`` URL and request headers for *provider*.

    The API key is read from the environment variable the provider names
    (``api_key_env``); an unset or empty variable sends no ``Authorization``
    header, which is what a keyless local server expects.
    """
    url = f"{provider.base_url.rstrip('/')}/chat/completions"
    headers: dict[str, str] = {**provider.extra_headers}
    api_key = os.environ.get(provider.api_key_env, "") if provider.api_key_env else ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return url, headers


def build_chat_request(
    slot: ModelSlotConfig,
    prompt: str,
    *,
    strict_response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the chat-completions body for *slot* carrying *prompt*.

    ``reasoning_effort`` is sent only when the slot sets it.  Reasoning
    models reject sampling parameters whenever reasoning is on, so
    ``temperature`` is sent only when the slot's effort is unset or
    ``"none"`` (GPT-6 developer guide, "Unsupported parameters").

    *strict_response_format* is the OpenAI structured-outputs
    ``response_format`` pinning the caller's output schema; it is used
    only when the slot opts in via ``json_schema_strict``.  Otherwise a
    ``json_mode`` slot gets plain ``json_object`` mode.
    """
    body: dict[str, Any] = {
        "model": slot.model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if slot.reasoning_effort is not None:
        body["reasoning_effort"] = slot.reasoning_effort
    if slot.reasoning_effort in (None, "none"):
        body["temperature"] = slot.temperature
    if slot.max_tokens is not None:
        body["max_tokens"] = slot.max_tokens
    if slot.json_schema_strict and strict_response_format is not None:
        body["response_format"] = strict_response_format
    elif slot.json_mode:
        body["response_format"] = {"type": "json_object"}
    return body
