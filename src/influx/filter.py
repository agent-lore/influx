"""Source-agnostic relevance filter and its production-default scorer.

The :class:`Filter` is the single score-gated entry to ingestion for
every bulk source (arXiv, RSS).  :func:`make_default_batch_scorer` wraps
the configured ``[models.filter]`` slot + ``[prompts.filter]`` prompt
into a :data:`BatchScorer` that scores a batch of fetched
:class:`~influx.source.Candidate` records in one LLM call.

This is the production default that satisfies the score-gating contract
in PRD 07 §5.6 / US-014 / US-015 — without it the production
``InfluxService`` path would score nothing and the run would yield zero
items, regardless of the candidates' real relevance.

Design notes
------------
- The seam stays test-injectable: each source provider
  (``make_arxiv_item_provider`` / ``make_rss_item_provider``) and
  ``influx.service.create_app`` accept a ``scorer`` override so
  integration tests can substitute a deterministic batch scorer without
  standing up a real LLM filter.
- ``models.filter`` configuration is required for the default scorer to
  exist.  When it is missing we return ``None``; :meth:`Filter.score`
  then yields zero items (its scorer is ``None``) so misconfigured
  deployments complete the run cleanly rather than crashing.
- Scorer failure is handled at the batch level: items the LLM filter
  omits from its ``results`` array are dropped, and transport / parse /
  validation failures raise :class:`FilterScorerError`, which
  :meth:`Filter.score` catches to skip that batch (record a
  ``filter_error`` and drop only the failed batch's items — batches that
  already scored cleanly are kept) per FR-FLT-6 / §7.1.  An empty
  returned mapping therefore unambiguously means "the LLM intentionally
  scored nothing above ``filter.min_score_in_results``" — drop the items.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

from pydantic import ValidationError

from influx import metrics
from influx.config import AppConfig, ProfileConfig
from influx.errors import NetworkError
from influx.http_client import guarded_post_json_fetch
from influx.schemas import FilterResponse
from influx.source import Candidate, ScoredCandidate
from influx.telemetry import record_filter_error

__all__ = [
    "BatchScorer",
    "Filter",
    "FilterScorerError",
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
    all (provider misconfigured, HTTP error, malformed response).
    :meth:`Filter.score` catches this and skips the failing batch — its
    items are dropped and a ``filter_error`` is recorded (FR-FLT-6 /
    §7.1), while batches that already scored cleanly are still returned —
    so a misconfigured / transient-LLM-failure deployment fails cleanly
    instead of crashing or ingesting unscored notes.
    """


_log = logging.getLogger(__name__)

_RETRY_AFTER_MAX_SECONDS = 300.0


def _sleep(seconds: float) -> None:
    """Sleep wrapper for monkeypatching in tests."""
    time.sleep(seconds)


def _header_value(headers: dict[str, str], name: str) -> str | None:
    """Return an HTTP header value using case-insensitive lookup."""
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return None


def _parse_retry_after_seconds(value: str) -> float | None:
    """Parse RFC 7231 ``Retry-After`` as seconds or HTTP-date."""
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = retry_at.timestamp() - time.time()
    if seconds < 0:
        return 0.0
    return min(seconds, _RETRY_AFTER_MAX_SECONDS)


def _filter_429_delay(
    headers: dict[str, str],
    config: AppConfig,
    attempt: int,
) -> float:
    """Return the backoff delay for a filter-model 429 response."""
    retry_after = _header_value(headers, "Retry-After")
    if retry_after is not None:
        parsed = _parse_retry_after_seconds(retry_after)
        if parsed is not None:
            return parsed
    return float(config.resilience.backoff_base_seconds * (2**attempt))


