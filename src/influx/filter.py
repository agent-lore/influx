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
  / validation failures raise :class:`FilterScorerError` so the caller
  (the arXiv item provider) can fall every item in the batch back to
  abstract-only ingestion instead of dropping the batch entirely
  (§5.6 graceful degradation).  An empty returned mapping therefore
  unambiguously means "the LLM intentionally scored nothing above
  ``filter.min_score_in_results``" — drop the items, do not emit
  abstract-only notes for them.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from influx import metrics
from influx.config import AppConfig, ProfileConfig
from influx.errors import NetworkError
from influx.http_client import guarded_post_json_fetch
from influx.schemas import FilterResponse
from influx.source import Candidate, ScoredCandidate

__all__ = [
    "BatchScorer",
    "Filter",
    "FilterScorerError",
    "make_default_arxiv_filter_scorer",
    "make_default_batch_scorer",
]


# Source-agnostic batched scorer signature.  Receives the candidate list
# (post fetch_candidates) plus profile name + rendered filter prompt and
# returns a mapping of ``Candidate.item_id`` → ``ScoredCandidate``.
# Items omitted from the mapping are dropped by :class:`Filter.score`.
BatchScorer = Callable[
    [list[Candidate], str, str],
    Awaitable[dict[str, ScoredCandidate]],
]


class FilterScorerError(RuntimeError):
    """Hard failure of the production-default LLM filter scorer.

    Raised when the scorer cannot produce a valid scoring decision at
    all (provider misconfigured, HTTP error, malformed response).  The
    arXiv item provider catches this and falls every item in the batch
    back to abstract-only ingestion (score=0) so a misconfigured /
    transient-LLM-failure deployment still produces notes instead of
    silently dropping the run (PRD 07 §5.6 graceful degradation).
    """


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
                "filter provider %r not configured for profile %r; "
                "falling back to abstract-only ingestion",
                slot.provider,
                profile,
            )
            raise FilterScorerError(
                f"filter provider {slot.provider!r} not configured",
            )

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

        attempts = slot.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
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
                _log.warning(
                    "filter HTTP error for profile %r on attempt %d/%d: %s",
                    profile,
                    attempt + 1,
                    attempts,
                    exc,
                )
                continue

            if result.status_code >= 400:
                last_error = FilterScorerError(f"filter slot HTTP {result.status_code}")
                _log.warning(
                    "filter slot HTTP %d for profile %r on attempt %d/%d",
                    result.status_code,
                    profile,
                    attempt + 1,
                    attempts,
                )
                continue

            try:
                resp_json = json.loads(result.body.decode("utf-8"))
                content_str: str = resp_json["choices"][0]["message"]["content"]
                parsed = json.loads(content_str)
                response = FilterResponse.model_validate(parsed)
                return {
                    r.id: ArxivScoreResult(
                        score=r.score,
                        confidence=1.0,
                        reason=r.reason,
                        filter_tags=tuple(r.tags),
                    )
                    for r in response.results
                }
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
                ValidationError,
                ValueError,
            ) as exc:
                last_error = exc
                _log.warning(
                    "filter response parse failure for profile %r on attempt %d/%d: %s",
                    profile,
                    attempt + 1,
                    attempts,
                    exc,
                )
                continue

        raise FilterScorerError(f"filter failed after {attempts} attempts") from (
            last_error
        )

    return _scorer


# ── Source-agnostic Filter (issue #57) ───────────────────────────────


