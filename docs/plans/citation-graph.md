# Citation Graph Extension Plan

Version: 1.1.0
Date: 2026-05-10
Status: Approved for Phase 1+2 — simplified design locked
Scope: Influx (Phase 1 + Phase 2). Phases 3–5 (Lithos LCMA scouts) are
deferred to follow-up epics and tracked as forward references only.

---

## 1. Motivation

Influx today writes per-paper notes and a small set of relationships
(`builds_on`, `related_to`) inferred from LLM-extracted prose. The
`builds_on` resolver in §10.5 of the Influx specification is the only
mechanism connecting notes via citation lineage, and it depends on
Tier 3 emitting `arXiv:<id>` strings — partial recall and silent on
anything outside arXiv. Live `edges.db` evidence confirms this is
currently zero-yield in production: in a 48 h staging window, every
persisted edge was written by Lithos's own `EnrichWorker`
consolidation path (`provenance_type='consolidation'`); no edge has
been authored by Influx's `lithos_edge_upsert` call site.

Semantic Scholar's Academic Graph API (S2AG) exposes ground-truth
references, citation contexts, paper identifiers, and broad
classification metadata across a much larger corpus than the current
`arXiv:<id>` heuristic can cover. Pulling a small, stable subset of
that data into Influx improves note-to-note linkage immediately without
turning Influx into a corpus-maintenance service.

The work still splits cleanly along the existing Influx-vs-Lithos
contract:

- **Influx ingests citation data alongside each paper.** Same shape as
  Tier 1 / Tier 2 / Tier 3 enrichment — paper-in-hand, scoped to one
  tick, no corpus-wide reasoning.
- **Lithos LCMA maintains the citation graph over time.** Backfills
  edges as the corpus fills in, runs forward-citation sweeps, governs
  concept-tag vocabulary, refreshes metadata when preprints get
  published.

This plan is written so the two halves can be split into independent
project epics. v1 is **Phase 1 + Phase 2 (Influx-side only)** — the
Lithos scout work in §6 is deferred and lives here only as a forward
reference for the consumers of the frontmatter Phase 1+2 produces.

## 2. Goals

1. Replace the LLM-extracted `arXiv:<id>` resolver with ground-truth
   S2AG references. Higher precision, higher recall.
2. Emit simple citation edges (`cites`) into Lithos at note-write time
   for arXiv-source notes.
3. Land lightweight S2AG-derived enrichment
   (`s2_paper_id`, `s2FieldsOfStudy` tags, in-memory references) so
   Lithos has enough state on each note to drive its own scouts later.
4. Treat S2AG call failures as best-effort at item scope, never fatal.
   Influx without S2AG data must still ingest cleanly.

## 3. Non-Goals

1. **Influx does not maintain the citation graph.** Once Influx writes
   its seed edges + frontmatter, Lithos owns reconciliation.
2. **No new note types.** Citation references for papers Lithos does
   not yet know are dropped at Influx-write time, not stored as stub
   notes. The (deferred) Lithos backfill scout materialises edges later
   when the target appears.
3. **RSS-source candidates are not S2AG-enriched in v1.** The s2ag
   stage is gated to arXiv-source candidates only. RSS items
   (regardless of whether their URL happens to be an arXiv abs URL)
   skip the stage cleanly. Future RSS coverage is a follow-up.
4. **No TLDR fallback in v1.** Tier 1 remains the only summary source.
   If Tier 1 fails, Influx keeps its current degraded behaviour.
5. **No typed citation taxonomy in v1.** Edge semantics stay at
   `cites`; raw S2AG `intents` remain in `evidence` only.
6. **No OpenAlex integration in v1.** `s2FieldsOfStudy` is sufficient
   for an initial concept-tag pass.
7. **No SPECTER2 vector storage.** Out of scope.
8. **Influx remains a single-process service.** Scouts live in Lithos.
9. **No active S2AG probe or special outage latch.** Manual kill switch
   plus per-item best-effort handling is sufficient for v1.
10. **Phases 3–5 are out of v1 scope.** Citation backfill, forward-
    citation tracking, metadata refresh, concept-tag governance,
    influence reranking, and slug-collision adjudication are tracked as
    separate epics.

