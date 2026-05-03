# Influx Context

Influx ingests research and technical content for configured **Profiles**, scores it against per-Profile relevance, enriches it through a tiered cascade, and writes canonical notes into Lithos. This file pins the vocabulary the codebase and its docs should use.

Some terms below are marked _(proposed)_ — they name modules agreed during architecture grilling but not yet present in code. Drop the marker as each lands.

## Language

### Domain — content and scoring

**Profile**:
A named interest scope with its own description, score thresholds, source list, and notification rules. Almost every action in Influx is per-Profile.
_Avoid_: topic, channel, feed.

**Source**:
A place candidates come from — currently `arxiv` or `rss`/`blog`. Each Source is an adapter that exposes `fetch_candidates` (bulk per Profile) and `acquire` (per item: download, archive, extract). _(proposed as a unified seam; today the two sources duplicate the pipeline)_
_Avoid_: provider (used for OpenAI-compatible model providers).

**Candidate**:
An unscored item returned from `Source.fetch_candidates` — title, source URL, identity tag (`arxiv-id`, `feed-slug`), and provider-native metadata. Becomes a **ScoredCandidate** after the **Filter** assigns it a 1–10 score.

**Filter**:
The score-gated entry to ingestion. Calls `models.filter` with the configured prompt plus negative-feedback examples and returns `{score, tags, reason}` per Candidate. Items below `thresholds.relevance` (or absent from the response) are dropped.
_Avoid_: classifier, scorer.

**Cascade**:
The score-gated enrichment pipeline that turns an **Acquired** item into **EnrichedSections**: Tier 1 summary at `score >= relevance`, Tier 2 full text at `score >= full_text`, Tier 3 deep extraction at `score >= deep_extract`. _(proposed as a single module shared across Sources)_
_Avoid_: enrichment chain, tier pipeline.

**Tier 1 / Tier 2 / Tier 3**:
The three enrichment levels, each gated by a different threshold and producing different note sections. Tier 1 = `models.enrich` summary. Tier 2 = full text extraction. Tier 3 = `models.extract` claims/datasets/builds_on/open_questions/potential_connections.

**Acquired**:
The bundle a **Source** produces for one Candidate after download/archive/extract: identity, source URL, archive path (or repair flag), extracted text (or `None`), text source flavour (`html`/`pdf`/`summary-fallback`), and source-specific signals (e.g. `archive_terminal`). _(proposed)_

**EnrichedSections**:
The **Cascade**'s output for one Acquired: optional Tier 1 result, optional full text + flavour, optional Tier 3 result, plus `repair_flags` and `terminal_flags` for the Renderer to apply as note tags. _(proposed)_

**Renderer**:
Produces a **CanonicalNote** from an Acquired plus EnrichedSections plus the score/reason. Owns the canonical Markdown shape from spec section 9. _(proposed as a separate module from the cascade)_

**CanonicalNote**:
An Influx-authored Markdown note: typed frontmatter, fixed section order (`## Archive`, `## Summary`, `## Full Text`, `## Claims`, `## Datasets & Benchmarks`, `## Builds On`, `## Open Questions`, `## Profile Relevance`, `## User Notes`), and stable tag conventions. `## User Notes` is preserved byte-exactly across rewrites.

**RepairCounters**:
Per-tier attempt counter persisted in the note's `## Repair` section. Read on tier entry (skip if `tier{N}-terminal` is set), advanced on counted (parse/validate) failures, never advanced on transient failures. Reaches the cap → adds `influx:tier{N}-terminal`. _(proposed as a module shared between the create path and the repair sweep)_

### Domain — execution

