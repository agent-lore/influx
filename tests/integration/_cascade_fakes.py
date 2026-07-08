"""Fake enrichment Cascades for repair-sweep integration tests (3a.2).

The sweep runs Tier 3 through a :class:`~influx.cascade.Cascade` passed via
``sweep(cascade=...)``.  These fakes let the zero-monkeypatch integration
suite drive the Tier-3 recovery outcome — success or not-attempted spy —
without an LLM, exactly the way ``SweepHooks`` fakes drive the other
stages.  Inject with ``# type: ignore[arg-type]`` (as the hook fakes do),
since a fake is structurally-but-not-nominally a ``Cascade``.
"""

from __future__ import annotations

from influx.cascade import Acquired, EnrichedSections
from influx.repair_counters import RepairCounters
from influx.schemas import Tier3Extraction


class Tier3SuccessCascade:
    """``enrich`` returns a canned Tier-3 extraction (deep-extract succeeds)."""

    def __init__(self, tier3: Tier3Extraction | None = None) -> None:
        self.tier3 = tier3 or Tier3Extraction(claims=["A recovered claim."])
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
        return EnrichedSections(tier3=self.tier3, counters=counters or RepairCounters())


class SpyCascade:
    """``enrich`` records that it was called and returns an empty result.

    Used by tests that assert Tier 3 is *not* attempted (e.g. a terminal
    note): a non-zero ``calls`` count means the sweep reached the Cascade
    when it should have skipped the stage.
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
