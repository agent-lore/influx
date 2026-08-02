# Influx Context

Influx ingests research and technical content for configured **Profiles**, scores it against per-Profile relevance, enriches it through a tiered cascade, and writes canonical notes into Lithos. This file pins the vocabulary the codebase and its docs should use.

Some terms below are marked _(proposed)_ — they name modules agreed during architecture grilling but not yet present in code. Drop the marker as each lands.

## Language

### Domain — content and scoring

**Profile**:
A named interest scope with its own description, score thresholds, source list, and notification rules. Almost every action in Influx is per-Profile.
_Avoid_: topic, channel, feed.

**Source**:
A place candidates come from — currently `arxiv` or `rss`/`blog`. Each Source is an adapter that exposes `fetch_candidates` (bulk per Profile) and `acquire` (per item: download, archive, extract). Both arXiv and RSS run in production through this seam; scoring is delegated to the shared **Filter** (finding 2). The manual **Inbox** intake reuses the same scorer but stays a separate fan-out rather than a Source (it scores one submitted item against many Profiles).
_Avoid_: provider (used for OpenAI-compatible model providers).

**Candidate**:
An unscored item returned from `Source.fetch_candidates` — title, source URL, identity tag (`arxiv-id`, `feed-slug`), and provider-native metadata. Becomes a **ScoredCandidate** after the **Filter** assigns it a 1–10 score.

**Filter**:
The score-gated entry to ingestion. Calls `models.filter` with the configured prompt plus negative-feedback examples and returns `{score, tags, reason}` per Candidate. Items below `thresholds.relevance` (or absent from the response) are dropped.
_Avoid_: classifier, scorer.

**Cascade**:
The score-gated enrichment pipeline that turns an **Acquired** item into **EnrichedSections**: Tier 1 summary at `score >= relevance`, Tier 2 full text at `score >= full_text`, Tier 3 deep extraction at `score >= deep_extract`.
_Avoid_: enrichment chain, tier pipeline.

**Tier 1 / Tier 2 / Tier 3**:
The three enrichment levels, each gated by a different threshold and producing different note sections. Tier 1 = `models.enrich` summary. Tier 2 = full text extraction. Tier 3 = `models.extract` claims/datasets/builds_on/open_questions/potential_connections.

**Acquired**:
The bundle a **Source** produces for one Candidate after download/archive/extract: identity, source URL, archive path (or repair flag), extracted text (or `None`), text source flavour (`html`/`pdf`/`summary-fallback`), and source-specific signals (e.g. `archive_terminal`).

**EnrichedSections**:
The **Cascade**'s output for one Acquired: optional Tier 1 result, optional full text + flavour, optional Tier 3 result, plus `repair_flags` and `terminal_flags` for the Renderer to apply as note tags.

**Renderer**:
Produces a **CanonicalNote** from an Acquired plus EnrichedSections plus the score/reason. Owns the canonical Markdown shape from spec section 9.

**CanonicalNote**:
An Influx-authored Markdown note: typed frontmatter, fixed section order (`## Archive`, `## Summary`, `## Full Text`, `## Claims`, `## Datasets & Benchmarks`, `## Builds On`, `## Open Questions`, `## Profile Relevance`, `## User Notes`), and stable tag conventions. `## User Notes` is preserved byte-exactly across rewrites.

**RepairCounters**:
Per-tier attempt counter persisted in the note's `## Repair` section. Read on tier entry (skip if `tier{N}-terminal` is set), advanced on counted (parse/validate) failures, never advanced on transient failures. Reaches the cap → adds `influx:tier{N}-terminal`. The repair sweep owns the full lifecycle (read → advance → terminal) via `Cascade.enrich`; the create path enriches with a zero counter but preserves an existing note's `## Repair` section across a multi-profile re-ingest, so caps survive.

### Domain — execution

**Run**:
One end-to-end execution of the ingestion pipeline for one Profile. Constructed from a **RunPlan** plus dependencies; produces a **RunOutcome**. Lives in `src/influx/run.py` as `Run.execute()`. Currently used by the scheduled-tick path; manual + backfill entry points migrate in #59 / #60.
_Avoid_: job, task (Lithos has its own `lithos_task_*`), tick.