def _call_filter_model_with_retry(
    *,
    config: AppConfig,
    profile: str,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    attempts: int,
) -> FilterResponse:
    """POST to the configured filter model and parse the response with retries."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            result = guarded_post_json_fetch(
                url,
                body,
                headers=headers,
                allow_private_ips=config.security.allow_private_ips,
                max_response_bytes=config.storage.max_download_bytes,
                timeout_seconds=config.models["filter"].request_timeout,
            )
        except NetworkError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            delay = float(config.resilience.backoff_base_seconds * (2**attempt))
            _log.warning(
                "filter HTTP error for profile %r on attempt %d/%d: %s; "
                "retrying in %.1fs",
                profile,
                attempt + 1,
                attempts,
                exc,
                delay,
            )
            _sleep(delay)
            continue

        if result.status_code == 429:
            last_error = FilterScorerError("filter slot HTTP 429")
            if attempt + 1 >= attempts:
                break
            delay = _filter_429_delay(result.headers, config, attempt)
            _log.warning(
                "filter slot HTTP 429 for profile %r on attempt %d/%d; "
                "backing off %.1fs",
                profile,
                attempt + 1,
                attempts,
                delay,
            )
            _sleep(delay)
            continue

        if result.status_code >= 500:
            last_error = FilterScorerError(f"filter slot HTTP {result.status_code}")
            if attempt + 1 >= attempts:
                break
            delay = float(config.resilience.backoff_base_seconds * (2**attempt))
            _log.warning(
                "filter slot HTTP %d for profile %r on attempt %d/%d; "
                "retrying in %.1fs",
                result.status_code,
                profile,
                attempt + 1,
                attempts,
                delay,
            )
            _sleep(delay)
            continue

        if result.status_code >= 400:
            raise FilterScorerError(f"filter slot HTTP {result.status_code}")

        try:
            resp_json = json.loads(result.body.decode("utf-8"))
            content_str: str = resp_json["choices"][0]["message"]["content"]
            parsed = json.loads(content_str)
            return FilterResponse.model_validate(parsed)
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

    raise FilterScorerError(f"filter failed after {attempts} attempts") from last_error


async def _acall_filter_model_with_retry(
    *,
    config: AppConfig,
    profile: str,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    attempts: int,
) -> FilterResponse:
    """Async wrapper: offloads :func:`_call_filter_model_with_retry` to a worker thread.

    Issue #124: the sync retry path uses blocking ``time.sleep`` and a
    sync HTTP client. Calling it directly from the async event loop
    (RSS scoring path, batched-scorer path) starves the loop and stalls
    the admin HTTP API. Threaded offload preserves the existing retry,
    backoff, and Retry-After semantics verbatim.
    """
    return await asyncio.to_thread(
        _call_filter_model_with_retry,
        config=config,
        profile=profile,
        url=url,
        body=body,
        headers=headers,
        attempts=attempts,
    )


# ── Source-agnostic Filter (issue #57) ───────────────────────────────


def make_default_batch_scorer(config: AppConfig) -> BatchScorer | None:
    """Build the production-default source-agnostic batched scorer.

    Returns an async :data:`BatchScorer` that posts the rendered filter
    prompt + candidates to the ``models.filter`` slot and parses the
    response against :class:`FilterResponse`.  Returns ``None`` when
    ``[models.filter]`` is not configured so :meth:`Filter.score` yields
    zero items and the run completes cleanly (PRD 07 §5.6 graceful
    degradation).
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
        response = await _acall_filter_model_with_retry(
            config=config,
            profile=profile,
            url=url,
            body=body,
            headers=headers,
            attempts=slot.max_retries + 1,
        )
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
    - When the scorer raises :class:`FilterScorerError`, that batch is
      skipped (FR-FLT-6 / spec §7.1) — its items are dropped (without an
      ``articles_filtered{drop}`` tick, since a failed filter call is not
      a per-item rejection) and a ``filter_error`` is recorded, while
      batches that already scored cleanly are still returned.
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
        failed_item_ids: set[str] = set()
        for chunk_start in range(0, len(candidates), batch_size):
            chunk = candidates[chunk_start : chunk_start + batch_size]
            try:
                chunk_scores = await self._scorer(chunk, profile, filter_prompt)
            except FilterScorerError:
                _log.warning(
                    "filter scorer failed for profile %r source=%s; "
                    "skipping this batch (%d items)",
                    profile,
                    source,
                    len(chunk),
                    exc_info=True,
                )
                # #85 review: surface the scorer execution failure to the
                # run ledger so it fires ``filter_error`` rather than
                # misclassifying as ``filter_stall``.  Skip only THIS
                # batch (FR-FLT-6 / spec §7.1: "failed filter batches are
                # skipped") — batches that already scored cleanly are kept
                # rather than discarded by the first failure.  A failed
                # filter call is not a per-item rejection (2.3a), so its
                # items are dropped WITHOUT an ``articles_filtered{drop}``
                # tick.
                record_filter_error()
                failed_item_ids.update(c.item_id for c in chunk)
                continue
            all_scores.update(chunk_scores)

        drop_attrs = {"profile": profile, "decision": "drop"}
        pass_attrs = {"profile": profile, "decision": "pass"}
        kept: list[ScoredCandidate] = []
        for cand in candidates:
            if cand.item_id in failed_item_ids:
                # Scorer errored on this candidate's batch — the failure is
                # already surfaced as ``filter_error``; do not double-count
                # it as a per-item relevance drop.
                continue
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
