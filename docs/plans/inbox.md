# Influx Inbox — Manual Submission Pipeline

Status: **Plan / Not Yet Implemented**
Date: 2026-05-09

Forward spec for a manual-submission pathway into the existing Influx ingestion pipeline. Submitting agents (e.g. daily-report agents) hand Influx a URL or a local PDF; Influx treats the item the way it treats RSS-discovered candidates — runs the same Filter, Cascade, Renderer, write, and LCMA wiring — except submitters don't pick a Profile.

This document describes design decisions only. It is not yet reflected in code. `docs/SPECIFICATION.md` continues to describe what is actually shipped.

---

## 1. Goals & Non-Goals

### 1.1 Goals

1. **Agent-driven intake**: Submitting agents add candidate items to Influx without knowing about Profiles, thresholds, or any Influx-internal API.
2. **Pipeline reuse**: Submitted items flow through the existing Filter → Cascade → Renderer → LithosClient → LcmaWiring stages with no parallel pipeline.
3. **Multi-profile fan-out**: Each item is scored against every enabled Profile; every Profile clearing its own threshold contributes to one shared canonical note via existing `_merge_profile_relevance_in_content` machinery.
4. **Both web URLs and local PDFs**: Web articles via URL, plus PDFs already on the host filesystem.
5. **Closed feedback loop**: Submitters can read back the outcome (which Profiles ingested, scores, canonical note ID, rejection reasons) without polling Influx directly.
6. **Operationally visible**: Inbox processing shows up in the run ledger, metrics, and `/status` distinctly from scheduled per-Profile work.

### 1.2 Non-Goals

1. **No Influx HTTP intake**: Submission is via Lithos task only. No new `POST /inbox` endpoint.
2. **No bypass of the Filter**: Submitters cannot force ingestion. Items below the per-Profile relevance threshold are dropped, exactly as RSS-discovered items would be.
3. **No submitter Profile selection**: Submitters cannot hint a Profile. Multi-profile fan-out is the model.
4. **No new CLI**: Submission uses the existing Lithos MCP surface; no `influx inbox submit` command.
5. **No cross-host filesystem semantics**: Local PDF support assumes submitter and Influx see the same `pdf_root` directory. Distributed setups out of scope.
6. **No bytes-in-task payloads**: Submitters don't inline PDF bytes into Lithos task metadata.

---

## 2. Vocabulary additions

These extend the vocabulary defined in `CONTEXT.md`. Once landed, the `_(proposed)_` markers below should be dropped and the entries moved into `CONTEXT.md` proper.

**InboxTask** _(proposed)_:
A Lithos task tagged `influx:inbox` carrying submission metadata. Created by external agents via `lithos_task_create`; consumed by Influx's inbox tick. Each task represents one candidate item.

**InboxRun** _(proposed)_:
One execution of `RunKind.INBOX` — claims pending InboxTasks, fans out filter scoring across enabled Profiles, ingests the matches, and writes outcomes back via `lithos_task_complete`. Distinct from per-Profile scheduled Runs.

**InboxSource** _(proposed)_:
Internal alias for the InboxRun's acquire/extract steps; not a `Source` adapter under `influx.sources.*` because the `Source` protocol is per-Profile and InboxRuns aren't.

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

### 3.2 Metadata schema

Required fields:

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `"url"` \| `"pdf"` | Discriminator. Forward-compatible (e.g. `"text"` could be added later). |
| `submitted_by` | string | Submitter agent identifier. Populates `provenance_actor` on resulting LCMA edges and the `## Profile Relevance` reason. |
| `url` | string | Required when `kind="url"`. Canonical web URL. Influx normalises via `influx.urls.normalise_url` and hashes via `url_hash`. |
| `local_path` | string | Required when `kind="pdf"`. Absolute path on the Influx host filesystem; MUST resolve inside `[inbox] pdf_root` (see §4). |

Optional fields:

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Hint for the candidate's `title` slot. Used as fallback if HTML/PDF extraction can't recover one. |
| `summary` | string | Pre-fetched summary or excerpt. Used as the candidate's `abstract` for the Filter prompt, saving an extract round-trip when reliable. |
| `source_tag` | string | Sets the resulting note's `source:*` tag (see §13.1 for the security clarification). Defaults to `"inbox"`. |