**Run**:
One end-to-end execution of the ingestion pipeline for one Profile. Constructed from a **RunPlan** plus dependencies; produces a **RunOutcome**. _(proposed as the deepened replacement for today's `_run_profile_body` orchestration)_
_Avoid_: job, task (Lithos has its own `lithos_task_*`), tick.

**RunPlan**:
The data-driven specification a Run executes: profile, kind, date window, `skip_repair`, `skip_cache_hits`, `notify`, ledger ID, request ID. Built once per request type by the scheduler. _(proposed)_

**RunKind**:
One of `scheduled`, `manual`, `backfill`. Carried as a tag for ledger and metric labels even though behaviour is driven by the boolean flags on the RunPlan.

**RunOutcome**:
The post-execution record: `sources_checked`, `ingested`, `error`, `degraded`, `degraded_reasons`, `source_acquisition_errors`, plus the items needed for post-run notification dispatch. _(proposed)_

**Repair sweep**:
The per-Run stage that lists `influx:repair-needed` notes for the Profile and re-runs stage-specific recovery (archive re-extract, text re-extract, Tier 2, Tier 3). Skipped on backfills.

**Backfill**:
A Run over an explicit date window that skips the repair sweep, skips already-cached items, and emits no notifications. Estimates above 1000 require explicit `confirm`.

**LcmaWiring**:
The post-write step that calls `lithos_retrieve` for related notes, upserts `related_to` edges above `thresholds.lcma_edge_score`, and resolves Tier 3 `builds_on` entries via `lithos_cache_lookup` to upsert `builds_on` edges. Runs after every successful write. _(proposed as a separate collaborator of the Run module)_

**RunService**:
The collaborator that owns "build RunPlan → execute Run → dispatch notifications → record outcome" for one request. The scheduler's three entry points (scheduled tick, `POST /runs`, `POST /backfills`) become thin RunPlan builders that hand off to RunService. _(proposed)_

### Domain — Lithos integration

**Lithos**:
The downstream note store. Influx is a write-mostly client over MCP/SSE.

**LithosClient**:
Influx's MCP/SSE wrapper. Parses `lithos_write` envelopes into a **WriteResult** and owns all retry strategies internal to the write call.

**WriteResult**:
The typed outcome of `lithos_write`: `created`, `updated`, `duplicate`, `invalid_input`, `slug_collision`, `version_conflict`, `content_too_large`, or another envelope captured into `WriteResult.detail`.

**Squatter-shape dispatch**:
The recovery strategy when `lithos_write` returns `slug_collision`. Influx reads the colliding note and routes by shape: **duplicate squatter** (carries matching `arxiv-id` or `source_url`) → treat as `duplicate`; **reclaimable squatter** (empty residue from an aborted write) → delete and retry; **distinct squatter** (genuinely different paper) → suffix-retry with `[arXiv <id>]` or `[<host>]`. Anything still colliding is appended to `unresolved-slug-collisions.jsonl`.

### Operational state

**RunLedger**:
The local persistent record of Run history. Lives under `storage.state_dir` as `active-runs.json` (in-flight) plus `runs.jsonl` (terminal). Owns the `ingestion_stall` heuristic (consecutive zero-ingestion scheduled runs for the same Profile, backfills excluded). Not stored in Lithos — operational state, not knowledge.

**Degraded reasons**:
The structured list on a Run's ledger entry explaining why it was marked `degraded`. Current values: `source_acquisition` (a source-fetch error was swallowed), `ingestion_stall` (this and the prior scheduled run both ingested zero with `sources_checked > 0`).

**Health**:
The aggregate readiness state — cached probe results plus three sticky latches (`repair_write_failure`, `lcma_unknown_tool_failure`, `lithos_circuit_open`). Drives `/ready` and gates whether new Runs proceed.
_Avoid_: probes (one input to Health), readiness (one output of Health).

## Relationships

- A **Profile** has many **Runs** over time; at most one Run per Profile is active at once (enforced by the **Coordinator**).
- A **Run** consumes a **RunPlan** and produces a **RunOutcome**; its history lives in the **RunLedger**.
- A **Run**'s Acquire stage walks: **Source**.fetch_candidates → **Filter** → **Source**.acquire → **Acquired**.
- A **Run**'s Ingest stage walks: cache_lookup → **Cascade**.enrich → **Renderer** → **LithosClient**.write_note → **LcmaWiring**.wire.
- A **Cascade** consults **RepairCounters** before each tier and after counted failures.
- A **LithosClient** owns **WriteResult** parsing and **Squatter-shape dispatch** internally.
- **Health** latches are flipped by Run stages and read by the scheduler before starting a new Run.

## Example dialogue

> **Dev:** "When a backfill **Run** hits `slug_collision`, do we still try **Squatter-shape dispatch**?"
>
> **Domain expert:** "Yes — the dispatch is internal to **LithosClient.write_note**, so it runs the same way regardless of **RunKind**. What backfills skip is the **Repair sweep** and cache-hit attempts, not the write-recovery chain."
>
> **Dev:** "If **Tier 2** extraction fails three times for one Acquired, what happens on the next scheduled Run?"
>
> **Domain expert:** "**RepairCounters** sees `tier2_attempts >= cap`, the **Cascade** skips Tier 2, the **Renderer** emits the note with `influx:tier2-terminal`. Operator removes the tag manually to re-enable that stage on that note."

## Flagged ambiguities

- "task" was used for both Lithos's `lithos_task_*` tool calls and Python `asyncio.Task` background tasks. Resolved: keep "task" only for Lithos tasks; call asyncio tasks "active tasks" or "background tasks" matching the existing `active_tasks` set.
- "provider" was used for both source providers (arXiv, RSS) and OpenAI-compatible model providers. Resolved: source things are **Sources**; model things are **providers** (matching `[providers.*]` config).
- "run" overloaded historically with "tick" (scheduler firing) and "job" (CLI invocation). Resolved: a tick may dispatch many **Runs**; CLI commands either submit a Run request or perform read-only operations. There is no separate "job" concept.