## 4. The Split

### 4.1 Belongs to Influx (v1)

| Activity | Reason |
|---|---|
| Fetching S2AG records for an arXiv paper being ingested right now | Same shape as Tier 1 / 2 / 3 — paper-in-hand enrichment. |
| Writing `cites` edges between arXiv-source notes Influx is creating | Influx already calls `lithos_edge_upsert` for `builds_on` (§10.5). |
| `s2FieldsOfStudy` tag emission at write time | Tag application is part of the canonical note write. |
| Recording `s2_paper_id` and `s2_fields_of_study` in note frontmatter | Influx is the single writer of Influx-authored notes. |

### 4.2 Belongs to Lithos LCMA (Phase 3+, deferred)

See §6 for forward references only.

### 4.3 Boundary handoff

Influx writes the following on every arXiv-source note it authors
after S2AG enrichment lands (RSS-source notes are unchanged from
today's behaviour):

```yaml
---
note_type: summary
namespace: influx
source_url: https://arxiv.org/abs/2402.12345
s2_paper_id: "abc123def456abcdef0123456789abcdef012345"
s2_fields_of_study:
  - "Computer Science"
  - "Mathematics"
tags:
  - profile:ai-agents
  - source:arxiv
  - arxiv-id:2402.12345
  - s2-paper-id:abc123def456abcdef0123456789abcdef012345
  - field:computer-science
  - field:mathematics
  - ingested-by:influx
  - schema:1
confidence: 0.78
---
```

Plus `cites` edges via `lithos_edge_upsert` for any reference whose
target is already a note in Lithos (resolved via the cache-lookup
chain in §5.3).

That is the entire Influx → Lithos interface for citation data. Every
(deferred) LCMA scout reads from this state.

## 5. Influx Work Units

File-level pointers are tentative; final placement will be decided in
the implementation PRs.

### 5.1 S2AG provider

**New module:** `src/influx/s2ag.py`

- `S2agClient` — async HTTP client wrapping the guarded HTTP path,
  honouring `[s2ag].request_timeout` and the resilience retry settings
  (`max_retries`, `s2ag_429_backoff_seconds`).
- Endpoints used:
  - `POST /graph/v1/paper/batch` — batched lookup
    (up to `[s2ag].batch_size` IDs/POST, default 500)
  - `GET /graph/v1/paper/{paperId}/references` — references with
    `intents` and `externalIds`
- Accepts `paperIdType=ARXIV` so we can pass the unversioned arXiv ID
  directly (Influx already strips version suffixes at parse time —
  `arxiv.py:266`).
- API key loaded from `s2ag.api_key_env` if set; without one, falls
  back to the unauthenticated shared bucket.
- Per-tick `FetchCache` integration — re-uses the existing
  `FetchCache.get_or_fetch` semantics
  (`src/influx/sources/__init__.py:84`); concurrent calls for the same
  `paperId` collapse onto a shared `asyncio.Future`.
- No dedicated `ProbeLoop` integration in v1. S2AG is best-effort at
  item scope and can be disabled manually via config.

### 5.2 `s2ag_enrich` cascade stage

**Edited:** `src/influx/cascade.py`

- New stage **between Tier 2 (full-text) and Tier 1 (relevance
  enrichment)** in the cascade execution order. Mirrors the existing
  `tier2_extractor` injection pattern:
  `s2ag_enricher: S2agEnricher | None`.
- Gate: `score >= thresholds.relevance` (same gate as Tier 1).
- **Applicability**: arXiv-source candidates only. RSS-source
  candidates always pass `arxiv_id=None`; the cascade short-circuits
  the s2ag stage cleanly.
- Inputs: `arxiv_id` from the candidate dict.
- Outputs added to the candidate's enrichment payload (in-memory only —
  see §5.5 for the subset that is persisted to frontmatter):
  - `s2_paper_id`
  - `s2_fields_of_study`
  - `s2_reference_count`, `s2_citation_count`
  - `s2_references` (list of `{paperId, externalIds, intents,
    contexts, year, influentialCitationCount}`) — **not persisted**
    to frontmatter; lives on the cascade output for the post-write edge
    fan-out and is re-fetched from S2AG on repair.
