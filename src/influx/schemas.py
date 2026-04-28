"""Pydantic models for the LLM filter pipeline (FR-FLT-3).

``FilterResult`` and ``FilterResponse`` validate JSON-mode LLM output
with bounded score and tag-list constraints.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

_TIER3_MAX_CHARS = 500
_TIER3_MAX_ITEMS = 10
_FILTER_MAX_TAGS = 5


def _trim_truncate_and_cap(values: list[str]) -> list[str]:
    """Keep Tier-3 LLM list output within the documented contract."""
    return [v.strip()[:_TIER3_MAX_CHARS] for v in values[:_TIER3_MAX_ITEMS]]


def _check_non_empty(values: list[str]) -> list[str]:
    """Reject empty/whitespace-only elements after trim (FR-ENR-5)."""
    for v in values:
        if not v:
            msg = "List elements must be non-empty after trimming"
            raise ValueError(msg)
    return values


class Tier3Extraction(BaseModel):
    """Tier-3 deep extraction output validated against FR-ENR-5 (PRD 07 §5.3).

    Constraints:
    - ``claims`` length must be in ``[1, 10]`` inclusive.
    - ``datasets``, ``builds_on``, ``open_questions``, ``potential_connections``
      lengths must be in ``[0, 10]`` inclusive.
    - All string elements are trimmed and truncated to 500 characters on ingest.
    - Empty/whitespace-only elements fail validation.
    """

    claims: list[str] = Field(min_length=1, max_length=10)
    datasets: list[str] = Field(default_factory=list, max_length=10)
    builds_on: list[str] = Field(default_factory=list, max_length=10)
    open_questions: list[str] = Field(default_factory=list, max_length=10)
    potential_connections: list[str] = Field(default_factory=list, max_length=10)

    @field_validator(
        "claims",
        "datasets",
        "builds_on",
        "open_questions",
        "potential_connections",
        mode="before",
    )
    @classmethod
    def trim_and_truncate(cls, v: list[str]) -> list[str]:
        """Trim strings and cap list length before field validation."""
        return _trim_truncate_and_cap(v)

    @field_validator(
        "claims",
        "datasets",
        "builds_on",
        "open_questions",
        "potential_connections",
        mode="after",
    )
    @classmethod
    def check_non_empty(cls, v: list[str]) -> list[str]:
        """Reject empty/whitespace-only elements."""
        return _check_non_empty(v)


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
    tags: list[str] = Field(default_factory=list, max_length=_FILTER_MAX_TAGS)
    reason: str

    @field_validator("tags", mode="before")
    @classmethod
    def cap_tags(cls, v: object) -> object:
        """Keep LLM tag output within the documented filter contract."""
        if isinstance(v, list):
            return v[:_FILTER_MAX_TAGS]
        return v


class FilterResponse(BaseModel):
    """Top-level wrapper for a batch of filter results (FR-FLT-3)."""

    results: list[FilterResult]