**RunPlan**:
The data-driven specification a Run executes: profile, kind, date window, `skip_repair`, `skip_cache_hits`, `notify`, ledger ID, request ID. Built once per request by `RunPlan.for_request`, the single home of the RunKind → flag mapping (a backfill skips repair + cache-hit writes and suppresses notifications; every other kind runs the full pipeline and notifies).

**RunKind**:
One of `scheduled`, `manual`, `backfill`, `inbox`. Carried as a tag for ledger and metric labels even though behaviour is driven by the boolean flags on the RunPlan. `inbox` marks the per-(item, Profile) Runs an InboxTick dispatches; it is excluded from the scheduled-only stall heuristics.

**RunOutcome**:
The post-execution record: `sources_checked`, `ingested`, `error`, `degraded`, `degraded_reasons`, `source_acquisition_errors`, plus the items needed for post-run notification dispatch.

**Repair sweep**:
The per-Run stage that lists `influx:repair-needed` notes for the Profile and re-runs stage-specific recovery (archive re-extract, text re-extract, Tier 2, Tier 3). Skipped on backfills.

**Backfill**:
A Run over an explicit date window that skips the repair sweep, skips already-cached items, and emits no notifications. Estimates above 1000 require explicit `confirm`.

**LcmaWiring**:
The post-write step that calls `lithos_retrieve` for related notes, upserts `related_to` edges above `thresholds.lcma_edge_score`, and resolves Tier 3 `builds_on` entries via `lithos_cache_lookup` to upsert `builds_on` edges. Runs after every successful write. _(proposed as a separate collaborator of the Run module)_

**RunService**:
The collaborator that owns "build RunPlan → execute Run → dispatch notifications → record outcome" for one request. The scheduler's three entry points (scheduled tick, `POST /runs`, `POST /backfills`) route through `scheduler.run_profile`, which builds the RunPlan via `RunPlan.for_request` and hands it to RunService. Lives in `src/influx/run_service.py` as `RunService.execute()`.

**RunDispatcher**:
The request-orchestration collaborator that turns an admin `POST /runs` or `POST /backfills` request into background Runs. Acquires the per-Profile Coordinator locks all-or-nothing (any busy Profile releases everything acquired so far and rejects the request — no partial fan-out), launches the Run(s) as tracked background tasks so the response returns immediately, registers them on the shutdown-grace set so `InfluxService.stop` can drain them, and releases the locks. Kind-agnostic — manual runs and backfills share one lock lifecycle and one fan-out path — so the HTTP router stays thin translation, turning a `RunAccepted` / `RunRejectedBusy` outcome into a `202` / `409`. Lives in `src/influx/run_dispatch.py`.
_Avoid_: for the single per-(item, Profile) inbox dispatch use InboxTick, not RunDispatcher.

### Domain — Inbox (manual submission)

**InboxTask**:
A Lithos task tagged `influx:inbox` carrying submission metadata. `kind="url"` (`url` + `submitted_by`, optional `title`/`summary`/`source_tag`) submits a web URL; `kind="pdf"` (v2 — `local_path` + `submitted_by`) submits a PDF already on the Influx host under `[inbox] pdf_root`. Created by external agents via `lithos_task_create`; consumed by the InboxTick. See `docs/plans/inbox.md`.

**Local-PDF submission** (v2):
A `kind="pdf"` InboxTask whose `local_path` resolves under `[inbox] pdf_root` is read once, identified by SHA-256 of its bytes (synthetic `source_url = inbox-pdf:sha256:<hex>`, so identical bytes dedup to one note), archive-copied to `archive_root/inbox-pdf/YYYY/MM/<sha256>.pdf`, extracted via `extract_pdf`, then ingested through the same per-Profile fan-out as a URL item. Unsafe / absent paths complete terminally (`pdf_root_not_configured` / `path_not_in_pdf_root` / `file_missing`). `acquire_inbox_pdf` (`src/influx/sources/inbox.py`) is the local-file sibling of `acquire_inbox_bytes`.

