"""Production-default arXiv filter scorer.

Wraps the configured ``[models.filter]`` slot + ``[prompts.filter]``
prompt into an :data:`~influx.sources.arxiv.ArxivFilterScorer` callable
that scores a batch of fetched arXiv items in one LLM call.

This is the production default that satisfies the score-gating contract
in PRD 07 §5.6 / US-014 / US-015 — without it the production
``InfluxService`` path would write every arXiv item abstract-only with
no extraction or enrichment, regardless of the candidate's real
relevance.

Design notes
------------
- The seam stays test-injectable: ``influx.service.create_app`` accepts
  an ``arxiv_filter_scorer`` override so integration tests can substitute
  a deterministic batch scorer without standing up a real LLM filter.
- ``models.filter`` configuration is required for the default scorer to
  exist.  When it is missing we return ``None`` and the provider falls
  back to its existing no-scorer behaviour (every item written
  abstract-only) so misconfigured deployments still produce notes
  rather than crashing.
- Scorer failure is non-fatal at the per-item level: items the LLM
  filter omits from its ``results`` array are dropped; transport / parse
  / validation failures cause the scorer to return an empty mapping so
  every item falls back to abstract-only ingestion (§5.6 graceful
  degradation).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from pydantic import ValidationError

from influx.config import AppConfig
from influx.schemas import FilterResponse

__all__ = [
    "make_default_arxiv_filter_scorer",
]

_log = logging.getLogger(__name__)


def make_default_arxiv_filter_scorer(
    config: AppConfig,
) -> Any | None:
    """Build the production-default LLM filter scorer.

    Returns an async ``ArxivFilterScorer`` that POSTs the rendered
    filter prompt + items to the ``models.filter`` slot
    (OpenAI-compatible ``/chat/completions``) and parses the response
    against :class:`~influx.schemas.FilterResponse`.

    Returns ``None`` when ``[models.filter]`` is not configured — the
    provider then falls back to its no-scorer behaviour (every item
    written abstract-only) instead of crashing.
    """
    if "filter" not in config.models:
        return None

    async def _scorer(
        items: list[Any],
        profile: str,
        filter_prompt: str,
    ) -> dict[str, Any]:
        from influx.sources.arxiv import ArxivScoreResult

        if not items:
            return {}

        slot = config.models["filter"]
        provider = config.providers.get(slot.provider)
        if provider is None:
            _log.warning(
                "filter provider %r not configured; skipping LLM filter for %r",
                slot.provider,
                profile,
            )
            return {}

        item_payload = [
            {
                "id": item.arxiv_id,
                "title": item.title,
                "abstract": item.abstract,
            }
            for item in items
        ]
        # Append the candidate batch to the rendered filter prompt so
        # the LLM has both the scoring rubric (profile description +
        # negative examples + min_score_in_results, already rendered
        # by ``scheduler.run_profile``) AND the items to score.
        user_message = (
            f"{filter_prompt}\n\n"
            "## CANDIDATES\n"
            f"{json.dumps(item_payload, ensure_ascii=False)}"
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
            "messages": [{"role": "user", "content": user_message}],
        }
        if slot.max_tokens is not None:
            body["max_tokens"] = slot.max_tokens
        if slot.json_mode:
            body["response_format"] = {"type": "json_object"}

        try:
            resp = httpx.post(
                url,
                json=body,
                headers=headers,
                timeout=float(slot.request_timeout),
            )
        except httpx.HTTPError as exc:
            _log.warning(
                "filter HTTP error for profile %r: %s; falling back to no scores",
                profile,
                exc,
            )
            return {}

        if resp.status_code >= 400:
            _log.warning(
                "filter slot HTTP %d for profile %r; falling back to no scores",
                resp.status_code,
                profile,
            )
            return {}

        try:
            resp_json = resp.json()
            content_str: str = resp_json["choices"][0]["message"]["content"]
            parsed = json.loads(content_str)
            response = FilterResponse.model_validate(parsed)
        except (
            json.JSONDecodeError,
            KeyError,
            IndexError,
            ValidationError,
            ValueError,
        ) as exc:
            _log.warning(
                "filter response parse failure for profile %r: %s; "
                "falling back to no scores",
                profile,
                exc,
            )
            return {}

        return {
            r.id: ArxivScoreResult(
                score=r.score,
                confidence=1.0,
                reason=r.reason,
            )
            for r in response.results
        }

    return _scorer
