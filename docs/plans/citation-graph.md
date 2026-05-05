# Citation Graph Extension Plan

Version: 0.1.0
Date: 2026-05-05
Status: Draft — pre-implementation
Scope: Influx + Lithos LCMA

---

## 1. Motivation

Influx today writes per-paper notes and a small set of relationships
(`builds_on`, `related_to`) inferred from LLM-extracted prose.  The
`builds_on` resolver in §10.5 of the Influx specification is the only
mechanism connecting notes via citation lineage, and it depends on
Tier 3 emitting `arXiv:<id>` strings — partial recall, no DOI fallback,
and silent on anything outside arXiv.

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
project epics once we decide to implement.

## 2. Goals

1. Replace the LLM-extracted `arXiv:<id>` resolver with ground-truth
   S2AG references.  Higher precision, higher recall, DOI-aware.
2. Emit typed citation edges (`cites`, `extends`, `applies_method_of`,
   `cites_background`) into Lithos at note-write time.
3. Land lightweight S2AG-derived enrichment (TLDR fallback,
   `s2FieldsOfStudy` tags, `s2_paper_id` on every note) so Lithos has
   enough state on each note to drive its own scouts later.
4. Build LCMA scouts in Lithos for forward-citation discovery,
   citation-edge backfill, metadata refresh, and concept-tag
   governance.
5. Treat S2AG outage as `degraded`, never fatal.  Influx without S2AG
   must still ingest cleanly; LCMA scouts must skip cleanly.

## 3. Non-Goals

1. **Influx does not maintain the citation graph.**  Once Influx
   writes its seed edges + frontmatter, Lithos owns reconciliation.
2. **No new note types.**  Citation references for papers Lithos
   doesn't yet know are dropped at Influx-write time, not stored as
   stub notes.  The Lithos backfill scout materialises edges later
   when the target appears.
3. **No OpenAlex integration in v1.**  `s2FieldsOfStudy` is sufficient
   for an initial concept-tag pass; OpenAlex is a follow-up.
4. **No SPECTER2 vector storage.**  Worth considering once the simpler
   wins land, but out of scope for this plan.
5. **Influx remains a single-process service.**  Scouts live in Lithos;
   nothing here changes Influx's process model.

## 4. The Split

### 4.1 Belongs to Influx

| Activity | Reason |
|---|---|
| Fetching S2AG records for a paper being ingested *right now* | Same shape as Tier 1 / 2 / 3 — paper-in-hand enrichment. |
| Writing typed citation edges between notes Influx is creating | Influx already calls `lithos_edge_upsert` for `builds_on` (§10.5). |
| TLDR fallback when Tier 1 / Tier 3 fails | Per-item enrichment, runs inside `Cascade.enrich`. |
| `s2FieldsOfStudy` tag emission at write time | Tag application is part of the canonical note write. |
| Reacting to scout-emitted ingest requests via admin API | Influx already owns the relevance filter, ingest pipeline, and webhook fan-out. |
| Recording `s2_paper_id` and `s2_citations_seen` in note frontmatter | Influx is the single writer of Influx-authored notes. |

### 4.2 Belongs to Lithos LCMA

| Activity | Reason |
|---|---|
| Forward-citation sweeps over the existing corpus | Corpus-wide read; no per-profile / per-tick scope. |
| Backfilling edges when a previously-shadowed citation becomes a real note | Operates on graph state, not on candidates. |
| Influence-driven re-ranking of existing notes | Reads corpus, writes annotations — pure curation. |
| Concept-tag vocabulary control + near-duplicate consolidation | Vocabulary lives with the corpus; Influx is a writer, not a curator. |
| Metadata refresh (preprint → published, venue assignment) | "Is the world's view of this paper still in sync with ours?" is a graph-maintenance question. |
| Slug-collision adjudication via S2AG paperId | Reads Lithos squatter state; not in Influx's per-tick scope. |

### 4.3 Boundary handoff

Influx writes the following on every note it authors after S2AG
enrichment lands:

