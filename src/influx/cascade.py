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
from influx.repair_counters import REPAIR_COUNTED_CAP, RepairCounters
from influx.schemas import Tier1Enrichment, Tier3Extraction
from influx.telemetry import current_run_id, get_tracer

__all__ = [
    "Acquired",
    "Cascade",
    "EffectiveExtractionTier",
    "EnrichedSections",
    "TextFlavour",
    "Tier2Extractor",
    "Tier2Result",
]

logger = logging.getLogger(__name__)


TextFlavour = Literal["html", "pdf", "summary-fallback"]


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
        counters: RepairCounters | None = None,
    ) -> EnrichedSections:
        """Run the score-gated cascade for one Acquired.

        Parameters
        ----------
        acquired:
            The Source's acquired bundle.
        score:
            The 1–10 LLM-filter score for this Candidate.
        counters:
            Optional :class:`RepairCounters` to consult before each
            tier.  When omitted, defaults to a zero-counter value
            (initial-write path: counters live on existing notes only).

        Returns
        -------
        EnrichedSections
            The cascade output.  ``repair_flags`` carries
            ``influx:repair-needed`` when any tier emitted a counted
            failure; ``terminal_flags`` carries
            ``influx:tier{2,3}-terminal`` when the cap had already been
            reached on entry.
        """
        counters = counters or RepairCounters()
        repair_flags: list[str] = []
        terminal_flags: list[str] = []
        tier2_failed = False
        tier3_failed = False

        # ── Tier 2 (full-text gate) ────────────────────────────────
        extracted_text = acquired.extracted_text
        text_flavour = acquired.text_flavour
        text_tag = _text_tag_for(text_flavour)

        if (
            score >= self.thresholds.full_text
            and extracted_text is None
            and self.tier2_extractor is not None
        ):
            if counters.tier2_attempts >= REPAIR_COUNTED_CAP:
                terminal_flags.append("influx:tier2-terminal")
            else:
                tier2 = self._run_tier2(acquired)
                if tier2 is not None:
                    extracted_text = tier2.text
                    text_flavour = tier2.flavour
                    text_tag = tier2.text_tag
                else:
                    repair_flags.append("influx:repair-needed")
                    tier2_failed = True

        # ``full_text`` for the renderer only when extraction succeeded
        # AND the score crosses the gate (FR-ENR-6, US-011).
        full_text: str | None = None
        full_text_flavour: TextFlavour | None = None
        if extracted_text is not None and score >= self.thresholds.full_text:
            full_text = extracted_text
            full_text_flavour = text_flavour

        # ── Tier 1 (relevance gate) ────────────────────────────────
        tier1: Tier1Enrichment | None = None
        tier1_attempted = score >= self.thresholds.relevance
        if tier1_attempted:
            tier1 = self._run_tier1(acquired)
            if tier1 is None:
                repair_flags.append("influx:repair-needed")

        # ── Tier 3 (deep-extract gate, requires extracted text) ─────
        tier3: Tier3Extraction | None = None
        tier3_attempted = False
        if score >= self.thresholds.deep_extract and full_text is not None:
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
                tier3 = self._run_tier3(
                    acquired.item_id,
                    acquired.title,
                    full_text,
                    lower_tier_acceptable=lower_tier_acceptable,
                )
                if tier3 is None:
                    repair_flags.append("influx:repair-needed")
                    tier3_failed = True

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

    def _run_tier2(self, acquired: Acquired) -> Tier2Result | None:
        """Dispatch Tier 2 with telemetry + degrade-on-failure semantics."""
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
                return self.tier2_extractor(acquired)
            except ExtractionError:
                # Both HTML and PDF failed (or whatever stages the
                # source-specific extractor walks).  Fall through to
                # abstract-only + repair-needed.
                return None

    def _run_tier3(
        self,
        item_id: str,
        title: str,
        full_text: str,
        *,
        lower_tier_acceptable: bool,
    ) -> Tier3Extraction | None:
        """Dispatch Tier 3 with telemetry + degrade-on-failure semantics.

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
                return tier3_extract(
                    title=title,
                    full_text=full_text,
                    config=self.config,
                )
            except LCMAError:
                self._log_tier3_failure(
                    item_id=item_id,
                    title=title,
                    lower_tier_acceptable=lower_tier_acceptable,
                    failure_kind="lcma_error",
                    exc_info=False,
                )
                return None
            except Exception:
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
                return None

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
        """
        fallback_kind = "harmless" if lower_tier_acceptable else "degraded"
        effective_tier = "tier1+tier2" if lower_tier_acceptable else "tier1"
        extra = {
            "profile": self.profile_name,
            "item_id": item_id,
            "tier": "3",
            "fallback_used": lower_tier_acceptable,
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
