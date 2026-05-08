# Citation Graph Extension Plan

Version: 1.0.0
Date: 2026-05-07
Status: Approved for Phase 1+2 — design locked
Scope: Influx (Phase 1 + Phase 2).  Phases 3–5 (Lithos LCMA scouts) are
deferred to follow-up epics and tracked as forward references only.

---

## 1. Motivation

Influx today writes per-paper notes and a small set of relationships
(`builds_on`, `related_to`) inferred from LLM-extracted prose.  The
`builds_on` resolver in §10.5 of the Influx specification is the only
mechanism connecting notes via citation lineage, and it depends on
Tier 3 emitting `arXiv:<id>` strings — partial recall, no DOI fallback,
and silent on anything outside arXiv.  Live `edges.db` evidence
confirms this is currently zero-yield in production: in a 48 h staging
window, every persisted edge was written by Lithos's own
`EnrichWorker` consolidation path (`provenance_type='consolidation'`);
no edge has been authored by Influx's `lithos_edge_upsert` call site.

Semantic Scholar's Academic Graph API (S2AG) exposes ground-truth
references, citations, citation contexts and intents, paper-level
embeddings, and bibliometric signals across 214M papers.  Pulling that
into the Influx-Lithos pipeline turns a per-paper note store into a
genuine citation graph that LCMA retrieve can traverse intelligently.

The work splits cleanly along the existing Influx-vs-Lithos contract:

- **Influx ingests citation data alongside each paper.**  Same shape
  as Tier 1 / Tier 2 / Tier 3 enrichment — paper-in-hand, scoped to
  one tick, no corpus-wide reasoning.
- **Lithos LCMA maintains the citation graph over time.**  Backfills
  edges as the corpus fills in, runs forward-citation sweeps, governs
  concept-tag vocabulary, refreshes metadata when preprints get
  published.

This plan is written so the two halves can be split into independent
project epics.  v1 is **Phase 1 + Phase 2 (Influx-side only)** — the
Lithos scout work in §6 is deferred and lives here only as a forward
reference for the consumers of the frontmatter Phase 1+2 produces.

## 2. Goals

1. Replace the LLM-extracted `arXiv:<id>` resolver with ground-truth
   S2AG references.  Higher precision, higher recall, DOI-aware.
2. Emit typed citation edges (`cites`, `extends`, `applies_method_of`,
   `compares_with`, `cites_background`) into Lithos at note-write time
   for arXiv-source notes.
3. Land lightweight S2AG-derived enrichment (TLDR fallback,
   `s2FieldsOfStudy` tags, `s2_paper_id` on every arXiv-source note)
   so Lithos has enough state on each note to drive its own scouts
   later.
4. Treat S2AG outage as `degraded`, never fatal.  Influx without S2AG
   must still ingest cleanly.

## 3. Non-Goals

1. **Influx does not maintain the citation graph.**  Once Influx
   writes its seed edges + frontmatter, Lithos owns reconciliation.
2. **No new note types.**  Citation references for papers Lithos
   doesn't yet know are dropped at Influx-write time, not stored as
   stub notes.  The (deferred) Lithos backfill scout materialises
   edges later when the target appears.
3. **RSS-source candidates are not S2AG-enriched in v1.**  The s2ag
   stage is gated to arXiv-source candidates only.  RSS items
   (regardless of whether their URL happens to be an arXiv abs URL)
   skip the stage cleanly: no counter, no terminal tag, no degraded
   reason.  Future RSS coverage is a follow-up.
4. **TLDR fallback substitutes for Tier 1 only in v1.**  The Tier-3
   body-context substitution proposed in v0.1 is deferred — Tier 3
   produces a structured payload, not free prose, so the substitution
   semantics need separate design.
5. **No OpenAlex integration in v1.**  `s2FieldsOfStudy` is sufficient
   for an initial concept-tag pass.
6. **No SPECTER2 vector storage.**  Out of scope.
7. **Influx remains a single-process service.**  Scouts live in Lithos.
8. **Phases 3–5 are out of v1 scope.**  Citation backfill, forward-
   citation tracking, metadata refresh, concept-tag governance,
   influence reranking, and slug-collision adjudication are tracked
   as separate epics.

## 4. The Split

### 4.1 Belongs to Influx (v1)

