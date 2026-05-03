"""Unit tests for the score-gated enrichment Cascade (issue #55).

Covers:

- All three tier gates (relevance / full_text / deep_extract).
- Tier-2 extractor injection seam.
- Counted-failure → ``influx:repair-needed`` repair_flags.
- :class:`RepairCounters` integration: ``influx:tier{2,3}-terminal``
  flags surface when the cap has been reached on entry.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from influx.cascade import (
    Acquired,
    Cascade,
    EnrichedSections,
    Tier2Result,
)
from influx.errors import ExtractionError, LCMAError
from influx.repair_counters import REPAIR_COUNTED_CAP, RepairCounters
from influx.schemas import Tier1Enrichment, Tier3Extraction


def _make_config() -> Any:
    """Minimal AppConfig sufficient for Cascade construction."""
    from influx.config import (
        AppConfig,
        ProfileConfig,
        PromptEntryConfig,
        PromptsConfig,
        ScheduleConfig,
    )

    return AppConfig(
        schedule=ScheduleConfig(cron="0 6 * * *", timezone="UTC"),
        profiles=[ProfileConfig(name="research")],
        prompts=PromptsConfig(
            filter=PromptEntryConfig(text="filter"),
            tier1_enrich=PromptEntryConfig(text="t1"),
            tier3_extract=PromptEntryConfig(text="t3"),
        ),
    )


def _make_acquired(**overrides: Any) -> Acquired:
    """Build an Acquired with sane test defaults."""
    base: dict[str, Any] = {
        "item_id": "2601.00001",
        "source_url": "https://arxiv.org/abs/2601.00001",
        "title": "A Paper",
        "abstract": "An abstract.",
        "identity_tags": ("cat:cs.AI",),
        "archive_path": "papers/arxiv/2026/01/2601.00001.pdf",
    }
    base.update(overrides)
    return Acquired(**base)


def _make_cascade(
    *,
    thresholds: Any = None,
    tier2_extractor: Any = None,
) -> Cascade:
    from influx.config import ProfileThresholds

    return Cascade(
        config=_make_config(),
        profile_name="research",
        profile_summary="A research profile.",
        thresholds=thresholds or ProfileThresholds(),
        tier2_extractor=tier2_extractor,
    )


# ── Tier gate: below all thresholds ────────────────────────────────


class TestBelowAllThresholds:
    """Score below relevance / full_text / deep_extract: no tier fires."""

    def test_no_tiers_run(self) -> None:
        with (
            patch("influx.cascade.tier1_enrich") as t1,
            patch("influx.cascade.tier3_extract") as t3,
        ):
            sections = _make_cascade().enrich(_make_acquired(), score=3)

        assert sections.tier1 is None
        assert sections.tier1_attempted is False
        assert sections.full_text is None
        assert sections.tier3 is None
        assert sections.text_tag == "text:abstract-only"
        assert sections.repair_flags == ()
        assert sections.terminal_flags == ()
        t1.assert_not_called()
        t3.assert_not_called()


# ── Tier 1 (relevance gate) ────────────────────────────────────────


class TestTier1RelevanceGate:
    """Tier 1 fires iff score >= relevance."""

    def test_below_relevance_skips_tier1(self) -> None:
        with patch("influx.cascade.tier1_enrich") as t1:
            sections = _make_cascade().enrich(_make_acquired(), score=6)
        t1.assert_not_called()
        assert sections.tier1 is None
        assert sections.tier1_attempted is False

    def test_above_relevance_runs_tier1(self) -> None:
        tier1 = Tier1Enrichment(
            contributions=["c"], method="m", result="r", relevance="rel"
        )
        with patch("influx.cascade.tier1_enrich", return_value=tier1) as t1:
            sections = _make_cascade().enrich(_make_acquired(), score=7)
        t1.assert_called_once()
        assert sections.tier1 == tier1
        assert sections.tier1_attempted is True
        assert sections.repair_flags == ()

    def test_tier1_lcma_failure_emits_repair_needed(self) -> None:
        with patch(
            "influx.cascade.tier1_enrich",
            side_effect=LCMAError("schema fail", model="enrich", stage="validate"),
        ):
            sections = _make_cascade().enrich(_make_acquired(), score=7)
        assert sections.tier1 is None
        assert sections.tier1_attempted is True
        assert sections.repair_flags == ("influx:repair-needed",)

    def test_tier1_unexpected_exception_degrades_to_repair(self) -> None:
        """Unexpected Tier-1 exceptions must not abort the run.

        Mirrors the staging-2026-05-01 incident: a validator AttributeError
        bypassed the LCMAError envelope and took the whole run down.
        """
        with patch(
            "influx.cascade.tier1_enrich",
            side_effect=AttributeError("validator bug"),
        ):
            sections = _make_cascade().enrich(_make_acquired(), score=7)
        assert sections.tier1 is None
        assert sections.repair_flags == ("influx:repair-needed",)


# ── Tier 2 (full_text gate) ────────────────────────────────────────


class TestTier2FullTextGate:
    """Tier 2 fires iff score >= full_text AND no extracted text yet."""

    def test_below_full_text_skips_tier2(self) -> None:
        called: list[Acquired] = []

        def extractor(acq: Acquired) -> Tier2Result:
            called.append(acq)
            raise AssertionError("must not be called")

        cascade = _make_cascade(tier2_extractor=extractor)
        sections = cascade.enrich(_make_acquired(), score=6)
        assert called == []
        assert sections.full_text is None
        assert sections.text_tag == "text:abstract-only"

    def test_above_full_text_runs_tier2_html(self) -> None:
        def extractor(_acq: Acquired) -> Tier2Result:
            return Tier2Result(text="full body", flavour="html", text_tag="text:html")

        cascade = _make_cascade(tier2_extractor=extractor)
        sections = cascade.enrich(_make_acquired(), score=8)
        assert sections.full_text == "full body"
        assert sections.full_text_flavour == "html"
        assert sections.text_tag == "text:html"

    def test_tier2_extraction_failure_emits_repair_needed(self) -> None:
        def extractor(_acq: Acquired) -> Tier2Result:
            raise ExtractionError("both html and pdf failed", url="x", stage="parse")

        cascade = _make_cascade(tier2_extractor=extractor)
        sections = cascade.enrich(_make_acquired(), score=8)
        assert sections.full_text is None
        assert sections.text_tag == "text:abstract-only"
        assert "influx:repair-needed" in sections.repair_flags

    def test_no_extractor_skips_tier2_silently(self) -> None:
        """If no Tier-2 extractor is injected, the gate is a no-op."""
        tier1 = Tier1Enrichment(
            contributions=["c"], method="m", result="r", relevance="rel"
        )
        with patch("influx.cascade.tier1_enrich", return_value=tier1):
            sections = _make_cascade(tier2_extractor=None).enrich(
                _make_acquired(), score=8
            )
        assert sections.full_text is None
        assert sections.text_tag == "text:abstract-only"
        assert sections.repair_flags == ()

    def test_pre_extracted_text_skips_tier2_call(self) -> None:
        """When Acquired already has extracted_text (e.g. RSS), Tier 2 is a no-op."""
        called: list[Acquired] = []

        def extractor(acq: Acquired) -> Tier2Result:
            called.append(acq)
            raise AssertionError("must not be called")

        cascade = _make_cascade(tier2_extractor=extractor)
        sections = cascade.enrich(
            _make_acquired(extracted_text="prepopulated", text_flavour="html"),
            score=8,
        )
        assert called == []
        assert sections.full_text == "prepopulated"
        assert sections.full_text_flavour == "html"
        assert sections.text_tag == "text:html"


# ── Tier 3 (deep_extract gate) ────────────────────────────────────


class TestTier3DeepExtractGate:
    """Tier 3 fires iff score >= deep_extract AND full text is available."""

    def _tier1(self) -> Tier1Enrichment:
        return Tier1Enrichment(
            contributions=["c"], method="m", result="r", relevance="rel"
        )

    def _tier3(self) -> Tier3Extraction:
        return Tier3Extraction(claims=["claim"], builds_on=["b"])

    def test_below_deep_extract_skips_tier3(self) -> None:
        def extractor(_acq: Acquired) -> Tier2Result:
            return Tier2Result(text="body", flavour="html", text_tag="text:html")

        with (
            patch("influx.cascade.tier1_enrich", return_value=self._tier1()),
            patch("influx.cascade.tier3_extract") as t3,
        ):
            sections = _make_cascade(tier2_extractor=extractor).enrich(
                _make_acquired(), score=8
            )
        t3.assert_not_called()
        assert sections.tier3 is None

    def test_above_deep_extract_runs_tier3_when_text_available(self) -> None:
        def extractor(_acq: Acquired) -> Tier2Result:
            return Tier2Result(text="body", flavour="html", text_tag="text:html")

        tier3 = self._tier3()
        with (
            patch("influx.cascade.tier1_enrich", return_value=self._tier1()),
            patch("influx.cascade.tier3_extract", return_value=tier3) as t3,
        ):
            sections = _make_cascade(tier2_extractor=extractor).enrich(
                _make_acquired(), score=10
            )
        t3.assert_called_once()
        assert sections.tier3 == tier3

    def test_tier3_skipped_without_full_text_even_above_threshold(self) -> None:
        """If Tier 2 produced no text, Tier 3 must skip even at deep_extract."""
        with patch("influx.cascade.tier3_extract") as t3:
            sections = _make_cascade(tier2_extractor=None).enrich(
                _make_acquired(), score=10
            )
        t3.assert_not_called()
        assert sections.tier3 is None

    def test_tier3_lcma_failure_emits_repair_needed(self) -> None:
        def extractor(_acq: Acquired) -> Tier2Result:
            return Tier2Result(text="body", flavour="html", text_tag="text:html")

        with (
            patch("influx.cascade.tier1_enrich", return_value=self._tier1()),
            patch(
                "influx.cascade.tier3_extract",
                side_effect=LCMAError("validate fail", stage="validate"),
            ),
        ):
            sections = _make_cascade(tier2_extractor=extractor).enrich(
                _make_acquired(), score=10
            )
        assert sections.tier3 is None
        assert "influx:repair-needed" in sections.repair_flags


# ── RepairCounters integration ────────────────────────────────────


class TestRepairCountersIntegration:
    """Cascade consults RepairCounters before each tier."""

    def test_tier2_terminal_when_counter_at_cap(self) -> None:
        called: list[Acquired] = []

        def extractor(acq: Acquired) -> Tier2Result:
            called.append(acq)
            raise AssertionError("must not run when counter at cap")

        counters = RepairCounters(tier2_attempts=REPAIR_COUNTED_CAP)
        sections = _make_cascade(tier2_extractor=extractor).enrich(
            _make_acquired(), score=8, counters=counters
        )
        assert called == []
        assert "influx:tier2-terminal" in sections.terminal_flags
        assert sections.full_text is None

    def test_tier3_terminal_when_counter_at_cap(self) -> None:
        def extractor(_acq: Acquired) -> Tier2Result:
            return Tier2Result(text="body", flavour="html", text_tag="text:html")

        counters = RepairCounters(tier3_attempts=REPAIR_COUNTED_CAP)
        with (
            patch("influx.cascade.tier1_enrich", return_value=None),
            patch("influx.cascade.tier3_extract") as t3,
        ):
            sections = _make_cascade(tier2_extractor=extractor).enrich(
                _make_acquired(), score=10, counters=counters
            )
        t3.assert_not_called()
        assert "influx:tier3-terminal" in sections.terminal_flags
        assert sections.tier3 is None

    def test_below_cap_runs_tier2_normally(self) -> None:
        def extractor(_acq: Acquired) -> Tier2Result:
            return Tier2Result(text="body", flavour="html", text_tag="text:html")

        counters = RepairCounters(tier2_attempts=REPAIR_COUNTED_CAP - 1)
        sections = _make_cascade(tier2_extractor=extractor).enrich(
            _make_acquired(), score=8, counters=counters
        )
        assert "influx:tier2-terminal" not in sections.terminal_flags
        assert sections.full_text == "body"

    def test_default_counters_means_no_terminal_flags(self) -> None:
        """Initial-write path: no counters → no skips."""

        def extractor(_acq: Acquired) -> Tier2Result:
            return Tier2Result(text="body", flavour="html", text_tag="text:html")

        sections = _make_cascade(tier2_extractor=extractor).enrich(
            _make_acquired(), score=8
        )
        assert sections.terminal_flags == ()
        assert sections.full_text == "body"


# ── Result dataclass shape ─────────────────────────────────────────


class TestEnrichedSectionsShape:
    """``EnrichedSections`` exposes the fields the renderer consumes."""

    def test_default_values(self) -> None:
        sections = EnrichedSections()
        assert sections.tier1 is None
        assert sections.tier1_attempted is False
        assert sections.full_text is None
        assert sections.full_text_flavour is None
        assert sections.text_tag == "text:abstract-only"
        assert sections.tier3 is None
        assert sections.repair_flags == ()
        assert sections.terminal_flags == ()


class TestAcquiredShape:
    """``Acquired`` exposes the fields the cascade consumes."""

    def test_minimal_construction(self) -> None:
        acq = Acquired(
            item_id="2601.00001",
            source_url="https://arxiv.org/abs/2601.00001",
            title="t",
            abstract="a",
        )
        assert acq.item_id == "2601.00001"
        assert acq.archive_path is None
        assert acq.archive_missing is False
        assert acq.archive_terminal is False
        assert acq.extracted_text is None
        assert acq.text_flavour is None
        assert acq.identity_tags == ()