```yaml
---
note_type: summary
namespace: influx
source_url: https://arxiv.org/abs/2402.12345
s2_paper_id: "abc123def456..."
s2_citations_seen: 47
s2_fields_of_study:
  - "Computer Science"
  - "Mathematics"
tags:
  - profile:ai-agents
  - source:arxiv
  - arxiv-id:2402.12345
  - field:cs.LG
  - concept:reinforcement-learning
  - ingested-by:influx
  - schema:1
confidence: 0.78
---
```

Plus typed edges via `lithos_edge_upsert` for any reference whose
target is already a note in Lithos (cache lookup hit).

That is the entire Influx → Lithos interface for citation data.  Every
LCMA scout reads from this state; no scout calls into Influx except to
*request* an ingest via the new admin endpoint (§5.6).

## 5. Influx Work Units

File-level pointers are tentative; final placement will be decided in
the implementation PRs.

### 5.1 S2AG provider + probe

**New module:** `src/influx/s2ag.py`

- `S2agClient` — async HTTP client wrapping the guarded HTTP path,
  honouring `[s2ag].request_timeout` and the resilience retry settings.
- Endpoints used:
  - `GET /graph/v1/paper/{paperId}` — single-paper lookup
  - `POST /graph/v1/paper/batch` — batched lookup (up to 500 IDs/POST)
  - `GET /graph/v1/paper/{paperId}/references` — references with
    intents and externalIds
  - `GET /graph/v1/paper/{paperId}/citations` — citations (used by
    Lithos scouts, but the client lives in Influx for reuse)
- Accepts `paperIdType=ARXIV` so we can pass `arXiv:<id>` directly
  without resolving via DOI first.
- API key loaded from `s2ag.api_key_env` if set; without one, falls
  back to the unauthenticated 1 RPS shared bucket.
- New `ProbeLoop` probe: HEAD on `/graph/v1/paper/search?query=test&limit=1`
  every 60s, mirroring the Lithos probe semantics.
- Latches `s2ag_unavailable` on the probe loop after N consecutive
  failures, surfaced as a `degraded_reasons` entry in the run ledger.

### 5.2 `s2ag_enrich` cascade stage

**Edited:** `src/influx/cascade.py`

- New stage between Tier 1 and Tier 2.  Runs only on items that passed
  the relevance filter (§7.1) — never on rejects, so the S2AG budget
  is bounded by ingest-eligible candidates per tick.
- Inputs: `arxiv-id` (or DOI) from the candidate dict.
- Outputs added to the candidate's enrichment payload:
  - `s2_paper_id`, `s2_external_ids`
  - `s2_tldr` (string, may be empty)
  - `s2_fields_of_study` (list)
  - `s2_reference_count`, `s2_citation_count`,
    `s2_influential_citation_count`
  - `s2_references` (list of `{paperId, externalIds, intents,
    contexts}`)
- Failure classification reuses the spec §11.1 partition:
  - Transient (HTTP, transport, timeout) — counter not advanced.
  - Counted (parse, validate, missing-paper) — advances
    `s2ag_attempts` in the note's `## Repair` block.
- `influx:s2ag-terminal` set after `REPAIR_COUNTED_CAP` (currently 3)
  counted failures.  Re-arm by removing the tag, mirroring
  `influx:tier3-terminal`.

### 5.3 Reference → typed edge emission

**Edited:** `src/influx/lcma_wiring.py`

- After `lithos_write` succeeds, iterate over `s2_references`:
  1. For each reference, attempt `lithos_cache_lookup` by `source_url`
     (built from `externalIds.DOI` or `externalIds.ArXiv`).
  2. If hit: `lithos_edge_upsert` with edge type derived from intents:
     | S2AG intent | Edge type |
     |---|---|
     | `extension` | `extends` |
     | `methodology` | `applies_method_of` |
     | `result` | `compares_with` |
     | `background` | `cites_background` |
     | (none / multiple) | `cites` |
  3. If miss: drop the edge.  The Lithos backfill scout (§6.1) will
     materialise it later when the target becomes a note.