| Activity | Reason |
|---|---|
| Fetching S2AG records for an arXiv paper being ingested *right now* | Same shape as Tier 1 / 2 / 3 — paper-in-hand enrichment. |
| Writing typed citation edges between arXiv-source notes Influx is creating | Influx already calls `lithos_edge_upsert` for `builds_on` (§10.5). |
| TLDR fallback when Tier 1 fails | Per-item enrichment, runs inside `Cascade.enrich`. |
| `s2FieldsOfStudy` tag emission at write time | Tag application is part of the canonical note write. |
| Recording `s2_paper_id`, `s2_doi`, `s2_citations_seen`, `s2_fields_of_study` in note frontmatter | Influx is the single writer of Influx-authored notes. |

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
s2_doi: "10.1234/example.2024.1234"
s2_citations_seen: 47
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
  - concept:reinforcement-learning
  - ingested-by:influx
  - schema:1
confidence: 0.78
---
```

Plus typed edges via `lithos_edge_upsert` for any reference whose
target is already a note in Lithos (resolved via the cache-lookup
chain in §5.3).

That is the entire Influx → Lithos interface for citation data.  Every
(deferred) LCMA scout reads from this state.

## 5. Influx Work Units

File-level pointers are tentative; final placement will be decided in
the implementation PRs.

### 5.1 S2AG provider + probe

**New module:** `src/influx/s2ag.py`

- `S2agClient` — async HTTP client wrapping the guarded HTTP path,
  honouring `[s2ag].request_timeout` and the resilience retry settings
  (`max_retries`, `s2ag_429_backoff_seconds`).
- Endpoints used:
  - `GET /graph/v1/paper/{paperId}` — single-paper lookup
  - `POST /graph/v1/paper/batch` — batched lookup
    (up to `[s2ag].batch_size` IDs/POST, default 500)
  - `GET /graph/v1/paper/{paperId}/references` — references with
    `intents` and `externalIds`
- Accepts `paperIdType=ARXIV` so we can pass the unversioned arXiv ID
  directly (Influx already strips version suffixes at parse time —
  `arxiv.py:266`).
- API key loaded from `s2ag.api_key_env` if set; without one, falls
  back to the unauthenticated 1 RPS shared bucket.
- Per-tick `FetchCache` integration — re-uses the existing
  `FetchCache.get_or_fetch` semantics
  (`src/influx/sources/__init__.py:84`); concurrent calls for the same
  `paperId` collapse onto a shared `asyncio.Future`.
- New `ProbeLoop` probe: `GET /graph/v1/paper/search?query=test&limit=1`
  every 60 s (GET, not HEAD — REST APIs do not always support HEAD).
  Latches `s2ag_unavailable` after **3 consecutive failures**; clears
  on the first success.

### 5.2 `s2ag_enrich` cascade stage

**Edited:** `src/influx/cascade.py`

- New stage **between Tier 2 (full-text) and Tier 1 (relevance
  enrichment)** in the cascade execution order.  Mirrors the existing
  `tier2_extractor` injection pattern: `s2ag_enricher: S2agEnricher | None`.
- Gate: `score >= thresholds.relevance` (same gate as Tier 1).
- **Applicability**: arXiv-source candidates only.  RSS-source
  candidates always pass `arxiv_id=None`; the cascade short-circuits
  the s2ag stage with no counter, no tag, no degraded reason.
- Inputs: `arxiv_id` from the candidate dict.
- Outputs added to the candidate's enrichment payload (in-memory only —
  see §5.7 for the subset that is persisted to frontmatter):
  - `s2_paper_id`, `s2_doi`, `s2_external_ids`
  - `s2_tldr` (string, may be empty)
  - `s2_fields_of_study` (list)
  - `s2_reference_count`, `s2_citation_count`,
    `s2_influential_citation_count`
  - `s2_references` (list of `{paperId, externalIds, intents,
    contexts, year, influentialCitationCount}`) — **not persisted**
    to frontmatter; lives on the cascade output for the post-write
    edge fan-out and is re-fetched from S2AG on repair.
- `EnrichedSections` grows two fields: `s2ag: S2agEnrichment | None`
  and `s2ag_attempted: bool` (mirrors `tier1_attempted`).
- Failure classification (mirrors §11.1 of the spec):
  - **Transient** (HTTP 5xx, transport, timeout, 429-respected) —
    counter not advanced; repair sweep retries indefinitely.
  - **Counted** (HTTP 4xx other than 429, parse, validation) —
    advances `s2ag_attempts`.
  - **Paper-not-in-S2-corpus** (404 or empty body for a known good
    `arxiv_id`) — counted-once, **immediately latches**
    `influx:s2ag-terminal` and tags `summary:s2ag-miss`.  Distinct from
    generic counted failures because retrying changes nothing.
- `influx:s2ag-terminal` after `REPAIR_COUNTED_CAP` (3) counted
  failures, OR after the single counted-once for paper-not-in-S2.
  Re-arm by removing the tag, mirroring `influx:tier3-terminal`.

### 5.3 Reference → typed edge emission

**Edited:** `src/influx/lcma_wiring.py`, `src/influx/lcma.py`

After `lithos_write` succeeds for an arXiv-source note that has a
non-empty `s2_references` payload:

1. **Cap and order**: take the top **50** references sorted by
   `(influentialCitationCount DESC, year DESC, paperId ASC)` *before*
   any cache lookup.  Three-key tuple is deterministic even when
   the leading two keys tie.
2. For each reference, run the resolution chain:
   1. `lithos_cache_lookup(tags=["s2-paper-id:<target_hex>"])` — exact
      match on S2 canonical ID (works for S2AG-era notes).
   2. If miss and `externalIds.ArXiv` present:
      `lithos_cache_lookup(source_url="https://arxiv.org/abs/<arxiv_id>")` —
      fallback for legacy pre-Phase-1 notes.
   3. If still miss: silently drop.  No counter, no warning, no tag.
      The (deferred) Phase 3 backfill scout materialises later.
3. On hit, call `lithos_edge_upsert` with:

| Field | Value |
|---|---|
| `from_id` | source note id |
| `to_id` | resolved target note id |
| `type` | derived from S2AG `intents` (table below) |
| `weight` | `1.0` (constant in v1; reserve for a future scout-computed influence score) |
| `namespace` | `"influx"` |
| `provenance_actor` | `"influx-s2ag"` |
| `provenance_type` | `"s2ag_reference"` |
| `evidence` | `{"s2_paper_id": <target_hex>, "intents": [...], "contexts": [<≤1 short snippet>], "source_paper_id": <source_hex>}` |
| `conflict_state` | `None` |

Edge type derivation:

| S2AG intent | Edge `type` |
|---|---|
| `extension` | `extends` |
| `methodology` | `applies_method_of` |
| `result` | `compares_with` |
| `background` | `cites_background` |
| (none / multiple) | `cites` |

**Best-effort fan-out**, never fails the parent write.  Per-reference:
wrap each `cache_lookup` + `edge_upsert` pair in try/except; log a
warning, increment `influx_s2ag_edge_upsert_failed_total`, continue to
the next reference.  When ≥1 reference fails: bump `s2ag_attempts` once
(not per-edge), set `s2ag_last_stage="edges"`, tag
`influx:repair-needed`.  Repair sweep re-runs the whole fan-out;
idempotent edge upsert means already-emitted edges no-op.

This step **replaces** the existing `arXiv:<id>` resolver in §10.5.
Tier 3's `builds_on` field remains in the note for human readability
but is no longer load-bearing for edge creation.

> **Prerequisite:** `agent-lore/influx#99` must land first.  The
> existing `LithosClient.edge_upsert` wrapper sends only
> `source_note_id` / `target_note_id` / `evidence`; the Lithos MCP
> tool requires `from_id` / `to_id` / `weight` / `namespace`.  The
> citation-graph fan-out cannot function until #99 is merged.  See §11.

