"""Pydantic models for the LLM filter pipeline (FR-FLT-3).

``FilterResult`` and ``FilterResponse`` validate JSON-mode LLM output
with bounded score and tag-list constraints.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Tier1Enrichment(BaseModel):
    """Tier-1 enrichment output validated against FR-ENR-4 (PRD 07 §5.2).

    Constraints:
    - ``contributions`` length must be in ``[1, 6]`` inclusive.
    """

    contributions: list[str] = Field(min_length=1, max_length=6)
    method: str
    result: str
    relevance: str


class FilterResult(BaseModel):
    """One scored item from the LLM filter response (FR-FLT-3).

    Constraints:
    - ``score`` must be in ``[1, 10]`` inclusive.
    - ``tags`` list length must be in ``[0, 5]`` inclusive.
    """

    id: str
    score: int = Field(ge=1, le=10)
    tags: list[str] = Field(default_factory=list, max_length=5)
    reason: str


class FilterResponse(BaseModel):
    """Top-level wrapper for a batch of filter results (FR-FLT-3)."""

    results: list[FilterResult]