- Cap edges per source note: top 50 references by
  `s2_influential_citation_count` (with ties broken by recency).  Avoids
  a single mega-survey paper saturating the graph.
- Replaces the existing `arXiv:<id>` resolver in §10.5 entirely.  Tier 3's
  `builds_on` field stays in the note for human readability but is no
  longer load-bearing for edge creation.

### 5.4 TLDR Tier 0 fallback

**Edited:** `src/influx/enrich.py`, `src/influx/renderer.py`

- When Tier 1 enrichment fails (transient OR counted) AND `s2_tldr` is
  non-empty, render the `## Summary` section from the TLDR with a
  trailing "(summary auto-generated by Semantic Scholar)" attribution
  line.
- When Tier 3 fails terminally (`tier3-terminal`) AND the note has only
  abstract-only content, the same TLDR can stand in for the body
  context — keeps notes from sitting in `text:abstract-only` purgatory
  with no Summary at all.
- Adds tag `summary:s2ag-tldr` when this fallback applies, so operators
  can spot it.

### 5.5 Concept tagging from `s2FieldsOfStudy`

**Edited:** `src/influx/notes.py`

- Emit up to 3 `field:<value>` tags from `s2_fields_of_study`, lowercased
  and slug-normalised (`Computer Science` → `field:computer-science`).
- Cap at top-3 to bound cardinality.
- Concept-tag governance (deduplication, vocabulary control) is a
  Lithos scout responsibility (§6.4); Influx emits raw tags.

### 5.6 Citation-alert ingest endpoint

**New:** `POST /citation-alerts` in `src/influx/http_api.py`

- Body: `{paper_id: <s2_paper_id>, profile: <name>, reason: <string>}`
- Used by the Lithos forward-citation scout (§6.2) when it discovers a
  paper that cites a tracked Influx note.
- Influx resolves the S2AG paper to an arXiv ID or DOI, runs the same
  per-profile relevance filter, and ingests if it passes.
- Auth: same loopback / `allow_remote_admin` policy as the rest of the
  admin API.  In multi-host setups this needs a shared token —
  defer to a later iteration.
- Returns `202` with `request_id` like the existing `/runs` endpoint.

### 5.7 Note frontmatter additions

Three new fields on Influx-authored notes:

| Field | Type | Purpose |
|---|---|---|
| `s2_paper_id` | string | Stable handle for all subsequent S2AG queries; lets Lithos scouts batch without re-resolving IDs. |
| `s2_citations_seen` | int | Last-known citation count, recorded on each ingest / refresh.  Forward-citation scout uses the delta to detect new citations cheaply. |
| `s2_fields_of_study` | list[string] | Raw field names; tags are derived but the list is preserved for governance scouts to canonicalise. |

`s2_influential_citation_count` is intentionally **not** stored on the
note itself — it changes too often and is a corpus-side concern.  The
Lithos influence reranker scout (§6.5) maintains it as graph metadata.

### 5.8 Repair sweep `s2ag` stage

**Edited:** `src/influx/repair.py`, `src/influx/repair_counters.py`

- Mirrors Tier 2 / Tier 3 stages.  Adds `s2ag_attempts`,
  `s2ag_last_stage`, `s2ag_last_error` to the `## Repair` block.
- Sweep retries S2AG enrichment for notes tagged `influx:repair-needed`
  whose `s2ag_attempts < REPAIR_COUNTED_CAP`.
- Successful retry clears `influx:repair-needed` only when no other
  stage is also failing.

### 5.9 Config schema additions

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
s2ag_429_backoff_seconds = 30    # mirror arxiv_429_backoff_seconds

