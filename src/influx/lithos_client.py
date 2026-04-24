"""Stub Lithos API client — replaced by PRD 05.

This module provides in-process stub implementations of ``write`` and
``cache_lookup`` so that the arXiv flow (PRD 04) can call these entry
points while PRD 05 swaps in the real MCP-backed implementation.

Every ``write`` call is recorded in-process for test assertions.
``cache_lookup`` always returns a "not cached" result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CacheLookupResult:
    """Result of a cache lookup — ``cached`` indicates a hit."""

    cached: bool


@dataclass
class WriteRecord:
    """One recorded ``write`` invocation for test inspection."""

    source_url: str
    note_content: str
    metadata: dict[str, Any]


_write_log: list[WriteRecord] = []


def write(
    *,
    source_url: str,
    note_content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a note write (stub — replaced by PRD 05).

    All arguments are retained in-process so tests can inspect what the
    pipeline would have written.
    """
    _write_log.append(
        WriteRecord(
            source_url=source_url,
            note_content=note_content,
            metadata=metadata or {},
        )
    )


def cache_lookup(*, source_url: str) -> CacheLookupResult:
    """Check if a note already exists for *source_url* (stub — replaced by PRD 05).

    Always returns "not cached" so the pipeline proceeds.
    """
    return CacheLookupResult(cached=False)


def get_write_log() -> list[WriteRecord]:
    """Return all recorded ``write`` calls (test helper)."""
    return list(_write_log)


def clear_write_log() -> None:
    """Reset the recorded ``write`` calls (test helper)."""
    _write_log.clear()