- `EnrichedSections` grows two fields: `s2ag: S2agEnrichment | None`
  and `s2ag_attempted: bool` (mirrors `tier1_attempted`).
- Failure classification:
  - **Transient** (HTTP 5xx, transport, timeout, 429-respected) —
    does not fail the note write; leaves the note repairable.
  - **Counted** (HTTP 4xx other than 429, parse, validation) —
    advances `s2ag_attempts`.
  - **Paper-not-in-S2-corpus** (404 or empty body for a known good
    `arxiv_id`) — counted-once, immediately latches
    `influx:s2ag-terminal`.
- For arXiv-source notes, frontmatter is canonical:
  `s2_paper_id` is always present; empty string means enrichment was
  skipped, failed, or produced no match. This gives repair a stable
  selection key.

### 5.3 Reference → citation edge emission

**Edited:** `src/influx/lcma_wiring.py`, `src/influx/lcma.py`

After `lithos_write` succeeds for an arXiv-source note that has a
non-empty `s2_references` payload:

1. **Cap and order**: take the top **50** references sorted by
   `(influentialCitationCount DESC, year DESC, paperId ASC)` *before*
   any cache lookup. Three-key tuple is deterministic even when the
   leading two keys tie.
2. For each reference, run the resolution chain:
   1. `lithos_cache_lookup(tags=["s2-paper-id:<target_hex>"])` — exact
      match on S2 canonical ID (works for S2AG-era notes).
   2. If miss and `externalIds.ArXiv` present:
      `lithos_cache_lookup(source_url="https://arxiv.org/abs/<arxiv_id>")` —
      fallback for legacy pre-Phase-1 notes.
   3. If still miss: silently drop. No counter, no warning, no tag.
      The (deferred) Phase 3 backfill scout materialises later.
3. On hit, call `lithos_edge_upsert` with:

| Field | Value |
|---|---|
| `from_id` | source note id |
| `to_id` | resolved target note id |
| `type` | `cites` |
| `weight` | `1.0` |
| `namespace` | `"influx"` |
| `provenance_actor` | `"influx-s2ag"` |
| `provenance_type` | `"s2ag_reference"` |
| `evidence` | `{"s2_paper_id": <target_hex>, "intents": [...], "contexts": [<≤1 short snippet>], "source_paper_id": <source_hex>}` |
| `conflict_state` | `None` |

**Best-effort fan-out**, never fails the parent write. Per-reference:
wrap each `cache_lookup` + `edge_upsert` pair in try/except; log a
warning, increment `influx_s2ag_edge_upsert_failed_total`, continue to
the next reference. When ≥1 reference fails: bump `s2ag_attempts` once
(not per-edge), set `s2ag_last_stage="edges"`, tag
`influx:repair-needed`. Repair sweep re-runs the whole fan-out;
idempotent edge upsert means already-emitted edges no-op.

This step **replaces** the existing `arXiv:<id>` resolver in §10.5.
Tier 3's `builds_on` field remains in the note for human readability
but is no longer load-bearing for edge creation.

> **Prerequisite:** `agent-lore/influx#99` must land first. The
> existing `LithosClient.edge_upsert` wrapper sends only
> `source_note_id` / `target_note_id` / `evidence`; the Lithos MCP
> tool requires `from_id` / `to_id` / `weight` / `namespace`. The
> citation-graph fan-out cannot function until #99 is merged. See §11.

### 5.4 Concept tagging from `s2FieldsOfStudy`

**Edited:** `src/influx/notes.py`