### 5.4 TLDR fallback (Tier 1 only in v1)

**Edited:** `src/influx/renderer.py`

Renderer becomes a small finite-state machine over Tier 1 status and
S2AG TLDR availability:

| Tier 1 | `s2_tldr` | Output | Tags |
|---|---|---|---|
| OK | * | Tier 1 prose | (no s2ag-tldr) |
| Failed | non-empty | TLDR + trailing parenthetical attribution `_(summary auto-generated by Semantic Scholar)_` | `summary:s2ag-tldr` |
| Failed | empty | (no `## Summary` section) | `summary:tier1-failed-no-tldr` |
| Skipped (`score < relevance`) | * | (no `## Summary` section) | (no S2AG tags) |

When a subsequent re-write succeeds at Tier 1, the renderer always
prefers Tier 1; the `summary:s2ag-tldr` /
`summary:tier1-failed-no-tldr` tags are removed in the same write.

The Tier-3 body-context fallback proposed in v0.1 is **deferred** —
see §3.

### 5.5 Concept tagging from `s2FieldsOfStudy`

**Edited:** `src/influx/notes.py`

- Take the **first 3** entries of `s2_fields_of_study` as returned by
  S2AG (trust S2AG's internal ranking — it is stable per call).
- Slug-normalise: `"Computer Science"` → `"computer-science"`.
- Emit as `field:<slug>` tags.
- **No top-level filtering.**  Filtering broad fields like
  `Computer Science` globally is brittle (a CS paper genuinely about
  Linguistics may have Linguistics as the discriminator; a
  `Mathematics` paper may genuinely lead with Mathematics).  Cap-3
  already gives two slots beyond a dominant top-level field.
- Concept-tag governance (deduplication, vocabulary control) is a
  Phase 5 Lithos scout responsibility, out of v1 scope.

### 5.6 Citation-alert ingest endpoint — DEFERRED to Phase 4

Out of v1 scope.  Listed in v0.1 §5.6; left as a forward reference for
when forward-citation tracking lands.

### 5.7 Note frontmatter additions (arXiv-source notes only)

| Field | Type | Purpose |
|---|---|---|
| `s2_paper_id` | string (40-char hex; empty when enrichment skipped/failed) | Stable handle for S2AG queries; lets future scouts batch without re-resolving IDs. |
| `s2_doi` | string (empty when not present) | DOI from `externalIds.DOI`; sibling lookup key for the (deferred) Phase 3 scout. |
| `s2_citations_seen` | int | Last-known citation count; deltaable by Phase 3+ scouts. |
| `s2_fields_of_study` | list[string] | Raw category names; tags are derived but the list is preserved for Phase 5 governance. |

Plus tag emission: when `s2_paper_id` is non-empty, also emit
`s2-paper-id:<hex>` as a tag.  Mirrors the existing `arxiv-id:<id>`
pattern; lets `lithos_cache_lookup` queries find notes by S2 canonical
ID directly (used by §5.3 step 1).

`s2_influential_citation_count` is intentionally **not** persisted to
the note — it changes too often and is a corpus-side concern owned by
the (deferred) Phase 5 influence reranker scout.

### 5.8 Repair sweep `s2ag` stage

**Edited:** `src/influx/repair.py`, `src/influx/repair_counters.py`,
`src/influx/repair_hooks.py`

- New stage in the repair sweep, ordered **last** (after
  `tier3_extract`).  S2AG enrichment is independent of every other
  repair stage's output and is the slowest stage (network-bound);
  putting it last reports earlier-stage failures first and minimises
  wasted work when an earlier stage fails.
- New fields in `RepairCounters`: `s2ag_attempts`, `s2ag_last_stage`,
  `s2ag_last_error`.  `CountedStage` Literal grows `"s2ag"`.  Adds
  `bump_s2ag(stage=, error=)` and an `attempts_for("s2ag")` mapping.
  Render/parse logic in `repair_counters.py` extended for the three
  new fields.
- The `Stages` decision struct grows `s2ag_retry: bool`, set when:
  - `not influx:s2ag-terminal` AND
  - `s2ag_attempts < REPAIR_COUNTED_CAP` AND
  - (`frontmatter.s2_paper_id == ""` OR `s2ag_last_stage == "edges"`).
- Behaviour:
  1. If `s2_paper_id == ""`: re-run `/paper/batch` enrichment.  On
     success update frontmatter / tags.  On counted failure:
     `bump_s2ag(stage="enrich", ...)`.  On transient: leave for next
     sweep.
  2. Whether or not we ran step 1, if `s2_paper_id` is now non-empty
     AND not terminal: re-fetch references via
     `/paper/{paperId}/references` and re-run the §5.3 fan-out.  On
     any per-edge failure: `bump_s2ag(stage="edges", ...)`.  Idempotent
     edge upsert means already-emitted edges no-op.
- Successful retry clears `influx:repair-needed` only when no other
  stage is also failing.

### 5.9 Config schema additions

**Edited:** `src/influx/config.py`

```toml
[s2ag]
enabled = true
base_url = "https://api.semanticscholar.org"
api_key_env = "S2AG_API_KEY"     # optional; falls back to anon 1 RPS
request_timeout = 30
batch_size = 500                 # max IDs per /paper/batch POST

[resilience]
# existing fields unchanged
s2ag_429_backoff_seconds = 30    # mirrors arxiv_429_backoff_seconds

[[profiles]]
# existing fields unchanged
[profiles.s2ag]
enrich_references = true         # default true when [s2ag].enabled
```

`[profiles.s2ag]` is omitted ⇒ defaults applied.

`track_forward_citations` and `tracked_score_threshold` from v0.1 are
**dropped** (Phase 4 keys, not needed in v1).

### 5.10 Metrics + observability

OTEL instruments (v1 surface):

| Instrument | Type | Labels |
|---|---|---|
| `influx_s2ag_calls_total` | Counter | `endpoint`, `status` |
| `influx_s2ag_edges_emitted_total` | Counter | `profile`, `edge_type` |
| `influx_s2ag_tldr_fallback_total` | Counter | `profile`, `tier` |
| `influx_s2ag_enrichment_duration_seconds` | Histogram | `profile` |
| `influx_s2ag_cache_lookup_total` | Counter | `result=tag_hit\|url_hit\|miss` |
| `influx_s2ag_skip_total` | Counter | `reason=non_arxiv_source\|not_in_s2_corpus` |
| `influx_s2ag_edge_upsert_failed_total` | Counter | `profile`, `reason` |

(`influx_s2ag_dedup_recovery_total` from v0.1 is **dropped** — it was
for the Phase 5 §6.6 slug-collision adjudicator.)

These give operators the chain:
`skip_total` (why no calls) → `calls_total{status}` (per-call success) →
`cache_lookup_total{result}` (why no edges) →
`edges_emitted_total` / `edge_upsert_failed_total` (final delta).

New `degraded_reasons` value: `s2ag_unavailable`.  Single-run signal,
no consecutive-runs gate (mirrors `filter_error`'s shape).

New ledger fields (additive): `s2ag_calls`, `s2ag_edges_written`.

### 5.11 Per-tick S2AG cache

The existing `FetchCache` (`src/influx/sources/__init__.py`) already
provides the in-flight collapsing semantics S2AG needs:
`get_or_fetch` shares an `asyncio.Future` across concurrent callers
for the same key.  No new cache module — reuse `FetchCache` keyed by
`("s2ag", paperId)`.  Lifecycle bracketed by the existing
`_fire_tick` `begin_fire` / `end_fire` window.

## 6. Lithos LCMA Work Units (deferred to follow-up epics)

The scouts described in v0.1 §6 are out of v1 scope.  Listed here as
forward references for the consumers of Phase 1+2's frontmatter:

| v0.1 § | Scout | Phase |
|---|---|---|
| 6.1 | Citation backfill scout | 3 |
| 6.2 | Forward citation scout | 4 |
| 6.3 | Metadata refresh scout | 3 |
| 6.4 | Concept-tag governance scout | 5 |
| 6.5 | Influence reranker scout | 5 |
| 6.6 | Slug-collision adjudicator | 5 |

Phase 1+2 lays down the frontmatter (`s2_paper_id`, `s2_doi`,
`s2_citations_seen`, `s2_fields_of_study`), tags
(`s2-paper-id:<hex>`, `field:<slug>`), and edges these scouts read.
They can be designed once Phase 1+2 has been live long enough to
characterise real-world S2AG payloads in the corpus.

## 7. Cross-Cutting Concerns

### 7.1 Rate-limit budget

S2AG: 1 RPS authenticated, 1 RPS shared unauthenticated.

| Workload | Rough volume | Mitigation |
|---|---|---|
| Influx per-tick enrichment (arXiv-only) | ~6 profiles × ~10 candidates × 4 ticks/day = ~240 papers/day | `/paper/batch` (up to 500 IDs/POST) → ~5 calls/day |
| Influx per-tick references | ~240 papers × 1 call each | `/paper/{paperId}/references` per paper |
| Probe loop | 1 call/60 s = 1440/day | Search with `limit=1` |

Total daily v1 S2AG budget: well under 2,000 calls/day.  Fits inside
the anonymous 1 RPS bucket with substantial headroom.

### 7.2 Per-tick cache

See §5.11.

### 7.3 Degraded path

S2AG outage is **never fatal**.  Behaviour:

- Influx ingest continues without S2AG enrichment.  arXiv-source notes
  are written with no `s2_*` frontmatter, no `s2-paper-id` tag, no
  S2AG-derived edges.
- Run ledger flags `degraded_reasons += ["s2ag_unavailable"]` when
  the probe is latched (3 consecutive failures).
- When S2AG recovers, the Influx repair sweep picks up un-enriched
  arXiv-source notes (`influx:repair-needed`) on the next sweep pass.

### 7.4 Edge cardinality guards

- **Per source note:** cap at top 50 references by
  `(influentialCitationCount DESC, year DESC, paperId ASC)` (§5.3).
  Cap is applied *before* cache lookup.
- **Per target note:** no cap in v1.  The (deferred) Phase 3 backfill
  scout will surface high-fan-in targets when it lands.

### 7.5 Idempotency contract

- Re-running `s2ag_enrich` on a note with non-empty `s2_paper_id` (and
  not terminal) short-circuits before any API call.
- Reference fan-out is always re-run on repair when `s2_paper_id` is
  set (idempotent `lithos_edge_upsert` means already-emitted edges
  no-op cleanly).
- No external state — frontmatter is the source of truth.

## 8. Rollout Plan

Phase 1+2 ship as a single Influx-only epic.  The phase boundary in
v0.1 (Phase 1 = frontmatter only, Phase 2 = edges) is dissolved
because the same module set, configuration surface, S2AG client,
per-tick cache, and repair stage support both — splitting them
doubles integration tax for no real revertibility win.

Reverse-out:
- Gated by `[s2ag].enabled`.
- Disabling the flag returns the system to its pre-extension behaviour
  with no data loss — frontmatter remains but is unused; edges remain
  but aren't extended.

Phases 3–5 (Lithos scouts) are tracked as separate epics and depend
on Phase 1+2 having been live long enough to characterise the data.

## 9. Open Questions

(Updated 2026-05-07 after design grilling; see commit history for
the v0.1 list.)

1. **Edge directionality in Lithos** — *answered:* `lithos/lcma/edges.py:44-45`
   indexes both `from_id` and `to_id`; reverse traversal is already
   cheap.  Don't write `cited_by` inverses.
2. **Forward-citation ingest authorisation** — *deferred to Phase 4*,
   out of v1 scope.
3. **Scout scheduling** — *deferred to Phase 3+*, out of v1 scope.
4. **API key rotation** — *v1 answer:* env-var (`s2ag.api_key_env`)
   is sufficient.  Rotation is an operator concern; restart picks up
   the new value.  Revisit when the (deferred) forward-citation scout
   makes S2AG critical-path.
5. **Concept-vocabulary seed** — *deferred to Phase 5*, out of v1
   scope.
6. **Edge-type closure** — *answered:* Lithos doesn't validate edge
   `type` strings (`lithos/server.py:1860`); the 5-type closure is an
   Influx-side convention, not a Lithos contract.  Raw S2AG `intents`
   are preserved in `evidence` so future re-bucketing is reversible
   without re-fetching from S2AG.

## 10. References

- Influx specification §10.5 (LCMA Hooks) — current `builds_on`
  resolver this work replaces.
- Influx specification §11.1 (Per-stage cap and self-repair) — the
  pattern reused for `s2ag_attempts`.
- Influx specification §13.1 (`degraded_reasons`) — where
  `s2ag_unavailable` plugs in.
- Influx issue #87 — scheduler stagger work; this plan does not add
  any cross-tick scheduling pressure beyond what's already accounted
  for there.
- Influx issue #99 — `LithosClient.edge_upsert` wrapper bug; see §11.
- Semantic Scholar Academic Graph API
  (https://api.semanticscholar.org/api-docs/graph).

## 11. Prerequisite

**`agent-lore/influx#99`** — `LithosClient.edge_upsert` wrapper
signature does not match the Lithos MCP tool.

The existing wrapper sends `source_note_id` / `target_note_id` /
`evidence` only; the Lithos MCP tool requires `from_id` / `to_id` /
`weight` / `namespace` and accepts `provenance_*` / `conflict_state`.
Live `edges.db` confirms zero `LithosClient.edge_upsert`-authored
edges in 48 h staging — the only persisted edges are written by
Lithos's own `EnrichWorker` (`provenance_type='consolidation'`).
The wrapper is a latent bug today because both call sites
(`after_write`, `resolve_builds_on`) are upstream-gated and rarely
fire; the §5.3 reference fan-out will exercise it on every arXiv
ingest and would fail without the fix.

#99 must merge before the §5.3 edge-emission work lands.  The
non-edge work (S2AG client, frontmatter, tags, TLDR fallback,
metrics) does **not** depend on #99 and can land in parallel.
