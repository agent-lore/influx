"""Eval case and result records plus their JSONL persistence.

A *dataset* is a directory holding one JSONL file per slot
(``filter.jsonl``, ``tier1.jsonl``, ``tier3.jsonl``) and a ``manifest.json``
describing where the cases came from.  A *run* is a directory holding the
per-case ``results.jsonl`` produced by one model specification against one
dataset.  Every record is a frozen dataclass so results are never mutated
after they are written.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SLOTS: tuple[str, ...] = ("filter", "tier1", "tier3")


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    """One candidate inside a filter batch (what the scorer sees)."""

    id: str
    title: str
    abstract: str


@dataclass(frozen=True, slots=True)
class FilterCase:
    """One production-shaped filter request: a profile plus a candidate batch."""

    case_id: str
    profile: str
    candidates: tuple[CandidateRecord, ...]
    source: str = "arxiv"


@dataclass(frozen=True, slots=True)
class Tier1Case:
    """One Tier-1 enrichment request (title + abstract for a profile)."""

    case_id: str
    profile: str
    title: str
    abstract: str


@dataclass(frozen=True, slots=True)
class Tier3Case:
    """One Tier-3 deep-extraction request (title + full text).

    ``reference`` optionally carries the Tier-3 output already in the
    production note the case was mined from, for eyeballing only.
    """

    case_id: str
    title: str
    full_text: str
    reference: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Token and cost accounting for one request, as the provider reported it."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Outcome of sending one case to one model.

    ``output`` is the schema-validated response as a plain dict (``None``
    when the call failed or failed validation); ``error`` names the stage
    that failed (``http``, ``parse``, ``validate``, ``network``).
    """

    case_id: str
    slot: str
    model: str
    latency_seconds: float
    usage: Usage
    output: dict[str, Any] | None = None
    error: str | None = None
    error_detail: str = ""
    raw_content: str = ""
    attempts: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.output is not None and self.error is None


# ── JSONL IO ──────────────────────────────────────────────────────────


def _to_jsonable(record: Any) -> dict[str, Any]:
    return asdict(record)


def write_jsonl(path: Path, records: Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(_to_jsonable(record), ensure_ascii=False) + "\n")


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_filter_cases(path: Path) -> list[FilterCase]:
    return [
        FilterCase(
            case_id=row["case_id"],
            profile=row["profile"],
            candidates=tuple(CandidateRecord(**c) for c in row["candidates"]),
            source=row.get("source", "arxiv"),
        )
        for row in _iter_jsonl(path)
    ]


def read_tier1_cases(path: Path) -> list[Tier1Case]:
    return [Tier1Case(**row) for row in _iter_jsonl(path)]


def read_tier3_cases(path: Path) -> list[Tier3Case]:
    return [Tier3Case(**row) for row in _iter_jsonl(path)]


def read_results(path: Path) -> list[CaseResult]:
    out: list[CaseResult] = []
    for row in _iter_jsonl(path):
        usage = Usage(**row.pop("usage", {}))
        out.append(CaseResult(usage=usage, **row))
    return out


def load_dataset(
    dataset_dir: Path,
) -> tuple[list[FilterCase], list[Tier1Case], list[Tier3Case]]:
    return (
        read_filter_cases(dataset_dir / "filter.jsonl"),
        read_tier1_cases(dataset_dir / "tier1.jsonl"),
        read_tier3_cases(dataset_dir / "tier3.jsonl"),
    )
