# Influx Inbox — Manual Submission Pipeline

Status: **v1 Shipped** (slices #195 fan-out groundwork → #198 operational surface)
Date: 2026-05-09 (design) · v1 landed 2026-06

Forward spec for a manual-submission pathway into the existing Influx ingestion pipeline. Submitting agents (e.g. daily-report agents) hand Influx a URL; Influx treats the item the way it treats RSS-discovered candidates — runs the same Filter, Cascade, Renderer, write, and LCMA wiring — except submitters don't pick a Profile.

v1 is implemented (default-off `[inbox]` block; opt in to enable). This document remains the design source of truth; `docs/SPECIFICATION.md` describes the shipped behaviour. v2 (local PDF, §16) is now implemented (opt in via `[inbox] pdf_root`).

**Scope:** v1 covers URL submission; v2 adds local-PDF submission (`kind="pdf"` + `local_path` under `pdf_root`). See §16 for the v2 design.

---

## 1. Goals & Non-Goals

### 1.1 Goals (v1)

1. **Agent-driven intake**: Submitting agents add candidate URLs to Influx without knowing about Profiles, thresholds, or any Influx-internal API.
2. **Pipeline reuse**: Submitted items flow through the existing Filter → Cascade → Renderer → LithosClient → LcmaWiring stages with no parallel pipeline.
3. **Multi-profile fan-out**: Each item is scored against every enabled Profile; every Profile clearing its own threshold contributes to one shared canonical note via existing `_merge_profile_relevance_in_content` machinery.
4. **Closed feedback loop**: Submitters can read back the outcome (which Profiles ingested, scores, canonical note ID, rejection reasons) without polling Influx directly.
5. **Operationally visible**: Inbox processing surfaces in the run ledger, metrics, and `/status` distinctly from scheduled per-Profile work.
6. **Minimal surface change to existing per-Profile model**: Reuse the existing single-Profile `Run`, `RunLedger`, `RunService`, `Coordinator`, `ProfileRunResult`, and `/status` shapes; do not generalize them to multi-Profile.

### 1.2 Non-Goals (v1)

1. **No Influx HTTP intake**: Submission is via Lithos task only. No new `POST /inbox` endpoint.
2. **No bypass of the Filter**: Submitters cannot force ingestion. Items below the per-Profile relevance threshold are dropped, exactly as RSS-discovered items would be.
3. **No submitter Profile selection**: Submitters cannot hint a Profile. Multi-profile fan-out is the model.
4. **No new CLI subcommand on `influx`**: Submission uses the existing Lithos MCP surface; the operator helper is a separate script under `scripts/`.
5. **No local PDF support**: URLs only. PDF support is a planned v2 addition (§16). A URL pointing at a publicly-accessible PDF (e.g. `https://arxiv.org/pdf/...`) DOES work in v1 because the existing `download_archive` + `extract_pdf` cascade handles content-type-based branching.
6. **No persisted rejection memory**: A Profile that filter-rejects a candidate has no record on the canonical note. Resubmission re-filters all not-yet-ingested Profiles. See §6.
7. **No coordinator wait-and-renew primitives**: The Coordinator's existing non-blocking `try_acquire` is the only lock primitive used; no new blocking-acquire-with-timeout API. See §10.
8. **No multi-Profile generalization of `Run` / `RunLedger` / `ProfileRunResult` / `/status`**: Each (item, Profile) ingestion is a real existing single-Profile Run. The inbox tick is a scheduler-side orchestrator above the Run layer, not a new Run shape.

---

## 2. Vocabulary additions

These extend the vocabulary defined in `CONTEXT.md`. v1 has landed (slices #195–#198); these entries are now also recorded in `CONTEXT.md` proper.

**InboxTask**:
A Lithos task tagged `influx:inbox` carrying submission metadata. Created by external agents via `lithos_task_create`; consumed by Influx's inbox tick. Each task represents one candidate URL.

**InboxTick**:
One execution of the inbox-tick scheduler entry. Claims pending InboxTasks, fans out per-(item, Profile) ingestion across enabled Profiles, dispatches each ingestion as a real single-Profile Run, and writes outcomes back via `lithos_task_complete`. NOT itself a `Run` — it is an orchestrator above the Run layer.

**Submitter**:
The external agent that creates an InboxTask. Identified by the `submitted_by` metadata field.

---

## 3. Submission contract

### 3.1 Lithos task shape

Submitters call `lithos_task_create` with:

| Field | Value |
|-------|-------|
| `title` | `Influx inbox: <human-readable item title or URL>` |
| `agent` | Submitter identifier (free-text, e.g. `daily-report:ai-news`) |
| `tags` | MUST include `influx:inbox`. Other tags are preserved verbatim and ignored by Influx. |
| `description` | Optional free-text rationale ("from today's AI news scan"). Not parsed. |
| `metadata` | See §3.2 |

### 3.2 Metadata schema (v1)

Required fields:

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"url"` | Discriminator. Reserved enum so v2 can add `"pdf"` without breaking submitters. |
| `submitted_by` | string | Submitter agent identifier. Populates `provenance_actor` on resulting LCMA edges and the `## Profile Relevance` reason. |
| `url` | string | Canonical web URL. Influx normalises via `influx.urls.normalise_url` and hashes via `url_hash`. |

Optional fields:

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Hint for the candidate's `title` slot. Used as fallback if HTML extraction can't recover one. |
| `summary` | string | Pre-fetched summary or excerpt. Used as the candidate's `abstract` for the Filter prompt, saving an extract round-trip when reliable. |
| `source_tag` | string | Sets the resulting note's `source:*` tag. Defaults to `"inbox"`. In v1 this does **not** control archive layout; it is validated as a conservative slug (`^[a-z0-9][a-z0-9-]{0,31}$`) and is used for note tagging only. |

Explicitly absent from the contract:
- No `profile` hint field. Multi-profile fan-out is the model.
- No `priority` field.
- No `force` / `bypass_filter` field.
- No `notify` override.
- No score / threshold / notification configuration.

### 3.3 Tag convention

Influx polls `lithos_task_list(tags=["influx:inbox"], status="open")`. Single tag is sufficient; the `kind` enum exists for forward-compatibility with v2 PDF support, not to control discovery.

---

## 4. Prerequisites

This feature depends on Influx-side `LithosClient` wrappers that do not exist today. The underlying lithos MCP tools all already exist on the lithos server; the prerequisite is purely Influx-side wrapping.

### 4.1 New `LithosClient` methods

Add to `src/influx/lithos_client.py`. Each wraps an existing lithos MCP tool with the same retry/error semantics as the existing `task_create` and `task_complete` wrappers.

| Method | Wraps | Notes |
|--------|-------|-------|
| `task_list(tags, status, limit)` | `lithos_task_list` | Returns the open inbox tasks. |
| `task_claim(task_id, agent, aspect)` | `lithos_task_claim` | Aspect-locked claim. Suggested `aspect="ingest"`. |
| `task_renew(task_id, agent, aspect)` | `lithos_task_renew` | Lease renewal. **NOT used in v1** under the §10 try-acquire-skip model; included here only because v2 may need it. |
| `task_update(task_id, agent, metadata)` | `lithos_task_update` | Used to attach the structured `inbox_result` payload before completion (§7.3). |

### 4.2 Corrected `cache_lookup` call shape

The existing `cache_lookup` (lithos_client.py:548) requires BOTH `query` and `source_url` and raises `LithosError("missing_lookup_arg")` if either is absent. Inbox callers MUST supply both. Recommended call:

- `query` = the candidate's title (or, if absent, the URL itself)
- `source_url` = the normalised candidate URL

### 4.3 No new lithos-side work

`lithos_task_list`, `lithos_task_claim`, `lithos_task_renew`, `lithos_task_update`, and `lithos_task_cancel` already exist in `lithos/src/lithos/server.py` (around lines 3116, 2746, 2803, 2689, 3010). No lithos changes are required for v1.

---

## 5. Execution model

### 5.1 No new RunKind enum value for inbox-as-batch

Each ingestion an inbox tick triggers is a real, existing single-Profile `Run`. To distinguish inbox-driven Runs from scheduled/manual/backfill in the ledger, metrics, and notifications, add `RunKind.INBOX` to `influx.coordinator.RunKind` — but use it ONLY to tag per-(item, Profile) Runs. There is no batch-level "inbox tick Run."

### 5.2 The InboxTick orchestrator

A new scheduler entry — independent of the per-Profile cron — fires the inbox tick on its own cadence:

```toml
[inbox]
enabled = false                       # default off; opt-in
poll_cron = "*/5 * * * *"             # 5 min
max_items_per_tick = 20
task_tag = "influx:inbox"             # constant; exposed for ops only
agent_id = "influx-inbox"             # used in lithos_task_claim/complete/update calls
```

Each inbox tick performs:

1. **List pending InboxTasks**: `LithosClient.task_list(tags=[task_tag], status="open", limit=max_items_per_tick)`.
2. **Claim each**: `LithosClient.task_claim(task_id=…, agent=agent_id, aspect="ingest")`. Failed claims (already claimed by another influx instance) are silently skipped.
3. **Per-item processing** (§5.3) — for each claimed task, fan out to per-(item, Profile) inbox dispatches.
4. **Per-tick metrics tick** — emit `inbox_items_processed` per-item, no batch-level ledger record.

The InboxTick is NOT a `Run`. It does not call `RunLedger.start()` for itself, does not appear in `/runs/recent`, does not produce a `ProfileRunResult`. It is purely an orchestrator above the Run layer.

### 5.3 Per-item processing

For one claimed InboxTask:

1. **Cache lookup**: `LithosClient.cache_lookup(query=<title or url>, source_url=<normalised_url>)` — both arguments required (§4.2). If hit → §6.
2. **Acquire (once per item)**: download URL via existing `download_archive` machinery, extract HTML or PDF text via existing extraction cascade based on response content-type. Bytes are reused across all per-Profile filter calls below.
3. **Filter fan-out**: in parallel for each enabled Profile, call `models.filter` with that Profile's description, prompt, and negative examples. Submitter-provided `title`/`summary` populate the candidate's title/abstract slots when extraction yields nothing better.
4. **Per-Profile inbox dispatch**: for each Profile that scored above its `relevance` threshold, dispatch a real single-Profile execution unit with `RunKind.INBOX` and a synthetic single-item provider feeding only this candidate.

   **Important seam clarification:** v1 does **not** reuse `Run.execute()` literally from its current stage-1/2/3 entrypoint. The current Run owns `Acquire = Source.fetch_candidates → Filter.score → Source.acquire` and `Ingest = cache_lookup → Cascade → Renderer → write → LCMA`. Inbox has already done URL-level acquire and per-Profile filtering before it reaches this step, so it needs an explicit internal seam that starts from an already-filtered, already-acquired single item.

   Concretely, implementation must introduce one of these equivalent forms and pick one explicitly:

   - a new Run/RunService entrypoint for a pre-acquired single item, or
   - a lower-level shared execution helper reused by both the existing Run and inbox dispatch.

   The first such inbox dispatch for this URL creates the canonical note; subsequent dispatches (for additional scoring Profiles on the same URL) hit `slug_collision` → duplicate-squatter → merge via `_merge_profile_relevance_in_content`.
5. **Outcome aggregation**: collect per-Profile Run outcomes (ingested / failed / score), build the inbox_result payload (§7), call `LithosClient.task_update(task_id, agent_id, metadata={"inbox_result": …})` and `LithosClient.task_complete(task_id, agent_id, outcome=…, cited_nodes=…)`.

### 5.4 Per-Profile execution shape under inbox

Inbox-dispatched executions are real per-Profile operational units and produce real `RunLedger` entries with `kind="inbox"` and `profile=<the_profile>`. This means:

- One InboxTask scoring in 3 Profiles produces 3 ledger entries, one per Profile.
- `/runs/recent` shows them as 3 separate per-Profile entries with `kind="inbox"`.
- Per-Profile notifications fire as normal (subject to `notify_on=["inbox"]` opt-in, §9).
- `Coordinator` interaction is per-Profile, exactly the same as scheduled/manual Runs (§10).
- `RunOutcome` per Profile is the existing `ProfileRunResult` shape — no changes.

The "tick boundary" exists only in metrics + `/status` and in timestamp clustering in `/runs/recent`. There is no per-tick ledger record.

The document intentionally says "execution" rather than assuming the current `Run.execute()` can be called unchanged. The implementation may reuse RunService/Run internals, but the inbox path must start from the pre-acquired single-item seam described in §5.3, not from the ordinary fetch/filter entrypoint.

### 5.5 Failure isolation

Per-item, per-Profile isolation:

- A filter timeout for Profile A on item 7 doesn't fail item 7 — proceeds with the remaining Profiles' scores, ingests where they matched, reports the partial result in the outcome string.
- A failure on item 7 doesn't fail items 8–20.
- A whole-tick failure (e.g. lithos circuit opens mid-tick) leaves un-completed task claims to expire naturally via lithos's task lease semantics; next tick re-claims any that re-open.

### 5.6 No auto-retry on terminal failure

InboxTasks that complete with `outcome="error"` are NOT re-claimed by a future tick. Submitters resubmit if they want. This avoids the poison-item failure mode where a malformed item burns inbox quota every tick.

Transient retries inside the existing per-call retry machinery (e.g. `arxiv_429_backoff_seconds` for filter-model rate limits) still apply — "no auto-retry" means "no inbox-level retry after the per-call retries exhaust."

---

## 6. Cache-hit behaviour

When `LithosClient.cache_lookup(query=…, source_url=…)` returns an existing note, Influx:

1. Parses the existing `## Profile Relevance` section to identify Profiles that have **ingested** this URL (i.e. Profiles whose names appear as `ProfileRelevanceEntry` in the rendered list).
2. Identifies Profiles tagged `influx:rejected:<profile>` on the existing note — operator-applied suppression. These are also skipped.
3. Filters the candidate against the **complement set** — every other enabled Profile, including Profiles that previously filter-rejected this URL.
4. For each newly-scoring Profile (above threshold), dispatches a per-Profile Run as in §5.3 step 4 — the existing merge logic adds the new Profile's relevance entry.
5. Reports the cache-hit case in the outcome string (§7.1).

### 6.1 No persisted filter-rejection memory in v1

There is no record on the canonical note that Profile X scored this URL at 4 below threshold 7 last week. Re-submission re-filters Profile X. Cost on resubmission is bounded: one filter call per not-yet-ingested-and-not-suppressed Profile per resubmission. With cheap filter models this is acceptable.

The `influx:rejected:<profile>` tag mechanism (renderer.py:398, renderer.py:452) is operator-applied retroactive suppression — a different concept from auto-recorded filter rejection. v1 does NOT auto-emit `influx:rejected:<profile>` after a low-score filter result; reusing that tag for two semantically different purposes is explicitly avoided.

If filter-resubmit cost shows up in metrics in production, persisted rejection memory is a v1.5 addition. Out of scope for v1.

---

## 7. Outcome reporting

Each InboxTask is completed via `LithosClient.task_complete` with outcome data assembled from the per-Profile Runs' results.

### 7.1 Outcome string (free-text)

Human-readable, surfaces in the lithos task UI. Conventions:

- `ingested into 2 profile(s): ai-robotics, web-tech`
- `ingested into 1/2 profiles (web-tech); ai-robotics filter failed: timeout`
- `filtered out: top score 4 (ai-robotics) below threshold 7`
- `cache_hit: existing note <slug>; added 1 profile entry`
- `cache_hit: existing note <slug>; no new profiles matched`
- `fetch failed: HTTP 404`
- `extract failed: PDF too short (<min_web_chars>)`

### 7.2 `cited_nodes`

The Lithos node IDs of the canonical note(s) created or updated. With multi-profile merge into one canonical note, this is usually a single ID. Lets the submitter (or downstream LCMA consolidation) link directly to the result without parsing the outcome string.

### 7.3 Structured `metadata` (via `LithosClient.task_update`)

Before completion, Influx attaches a structured `inbox_result` object to the task:

```json
{
  "inbox_result": {
    "per_profile": {
      "ai-robotics": {"score": 8, "ingested": true, "note_id": "...", "run_id": "..."},
      "web-tech":    {"score": 4, "ingested": false, "reason": "below_threshold"}
    },
    "source_url": "https://example.com/article",
    "archive_path": "blog/2026/05/abc...html",
    "processing_time_ms": 1234
  }
}
```

The `run_id` per Profile lets a submitter cross-reference back to `/runs/recent` for the actual per-Profile Run record.

### 7.4 `misleading_nodes`

Always unset. Inbox processing has no signal about misleading-ness.

---

## 8. Profile fan-out and the Filter

### 8.1 Filter call shape

Same `models.filter` model, same `prompts.filter` template, same `negative_example_max_title_chars` truncation as scheduled per-Profile filtering. The prompt is constructed per Profile with that Profile's `{profile_description}` and `{negative_examples}`.

### 8.2 Cost shape

Each inbox item produces up to N filter calls where N is the number of enabled Profiles minus those already ingested or suppressed (§6). With the default `models.filter` model (`gpt-4.1-mini` in the example config), this is cheap but not free. The `[inbox] max_items_per_tick = 20` cap bounds per-tick filter spend at `20 × N`.

This is a new load shape for the filter. Scheduled ticks today do `items_per_profile × 1` filter calls per Profile because each Profile fetches its own candidates; inbox inverts that to `1 × N` per item.

---

## 9. Notifications

The inbox-driven Runs are tagged distinctly so existing notification configs aren't surprised:

- New `notify_on` value `"inbox"`. Webhook configs that want to receive inbox notifications opt in by listing `"inbox"` in their `notify_on` array.
- Existing webhook configs (which list only `"scheduled"`, `"manual"`, etc.) remain silent on inbox runs — backwards-compatible.
- Per-Profile `notify_immediate` thresholds apply unchanged. Each per-(item, Profile) inbox Run produces a `ProfileRunResult` and goes through the existing notification pipeline.

Webhook `event_mode = "article"` fires per ingested item; `event_mode = "digest"` fires the per-Run digest as it would for any other Run kind.

---

## 10. Coordinator interaction

### 10.1 try-acquire only; skip-this-Profile-this-tick on contention

The existing `Coordinator` provides only non-blocking `try_acquire` and fail-fast `hold` (coordinator.py:54, 87). v1 uses these primitives unchanged.

For each per-(item, Profile) ingestion the InboxTick wants to dispatch:

1. The per-Profile Run is started exactly like a scheduled Run — through `RunService.execute(plan)`, which is wrapped by `Coordinator.hold(profile)`.
2. If the Profile's lock is held (a scheduled Run is in flight, or another inbox-driven Run for the same Profile is mid-write), `hold(profile)` raises `ProfileBusyError`.
3. The InboxTick catches `ProfileBusyError` and **skips this Profile for this item this tick**. The other scoring Profiles for the same item proceed as normal.
4. If at least one scoring Profile was successfully dispatched this tick, the InboxTask completes normally with whatever results were produced. The skipped Profile is NOT recorded in `## Profile Relevance` (it never ingested), so the next inbox tick's cache-hit replay (§6) will re-score it and try again.

### 10.2 No blocking wait, no `task_renew` loop

The earlier draft proposed a blocking-wait-and-renew model. That required new Coordinator primitives (blocking acquire with timeout, owner metadata, cancellation) that don't exist. v1 sidesteps the entire problem by not blocking — the cache-hit replay in §6 picks up the work on a future tick.

Worst-case latency / semantics for an inbox item targeting a busy Profile:

- The item completes this tick with the non-busy Profiles satisfied.
- The submitter sees a partial outcome (§7.1: `ingested into 1/2 profiles; web-tech profile_busy`).
- If the submitter doesn't resubmit and the task is already completed, the busy Profile never picks up this item — that is an explicit v1 product tradeoff, not an implementation accident.
- If the submitter resubmits later (or another tick re-claims a never-completed task), the next tick's cache-hit path scores the previously-busy Profile against the existing note and merges if it clears threshold.

To avoid surprising submitters, the outcome string in the partial-skip case explicitly lists the skipped Profiles so resubmission is an informed choice.

This tradeoff is accepted for v1 because it avoids inventing new Coordinator waiting primitives, task-lease renewal loops, and fairness rules in the same feature. If production use shows that "submitter must resubmit to satisfy a busy Profile" is unacceptable UX, a later version can add blocking wait + renew semantics as a deliberate follow-up feature.

### 10.3 No `lithos_task_renew` in v1

Because no inbox path blocks, lease renewal is unnecessary. The `LithosClient.task_renew` wrapper is added in §4.1 as a v2 stub, not used in v1.

---

## 11. Repair, backfill, ingestion-stall

### 11.1 Repair sweep

Inbox-spawned notes are tagged identically to scheduled-spawned notes (e.g. `influx:repair-needed` when applicable). The per-Profile repair sweep picks them up by tag with no special-casing. No changes to `RepairCounters` or `repair.py`.

### 11.2 Backfill

Inbox has no date window. `RunKind.BACKFILL` does not apply. There is no `inbox-backfill` concept. `RunPlan.skip_repair`, `skip_cache_hits`, and `notify` for inbox-dispatched per-Profile Runs are: `skip_repair=False`, `skip_cache_hits=False`, `notify=True`.

### 11.3 Ingestion-stall heuristic

The existing `ingestion_stall`, `fetch_stall`, and related per-Profile stall heuristics are evaluated on `RunKind.SCHEDULED` Runs only. Inbox per-Profile Runs (with `RunKind.INBOX`) are excluded — an inbox Run that filters out is not a stall, just a low-score candidate.

---

## 12. Probes and health gating

No new probe latch. Inbox Runs gate on the existing two latches:

- `lithos_circuit_open`: lithos SSE health. If lithos is unreachable, the InboxTick skips its run (no list/claim/dispatch), same observability as scheduled `runs_skipped{reason="lithos_unhealthy"}`.
- `lcma_tools_unavailable`: LCMA tools present. Still applies to per-Profile inbox Runs because they go through `LcmaWiring` after write.

The `lithos_task_*` tools are core MCP surface and are present whenever lithos is alive — no separate probe required.

---

## 13. Operational surface

### 13.1 Archive layout

URL-submitted items use a fixed inbox-controlled archive layout:

```
archive_root/inbox/YYYY/MM/<url_hash>.html
```

The submitter's `source_tag` (default `"inbox"`) affects only the resulting note's `source:<tag>` metadata. It does **not** select an archive subtree in v1.

Rationale:

- archive layout is operational state owned by Influx, not submitters
- keeping the subtree fixed avoids mixing user-provided metadata with on-disk path selection
- it removes the need to answer whether inbox `source_tag` is as trusted as config-authored RSS `source_tag`

Validation rule for `source_tag` in v1: accept only conservative slug values matching `^[a-z0-9][a-z0-9-]{0,31}$`; otherwise complete the task terminally with `outcome="error: invalid_source_tag"`.

### 13.2 Note tags

The canonical note carries:

- `source:<tag>` — from the submitter's `source_tag`, defaulting to `source:inbox`
- `profile:<name>` — one per scoring Profile, union-merged via existing `notes.merge_tags`
- `submitter:<id>` — derived from `submitted_by` for the first ingestion; preserved across merges

### 13.3 Slug-collision suffix

When `slug_collision` is returned and the colliding note is a distinct item, the suffix-retry uses `[inbox]`, matching the shape of the existing `[arXiv <id>]` and `[<host>]` suffixes.

### 13.4 Run ledger

Inbox per-(item, Profile) Runs use the existing `RunLedger.start(profile, kind="inbox", …)` shape unchanged. `sources_checked=1`, `ingested ∈ {0, 1}` per Run. The existing `RunEntry` fields all apply per Profile.

There is NO inbox-tick-level ledger record. The InboxTick orchestrator does not call `RunLedger.start()` for itself.

To find "all inbox activity in the last 5 minutes" an operator filters `runs.jsonl` by `kind="inbox"` and timestamp range — no new query shape required.

### 13.5 Metrics

Existing `run_starts`, `run_completions`, `run_duration` get a new `run_type="inbox"` value alongside existing labels. The `runs_skipped` metric similarly gets `run_type="inbox"` for circuit-open and LCMA-unavailable skips of inbox-dispatched Runs.

New tick-level metrics emitted by the InboxTick orchestrator (NOT by Runs):

```
inbox_tick_started{}                                       # counter, ticks/min
inbox_tasks_listed{}                                       # gauge, set per tick
inbox_tasks_claimed{}                                      # counter, claims/tick
inbox_items_processed{outcome="ingested"|"filtered_out"|"cache_hit"|"profile_busy_skipped"|"error"}   # counter
```

The `profile_busy_skipped` outcome captures the §10.1 skip case for observability.

### 13.6 `/status` endpoint

Adds an `inbox` section:

```json
{
  "inbox": {
    "enabled": true,
    "pending": 7,
    "in_flight": 2,
    "last_tick_at": "2026-05-09T12:35:00Z",
    "last_tick_outcome": "success"
  }
}
```

`pending` comes from `LithosClient.task_list(tags=["influx:inbox"], status="open")`. The read is cached on the same tick the probe loop refreshes other `/status` data — `/status` MUST NOT issue a fresh `lithos_task_list` per request (consistent with FR-HTTP-7).

The existing per-Profile `/status` last-run state continues to compute as today (http_api.py:184). Inbox-driven Runs land in those per-Profile fields naturally because they ARE per-Profile Runs.

### 13.7 `/runs/recent`

No structural change. Inbox per-Profile Runs appear with `kind="inbox"` alongside `scheduled`, `manual`, and `backfill` entries.

### 13.8 Agent identity

InboxTick-side `LithosClient.task_*` calls (list, claim, update, complete) MUST use `agent="influx-inbox"`. Distinct from `agent="influx"` used by scheduled-Run task tracking. Distinguishability matters for the lithos LCMA learning loop — inbox-driven `cited_nodes` feedback should not blur with scheduled-Run feedback in per-agent reinforcement.

---

## 14. Configuration summary

New `[inbox]` block in `influx.toml`. Default-disabled — existing deployments are unaffected unless the operator opts in.

```toml
[inbox]
enabled = false                       # default off
poll_cron = "*/5 * * * *"             # 5-minute tick
max_items_per_tick = 20
agent_id = "influx-inbox"
# task_tag = "influx:inbox"           # constant; not operator-tunable
```

`enabled=false` is the default to preserve backwards compatibility with current configs.

---

## 15. Human submission helper script (v1)

Agents create InboxTasks directly via Lithos MCP. Humans don't. A helper script under `scripts/` provides a convenient CLI wrapper.

### 15.1 Entrypoint

`scripts/influx-inbox-submit.py` — same conventions as the existing `scripts/influx-diagnose.py` and `scripts/influx-report.py` (Python `__main__`, module docstring, argparse, env loaded from `docker/.env.<env>`).

### 15.2 Behaviour

The script takes a single positional argument, auto-detected by shape: an `http(s)://` URL submits `kind="url"`; any other value is treated as a local PDF path and submits `kind="pdf"` (v2 §16). A URL-shaped value with a non-http scheme (`file:`, `ftp:`, …) is rejected. For a local PDF the script validates the file, resolves `[inbox] pdf_root`, stages the file into it (§16.6), and sends the in-`pdf_root` `local_path`. It then creates the task via Lithos MCP.

```
influx-inbox-submit https://example.com/article
influx-inbox-submit https://arxiv.org/pdf/2401.12345.pdf --title "Some Paper Title"
influx-inbox-submit ./papers/attention-is-all-you-need.pdf --title "Attention Is All You Need"
```

A URL pointing at a PDF also works (the URL path's content-type routing handles it); `kind="pdf"` is for PDFs that are only on the local filesystem.

### 15.3 Optional flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--title <text>` | absent | Hint for the candidate's title slot |
| `--summary <text>` | absent | Pre-fetched summary for the Filter prompt |
| `--summary-file <path>` | absent | Read summary from a file (alternative to `--summary` for long text) |
| `--source-tag <tag>` | `inbox` | Sets the resulting note's `source:*` tag |
| `--submitted-by <id>` | `manual:<username>` | Submitter agent identifier; defaults to a `manual:` prefix plus the OS username |
| `--env <name>` | `staging` | Selects `docker/.env.<name>` for config resolution |
| `--pdf-root <path>` | from config | Override `[inbox] pdf_root` for staging a local PDF |
| `--dry-run` | false | Print the task body that would be sent (no copy, no MCP call) |

### 15.4 Output

On success, prints the created task ID and a one-line summary:

```
Created task task_abc123 (kind=url)
Track outcome: lithos task show task_abc123
```

The trailing line points the human at the Lithos CLI (or equivalent) for checking back on the outcome — keeps the script focused on submission and avoids re-implementing task-status display.

### 15.5 Posture

- Read-only against the Influx service surface — no HTTP calls to Influx itself.
- Write-only against Lithos — single `lithos_task_create` MCP call.
- No `lithos_task_update` / `_complete` calls; that lifecycle belongs to Influx.
- Exits non-zero on validation failure (URL malformed, missing scheme) BEFORE making any MCP call.

---

## 16. v2 — Local PDF support (shipped)

PDF support shipped as v2 (opt in via `[inbox] pdf_root`); it is purely additive — no v1 behaviour changed. The design below describes the implemented behaviour.

### 16.1 v2 goal

Allow submitters to ingest PDF documents already on the Influx host filesystem (typically arxiv papers downloaded by an operator, conference papers received by email, or non-public research papers that cannot be fetched via URL). The v1 "submit a public PDF URL" path remains the right answer when the PDF is publicly accessible.

### 16.2 v2 contract additions

The metadata `kind` enum extends to accept `"pdf"`. New metadata fields:

| Field | Type | Description |
|-------|------|-------------|
| `local_path` | string | Required when `kind="pdf"`. A path on the Influx host that MUST resolve inside `[inbox] pdf_root`. An absolute path is used as-is; a relative path is resolved **against `pdf_root`** (not the server's CWD), so `papers/foo.pdf` works regardless of where the service was started. |

The `url` field becomes mutually exclusive with `local_path` based on `kind`. v1 submitters that send `kind="url"` are unaffected.

### 16.3 v2 path trust model

A new `[inbox] pdf_root` config setting names a directory under which all submitted `local_path` values must resolve. A relative `local_path` is anchored to `pdf_root`; an absolute one is taken as-is. Influx canonicalises the result (`Path.resolve()`, which also collapses `..` and follows symlinks) and rejects anything not under `pdf_root` with a terminal `outcome="error: path_not_in_pdf_root"`.

This mirrors the existing security posture (`security.allow_private_ips=false`, SSRF-guarded HTTP fetch) on the file-system side.

Single-host setups only — submitter and Influx must share the same `pdf_root` directory. Cross-host filesystem semantics out of scope.

### 16.4 v2 identity, synthetic source URL, archive copy

- **Identity**: SHA-256 of the file's byte content. Path-based identity is wrong (same file under two filenames would otherwise create two notes); content-hash identity also auto-deduplicates "submitter sent the same paper twice."
- **Synthetic `source_url`**: `inbox-pdf:sha256:<hex>`. Stable, never collides with HTTP URLs, accepted by the existing slug machinery.
- **Archive copy**: on first poll of a `kind="pdf"` task, read bytes, compute SHA-256, copy bytes into `archive_root/inbox-pdf/YYYY/MM/<sha256>.pdf` via the same `build_archive_path` machinery used by `download_archive`. The archive copy is the canonical, repair-replayable record. Hand the bytes (in-memory) to `extract_pdf`; do not re-read from disk.
- **Submitter file lifecycle**: Influx does not delete the source file. The submitter owns the lifecycle of their staging directory.

### 16.5 v2 file-missing handling

If `local_path` does not exist when Influx polls the task (submitter cleaned up between submit and processing), complete the task terminally with `outcome="file_missing: <path>"`. No auto-retry. Submitters resubmit if they want.

### 16.6 v2 helper script extensions

`scripts/influx-inbox-submit.py` learns to detect file-path arguments:

- If the argument matches `^https?://` → `kind="url"` (v1 behaviour).
- Otherwise, treat as a local path → `kind="pdf"`. Resolve to absolute, verify the file exists and is a PDF (`.pdf` extension or magic-byte check).

For `kind="pdf"`:

1. Read `[inbox] pdf_root` from the resolved Influx config (same loader path the service uses).
2. If the source path is NOT already inside `pdf_root`, copy it into `pdf_root` with a deterministic filename — `<sha256-prefix>-<original-basename>` (collision-safe + retains the human-readable name for operator inspection).
3. The `local_path` sent in the task metadata is the path inside `pdf_root` — never the user's original path.
4. The user's source file is left in place; the script only copies, never moves or deletes.

### 16.7 v2 prerequisites

No new `LithosClient` wrappers beyond v1's §4.1. No new lithos-side tools. The v2 work is entirely additive on the Influx side: extract-path branching, path-trust check, archive-copy logic, synthetic source URL, and helper-script PDF mode.

### 16.8 v2 config additions

Additive to the v1 `[inbox]` block:

```toml
[inbox]
# … v1 fields unchanged …
pdf_root = "/inbox-pdfs"              # required when v2 PDF kind is used
```

Until `pdf_root` is configured, v2 PDF submissions complete terminally with `outcome="error: pdf_root_not_configured"`.

---

## 17. Open implementation questions (v1)

These are deliberately deferred — they are implementation choices, not foundational design decisions:

- **`lithos_task_claim` aspect string** — suggested `aspect="ingest"`.
- **`lithos_task_list` sort/limit semantics** — suggested oldest-first, `limit=max_items_per_tick`.
- **`influx-diagnose` CLI extensions** — adding inbox triage subcommands (`influx-diagnose inbox`) for the operator workflow.
- **Per-tick concurrency for items** — items can be processed in parallel within one tick subject to per-Profile lock contention; the parallelism cap (e.g. asyncio gather chunk size) is an implementation tuning knob.
- **Test plan** — unit tests around the cache-hit replay logic, the per-Profile try-acquire-skip path, the `task_list`/`task_claim`/`task_update` wrapper retries, and the InboxTick orchestrator's failure-isolation loop are likely the highest-value coverage targets.

---

## 18. Out of scope for v1

- **Local PDF submission** — planned v2, design preserved in §16.
- **Push-based discovery via Lithos's `TASK_CREATED` SSE events.** Polling is sufficient for the 5-minute cadence target. SSE subscription is a future latency optimisation if needed.
- **A `kind="text"` or `kind="html_blob"` for inline content submission.** The schema is forward-compatible (open enum); add when a real submitter needs it.
- **Persisted filter-rejection memory.** v1 re-filters previously-rejected Profiles on every resubmission. Promote to v1.5 if filter-call cost shows up in metrics.
- **Blocking-wait-and-renew Coordinator primitives.** v1 uses try-acquire-skip-this-tick instead. If the latency penalty proves unacceptable in production, blocking primitives become a v1.5 addition.
- **A separate `inbox_stall` heuristic.** The fetch_stall / ingestion_stall heuristics are scheduled-only by design.
- **Submitter authentication / allow-list.** Today, any agent that can talk to Lithos can create a task; inbox inherits that trust boundary.
- **Multi-Profile generalization of `Run` / `RunLedger` / `ProfileRunResult` / `/status`.** v1 keeps each as single-Profile and uses per-(item, Profile) Runs as the unit of work.
