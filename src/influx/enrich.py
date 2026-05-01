"""Tier-1 enrichment & Tier-3 extraction — JSON-mode LLM callers.

``tier1_enrich`` renders ``prompts.tier1_enrich`` against the
``models.enrich`` slot and validates against ``Tier1Enrichment``
(PRD 07 §5.2 FR-ENR-4).

``tier3_extract`` renders ``prompts.tier3_extract`` against the
``models.extract`` slot and validates against ``Tier3Extraction``
(PRD 07 §5.3 FR-ENR-5).  Oversize string elements (> 500 chars) are
truncated on ingest by the schema's ``field_validator``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from pydantic import BaseModel, ValidationError

from influx.config import AppConfig
from influx.errors import LCMAError, NetworkError
from influx.http_client import guarded_post_json_fetch
from influx.prompts import load_prompt
from influx.schemas import (
    Tier1Enrichment,
    Tier3Extraction,
    openai_strict_response_format,
)

__all__ = [
    "tier1_enrich",
    "tier3_extract",
]

logger = logging.getLogger(__name__)


# ── JSON-mode model inference helper ─────────────────────────────────


def _call_json_model(
    config: AppConfig,
    slot_name: str,
    prompt: str,
    *,
    schema_class: type[BaseModel] | None = None,
) -> dict[str, Any]:
    """Call a JSON-mode model slot and return the parsed response dict.

    Resolves the model slot and provider from *config*, constructs an
    OpenAI-compatible ``/chat/completions`` request, and parses the
    JSON content from the response.

    When *schema_class* is provided AND the slot has
    ``json_schema_strict = true``, sends the OpenAI structured-outputs
    ``response_format = {"type": "json_schema", "strict": true, ...}``
    variant so the model is constrained to emit JSON conforming to
    *schema_class*.  Falls back to plain ``json_object`` mode otherwise.

    Provider compatibility
    ----------------------
    This caller only speaks the OpenAI-compatible ``/chat/completions``
    API surface — providers must expose that contract.  OpenAI itself
    and OpenRouter are compatible; Anthropic's native ``/v1/messages``
    API is NOT.  To use Anthropic models, route them through OpenRouter
    (``providers.openrouter``) rather than binding ``[models.*].provider``
    to ``"anthropic"`` directly.  The shipped ``influx.example.toml``
    illustrates this routing.

    Raises :class:`~influx.errors.LCMAError` on transport, HTTP, or
    parse failure.
    """
    slot = config.models.get(slot_name)
    if slot is None:
        raise LCMAError(
            f"Model slot {slot_name!r} not configured",
            model=slot_name,
            stage="resolve",
        )

    provider = config.providers.get(slot.provider)
    if provider is None:
        raise LCMAError(
            f"Provider {slot.provider!r} not configured for slot {slot_name!r}",
            model=slot_name,
            stage="resolve",
        )

    api_key = ""
    if provider.api_key_env:
        api_key = os.environ.get(provider.api_key_env, "")

    url = f"{provider.base_url.rstrip('/')}/chat/completions"

    headers: dict[str, str] = {**provider.extra_headers}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body: dict[str, Any] = {
        "model": slot.model,
        "temperature": slot.temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if slot.max_tokens is not None:
        body["max_tokens"] = slot.max_tokens
    if slot.json_schema_strict and schema_class is not None:
        body["response_format"] = openai_strict_response_format(schema_class)
    elif slot.json_mode:
        body["response_format"] = {"type": "json_object"}

    attempts = slot.max_retries + 1
    last_error: Exception | None = None
    for _attempt in range(attempts):
        try:
            result = guarded_post_json_fetch(
                url,
                body,
                headers=headers,
                allow_private_ips=config.security.allow_private_ips,
                max_response_bytes=config.storage.max_download_bytes,
                timeout_seconds=slot.request_timeout,
            )
        except NetworkError as exc:
            last_error = exc
            continue

        if result.status_code >= 400:
            last_error = LCMAError(
                f"Model slot {slot_name!r} returned HTTP {result.status_code}",
                model=slot_name,
                stage="http",
                detail=result.body[:500].decode("utf-8", errors="replace"),
            )
            continue

        try:
            resp_json = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = exc
            continue

        try:
            content_str: str = resp_json["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LCMAError(
                f"Unexpected response structure from model slot {slot_name!r}",
                model=slot_name,
                stage="parse",
                detail=str(resp_json)[:500],
            ) from exc

        try:
            parsed: dict[str, Any] = json.loads(content_str)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

        return parsed

    raise LCMAError(
        f"Model slot {slot_name!r} failed after {attempts} attempts",
        model=slot_name,
        stage="http",
        detail=str(last_error)[:500] if last_error is not None else "",
    ) from last_error


# ── Tier 1 enrichment caller ─────────────────────────────────────────


def tier1_enrich(
    *,
    title: str,
    abstract: str,
    profile_summary: str,
    config: AppConfig,
) -> Tier1Enrichment:
    """Invoke Tier-1 enrichment against the ``models.enrich`` slot (FR-ENR-4).

    Renders ``prompts.tier1_enrich`` with ``{title}``, ``{abstract}``,
    ``{profile_summary}`` and dispatches it to the configured JSON-mode
    enrichment model slot.  The response is validated against
    :class:`~influx.schemas.Tier1Enrichment`.

    Parameters
    ----------
    title:
        Paper or article title.
    abstract:
        Paper abstract or article summary.
    profile_summary:
        The interest profile description.
    config:
        Loaded :class:`~influx.config.AppConfig` — the model slot and
        prompt are resolved from this at runtime.

    Returns
    -------
    Tier1Enrichment
        Validated enrichment result.

    Raises
    ------
    LCMAError
        On transport failure, HTTP error, JSON parse failure, or
        schema validation failure — the caller can handle this per
        FR-ENR-6 (no placeholder text on failure).
    """
    prompt_cfg = config.prompts.tier1_enrich
    prompt_text = load_prompt(text=prompt_cfg.text, path=prompt_cfg.path)
    rendered = prompt_text.format(
        title=title,
        abstract=abstract,
        profile_summary=profile_summary,
    )

    raw = _call_json_model(config, "enrich", rendered, schema_class=Tier1Enrichment)

    try:
        return Tier1Enrichment.model_validate(raw)
    except ValidationError as exc:
        raise LCMAError(
            f"Tier 1 enrichment response failed validation: {exc}",
            model="enrich",
            stage="validate",
            detail=str(raw)[:500],
        ) from exc


# ── Tier 3 extraction caller ────────────────────────────────────────


def tier3_extract(
    *,
    title: str,
    full_text: str,
    config: AppConfig,
) -> Tier3Extraction:
    """Invoke Tier-3 deep extraction against the ``models.extract`` slot (FR-ENR-5).

    Renders ``prompts.tier3_extract`` with ``{title}`` and ``{full_text}``
    and dispatches it to the configured JSON-mode extraction model slot.
    The response is validated against :class:`~influx.schemas.Tier3Extraction`
    — oversize string elements (> 500 chars) are truncated by the schema's
    ``field_validator``, not rejected.

    Parameters
    ----------
    title:
        Paper or article title.
    full_text:
        Extracted full text (Tier 2 body).
    config:
        Loaded :class:`~influx.config.AppConfig` — the model slot and
        prompt are resolved from this at runtime.

    Returns
    -------
    Tier3Extraction
        Validated extraction result with truncated strings.

    Raises
    ------
    LCMAError
        On transport failure, HTTP error, JSON parse failure, or
        schema validation failure — the caller can handle this per
        FR-ENR-6 (no placeholder text on failure).
    """
    prompt_cfg = config.prompts.tier3_extract
    prompt_text = load_prompt(text=prompt_cfg.text, path=prompt_cfg.path)
    rendered = prompt_text.format(
        title=title,
        full_text=full_text,
    )

    raw = _call_json_model(config, "extract", rendered, schema_class=Tier3Extraction)

    try:
        return Tier3Extraction.model_validate(raw)
    except ValidationError as exc:
        raise LCMAError(
            f"Tier 3 extraction response failed validation: {exc}",
            model="extract",
            stage="validate",
            detail=str(raw)[:500],
        ) from exc