**InboxTick**:
One execution of the inbox-tick scheduler entry (`influx-inbox-tick`, registered only when `[inbox] enabled`). Claims pending InboxTasks, acquires each item once (URL fetch or local PDF read), scores it against every enabled Profile, and dispatches a real single-Profile `RunKind.INBOX` Run for each Profile that clears threshold (merging into one canonical note), then completes the task with an outcome string + `cited_nodes`. NOT itself a `Run` — an orchestrator above the Run layer. Lives in `src/influx/inbox.py` as `InboxTick.execute()`.

**Submitter**:
The external agent that creates an InboxTask, identified by the `submitted_by` metadata field (sanitised into a `submitter:<id>` note tag).

**Cache-hit replay**:
On a resubmitted URL the InboxTick re-scores only the *complement* of Profiles — enabled minus those already in the note's `## Profile Relevance` minus operator-suppressed (`influx:rejected:<profile>`) — so a Profile skipped earlier (e.g. busy) is picked up on a later tick. Best-effort: a read/parse failure falls back to replaying all Profiles (the write merge dedupes).

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
The local persistent record of Run history. Lives under `storage.state_dir` as `active-runs.json` (in-flight) plus `runs.jsonl` (terminal). Owns the `ingestion_stall` heuristic (consecutive zero-ingestion scheduled runs for the same Profile; non-scheduled kinds — backfill, inbox — excluded). Not stored in Lithos — operational state, not knowledge.

**Degraded reasons**:
The structured list on a Run's ledger entry explaining why it was marked `degraded`. Current values: `source_acquisition` (a source-fetch error was swallowed), `ingestion_stall` (this and the prior scheduled run both ingested zero with `sources_checked > 0`).

**Health**:
The aggregate readiness state — cached probe results plus three sticky latches (`repair_write_failure`, `lcma_unknown_tool_failure`, `lithos_circuit_open`). Drives `/ready` and gates whether new Runs proceed.
_Avoid_: probes (one input to Health), readiness (one output of Health).

## Relationships

- A **Profile** has many **Runs** over time; at most one Run per Profile is active at once (enforced by the **Coordinator**).
- A **Run** consumes a **RunPlan** and produces a **RunOutcome**; its history lives in the **RunLedger**.
- A **Run**'s Acquire stage walks: **Source**.fetch_candidates → **Filter** → pre-acquire `lithos_cache_lookup` → **Source**.acquire → **Acquired**. The pre-acquire `cache_lookup` is the primary `compose_dedup_query` lookup (title + first sentence of summary, #125). Backfill cache hits short-circuit before `Source.acquire`, so duplicate items skip the download / archive / extract cost; normal-run hits still acquire so the multi-profile merge path inside `LithosClient.write_note` runs.
- A **Run**'s Ingest stage walks: **Cascade**.enrich → **Renderer** → **LithosClient**.write_note → **LcmaWiring**.wire. On items the Acquire stage flagged as cache misses, Ingest also runs a defensive exact-`source_url` fallback (#128) before write — catching notes whose `source_url` is already in Lithos but whose title/first-sentence abstract drifted between runs, so they never silently fall through to a write-time `duplicate` rejection.
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
> **Domain expert:** "**RepairCounters** sees `tier2_attempts >= cap`, the **Cascade** skips Tier 2, the **Renderer** emits the note with `influx:tier2-terminal`, and the note drops out of the sweep set. Re-arming that stage takes three edits: reset the counter below the cap, remove the terminal tag, re-add `influx:repair-needed`. The counter is the authoritative gate — the **Cascade** checks it before calling the extractor, so removing the tag alone gets you a sweep that attempts nothing and re-terminates."

## Flagged ambiguities

- "task" was used for both Lithos's `lithos_task_*` tool calls and Python `asyncio.Task` background tasks. Resolved: keep "task" only for Lithos tasks; call asyncio tasks "active tasks" or "background tasks" matching the existing `active_tasks` set.
- "provider" was used for both source providers (arXiv, RSS) and OpenAI-compatible model providers. Resolved: source things are **Sources**; model things are **providers** (matching `[providers.*]` config).
- "run" overloaded historically with "tick" (scheduler firing) and "job" (CLI invocation). Resolved: a tick may dispatch many **Runs**; CLI commands either submit a Run request or perform read-only operations. There is no separate "job" concept.
