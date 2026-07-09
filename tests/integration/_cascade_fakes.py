"""Fake enrichment Cascades for repair-sweep integration tests (3a.2 / 3a.3).

The sweep runs Tier 2 and Tier 3 through a :class:`~influx.cascade.Cascade`
passed via ``sweep(cascade=...)``.  These fakes let the zero-monkeypatch
integration suite drive the recovery outcome — success or not-attempted
spy — without an LLM or archive I/O, exactly the way ``SweepHooks`` fakes
drive the other stages.  Inject with ``# type: ignore[arg-type]`` (as the
hook fakes do), since a fake is structurally-but-not-nominally a
``Cascade``.
"""

from __future__ import annotations

from influx.cascade import Acquired, EnrichedSections
from influx.repair_counters import RepairCounters
from influx.schemas import Tier3Extraction


class Tier2Tier3SuccessCascade:
    """``enrich`` succeeds for whichever stage the sweep requests.

    Dispatches on ``stages`` so one injected fake drives both the sweep's
    Tier-2 (full text) and Tier-3 (claims) recovery in a single end-to-end
    pass, without archive I/O or an LLM.  The sweep applies ``full_text``
    via ``insert_full_text_section`` (adding the ``full-text`` tag) and
    ``tier3`` via ``insert_tier3_sections`` (adding ``influx:deep-extracted``).
    """

    def __init__(
        self,
        *,
        full_text: str = "Recovered full text body.",
        tier3: Tier3Extraction | None = None,
    ) -> None:
        self.full_text = full_text
        self.tier3 = tier3 or Tier3Extraction(claims=["A recovered claim."])
        self.calls = 0

    def enrich(
        self,
        acquired: Acquired,
        score: int,
        *,
        stages: frozenset[str] | None = None,
        counters: RepairCounters | None = None,
    ) -> EnrichedSections:
        del acquired, score
        self.calls += 1
        stage_set = stages or frozenset()
        result_counters = counters or RepairCounters()
        if "tier2" in stage_set:
            return EnrichedSections(full_text=self.full_text, counters=result_counters)
        if "tier3" in stage_set:
            return EnrichedSections(tier3=self.tier3, counters=result_counters)
        return EnrichedSections(counters=result_counters)


class SpyCascade:
    """``enrich`` records that it was called and returns an empty result.

    Used by tests that assert Tier 2 / Tier 3 are *not* attempted (e.g. a
    terminal note): a non-zero ``calls`` count means the sweep reached the
    Cascade when it should have skipped both stages.
    """

    def __init__(self) -> None:
        self.calls = 0

    def enrich(
        self,
        acquired: Acquired,
        score: int,
        *,
        stages: object = None,
        counters: RepairCounters | None = None,
    ) -> EnrichedSections:
        del acquired, score, stages
        self.calls += 1
        return EnrichedSections(counters=counters or RepairCounters())