Explicitly absent from the contract:
- No `profile` hint field. Multi-profile fan-out is the model.
- No `priority` field.
- No `force` / `bypass_filter` field.
- No `notify` override.
- No score / threshold / notification configuration.

### 3.3 Tag convention

Influx polls `lithos_task_list(tags=["influx:inbox"], status="open")`. Single tag is sufficient — `kind` discrimination happens inside Influx via the metadata field.

---

## 4. Local PDF handling

### 4.1 Path trust model

A new `[inbox] pdf_root` config setting names a directory under which all submitted `local_path` values must resolve. Influx canonicalises (`Path(local_path).resolve()`) and rejects anything not under `pdf_root`. Submitters drop files into the configured directory; Influx never reads from arbitrary paths.

This mirrors the existing security posture (`security.allow_private_ips=false`, SSRF-guarded HTTP fetch) on the file-system side.

### 4.2 Identity

The PDF's identity is the SHA-256 of its byte content. Path-based identity is wrong — the same file under two different filenames would otherwise create two notes. Content-hash identity also auto-deduplicates "submitter sent the same paper twice" without extra logic.

### 4.3 Synthetic source URL

Notes need a `source_url` for slug-collision dispatch and `lithos_cache_lookup`. Inbox PDFs use `inbox-pdf:sha256:<hex>` as a synthetic opaque URI. Stable, never collides with HTTP URLs, accepted by the existing slug machinery.

### 4.4 File-to-archive copy

On first poll of an InboxTask with `kind="pdf"`:

1. Resolve `local_path`, verify containment in `pdf_root`. Reject otherwise.
2. Read bytes, compute SHA-256.
3. Copy bytes into `archive_root/inbox-pdf/YYYY/MM/<sha256>.pdf` via the same `build_archive_path` machinery used by `download_archive`. The archive copy is the canonical, repair-replayable record.
4. Hand the bytes (in-memory) to `extract_pdf` for text extraction; do not re-read from disk.

The submitter's original file is left in place. Influx does not delete it; the submitter owns the lifecycle of their staging directory.

### 4.5 File-missing handling

If `local_path` does not exist when Influx polls the task (submitter cleaned up between submit and processing), complete the task terminally with `outcome="file_missing: <path>"`. No auto-retry. Submitters that care can resubmit.

---

## 5. Execution model

### 5.1 New RunKind

A new `RunKind.INBOX` is added to `influx.coordinator.RunKind`. It joins `SCHEDULED`, `MANUAL`, and `BACKFILL`.

### 5.2 Inbox tick

A new scheduler entry — independent of the per-Profile cron — fires the inbox tick on its own cadence:

```toml
[inbox]
enabled = false                       # default off; opt-in
poll_cron = "*/5 * * * *"             # 5 min
max_items_per_tick = 20
pdf_root = "/inbox-pdfs"
task_tag = "influx:inbox"             # constant; exposed for ops only
agent_id = "influx-inbox"             # used in lithos_task_claim/complete/update calls
```

Each inbox tick performs:

1. **Claim**: `lithos_task_list(tags=[task_tag], status="open", limit=max_items_per_tick)` → for each, `lithos_task_claim(task_id=…, agent=agent_id, aspect="ingest")`. Failed claims (already claimed by another influx instance) are silently skipped.
2. **Per-item processing** (parallel across items, see §5.3 for the per-item shape).
3. **Tick completion**: write a ledger entry of `kind="inbox"` summarising the tick (see §13.4).

A 21st pending task waits for the next tick. Items are processed oldest-first (`lithos_task_list` ordering preference; implementation chooses the available sort key).

### 5.3 Per-item processing

For one claimed item:

1. **Cache lookup**: `lithos_cache_lookup(source_url=…)`. If a canonical note already exists, parse its `## Profile Relevance` section to identify Profiles already scored. The remaining set becomes the filter targets. (See §6 for the cache-hit handling.)
2. **Acquire**: download URL or copy PDF (per §4) into the archive. Done once per item; bytes are reused across all per-Profile filter calls.
3. **Filter fan-out**: in parallel for each filter-target Profile, call `models.filter` with that Profile's description, prompt, and negative examples. Submitter-provided `title`/`summary` populate the candidate's title/abstract slots.
4. **Per-Profile ingest** (sequential; see §10 for the lock model): for each Profile that scored above its `relevance` threshold, run the existing Cascade → Renderer → LithosClient.write_note → LcmaWiring chain. The first write creates (or merges into) the canonical note; subsequent writes for additional scoring Profiles hit `slug_collision` → duplicate-squatter classification → merge via `_merge_profile_relevance_in_content`.
5. **Outcome**: §7.