- Take the **first 3** entries of `s2_fields_of_study` as returned by
  S2AG (trust S2AG's internal ranking).
- Slug-normalise: `"Computer Science"` → `"computer-science"`.
- Emit as `field:<slug>` tags.
- **No top-level filtering.** Filtering broad fields like
  `Computer Science` globally is brittle. Cap-3 already gives two slots
  beyond a dominant top-level field.
- Concept-tag governance (deduplication, vocabulary control) is a
  Phase 5 Lithos scout responsibility, out of v1 scope.

### 5.5 Note frontmatter additions (arXiv-source notes only)

| Field | Type | Purpose |
|---|---|---|
| `s2_paper_id` | string (40-char hex; empty when enrichment skipped/failed) | Stable handle for S2AG queries and note-to-note joins. |
| `s2_fields_of_study` | list[string] | Raw category names; tags are derived but the list is preserved for later governance. |

Plus tag emission: when `s2_paper_id` is non-empty, also emit
`s2-paper-id:<hex>` as a tag. Mirrors the existing `arxiv-id:<id>`
pattern; lets `lithos_cache_lookup` queries find notes by S2 canonical
ID directly (used by §5.3 step 1).

### 5.6 Repair sweep `s2ag` stage

**Edited:** `src/influx/repair.py`, `src/influx/repair_counters.py`,
`src/influx/repair_hooks.py`

- New stage in the repair sweep, ordered **last** (after
  `tier3_extract`). S2AG enrichment is independent of every other
  repair stage's output and is the slowest stage (network-bound);
  putting it last reports earlier-stage failures first and minimises
  wasted work when an earlier stage fails.
- New fields in `RepairCounters`: `s2ag_attempts`, `s2ag_last_stage`,
  `s2ag_last_error`. `CountedStage` Literal grows `"s2ag"`. Adds
  `bump_s2ag(stage=, error=)` and an `attempts_for("s2ag")` mapping.
  Render/parse logic in `repair_counters.py` extended for the three
  new fields.
- The `Stages` decision struct grows `s2ag_retry: bool`, set when:
  - `not influx:s2ag-terminal` AND
  - `s2ag_attempts < REPAIR_COUNTED_CAP` AND
  - (`frontmatter.s2_paper_id == ""` OR `s2ag_last_stage == "edges"`).
- Behaviour:
  1. If `s2_paper_id == ""`: re-run `/paper/batch` enrichment. On
     success update frontmatter / tags. On counted failure:
     `bump_s2ag(stage="enrich", ...)`. On transient: leave for next
     sweep.
  2. Whether or not we ran step 1, if `s2_paper_id` is now non-empty
     AND not terminal: re-fetch references via
     `/paper/{paperId}/references` and re-run the §5.3 fan-out. On any
     per-edge failure: `bump_s2ag(stage="edges", ...)`. Idempotent edge
     upsert means already-emitted edges no-op.
- Successful retry clears `influx:repair-needed` only when no other
  stage is also failing.

### 5.7 Config schema additions

**Edited:** `src/influx/config.py`

```toml
[s2ag]
enabled = true
base_url = "https://api.semanticscholar.org"
api_key_env = "S2AG_API_KEY"     # optional
request_timeout = 30
batch_size = 500                 # max IDs per /paper/batch POST

[resilience]
# existing fields unchanged
s2ag_429_backoff_seconds = 30    # mirrors arxiv_429_backoff_seconds
```

No per-profile S2AG subsection in v1. `enabled = false` is the single
manual kill switch.

### 5.8 Metrics + observability

OTEL instruments (v1 surface):

| Instrument | Type | Labels |
|---|---|---|
| `influx_s2ag_calls_total` | Counter | `endpoint`, `status` |
| `influx_s2ag_edges_emitted_total` | Counter | `profile` |
| `influx_s2ag_enrichment_duration_seconds` | Histogram | `profile` |
| `influx_s2ag_cache_lookup_total` | Counter | `result=tag_hit\|url_hit\|miss` |
| `influx_s2ag_skip_total` | Counter | `reason=non_arxiv_source\|not_in_s2_corpus\|disabled` |
| `influx_s2ag_edge_upsert_failed_total` | Counter | `profile`, `reason` |

These give operators the chain:
`skip_total` (why no calls) → `calls_total{status}` (per-call success) →
`cache_lookup_total{result}` (why no edges) →
`edges_emitted_total` / `edge_upsert_failed_total` (final delta).

New ledger fields (additive): `s2ag_calls`, `s2ag_edges_written`.

### 5.9 Per-tick S2AG cache

The existing `FetchCache` (`src/influx/sources/__init__.py`) already
provides the in-flight collapsing semantics S2AG needs:
`get_or_fetch` shares an `asyncio.Future` across concurrent callers
for the same key. No new cache module — reuse `FetchCache` keyed by
`("s2ag", paperId)`. Lifecycle bracketed by the existing `_fire_tick`
`begin_fire` / `end_fire` window.

## 6. Lithos LCMA Work Units (deferred to follow-up epics)

The scouts described in v0.1 §6 are out of v1 scope. Listed here as
forward references for the consumers of Phase 1+2's frontmatter:

| v0.1 § | Scout | Phase |
|---|---|---|
| 6.1 | Citation backfill scout | 3 |
| 6.2 | Forward citation scout | 4 |
| 6.3 | Metadata refresh scout | 3 |
| 6.4 | Concept-tag governance scout | 5 |
| 6.5 | Influence reranker scout | 5 |
| 6.6 | Slug-collision adjudicator | 5 |

Phase 1+2 lays down the frontmatter (`s2_paper_id`,
`s2_fields_of_study`), tags (`s2-paper-id:<hex>`, `field:<slug>`), and
edges these scouts read. They can be designed once Phase 1+2 has been
live long enough to characterise real-world S2AG payloads in the
corpus.

## 7. Cross-Cutting Concerns

### 7.1 Rate-limit budget

S2AG: authenticated when available; otherwise shared anonymous budget.

| Workload | Rough volume | Mitigation |
|---|---|---|
| Influx per-tick enrichment (arXiv-only) | ~6 profiles × ~10 candidates × 4 ticks/day = ~240 papers/day | `/paper/batch` (up to 500 IDs/POST) → ~5 calls/day |
| Influx per-tick references | ~240 papers × 1 call each | `/paper/{paperId}/references` per paper |

This is still a small daily volume for a best-effort integration, and
the batch endpoint keeps the enrichment side cheap.

### 7.2 Per-tick cache

See §5.9.

### 7.3 Degraded path

S2AG failure is **never fatal**. Behaviour:

- Influx ingest continues without S2AG enrichment. arXiv-source notes
  are still written.
- For arXiv-source notes, `s2_paper_id: ""` is persisted when
  enrichment was skipped or failed so the repair sweep has a durable
  selector.
- When `[s2ag].enabled = false`, Influx skips the stage cleanly and
  records `influx_s2ag_skip_total{reason="disabled"}`.
- The repair sweep picks up un-enriched arXiv-source notes on the next
  sweep pass.

### 7.4 Edge cardinality guards

- **Per source note:** cap at top 50 references by
  `(influentialCitationCount DESC, year DESC, paperId ASC)` (§5.3).
  Cap is applied *before* cache lookup.
- **Per target note:** no cap in v1. The (deferred) Phase 3 backfill
  scout will surface high-fan-in targets when it lands.

### 7.5 Idempotency contract

- Re-running `s2ag_enrich` on a note with non-empty `s2_paper_id` (and
  not terminal) short-circuits before any API call.
- Reference fan-out is always re-run on repair when `s2_paper_id` is
  set (idempotent `lithos_edge_upsert` means already-emitted edges
  no-op cleanly).
- No external state — frontmatter is the source of truth.

## 8. Rollout Plan

Phase 1+2 ship as a single Influx-only epic. The phase boundary in
v0.1 (Phase 1 = frontmatter only, Phase 2 = edges) is dissolved
because the same module set, configuration surface, S2AG client,
per-tick cache, and repair stage support both — splitting them doubles
integration tax for no real revertibility win.

Reverse-out:
- Gated by `[s2ag].enabled`.
- Disabling the flag returns the system to its pre-extension behaviour
  with no data loss — frontmatter remains but is unused; edges remain
  but are not extended.

Phases 3–5 (Lithos scouts) are tracked as separate epics and depend on
Phase 1+2 having been live long enough to characterise the data.

## 9. Open Questions

(Updated 2026-05-10 after simplification pass.)

1. **Edge directionality in Lithos** — *answered:* `lithos/lcma/edges.py:44-45`
   indexes both `from_id` and `to_id`; reverse traversal is already
   cheap. Do not write `cited_by` inverses.
2. **Forward-citation ingest authorisation** — *deferred to Phase 4*,
   out of v1 scope.
3. **Scout scheduling** — *deferred to Phase 3+*, out of v1 scope.