[[profiles]]
# existing fields unchanged
[profiles.s2ag]
enrich_references = true                  # default true when [s2ag].enabled
track_forward_citations = false           # opt-in per profile
tracked_score_threshold = 8               # only notes ≥ this score are tracked
```

`[profiles.s2ag]` is omitted ⇒ defaults applied.

### 5.10 Metrics + observability

New OTEL instruments:

| Instrument | Type | Labels |
|---|---|---|
| `influx_s2ag_calls_total` | Counter | `endpoint`, `status` |
| `influx_s2ag_dedup_recovery_total` | Counter | _(no labels)_ |
| `influx_s2ag_edges_emitted_total` | Counter | `profile`, `edge_type` |
| `influx_s2ag_tldr_fallback_total` | Counter | `profile`, `tier` |
| `influx_s2ag_enrichment_duration_seconds` | Histogram | `profile` |

New `degraded_reasons` value: `s2ag_unavailable`.  Single-run signal,
no consecutive-runs gate (mirrors `filter_error`'s shape).

New ledger fields (additive): `s2ag_calls`, `s2ag_edges_written`.

### 5.11 Per-tick S2AG cache

**New:** `src/influx/sources/s2ag_cache.py` (or fold into existing
`FetchCache`).

- Mirrors the existing arXiv per-tick fetch dedup: when two profiles
  ingest the same paper in one tick, S2AG is queried once.
- Lifecycle bracketed by the existing `_fire_tick` `begin_fire` /
  `end_fire` window — no new cache scope to reason about.

## 6. Lithos LCMA Work Units (Scouts)

Each scout is a periodic LCMA worker.  All scouts read note frontmatter
written by Influx (§5.7); none calls back into Influx except via the
`POST /citation-alerts` endpoint (§5.6).

### 6.1 Citation backfill scout

**Trigger:** New note write event in Lithos (or periodic sweep, batch
per N minutes).

**Work:**
1. For the new note, look up `s2_paper_id` in Lithos's reference index
   (an internal index of "papers we have edges pointing AT but don't
   yet have a note for").
2. For every existing note that referenced this paper as a shadow
   target, materialise the previously-dropped citation edge with the
   appropriate type.
3. Update the reference index.

**Why a scout, not Influx:** Influx never knows which future notes will
fill in its dropped references.  This is a corpus-mutation event that
can fire long after the source note was written.

### 6.2 Forward citation scout

**Trigger:** Cron, e.g. daily at off-peak hours.

**Work:**
1. Read all notes with `s2_citations_seen` set (the "tracked" set —
   bounded by per-profile `tracked_score_threshold` at Influx-write
   time).
2. Batch S2AG `/paper/batch` to fetch current `citationCount` per note.
3. For notes with new citations (delta > 0), call
   `/paper/{paperId}/citations` and inspect the new entries.
4. For each new citing paper:
   - If it's already a Lithos note: emit the appropriate typed edge.
   - If not: hit `POST /citation-alerts` on Influx with the paperId,
     the profile that flagged the cited paper, and a reason string.
     Influx decides whether to ingest (relevance filter still applies).
5. Update `s2_citations_seen` on the cited note.

**Why a scout, not Influx:** Forward-citation tracking is a read-heavy
corpus traversal that needs to span profiles and span time.  Influx's
per-tick / per-profile model is the wrong shape.

### 6.3 Metadata refresh scout

**Trigger:** Cron, weekly.

**Work:**
1. Batch-query S2AG for notes' `s2_paper_id`s; check `journal`,
   `publicationVenue`, `publicationDate`, `externalIds`.
2. Update note frontmatter when something changed (preprint → journal
   acceptance, DOI assignment, etc.).
3. Add tags like `venue:NeurIPS-2024`, `published:true`.
4. Idempotent — running twice is a no-op when nothing has changed.

### 6.4 Concept-tag governance scout

**Trigger:** Cron, weekly.

**Work:**
1. Read raw `concept:*` and `field:*` tags emitted by Influx across the
   corpus.
2. Identify near-duplicates (`graph-neural-networks` vs `gnns` vs
   `graph-neural-nets`).
3. Map onto a controlled vocabulary (seeded from OpenAlex's stable
   concept set, OR from in-corpus tag frequency above a floor).
4. Rewrite tags on affected notes.

**Why a scout, not Influx:** The vocabulary is a corpus-level concept
that emerges from multiple Influx writes over time.  Influx writes raw
tags; Lithos canonicalises them.

### 6.5 Influence reranker scout

**Trigger:** Cron, weekly.

**Work:**
1. Batch S2AG for all tracked notes' `influentialCitationCount`.
2. Maintain a Lithos-side annotation (`s2_influential_citations_latest`,
   `s2_influence_updated_at`).
3. Optionally drives "rising star" or "becoming foundational"
   notifications via Lithos's own notification path — orthogonal to
   Influx's notification machinery.

### 6.6 Slug-collision adjudicator

**Trigger:** On every `slug_collision` event in Lithos's write path
(or batch-replay against the existing
`unresolved-slug-collisions.jsonl`).

**Work:**
1. Both squatter and incoming paper resolve their `s2_paper_id` via
   S2AG.
2. If both resolve to the same `paperId`: it's a true duplicate
   (e.g. arXiv preprint vs DOI'd journal version).  Merge tags and
   Profile Relevance, treat as `duplicate`.
3. If different: fall through to the existing AC-05-D suffix retry.

Augments — does not replace — the §10.4 chain.  Catches the
arXiv→journal republication case the URL-dedup misses today.

## 7. Cross-Cutting Concerns

### 7.1 Rate-limit budget

S2AG: 1 RPS authenticated, 1 RPS shared unauthenticated.

| Workload | Rough volume | Mitigation |
|---|---|---|
| Influx per-tick enrichment | 6 profiles × ~10 candidates × 4 ticks/day = ~240 papers/day | `/paper/batch` (up to 500 IDs/POST) → ~5 calls/day |
| Influx per-tick references | ~240 papers × 1 call each | Could be batched via per-paper expansion in `/paper/batch` |
| Lithos forward-citation scout | ~500 tracked notes, daily | `/paper/batch` for citation counts → 1 call; per-paper `/citations` only on delta > 0 |
| Lithos metadata refresh | ~corpus size, weekly | `/paper/batch` |

Total daily S2AG budget under realistic operation: well under 100
authenticated calls/day.  Negligible.

### 7.2 Per-tick cache

Influx wraps S2AG calls in a per-tick cache that mirrors the existing
arXiv `FetchCache` (#9.3 / R-8 / AC-09-D).  Two profiles ingesting the
same paper in one tick → one S2AG call.

### 7.3 Degraded path

S2AG outage is **never fatal**.  Behaviour:

- Influx ingest continues without S2AG enrichment.  Notes are written
  with no `s2_*` frontmatter and no S2AG-derived edges.
- Run ledger flags `degraded_reasons += ["s2ag_unavailable"]`.
- Lithos scouts skip cleanly when their probe says S2AG is down;
  `s2ag_unavailable` shows up in scout-side metrics but no scout fails.
- When S2AG recovers, the Influx repair sweep picks up the un-enriched
  notes (`influx:repair-needed`) on next run; Lithos scouts catch up
  on the next periodic tick.

### 7.4 Edge cardinality guards

- Per source note: cap at top 50 references by
  `s2_influential_citation_count` (Influx-side, §5.3).
- Per target note: no cap.  But the Lithos backfill scout should warn
  when an inbound edge count crosses 1000 — that's a corpus signal
  worth surfacing, not a problem to silently truncate.

### 7.5 Idempotency contract

Every operation in this plan must be safe to re-run:

- Influx S2AG enrichment runs on each ingest *and* on each repair pass.
  Re-running on an already-enriched note is a no-op (frontmatter
  comparison short-circuits before the API call).
- Lithos scouts read note state, decide what's missing, write deltas.
  No scout maintains its own external state — the corpus is the
  source of truth.

## 8. Rollout Plan

Phased rollout to land highest-leverage / lowest-risk pieces first.
Each phase is independently shippable and independently revertible.

### Phase 1 — Influx-side enrichment, no edges (Influx only)

- §5.1 S2AG client + probe
- §5.2 `s2ag_enrich` cascade stage (writes frontmatter only, no edges
  yet)
- §5.7 Note frontmatter additions
- §5.9 Config schema additions
- §5.10 Metrics
- §5.11 Per-tick S2AG cache
- §5.4 TLDR Tier 0 fallback
- §5.5 `s2FieldsOfStudy` tags

**Outcome:** Every Influx-authored note carries `s2_paper_id`,
`s2_citations_seen`, `s2_fields_of_study`.  No graph changes yet —
gives the Lithos work somewhere to read from.

### Phase 2 — Citation edges (Influx only)

- §5.3 Reference → typed edge emission
- §5.8 Repair sweep `s2ag` stage

**Outcome:** Replaces the LLM-extracted `builds_on` resolver with
ground-truth typed edges.  Lithos's existing graph traversal benefits
immediately.

### Phase 3 — Lithos backfill + metadata scouts (Lithos only)

- §6.1 Citation backfill scout
- §6.3 Metadata refresh scout

**Outcome:** Edges that Influx couldn't resolve at write time get
filled in retroactively.  Notes stay current as preprints get
published.

### Phase 4 — Forward citation tracking (Influx + Lithos)

- §5.6 Citation-alert ingest endpoint
- §6.2 Forward citation scout

**Outcome:** Net-new product capability.  "Last week, three new papers
cited your favourite 2024 paper on agent memory.  Two of them passed
your relevance filter."

### Phase 5 — Curation scouts (Lithos only)

- §6.4 Concept-tag governance
- §6.5 Influence reranker
- §6.6 Slug-collision adjudicator

**Outcome:** Corpus quality improves over time without operator
intervention.

### Reverse-out

Each phase is gated by `[s2ag].enabled` (Phase 1–2) or scout-level
`enabled` flags (Phase 3+).  Disabling the flag returns the system to
its pre-extension behaviour with no data loss — frontmatter remains
but is unused; edges remain but aren't extended.

## 9. Open Questions

1. **Edge directionality in Lithos.**  Does `lithos_edge_upsert`
   support symmetric inverses, or do we need both `cites` and
   `cited_by` edges?  Affects scout traversal cost.
2. **Forward-citation ingest authorisation.**  The scout-driven ingest
   path bypasses the regular cron tick.  Should it count against
   `feedback.recalibrate_after_runs`?  Should it write a distinct
   `RunKind.CITATION_ALERT` to the run ledger so it shows up in
   diagnose tooling?
3. **Scout scheduling.**  Where do LCMA scouts live operationally?
   Same process as Lithos's MCP server, or a separate worker tier?
   Affects deployment shape and `/ready` semantics.
4. **API key rotation.**  S2AG offers higher rate limits with an API
   key.  Is the existing env-var mechanism (`api_key_env`) sufficient,
   or do we need a key-management story given this becomes a critical
   path?
5. **Concept-vocabulary seed.**  Bootstrap from OpenAlex (one-time
   import) or grow organically from in-corpus tag frequency?
   Determines whether Phase 5's tag-governance scout has a vocabulary
   on day one.
6. **Edge-type closure.**  The `cites` / `extends` / `applies_method_of`
   / `compares_with` / `cites_background` set is opinionated.  Should
   we keep S2AG's raw `intents` as an edge attribute too, in case we
   want to refine the bucketing later?

## 10. References

- Influx specification §10.5 (LCMA Hooks) — current `builds_on`
  resolver this work replaces.
- Influx specification §11.1 (Per-stage cap and self-repair) — the
  pattern reused for `s2ag_attempts`.
- Influx specification §13.1 (`degraded_reasons`) — where
  `s2ag_unavailable` plugs in.
- Influx issue #87 — scheduler stagger work; this plan does NOT add
  any cross-tick scheduling pressure beyond what's already accounted
  for there.
- Semantic Scholar Academic Graph API
  ([api.semanticscholar.org/api-docs/graph](https://api.semanticscholar.org/api-docs/graph)).