### 5.4 Failure isolation

Per-item, per-Profile isolation is mandatory:

- A filter timeout for Profile A on item 7 does NOT fail item 7. The item proceeds with the remaining Profiles' scores, ingests where they matched, and reports the partial result in the outcome string.
- A failure on item 7 does NOT fail items 8–20. Each item completes independently.
- A whole-tick failure (e.g. lithos circuit opens mid-tick) releases all unfinished claims and lets the next tick re-claim.

### 5.5 No auto-retry on terminal failure

Items that fail terminally (fetch failed, extract failed, file_missing, all-Profiles filter rejection) complete the task with `outcome="error"` and a descriptive string. Influx does NOT re-claim and re-process. Submitters resubmit if they want.

This avoids the poison-item failure mode where a malformed PDF burns inbox quota every tick.

Transient retries inside the existing per-call retry machinery (e.g. `arxiv_429_backoff_seconds` for filter-model rate limits) still apply — "no auto-retry" means "no inbox-level retry after the per-call retries exhaust."

---

## 6. Cache-hit behaviour

When `lithos_cache_lookup(source_url=…)` returns an existing note, Influx:

1. Parses the existing `## Profile Relevance` section.
2. Identifies Profiles already represented (regardless of score — the presence of an entry, including a low-score one, counts).
3. Filters the candidate against the **complement set only** (Profiles not yet listed).
4. For each newly-scoring Profile (above threshold), runs the ingest chain as in §5.3 step 4 — the existing merge logic adds the new Profile's relevance entry to the canonical note.
5. Reports the cache-hit case in the outcome string (see §7.1).

Profiles that previously scored the article and were rejected (their relevance entry is present even at a low score) are NOT re-filtered. Profiles that have never seen the URL ARE re-filtered, even if a previous scheduled tick predates their addition to the config.

A future optimisation may track previously-rejected Profiles in a hidden section or tag to skip re-filter on resubmission of articles that scored just above zero. Out of scope for v1; revisit if the filter cost shows up in metrics.

---

## 7. Outcome reporting

Each inbox task is completed via `lithos_task_complete` with four pieces of information:

### 7.1 Outcome string (free-text)

Human-readable, surfaces in the lithos task UI. Conventions:

- `ingested into 2 profile(s): ai-robotics, web-tech`
- `ingested into 1/2 profiles (web-tech); ai-robotics filter failed: timeout`
- `filtered out: top score 4 (ai-robotics) below threshold 7`
- `cache_hit: existing note <slug>; added 1 profile entry`
- `cache_hit: existing note <slug>; no new profiles matched`
- `fetch failed: HTTP 404`
- `extract failed: PDF too short (<min_web_chars>)`
- `file_missing: <path>`

### 7.2 `cited_nodes`

The Lithos node IDs of the canonical note(s) created or updated. With multi-profile merge into one canonical note, this is usually a single ID. Lets the submitter (or downstream LCMA consolidation) link directly to the result without parsing the outcome string.

### 7.3 Structured `metadata`

Before completion, Influx calls `lithos_task_update` to attach a structured `inbox_result` object to the task. Machine-consumable detail:

```json
{
  "inbox_result": {
    "per_profile": {
      "ai-robotics": {"score": 8, "ingested": true, "note_id": "..."},
      "web-tech":    {"score": 4, "ingested": false, "reason": "below_threshold"}
    },
    "source_url": "https://example.com/article",
    "archive_path": "blog/2026/05/abc...html",
    "processing_time_ms": 1234
  }
}
```

For `kind="pdf"` items, `source_url` is the synthetic `inbox-pdf:sha256:<hex>` and `archive_path` is the inbox-pdf subtree path.

### 7.4 `misleading_nodes`

Always unset. Inbox processing has no signal about misleading-ness.

---

