"""Score-gated enrichment Cascade (CONTEXT.md ``Cascade``).

The Cascade turns one :class:`Acquired` bundle into
:class:`EnrichedSections` by running tier-gated enrichment:

- **Tier 1** (``score >= thresholds.relevance``) — ``models.enrich``
  summary via :func:`influx.enrich.tier1_enrich` (FR-ENR-4).
- **Tier 2** (``score >= thresholds.full_text``) — full-text extraction
  via the injected ``tier2_extractor``.  Source-agnostic: arXiv injects
  :func:`influx.extraction.pipeline.extract_arxiv_text`; RSS pre-populates
  ``Acquired.extracted_text`` and skips the extractor.
- **Tier 3** (``score >= thresholds.deep_extract`` and full text
  available) — ``models.extract`` via
  :func:`influx.enrich.tier3_extract` (FR-ENR-5).

The Cascade consults :class:`influx.repair_counters.RepairCounters`
before each tier and emits ``influx:tier{2,3}-terminal`` flags when the
cap has been reached, plus ``influx:repair-needed`` when a counted
failure needs a repair-sweep retry.

Tier-gating logic lives **only** here — Source builders that delegate
to the Cascade should not duplicate the threshold checks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, NamedTuple

from influx import metrics
from influx.config import AppConfig, ProfileThresholds
from influx.enrich import tier1_enrich, tier3_extract
from influx.errors import ExtractionError, LCMAError
from influx.repair_counters import (
    REPAIR_COUNTED_CAP,
    RepairCounters,
    classify_failure,
)
from influx.schemas import Tier1Enrichment, Tier3Extraction
from influx.telemetry import current_run_id, get_tracer, record_tier3_fallback

__all__ = [
    "ALL_TIERS",
    "Acquired",
    "Cascade",
    "EffectiveExtractionTier",
    "EnrichedSections",
    "TextFlavour",
    "Tier",
    "Tier2Extractor",
    "Tier2Result",
]

logger = logging.getLogger(__name__)


TextFlavour = Literal["html", "pdf", "summary-fallback"]


# The enrichment tiers a caller can ask ``Cascade.enrich`` to run.  The
# create path runs all of them (subject to the score gates); the repair
# sweep passes only the stage(s) it needs to re-run so a Tier-2-only
# repair doesn't regenerate the ``## Summary`` (see ``enrich``'s
# ``stages`` parameter).
Tier = Literal["tier1", "tier2", "tier3"]
ALL_TIERS: frozenset[Tier] = frozenset(("tier1", "tier2", "tier3"))


# ── Acquired bundle ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Acquired:
    """A Source's acquired bundle for one Candidate (CONTEXT.md).

    Carries everything the Cascade and Renderer need that does not
    depend on the per-tier enrichment outcome:

    - **Identity**: ``item_id``, ``source_url``, ``title``, ``abstract``
      (the pre-Tier-1 candidate text).
    - **Archive state**: ``archive_path`` (POSIX rel path or ``None``),
      plus the ``archive_missing`` / ``archive_terminal`` repair-flag
      booleans the source set during acquire.
    - **Extracted text**: ``extracted_text`` and ``text_flavour`` (when
      the source already has the body, e.g. RSS).  ``None`` when the
      Cascade's Tier 2 step needs to populate them (e.g. arXiv).
    - **Identity tags**: source-specific provenance tags (``arxiv-id:``,
      ``cat:``, ``feed-slug:``…) that the builder will combine with
      profile and schema tags before rendering.
    """

    item_id: str
    source_url: str
    title: str
    abstract: str
    identity_tags: tuple[str, ...] = ()
    archive_path: str | None = None
    archive_missing: bool = False
    archive_terminal: bool = False
    extracted_text: str | None = None
    text_flavour: TextFlavour | None = None


# ── Tier 2 extractor seam ──────────────────────────────────────────


class Tier2Result(NamedTuple):
    """The text + canonical text-tag a Tier-2 extractor returns."""

    text: str
    flavour: TextFlavour
    text_tag: str  # ``text:html`` | ``text:pdf`` | ``text:abstract-only``


# A Tier2Extractor takes the Acquired bundle and returns a Tier2Result
# or raises :class:`ExtractionError`.  The Cascade calls it only when
# ``score >= thresholds.full_text`` AND ``acquired.extracted_text is
# None``.  Sources that pre-populate ``extracted_text`` (e.g. RSS) can
# skip injecting an extractor entirely.
Tier2Extractor = Callable[[Acquired], Tier2Result]


# ── EnrichedSections ──────────────────────────────────────────────


EffectiveExtractionTier = Literal[
    "abstract-only",
    "tier1",
    "tier2",
    "tier1+tier2",
    "tier3",
]


@dataclass(frozen=True, slots=True)
class EnrichedSections:
    """The Cascade's output for one Acquired (CONTEXT.md).

    All tier results are optional — absent when the gate did not fire,
    when the cascade short-circuited on a terminal flag, or when the
    underlying call raised a counted-class failure.

    Observability fields (#151):

    - ``effective_extraction_tier`` — the highest tier that produced
      content used by the renderer.  Drives log-level selection when a
      higher tier subsequently fails: if the effective tier already
      covers ``tier1+tier2`` (a summary plus full text), a Tier 3
      failure is harmless fallback noise rather than material
      degradation.
    - ``fallback_used`` — true when at least one tier was attempted and
      failed *and* a lower tier still produced acceptable content.  The
      renderer / metrics layer can use this to distinguish "we fell
      back gracefully" from "the note is genuinely degraded".

    Counter state (Fork 6):

    - ``counters`` — the post-enrich :class:`RepairCounters`.  When the
      caller passed a ``counters`` value (the repair sweep), a
      counted-class Tier 2 / Tier 3 failure advances it; the caller
      persists the result.  The create path always enriches with the
      zero default (no ``counters``); when it re-ingests an existing note
      its accumulated ``## Repair`` counters are preserved at the
      multi-profile merge (``lithos_client``, 3a.4) rather than fed back
      into ``enrich``.
      Note ``repair_flags`` (``influx:repair-needed``) is emitted for
      *any* tier failure — transient or counted — but only *counted*
      failures advance ``counters`` (see
      :func:`influx.repair_counters.classify_failure`).
    """

    tier1: Tier1Enrichment | None = None
    tier1_attempted: bool = False
    full_text: str | None = None
    full_text_flavour: TextFlavour | None = None
    text_tag: str = "text:abstract-only"
    tier3: Tier3Extraction | None = None
    repair_flags: tuple[str, ...] = field(default_factory=tuple)
    terminal_flags: tuple[str, ...] = field(default_factory=tuple)
    effective_extraction_tier: EffectiveExtractionTier = "abstract-only"
    fallback_used: bool = False
    counters: RepairCounters = field(default_factory=RepairCounters)


# ── Cascade ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Cascade:
    """Score-gated enrichment cascade for one profile run.

    Built once per profile run with the resolved config + thresholds +
    optional Tier-2 extractor; reused for every Acquired the run produces.
    """

    config: AppConfig
    profile_name: str
    profile_summary: str
    thresholds: ProfileThresholds
    tier2_extractor: Tier2Extractor | None = None

    def enrich(
        self,
        acquired: Acquired,
        score: int,
        *,
        stages: frozenset[Tier] = ALL_TIERS,
        counters: RepairCounters | None = None,
    ) -> EnrichedSections:
        """Run the score-gated cascade for one Acquired.

        Parameters
        ----------
        acquired:
            The Source's acquired bundle.
        score:
            The 1–10 LLM-filter score for this Candidate.
        stages:
            Which tiers may run (still subject to their score gates).
            The create path uses the default (all tiers); the repair
            sweep passes only the stage(s) it is re-running, so a
            Tier-2-only repair does not regenerate the ``## Summary``.
            The ``full_text`` gate still fires from
            ``acquired.extracted_text`` when ``"tier2"`` is excluded — a
            Tier-3-only repair runs against the note's existing full
            text without re-extracting.
        counters:
            Optional :class:`RepairCounters` to consult.  When provided
            (the repair sweep) the Cascade owns the full counter
            lifecycle: it skips a tier already at the cap (emitting
            ``influx:tier{2,3}-terminal``), and on a counted-class failure
            advances the counter and emits the terminal flag when the cap
            is reached.  When omitted (the create path, always) it defaults
            to a zero-counter value and no advance happens — counted
            failures still emit ``influx:repair-needed`` for the first
            sweep to pick up.  A re-ingested note's existing ``## Repair``
            counters are preserved at the multi-profile merge, not here.

        Returns
        -------
        EnrichedSections
            The cascade output.  ``repair_flags`` carries
            ``influx:repair-needed`` when any tier failed — *transient or
            counted* — signalling a repair-sweep retry; counter
            advancement is counted-only.  ``terminal_flags`` carries
            ``influx:tier{2,3}-terminal`` when a cap was reached (on
            entry or via an advance); ``counters`` carries the
            post-enrich counter state for the caller to persist.
        """
        count_failures = counters is not None
        counters = counters if counters is not None else RepairCounters()
        repair_flags: list[str] = []
        terminal_flags: list[str] = []
        tier2_failed = False
        tier3_failed = False

        # ── Tier 2 (full-text gate) ────────────────────────────────
        extracted_text = acquired.extracted_text
        text_flavour = acquired.text_flavour
        text_tag = _text_tag_for(text_flavour)

        if (
            "tier2" in stages
            and score >= self.thresholds.full_text
            and extracted_text is None
            and self.tier2_extractor is not None
        ):
            if counters.tier2_attempts >= REPAIR_COUNTED_CAP:
                terminal_flags.append("influx:tier2-terminal")
            else:
                tier2, tier2_failure = self._run_tier2(acquired)
                if tier2 is not None:
                    extracted_text = tier2.text
                    text_flavour = tier2.flavour
                    text_tag = tier2.text_tag
                else:
                    repair_flags.append("influx:repair-needed")
                    tier2_failed = True
                    if count_failures:
                        counters, capped = _advance_on_counted(
                            counters, "tier2", tier2_failure
                        )
                        if capped:
                            terminal_flags.append("influx:tier2-terminal")

        # ``full_text`` for the renderer only when extraction succeeded
        # AND the score crosses the gate (FR-ENR-6, US-011).  Uses
        # ``acquired.extracted_text`` untouched when ``"tier2"`` is not in
        # ``stages`` — a Tier-3-only repair runs against existing text.
        full_text: str | None = None
        full_text_flavour: TextFlavour | None = None
        if extracted_text is not None and score >= self.thresholds.full_text:
            full_text = extracted_text
            full_text_flavour = text_flavour

        # ── Tier 1 (relevance gate) ────────────────────────────────
        tier1: Tier1Enrichment | None = None
        tier1_attempted = "tier1" in stages and score >= self.thresholds.relevance
        if tier1_attempted:
            tier1 = self._run_tier1(acquired)
            if tier1 is None:
                repair_flags.append("influx:repair-needed")

        # ── Tier 3 (deep-extract gate, requires extracted text) ─────
        tier3: Tier3Extraction | None = None
        tier3_attempted = False
        if (
            "tier3" in stages
            and score >= self.thresholds.deep_extract
            and full_text is not None
        ):
            if counters.tier3_attempts >= REPAIR_COUNTED_CAP:
                terminal_flags.append("influx:tier3-terminal")
            else:
                tier3_attempted = True
                # Decide harmless-fallback vs. materially-degraded
                # log level *before* the call so the dispatcher can
                # emit the right classification at the failure site
                # (#151).  Lower-tier content is "acceptable" when
                # Tier 1 produced a summary AND Tier 2 produced full
                # text — the note still has every section except the
                # Tier 3 deep-extract ones.
                lower_tier_acceptable = tier1 is not None
                tier3, tier3_failure = self._run_tier3(
                    acquired.item_id,
                    acquired.title,
                    full_text,
                    lower_tier_acceptable=lower_tier_acceptable,
                )
                if tier3 is None:
                    repair_flags.append("influx:repair-needed")
                    tier3_failed = True
                    if count_failures:
                        counters, capped = _advance_on_counted(
                            counters, "tier3", tier3_failure
                        )
                        if capped:
                            terminal_flags.append("influx:tier3-terminal")

        effective_tier = _classify_effective_tier(
            tier1=tier1,
            full_text=full_text,
            tier3=tier3,
        )
        fallback_used = _classify_fallback_used(
            tier1_attempted=tier1_attempted,
            tier1=tier1,
            tier2_failed=tier2_failed,
            tier3_attempted=tier3_attempted,
            tier3_failed=tier3_failed,
            effective_tier=effective_tier,
        )

        return EnrichedSections(
            tier1=tier1,
            tier1_attempted=tier1_attempted,
            full_text=full_text,
            full_text_flavour=full_text_flavour,
            text_tag=text_tag,
            tier3=tier3,
            repair_flags=tuple(dict.fromkeys(repair_flags)),
            terminal_flags=tuple(dict.fromkeys(terminal_flags)),
            effective_extraction_tier=effective_tier,
            fallback_used=fallback_used,
            counters=counters,
        )

    # ── Tier dispatchers ──────────────────────────────────────────

    def _run_tier1(self, acquired: Acquired) -> Tier1Enrichment | None:
        """Dispatch Tier 1 with telemetry + degrade-on-failure semantics."""
        tracer = get_tracer()
        with tracer.span(
            "influx.enrich.tier1",
            attributes={
                "influx.profile": self.profile_name,
                "influx.run_id": current_run_id.get() or "",
                "influx.item_count": 1,
            },
        ):
            try:
                return tier1_enrich(
                    title=acquired.title,
                    abstract=acquired.abstract,
                    profile_summary=self.profile_summary,
                    config=self.config,
                )
            except LCMAError:
                logger.warning(
                    "Tier 1 enrichment failed for %s",
                    acquired.item_id,
                    extra={
                        "profile": self.profile_name,
                        "item_id": acquired.item_id,
                        "tier": "1",
                        "tier1_failure_kind": "lcma_error",
                    },
                )
                metrics.llm_validation_failures().add(
                    1, {"profile": self.profile_name, "tier": "1"}
                )
                return None
            except Exception:
                # Defensive: any unexpected failure during Tier 1 (e.g.
                # an LLM response shape that bypasses the schema's
                # validators with an AttributeError, per staging
                # incident 2026-05-01) must degrade to a per-paper
                # repair, not take the whole scheduler run down.
                logger.warning(
                    "Tier 1 enrichment crashed unexpectedly for %s",
                    acquired.item_id,
                    extra={
                        "profile": self.profile_name,
                        "item_id": acquired.item_id,
                        "tier": "1",
                        "tier1_failure_kind": "unexpected_exception",
                    },
                    exc_info=True,
                )
                metrics.llm_validation_failures().add(
                    1, {"profile": self.profile_name, "tier": "1"}
                )
                return None

    def _run_tier2(
        self, acquired: Acquired
    ) -> tuple[Tier2Result | None, BaseException | None]:
        """Dispatch Tier 2 with telemetry + degrade-on-failure semantics.

        Returns ``(result, None)`` on success and ``(None, exc)`` on a
        caught :class:`ExtractionError`, so ``enrich`` can classify the
        failure (transient vs counted) for the counter lifecycle.
        """
        assert self.tier2_extractor is not None  # gate-checked by caller
        tracer = get_tracer()
        with tracer.span(
            "influx.enrich.tier2",
            attributes={
                "influx.profile": self.profile_name,
                "influx.run_id": current_run_id.get() or "",
                "influx.item_count": 1,
            },
        ):
            try:
                return self.tier2_extractor(acquired), None
            except ExtractionError as exc:
                # Both HTML and PDF failed (or whatever stages the
                # source-specific extractor walks).  Fall through to
                # abstract-only + repair-needed.
                return None, exc

    def _run_tier3(
        self,
        item_id: str,
        title: str,
        full_text: str,
        *,
        lower_tier_acceptable: bool,
    ) -> tuple[Tier3Extraction | None, BaseException | None]:
        """Dispatch Tier 3 with telemetry + degrade-on-failure semantics.

        Returns ``(result, None)`` on success and ``(None, exc)`` on a
        caught failure, so ``enrich`` can classify it (transient vs
        counted) for the counter lifecycle.

        ``lower_tier_acceptable`` controls log-level classification of a
        Tier 3 failure (#151):

        - ``True`` (Tier 1 summary + full text already in place) — the
          failure is harmless fallback noise: log at ``INFO`` with
          ``fallback_used=True`` and ``effective_extraction_tier=
          "tier1+tier2"`` so dashboards can filter it out from
          materially-degraded extractions.
        - ``False`` (Tier 1 or Tier 2 also produced no content) — the
          note is materially degraded: keep the ``WARNING`` so
          operators still see the actionable signal.

        The ``llm_validation_failures`` metric continues to tick on
        every failure (no behaviour change for repair counters or the
        ``influx:repair-needed`` flag); the new
        ``tier3_fallbacks`` metric tags each failure with
        ``fallback_kind=harmless|degraded`` so run summaries can
        distinguish them.
        """
        tracer = get_tracer()
        with tracer.span(
            "influx.enrich.tier3",
            attributes={
                "influx.profile": self.profile_name,
                "influx.run_id": current_run_id.get() or "",
                "influx.item_count": 1,
            },
        ):
            try:
                return (
                    tier3_extract(
                        title=title,
                        full_text=full_text,
                        config=self.config,
                    ),
                    None,
                )
            except LCMAError as exc:
                self._log_tier3_failure(
                    item_id=item_id,
                    title=title,
                    lower_tier_acceptable=lower_tier_acceptable,
                    failure_kind="lcma_error",
                    exc_info=False,
                )
                return None, exc
            except Exception as exc:
                # Defensive: same rationale as the Tier 1 catch — a
                # validator bug or unforeseen response shape must not
                # turn a single bad paper into a run-level abort
                # (staging incident 2026-05-01).
                self._log_tier3_failure(
                    item_id=item_id,
                    title=title,
                    lower_tier_acceptable=lower_tier_acceptable,
                    failure_kind="unexpected_exception",
                    exc_info=True,
                )
                return None, exc

    def _log_tier3_failure(
        self,
        *,
        item_id: str,
        title: str,
        lower_tier_acceptable: bool,
        failure_kind: str,
        exc_info: bool,
    ) -> None:
        """Emit the Tier 3 failure log + metrics with #151 classification.

        Centralises log-level selection and the structured ``extra`` fields
        so the LCMA-error path and the unexpected-exception path stay in
        lock-step.

        Tier 3 only runs when Tier 2 produced full text (the deep-extract
        gate requires ``full_text is not None``).  So every Tier 3 failure
        is a fallback to lower-tier content — what differs is how much
        lower-tier content exists:

        - ``lower_tier_acceptable=True`` (Tier 1 summary + Tier 2 full
          text) — harmless fallback: the note has every section except
          the Tier 3 deep-extract ones.  Effective tier is
          ``tier1+tier2``; log at INFO.
        - ``lower_tier_acceptable=False`` (Tier 1 missing, Tier 2 full
          text present) — degraded fallback: the note is missing its
          summary section.  Effective tier is ``tier2``; log at WARNING.

        Both branches set ``fallback_used=True`` because the cascade did
        in fact fall back from Tier 3 to whatever lower-tier content
        existed; what changes is the *completeness* of that lower-tier
        content, not whether a fallback happened.
        """
        fallback_kind = "harmless" if lower_tier_acceptable else "degraded"
        effective_tier = "tier1+tier2" if lower_tier_acceptable else "tier2"
        extra = {
            "profile": self.profile_name,
            "item_id": item_id,
            "tier": "3",
            "fallback_used": True,
            "fallback_kind": fallback_kind,
            "effective_extraction_tier": effective_tier,
            "tier3_failure_kind": failure_kind,
        }
        if lower_tier_acceptable:
            # Harmless fallback noise — earlier tiers already produced
            # acceptable note content.  Downgrade to INFO so operators
            # can still see the event when debugging without it
            # polluting the staging warning stream (#151).
            logger.info(
                "Tier 3 extraction failed for %s; lower tiers produced "
                "acceptable content, falling back",
                title,
                extra=extra,
                exc_info=exc_info,
            )
        else:
            logger.warning(
                "Tier 3 extraction failed for %s; note content materially "
                "degraded (no Tier 1 summary available)",
                title,
                extra=extra,
                exc_info=exc_info,
            )
        metrics.llm_validation_failures().add(
            1, {"profile": self.profile_name, "tier": "3"}
        )
        metrics.tier3_fallbacks().add(
            1,
            {"profile": self.profile_name, "fallback_kind": fallback_kind},
        )
        # Propagate the classification to the current run's ledger entry
        # (#151 gap 2): increment a per-run counter so run summaries /
        # ``/runs/recent`` can distinguish harmless Tier 3 fallback from
        # genuinely degraded extractions without scraping logs.  Safe
        # outside a run context — the helper no-ops when the contextvar
        # bucket is unset.
        record_tier3_fallback(kind=fallback_kind)


def _advance_on_counted(
    counters: RepairCounters,
    stage: Literal["tier2", "tier3"],
    failure: BaseException | None,
) -> tuple[RepairCounters, bool]:
    """Advance the per-*stage* counter iff *failure* is counted-class.

    Returns the (possibly-unchanged) counters plus whether the cap was
    reached on this advance — the trigger for emitting
    ``influx:<stage>-terminal``.  Transient failures (and the degenerate
    ``failure is None``) leave the counter untouched, matching
    :func:`influx.repair_counters.classify_failure`.  The ``## Repair``
    bullet grammar stays in ``repair_counters`` (via ``bump_tier{2,3}``);
    this only decides *when* to advance.
    """
    if failure is None or classify_failure(failure) != "counted":
        return counters, False
    failure_stage = getattr(failure, "stage", "") or ""
    error = str(failure)
    if stage == "tier2":
        counters = counters.bump_tier2(stage=failure_stage, error=error)
        return counters, counters.tier2_attempts >= REPAIR_COUNTED_CAP
    counters = counters.bump_tier3(stage=failure_stage, error=error)
    return counters, counters.tier3_attempts >= REPAIR_COUNTED_CAP


def _text_tag_for(flavour: TextFlavour | None) -> str:
    """Return the canonical ``text:*`` tag for *flavour*."""
    if flavour == "html":
        return "text:html"
    if flavour == "pdf":
        return "text:pdf"
    return "text:abstract-only"


def _classify_effective_tier(
    *,
    tier1: Tier1Enrichment | None,
    full_text: str | None,
    tier3: Tier3Extraction | None,
) -> EffectiveExtractionTier:
    """Pick the highest tier label that reflects what made it into the note (#151).

    Used by :class:`EnrichedSections` consumers (renderer, run
    summaries, metrics) to tell at a glance whether a tier failure
    materially degraded the output or was a harmless fallback.
    """
    if tier3 is not None:
        return "tier3"
    if tier1 is not None and full_text is not None:
        return "tier1+tier2"
    if tier1 is not None:
        return "tier1"
    if full_text is not None:
        return "tier2"
    return "abstract-only"


def _classify_fallback_used(
    *,
    tier1_attempted: bool,
    tier1: Tier1Enrichment | None,
    tier2_failed: bool,
    tier3_attempted: bool,
    tier3_failed: bool,
    effective_tier: EffectiveExtractionTier,
) -> bool:
    """Return whether any tier fell back to a lower one with usable output (#151).

    A run is "in fallback" when at least one attempted tier produced no
    output *and* the cascade still produced an effective tier above
    abstract-only.  Repair flags continue to be the source of truth for
    "this needs a repair sweep"; this flag is the operator-facing
    "harmless vs material" classifier for logs and metrics.
    """
    tier1_failed = tier1_attempted and tier1 is None
    any_tier_failed = tier1_failed or tier2_failed or (tier3_attempted and tier3_failed)
    return any_tier_failed and effective_tier != "abstract-only"
