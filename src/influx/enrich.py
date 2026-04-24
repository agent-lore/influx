"""Stub Tier-1 enrichment — replaced by PRD 07.

This module provides a canned ``tier1_enrich`` implementation that
derives a minimal Tier-1 summary shape from the provided abstract.
PRD 07 replaces this with the real LLM-backed enrichment pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier1Result:
    """Tier-1 enrichment output used by the note renderer's ``## Summary`` section."""

    summary: str
    keywords: list[str]


def tier1_enrich(*, abstract: str) -> Tier1Result:
    """Return a canned Tier-1 shape derived from *abstract* (stub — replaced by PRD 07).

    The summary is the first 500 characters of the abstract (or the
    full abstract if shorter).  Keywords are an empty list until the
    real enrichment pipeline is wired.
    """
    return Tier1Result(
        summary=abstract[:500] if abstract else "",
        keywords=[],
    )