## 8. Profile fan-out and the Filter

### 8.1 Filter call shape

Same `models.filter` model, same `prompts.filter` template, same `negative_example_max_title_chars` truncation as scheduled per-Profile filtering. The prompt is constructed per Profile with that Profile's `{profile_description}` and `{negative_examples}`.

### 8.2 Cost shape

Each inbox item produces up to N filter calls where N is the number of enabled Profiles (minus any already represented in the canonical note's `## Profile Relevance`, per §6). With the default `models.filter` model (`gpt-4.1-mini` in the example config), this is cheap but not free. The `[inbox] max_items_per_tick = 20` cap bounds per-tick filter spend at `20 × N`.

This is a new load shape for the filter. Scheduled ticks today do `items_per_profile × 1` filter calls per Profile because each Profile fetches its own candidates; inbox inverts that to `1 × N` per item.

---

## 9. Notifications

The inbox tick is treated as a notification source distinct from `scheduled` and `manual`:

- New `notify_on` value `"inbox"`. Webhook configs that want to receive inbox notifications opt in by listing `"inbox"` in their `notify_on` array.
- Existing webhook configs (which list only `"scheduled"`, `"manual"`, etc.) remain silent on inbox runs — backwards-compatible.
- Per-Profile `notify_immediate` thresholds apply unchanged. An item that ingests into `ai-robotics` at score 8 fires the same per-Profile notification machinery as a scheduled-tick ingestion at score 8 would.
- The `RunPlan.notify` flag for inbox runs is `True`. Backfill-style suppression is not appropriate.

Webhook `event_mode = "article"` fires per ingested item; `event_mode = "digest"` fires a summary at end of inbox tick. Existing semantics carry.

---

## 10. Coordinator interaction

### 10.1 Per-item, per-Profile lock acquisition

The existing `Coordinator` busy-lock is per-Profile and currently held for the entire duration of a per-Profile scheduled Run. Inbox uses the same lock but on a per-item, per-Profile basis:

- For each item, for each scoring Profile: acquire that Profile's lock → perform write + LCMA wiring → release.
- Lock is held for the duration of one canonical-note write (typically sub-second to a few seconds).
- Locks for different Profiles are acquired sequentially as the per-Profile writes proceed (the canonical note is shared across writes, so concurrent writes to it would race the merge logic).

### 10.2 Lock-renewal during waits

If a per-Profile scheduled Run is in flight when an inbox item attempts to acquire that Profile's lock, the inbox processing for that Profile blocks. While blocked, Influx calls `lithos_task_renew` periodically to keep the InboxTask claim alive past the scheduled-Run duration.

The renew cadence MUST be shorter than the `lithos_task_claim` lease duration. If an inbox item's wait exceeds renewal capacity, the claim auto-releases and another inbox tick may re-claim the task — re-processing is idempotent because the cache-hit path (§6) handles the case where the per-Profile lock-blocked write actually landed before the wait timed out.

### 10.3 Operator visibility

Inbox writes participate in the per-Profile lock so the Coordinator's `active_runs` view reflects inbox activity. A `/status` consumer asking "is `ai-robotics` busy right now?" gets a truthful answer regardless of whether the activity is scheduled or inbox-driven.

---

## 11. Repair, backfill, ingestion-stall

### 11.1 Repair sweep

Inbox-spawned notes are tagged identically to scheduled-spawned notes (e.g. `influx:repair-needed` when applicable). The per-Profile repair sweep picks them up by tag with no special-casing. No changes to `RepairCounters` or `repair.py`.

### 11.2 Backfill

Inbox has no date window. `RunKind.BACKFILL` does not apply. There is no `inbox-backfill` concept.

### 11.3 Ingestion-stall heuristic

The existing `ingestion_stall` and `fetch_stall` heuristics are evaluated on `RunKind.SCHEDULED` runs only. Inbox runs are excluded — an inbox tick with zero pending tasks is a quiet day, not a stall.

---

## 12. Probes and health gating

No new probe latch. Inbox runs gate on the existing two latches:

- `lithos_circuit_open`: lithos SSE health. Same skip behaviour as scheduled. If lithos is unreachable, inbox is skipped with `runs_skipped{reason="lithos_unhealthy"}`.
- `lcma_tools_unavailable`: LCMA tools present. Still applies because inbox-spawned notes flow through `LcmaWiring` after write.

The `lithos_task_*` tools are core MCP surface and are present whenever lithos is alive — no separate probe required.

---

## 13. Operational surface

### 13.1 Archive layout

Influx-controlled, NOT submitter-controlled:

| Item kind | Archive path |
|-----------|--------------|
| `kind="url"` | `archive_root/inbox-url/YYYY/MM/<url_hash>.html` |
| `kind="pdf"` | `archive_root/inbox-pdf/YYYY/MM/<sha256>.pdf` |

The submitter's `source_tag` from the metadata only flavours the resulting note's `source:*` tag. It does NOT influence archive subtree selection. This avoids a path-injection-style trust hole on the archive directory.

### 13.2 Note tags

The canonical note carries:

- `source:<tag>` — from the submitter's `source_tag`, defaulting to `source:inbox`
- `profile:<name>` — one per scoring Profile, union-merged via existing `notes.merge_tags`
- `submitter:<id>` — derived from `submitted_by` for the first ingestion; preserved across merges

### 13.3 Slug-collision suffix

When `slug_collision` is returned and the colliding note is a distinct paper, the suffix-retry uses `[inbox]`, matching the shape of the existing `[arXiv <id>]` and `[<host>]` suffixes.

### 13.4 Run ledger

`RunLedger` entries for inbox runs use `kind="inbox"`. Existing fields are reused with this semantic mapping:

| Field | Inbox semantic |
|-------|----------------|
| `sources_checked` | Number of InboxTasks claimed in this tick |
| `ingested` | Number of items that landed in ≥1 Profile note |

New optional fields on inbox-tick entries:

| Field | Semantic |
|-------|----------|
| `inbox_filtered_out` | Items claimed but no Profile cleared its threshold |
| `inbox_failed` | Items that completed terminally with `outcome="error"` |
| `inbox_cache_hits` | Items that hit cache (regardless of whether new Profile entries were added) |

### 13.5 Metrics

Existing `run_starts`, `run_completions`, `run_duration` get a new `run_type="inbox"` value alongside existing labels. The `runs_skipped` metric similarly gets `run_type="inbox"` for circuit-open and LCMA-unavailable skips.

New metric:

```
inbox_items_processed{outcome="ingested"|"filtered_out"|"cache_hit"|"error"}
```

Counter, ticked once per processed item per tick.

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

`pending` comes from `lithos_task_list(tags=["influx:inbox"], status="open")`. The read is cached on the same tick the probe loop refreshes other `/status` data — `/status` MUST NOT issue a fresh `lithos_task_list` per request (consistent with FR-HTTP-7).

### 13.7 `/runs/recent`

No structural change. Inbox runs appear with `kind="inbox"` and the new ledger fields above.

### 13.8 Agent identity

Inbox-side `lithos_task_*` calls (claim, renew, update, complete) MUST use `agent="influx-inbox"`. Distinct from `agent="influx"` used by scheduled-Run task tracking. Distinguishability matters for the lithos LCMA learning loop — inbox-driven `cited_nodes` feedback should not blur with scheduled-Run feedback in per-agent reinforcement.

---

## 14. Configuration summary

New `[inbox]` block in `influx.toml`. Default-disabled — existing deployments are unaffected unless the operator opts in.

```toml
[inbox]
enabled = false                       # default off
poll_cron = "*/5 * * * *"             # 5-minute tick
max_items_per_tick = 20
pdf_root = "/inbox-pdfs"              # absolute path; required if enabled and pdf submissions expected
agent_id = "influx-inbox"
# task_tag = "influx:inbox"           # constant; not operator-tunable
```

`enabled=false` is the default to preserve backwards compatibility with current configs.

---

## 15. Human submission helper script

Agents create InboxTasks directly via Lithos MCP. Humans don't. A helper script under `scripts/` provides a convenient CLI wrapper.

### 15.1 Entrypoint

`scripts/influx-inbox-submit.py` — same conventions as the existing `scripts/influx-diagnose.py` and `scripts/influx-report.py` (Python `__main__`, module docstring, argparse, env loaded from `docker/.env.<env>`).

### 15.2 Behaviour

The script takes a single positional argument that is either a URL or a local file path, decides which `kind` it is, performs any required local-side setup, and creates the Lithos task in the correct shape.

```
influx-inbox-submit https://example.com/article
influx-inbox-submit ./papers/transformers.pdf
influx-inbox-submit /home/dns/Downloads/paper.pdf --title "Attention Is All You Need"
```

Argument resolution:

- If the argument matches `^https?://` → `kind="url"`, no local work needed.
- Otherwise, treat as a local path → `kind="pdf"`. Resolve to absolute, verify the file exists and is a PDF (`.pdf` extension or magic-byte check).

For `kind="pdf"`:

1. Read `[inbox] pdf_root` from the resolved Influx config (same loader path the service uses).
2. If the source path is NOT already inside `pdf_root`, copy it into `pdf_root` with a deterministic filename — `<sha256-prefix>-<original-basename>` works (collision-safe, retains the human-readable name for operator inspection).
3. The `local_path` sent in the task metadata is the path inside `pdf_root` — never the user's original path.
4. The user's source file is left in place; the script only copies, never moves or deletes.

For `kind="url"`: no local work; the URL goes directly into `metadata.url`.

### 15.3 Optional flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--title <text>` | absent | Hint for the candidate's title slot |
| `--summary <text>` | absent | Pre-fetched summary for the Filter prompt |
| `--summary-file <path>` | absent | Read summary from a file (alternative to `--summary` for long text) |
| `--source-tag <tag>` | `inbox` | Sets the resulting note's `source:*` tag |
| `--submitted-by <id>` | `manual:<username>` | Submitter agent identifier; defaults to a `manual:` prefix plus the OS username |
| `--env <name>` | `staging` | Selects `docker/.env.<name>` for config resolution |
| `--dry-run` | false | Print the task body that would be sent without creating it |

### 15.4 Output

On success, prints the created task ID and a one-line summary:

```
Created task task_abc123 (kind=pdf, copied to /inbox-pdfs/9f3a-paper.pdf)
Track outcome: lithos task show task_abc123
```

The trailing line points the human at the Lithos CLI (or equivalent) for checking back on the outcome — keeps the script focused on submission and avoids re-implementing task-status display.

### 15.5 Posture

- Read-only against the Influx service surface — no HTTP calls to Influx itself.
- Write-only against Lithos — single `lithos_task_create` MCP call, plus a single local file copy when handling a PDF outside `pdf_root`.
- No `lithos_task_update` / `_complete` calls; that lifecycle belongs to Influx.
- Exits non-zero on validation failure (file missing, non-PDF extension, URL malformed) BEFORE making any MCP call or file copy.

---

## 16. Open implementation questions

These are deliberately deferred — they are implementation choices, not foundational design decisions:

- **`lithos_task_claim` aspect string** — suggested `aspect="ingest"`.
- **`lithos_task_list` sort/limit semantics** — suggested oldest-first, `limit=max_items_per_tick`.
- **`lithos_task_renew` cadence** — depends on the lithos task lease duration; should be roughly half the lease.
- **`influx-diagnose` CLI extensions** — adding inbox triage subcommands (`influx-diagnose inbox`) for the operator workflow.
- **Per-tick concurrency for items** — items can be processed in parallel within one tick subject to the per-Profile lock; the parallelism cap (e.g. asyncio gather chunk size) is an implementation tuning knob.
- **Test plan** — unit tests around the cache-hit replay logic, the path-containment check, and the per-Profile lock-renew loop are likely the highest-value coverage targets.

---

## 17. Out of scope for v1

- Push-based discovery via Lithos's `TASK_CREATED` SSE events. Polling is sufficient for the 5-minute cadence target. SSE subscription is a v2 latency optimisation if needed.
- A `kind="text"` or `kind="html_blob"` for inline content submission. The schema is forward-compatible (open enum); add when a real submitter needs it.
- Tracking previously-rejected Profiles to skip re-filter on resubmission (see §6).
- Cross-host filesystem semantics for `pdf_root` (see §1.2).
- A separate `inbox_stall` heuristic. The fetch_stall / ingestion_stall heuristics are scheduled-only by design.
- Submitter authentication / allow-list. Today, any agent that can talk to Lithos can create a task; inbox inherits that trust boundary.