def make_default_batch_scorer(config: AppConfig) -> BatchScorer | None:
    """Build the production-default source-agnostic batched scorer.

    Returns an async :data:`BatchScorer` that posts the rendered filter
    prompt + candidates to the ``models.filter`` slot and parses the
    response against :class:`FilterResponse`.  Returns ``None`` when
    ``[models.filter]`` is not configured so misconfigured deployments
    still ingest abstract-only notes (PRD 07 §5.6 graceful degradation).
    """
    if "filter" not in config.models:
        return None

    async def _scorer(
        candidates: list[Candidate],
        profile: str,
        filter_prompt: str,
    ) -> dict[str, ScoredCandidate]:
        if not candidates:
            return {}

        slot = config.models["filter"]
        provider = config.providers.get(slot.provider)
        if provider is None:
            raise FilterScorerError(
                f"filter provider {slot.provider!r} not configured",
            )

        candidate_payload = [
            {"id": c.item_id, "title": c.title, "abstract": c.abstract}
            for c in candidates
        ]
        user_message = (
            f"{filter_prompt}\n\n"
            "## CANDIDATES\n"
            f"{json.dumps(candidate_payload, ensure_ascii=False)}"
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

        by_id: dict[str, Candidate] = {c.item_id: c for c in candidates}
        attempts = slot.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
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
                _log.warning(
                    "filter HTTP error for profile %r on attempt %d/%d: %s",
                    profile,
                    attempt + 1,
                    attempts,
                    exc,
                )
                continue

            if result.status_code >= 400:
                last_error = FilterScorerError(f"filter slot HTTP {result.status_code}")
                continue

            try:
                resp_json = json.loads(result.body.decode("utf-8"))
                content_str: str = resp_json["choices"][0]["message"]["content"]
                parsed = json.loads(content_str)
                response = FilterResponse.model_validate(parsed)
                return {
                    r.id: ScoredCandidate(
                        candidate=by_id[r.id],
                        score=r.score,
                        confidence=1.0,
                        reason=r.reason,
                        filter_tags=tuple(r.tags),
                    )
                    for r in response.results
                    if r.id in by_id
                }
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
                ValidationError,
                ValueError,
            ) as exc:
                last_error = exc
                continue

        raise FilterScorerError(f"filter failed after {attempts} attempts") from (
            last_error
        )

    return _scorer


class Filter:
    """Score-gated entry to ingestion (CONTEXT.md ``Filter``).

    Wraps the LLM filter scorer + threshold gate + drop-missing logic
    in one source-agnostic seam.  The scheduler / source orchestrator
    calls :meth:`score` between ``Source.fetch_candidates`` and
    ``Source.acquire``.

    Behaviour
    ---------
    - Items absent from the scorer's response are dropped (the LLM
      explicitly chose not to score them).
    - Items below ``profile_cfg.thresholds.relevance`` are dropped
      (FR-FLT-7).
    - When the scorer raises :class:`FilterScorerError`, the entire
      batch is skipped (FR-FLT-6 / spec §7.1) — the run yields zero
      items rather than ingesting abstract-only notes for them.
    - When *scorer* is ``None`` (no ``[models.filter]`` configured),
      :meth:`score` returns an empty list so misconfigured deployments
      still complete the run cleanly.

    The ``profile_cfg``-bound shape mirrors :class:`influx.cascade.Cascade`
    and :class:`influx.lcma_wiring.LcmaWiringDeps` — built once per
    profile run, reused for every fetch_candidates batch.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        profile_cfg: ProfileConfig,
        scorer: BatchScorer | None,
    ) -> None:
        self._config = config
        self._profile_cfg = profile_cfg
        self._scorer = scorer

    @property
    def has_scorer(self) -> bool:
        """``True`` when a batched scorer is wired (production default)."""
        return self._scorer is not None

    async def score(
        self,
        candidates: list[Candidate],
        *,
        filter_prompt: str,
        source: str,
    ) -> list[ScoredCandidate]:
        """Score *candidates* and return those above the relevance threshold.

        Parameters
        ----------
        candidates:
            The unscored candidates returned by ``Source.fetch_candidates``.
        filter_prompt:
            The rendered prompt (profile description + negative-feedback
            examples + ``min_score_in_results``) composed by the
            scheduler.
        source:
            Source family for metric labels (``"arxiv"``, ``"rss"``…).
        """
        if not candidates:
            return []
        if self._scorer is None:
            return []

        profile = self._profile_cfg.name
        threshold = self._profile_cfg.thresholds.relevance
        batch_size = max(int(self._config.filter.batch_size), 1)

        all_scores: dict[str, ScoredCandidate] = {}
        for chunk_start in range(0, len(candidates), batch_size):
            chunk = candidates[chunk_start : chunk_start + batch_size]
            try:
                chunk_scores = await self._scorer(chunk, profile, filter_prompt)
            except FilterScorerError:
                _log.warning(
                    "filter scorer failed for profile %r source=%s; skipping batch",
                    profile,
                    source,
                    exc_info=True,
                )
                return []
            all_scores.update(chunk_scores)

        drop_attrs = {"profile": profile, "decision": "drop"}
        pass_attrs = {"profile": profile, "decision": "pass"}
        kept: list[ScoredCandidate] = []
        for cand in candidates:
            scored = all_scores.get(cand.item_id)
            if scored is None:
                metrics.articles_filtered().add(1, drop_attrs)
                continue
            if scored.score < threshold:
                metrics.articles_filtered().add(1, drop_attrs)
                continue
            metrics.articles_filtered().add(1, pass_attrs)
            kept.append(scored)
        return kept
