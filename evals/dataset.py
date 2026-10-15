"""Build eval datasets from the places production gets its inputs.

- **Filter and Tier-1 cases** come from a fresh arXiv / RSS candidate snapshot
  fetched through the production source parsers for each profile's configured
  categories and feeds, so the batches look exactly like a scheduled Run's.
  A snapshot naturally contains the sub-threshold majority a scorer must
  reject, which production notes (positives only) cannot supply.
- **Tier-3 cases** come from production notes that already carry a
  ``## Full Text`` section: that text *is* the Tier-2 output the extractor
  received, and the note's existing Tier-3 sections ride along as a
  reference.

Everything is deterministic for a given ``seed`` so a dataset can be
rebuilt from the same inputs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from evals.cases import (
    CandidateRecord,
    FilterCase,
    Tier1Case,
    Tier3Case,
    write_jsonl,
)
from influx.canonical_note import extract_section_body
from influx.config import AppConfig, ProfileConfig
from influx.sources.arxiv import fetch_arxiv
from influx.sources.rss import parse_feed

_log = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)
_TIER3_SECTIONS = {
    "claims": "Claims",
    "datasets": "Datasets & Benchmarks",
    "builds_on": "Builds On",
    "open_questions": "Open Questions",
}


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """Knobs for :func:`build_dataset`."""

    name: str
    lookback_days: int = 3
    batches_per_profile: int = 2
    tier1_per_profile: int = 4
    tier3_limit: int = 40
    tier3_min_chars: int = 4_000
    tier3_max_chars: int = 120_000
    seed: int = 1
    include_rss: bool = True
    profiles: tuple[str, ...] = ()


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _enabled_profiles(config: AppConfig, names: Sequence[str]) -> list[ProfileConfig]:
    wanted = set(names)
    out = []
    for p in config.profiles:
        if wanted and p.name not in wanted:
            continue
        if not getattr(p, "enabled", True):
            continue
        out.append(p)
    return out


# ── candidate snapshot ────────────────────────────────────────────────


def arxiv_candidates(
    config: AppConfig, profile: ProfileConfig, lookback_days: int
) -> list[CandidateRecord]:
    src = profile.sources.arxiv
    if not src.enabled:
        return []
    arxiv_cfg = src.model_copy(update={"lookback_days": lookback_days})
    items = fetch_arxiv(
        arxiv_config=arxiv_cfg,
        resilience=config.resilience,
        now=datetime.now(UTC),
        max_download_bytes=config.storage.max_download_bytes,
    )
    return [CandidateRecord(i.arxiv_id, i.title, i.abstract) for i in items]


def rss_candidates(config: AppConfig, profile: ProfileConfig) -> list[CandidateRecord]:
    out: list[CandidateRecord] = []
    for feed in profile.sources.rss:
        try:
            resp = httpx.get(feed.url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _log.warning("feed %s (%s) skipped: %s", feed.name, feed.url, exc)
            continue
        items = parse_feed(
            resp.content,
            feed,
            allow_private_ips=config.security.allow_private_ips,
            profile=profile.name,
        )
        out.extend(CandidateRecord(i.url, i.title, i.summary) for i in items)
    return out


def _case_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def build_filter_cases(
    config: AppConfig, spec: DatasetSpec, rng: random.Random
) -> list[FilterCase]:
    batch_size = config.filter.batch_size
    cases: list[FilterCase] = []
    for profile in _enabled_profiles(config, spec.profiles):
        pools: list[tuple[str, list[CandidateRecord]]] = [
            ("arxiv", arxiv_candidates(config, profile, spec.lookback_days))
        ]
        if spec.include_rss:
            pools.append(("rss", rss_candidates(config, profile)))
        for source, pool in pools:
            if not pool:
                continue
            shuffled = list(pool)
            rng.shuffle(shuffled)
            for n, batch in enumerate(_chunks(shuffled, batch_size)):
                if n >= spec.batches_per_profile:
                    break
                cases.append(
                    FilterCase(
                        case_id=_case_id(profile.name, source, str(n), batch[0].id),
                        profile=profile.name,
                        candidates=tuple(batch),
                        source=source,
                    )
                )
            _log.info(
                "profile=%s source=%s candidates=%d batches=%d",
                profile.name,
                source,
                len(pool),
                min(spec.batches_per_profile, -(-len(pool) // batch_size)),
            )
    return cases


def build_tier1_cases(
    filter_cases: Sequence[FilterCase], spec: DatasetSpec, rng: random.Random
) -> list[Tier1Case]:
    by_profile: dict[str, list[CandidateRecord]] = {}
    for case in filter_cases:
        by_profile.setdefault(case.profile, []).extend(case.candidates)
    out: list[Tier1Case] = []
    for profile, pool in sorted(by_profile.items()):
        for cand in rng.sample(pool, min(spec.tier1_per_profile, len(pool))):
            out.append(
                Tier1Case(
                    case_id=_case_id("tier1", profile, cand.id),
                    profile=profile,
                    title=cand.title,
                    abstract=cand.abstract,
                )
            )
    return out


# ── Tier 3 from production notes ──────────────────────────────────────


def _bullets(body: str) -> list[str]:
    return [line[2:].strip() for line in body.splitlines() if line.startswith("- ")]


def tier3_case_from_note(
    text: str, *, min_chars: int, max_chars: int
) -> Tier3Case | None:
    """Turn one canonical note into a Tier-3 case, or ``None`` if it has no
    usable full text."""
    full_text = extract_section_body(text, "Full Text")
    if len(full_text) < min_chars or len(full_text) > max_chars:
        return None
    match = _TITLE_RE.search(text)
    title = match.group(1).strip().strip("'\"") if match else ""
    if not title:
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    reference = {
        key: _bullets(extract_section_body(text, heading))
        for key, heading in _TIER3_SECTIONS.items()
    }
    has_reference = any(reference.values())
    return Tier3Case(
        case_id=_case_id("tier3", title, str(len(full_text))),
        title=title,
        full_text=full_text,
        reference=reference if has_reference else None,
    )


def build_tier3_cases(
    knowledge_dir: Path, spec: DatasetSpec, rng: random.Random
) -> list[Tier3Case]:
    paths = sorted(p for p in knowledge_dir.rglob("*.md"))
    rng.shuffle(paths)
    out: list[Tier3Case] = []
    for path in paths:
        if len(out) >= spec.tier3_limit:
            break
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "## Claims" not in text or "## Full Text" not in text:
            continue
        case = tier3_case_from_note(
            text, min_chars=spec.tier3_min_chars, max_chars=spec.tier3_max_chars
        )
        if case is not None:
            out.append(case)
    return out


# ── entry point ───────────────────────────────────────────────────────


def build_dataset(
    config: AppConfig,
    spec: DatasetSpec,
    *,
    out_dir: Path,
    knowledge_dir: Path | None,
    config_path: Path | None = None,
) -> dict[str, int]:
    rng = random.Random(spec.seed)
    filter_cases = build_filter_cases(config, spec, rng)
    tier1_cases = build_tier1_cases(filter_cases, spec, rng)
    tier3_cases = build_tier3_cases(knowledge_dir, spec, rng) if knowledge_dir else []
    out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(out_dir / "filter.jsonl", filter_cases)
    write_jsonl(out_dir / "tier1.jsonl", tier1_cases)
    write_jsonl(out_dir / "tier3.jsonl", tier3_cases)
    counts = {
        "filter": len(filter_cases),
        "tier1": len(tier1_cases),
        "tier3": len(tier3_cases),
        "filter_candidates": sum(len(c.candidates) for c in filter_cases),
    }
    manifest = {
        "name": spec.name,
        "built_at": datetime.now(UTC).isoformat(),
        "config_path": str(config_path) if config_path else None,
        "knowledge_dir": str(knowledge_dir) if knowledge_dir else None,
        "spec": asdict(spec),
        "counts": counts,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return counts
