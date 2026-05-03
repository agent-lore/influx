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


@dataclass(frozen=True, slots=True)
class EnrichedSections:
    """The Cascade's output for one Acquired (CONTEXT.md).

    All tier results are optional — absent when the gate did not fire,
    when the cascade short-circuited on a terminal flag, or when the
    underlying call raised a counted-class failure.
    """

    tier1: Tier1Enrichment | None = None
    tier1_attempted: bool = False
    full_text: str | None = None
    full_text_flavour: TextFlavour | None = None
    text_tag: str = "text:abstract-only"
    tier3: Tier3Extraction | None = None
    repair_flags: tuple[str, ...] = field(default_factory=tuple)
    terminal_flags: tuple[str, ...] = field(default_factory=tuple)


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
        if score >= self.thresholds.deep_extract and full_text is not None:
            if counters.tier3_attempts >= REPAIR_COUNTED_CAP:
                terminal_flags.append("influx:tier3-terminal")
            else:
                tier3 = self._run_tier3(acquired.title, full_text)
                if tier3 is None:
                    repair_flags.append("influx:repair-needed")

        return EnrichedSections(
            tier1=tier1,
            tier1_attempted=tier1_attempted,
            full_text=full_text,
            full_text_flavour=full_text_flavour,
            text_tag=text_tag,
            tier3=tier3,
            repair_flags=tuple(dict.fromkeys(repair_flags)),
            terminal_flags=tuple(dict.fromkeys(terminal_flags)),
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
                logger.warning("Tier 1 enrichment failed for %s", acquired.item_id)
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

    def _run_tier3(self, title: str, full_text: str) -> Tier3Extraction | None:
        """Dispatch Tier 3 with telemetry + degrade-on-failure semantics."""
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
                logger.warning("Tier 3 extraction failed for %s", title)
                metrics.llm_validation_failures().add(
                    1, {"profile": self.profile_name, "tier": "3"}
                )
                return None
            except Exception:
                # Defensive: same rationale as the Tier 1 catch — a
                # validator bug or unforeseen response shape must not
                # turn a single bad paper into a run-level abort
                # (staging incident 2026-05-01).
                logger.warning(
                    "Tier 3 extraction crashed unexpectedly for %s",
                    title,
                    exc_info=True,
                )
                metrics.llm_validation_failures().add(
                    1, {"profile": self.profile_name, "tier": "3"}
                )
                return None


def _text_tag_for(flavour: TextFlavour | None) -> str:
    """Return the canonical ``text:*`` tag for *flavour*."""
    if flavour == "html":
        return "text:html"
    if flavour == "pdf":
        return "text:pdf"
    return "text:abstract-only"
