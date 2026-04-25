# PRD 05 — Real Lithos MCP Client + Write Error Envelopes + Webhook + Feedback

**Part of:** Influx v1 (see `tasks/prd-influx-v1-index.md`)
**Covers master PRD story:** S-7a (split from the master PRD's S-7)
**Milestone:** M1
**Prerequisites:** PRD 01, 02, 03, 04
**Downstream PRDs that depend on this:** 06, 07, 08, 09, 10

> **Surface freeze.** When this PRD lands, the v1 public surface (HTTP
> responses, Lithos tool call shapes, note structure, config schema) is
> frozen per master PRD §14. Later PRDs add behaviour, never adapters.
> Treat the lithos_client.py wrapper API as the public Lithos surface.

---

## 1. Context

PRD 04 ships the arXiv flow against a stub `lithos_client.py` that
records calls. This PRD replaces that stub with the real SSE-transport
MCP client and wires:

- All `lithos_*` tool calls Influx uses today: `agent_register`,
  `cache_lookup` (with both `query` and `source_url`), `write` with
  full error-envelope handling (§9.6, §9.7), `list` for feedback.
- The webhook notifier that POSTs the per-run digest to Agent Zero.
- Real Lithos reachability probe (replacing the env-flag stub from
  PRD 03).
- The full `validate-config` dry-connect to Lithos (FR-CLI-5).

The repair sweep — `lithos_list(tags=["influx:repair-needed", ...])` —
is owned by PRD 06. This PRD does NOT call the sweep; it only ensures
`lithos_list` works generically (FR-FB-1 uses it for feedback, which IS
in this PRD).

## 2. In scope

- Real `lithos_client.py` over SSE using the official `mcp` SDK.
- `lithos_agent_register` on startup (FR-MCP-8).
- `lithos_cache_lookup(query=..., source_url=...)` with the chokepoint
  enforcement that BOTH args are always passed (R-7 mitigation).
- `lithos_write` with all error envelopes (FR-MCP-7) — including the
  full `content_too_large` create-vs-repair branching from §9.7 of the
  master PRD.
- `lithos_list` generic wrapper (consumed here for feedback; PRD 06
  will reuse it for the repair sweep).
- Feedback ingestion (FR-FB-1…3): pull `influx:rejected:<profile>`
  titles before each filter run and pipe into `negative_examples`.
- Webhook notifier (FR-NOT-1…6): JSON digest, 5s timeout, no retry.
- Replace PRD 03's stub Lithos probe with a real ping-style probe.
- Extend PRD 01's `validate-config` to dry-connect to Lithos and assert
  JSON-mode compatibility for each `[models.*]` slot.
- Remove the `lithos_client` and `lithos_cache_lookup` stubs from
  PRD 04 entirely.

## 3. Out of scope

- Repair sweep — PRD 06.
- `lithos_retrieve` / `lithos_edge_upsert` / `lithos_task_create` /
  `lithos_task_complete` — PRD 08 (LCMA). The wrapper file may stub
  these to raise `LCMAError("not implemented")` until PRD 08; the
  rest of the system MUST NOT call them yet.
- Rejection-rate logging cadence (FR-OBS-5) — PRD 10.

## 4. Internal seams permitted

- `lithos_retrieve`, `lithos_edge_upsert`, `lithos_task_create`,
  `lithos_task_complete` may exist as `LCMAError`-raising stubs in
  `lithos_client.py` until PRD 08. No call sites invoke them yet.

## 5. Functional Requirements

### 5.1 Transport (master PRD §6.7)

- **FR-MCP-1.** Transport is SSE via the official `mcp` Python SDK.
  `LITHOS_MCP_TRANSPORT=sse` is the only supported value in v1.
- **FR-MCP-2.** Client wrapper at `influx/lithos_client.py`; connection
  established lazily on first use and reused for the run.

### 5.2 Tool calls used in this PRD

- **FR-MCP-3.** Dedup uses `lithos_cache_lookup` with **both** `query`
  and `source_url` on every call. `query` is composed source-agnostically:
  1. `title + " " + first_sentence(abstract_or_summary)` when an
     abstract or feed summary is present and non-empty;
  2. `title` alone otherwise.

  "First sentence" is the substring up to (but not including) the first
  `.`, `!`, or `?` followed by whitespace or end-of-string, trimmed and
  capped at 200 characters. arXiv source: `<abstract>`. RSS/blog source:
  `<summary>`. `query` MUST always be non-empty.

  The wrapper enforces this at a single chokepoint — calls that omit
  `query` or `source_url` raise `LithosError("missing_lookup_arg")` BEFORE
  any RPC.
- **FR-MCP-5.** arXiv canonical: `source_url = https://arxiv.org/abs/{id}`
  + tag `arxiv-id:{id}`. `lithos_list(tags=["arxiv-id:..."])` is a
  deterministic secondary lookup.
- **FR-MCP-6.** `lithos_write` is called with `agent="influx"`, `path`,
  `source_url`, `tags`, `confidence`, `note_type="summary"`,
  `namespace="influx"`, and (when repair is needed) `expires_at = next
  retry boundary`.
- **FR-MCP-7.** Write error envelopes:
  - `duplicate` → treat as hit
  - `invalid_input` → log, skip item
  - `content_too_large` → trim full-text, retry once; on a second
    failure, branch on create vs repair per §9.7 of master PRD
    (see §5.3 below)
  - `slug_collision` → retry once with disambiguated title suffix
    (arXiv: ` [arXiv <id>]`, web: ` [<host>]`)
  - `version_conflict` → re-read, re-apply tag-merge and user-notes
    preservation, retry once; skip on second
- **FR-MCP-8.** `lithos_agent_register(id="influx",
  name="Influx Pipeline", type="ingestion-pipeline")` on startup.

### 5.3 Content-too-large handling (master PRD §9.7)

When `lithos_write` returns `content_too_large` the first time:

1. Drop `## Full Text` (Tier 2), keep Tier 1 + Tier 3, retry once.

If the retry also returns `content_too_large`:

- **Create path** (no existing note for this `source_url`):
  - DO NOT invent a degraded placeholder note.
  - Log `source_url`, count in `content_too_large_skipped`, skip the
    item this run.
  - Retry is best-effort via source rediscovery only (FR-REP-3).

- **Repair path** (an Influx-authored note already exists for this
  `source_url`):
  - Perform one additional trimmed write that preserves Tier 1 only
    (drops Tier 2 AND Tier 3), tags `influx:repair-needed`, writes
    the note.
  - If THAT trimmed write also returns `content_too_large`: leave
    the existing note untouched, log + count in
    `content_too_large_skipped`, sweep proceeds. The note's
    `updated_at` does NOT advance and the run does NOT abort. This
    is the sole exemption to FR-REP-1's retry-order advancement
    mechanism (PRD 06 enforces the rest of that mechanism).

### 5.4 Feedback (master PRD §6.9)

- **FR-FB-1.** Read at the start of every filter run via
  `lithos_list(tags=[f"influx:rejected:{profile}"],
  limit=feedback.negative_examples_per_profile)`.
- **FR-FB-2.** If `lithos_list` returns title metadata, use it directly.
  Only items missing titles trigger a `lithos_read(id=...)`.
- **FR-FB-3.** Influx does NOT author rejection tags itself. Lens is
  the sole writer of `influx:rejected:<profile>`.

### 5.5 Webhook notifier (master PRD §6.12)

- **FR-NOT-1.** After each profile run POST a JSON digest to
  `AGENT_ZERO_WEBHOOK_URL`. Timeout 5s, no retry.
- **FR-NOT-2.** Body shape per `docs/REQUIREMENTS.md` §11.1
  (`type`, `run_date`, `profile`, `stats`, `highlights` with up to
  the `notify_immediate`-threshold items, `all_ingested`).
- **FR-NOT-3.** Zero-ingest runs send a quiet digest (§11.2).
- **FR-NOT-4.** Backfills never send webhook notifications. (This PRD
  ensures the webhook hook is a no-op when `kind == "backfill"`; PRD
  09 owns the backfill flow itself.)
- **FR-NOT-5.** When `AGENT_ZERO_WEBHOOK_URL` is empty the webhook
  step is skipped silently.
- **FR-NOT-6.** Highlight entries include related-in-Lithos pointers
  (title + score) when `lithos_retrieve` returned high-scoring
  results. Since `lithos_retrieve` is owned by PRD 08, this PRD's
  highlights have an empty `related_in_lithos` list. PRD 08 fills
  it in via the same hook.

### 5.6 Resilience around Lithos (master PRD §6.13)

- **FR-RES-3.** Lithos fully unreachable after `max_retries` aborts the
  current run, marks readiness degraded, keeps the service alive.
  One-shot CLI invocations return exit code 2.

### 5.7 validate-config dry-connect (FR-CLI-5)

Extend the validator from PRD 01 to:

- Construct a client for each `[models.*]` slot. With `json_mode = true`,
  assert the constructed client supports `response_format={"type":
  "json_object"}` (or equivalent). Failure → exit non-zero.
- Open an SSE connection to Lithos using the configured endpoint. Call
  `lithos_agent_register`. Failure → exit non-zero.

## 6. Files to create / modify

### Create
- `src/influx/notifications.py` — webhook digest builder + sender
  (uses guarded HTTP client from PRD 02 for the POST; the SSRF guard
  applies to the webhook URL too).
- `tests/contract/test_lithos_client.py` — happy-path + error-envelope
  contract tests for every tool used in this PRD against a fake Lithos.
- `tests/unit/test_dedup_query.py` — first-sentence + chokepoint tests.
- `tests/unit/test_webhook_digest.py`
- `tests/integration/test_arxiv_to_lithos.py` — replaces stub assertions
  in PRD 04's integration test with real (recorded) MCP calls.

### Modify
- `src/influx/lithos_client.py` — replace stub with real SSE client.
- `src/influx/probes.py` — replace stub Lithos probe with real one.
- `src/influx/cli.py` — extend `validate-config` to dry-connect.
- `src/influx/service.py` — wire the post-run webhook hook.

### Delete
- `tests/fixtures/lithos/stub_recorder.json` (or whatever PRD 04 used)
  — no longer needed once the contract tests replace them.

## 7. Dependencies to add

| Purpose | Package |
|---|---|
| MCP client (SSE) | `mcp` |

## 8. Acceptance Criteria

### From master PRD §7.1

- **AC-M1-7** (full). After `POST /runs`, the service writes notes via
  the real `lithos_write` and POSTs a real digest to the webhook URL.
  Use a local fake-Lithos server + a local fake webhook receiver in
  the integration test.
- **AC-M1-9** (full). Re-running the same profile after completion
  skips already-ingested items via `lithos_cache_lookup`.
- **AC-M1-10**. (Re-asserted against real `lithos_write` calls.)
- **AC-M1-11.** If Lithos is unreachable when `POST /runs` fires, the
  run aborts, `/status` reports `status="degraded"` and
  `dependencies.lithos.status != "ok"`, and the service stays alive.

### From master PRD §7.5 (cross-cutting subset)

- **AC-X-1** (partial). All tunable values from the requirements that
  this PRD touches (max_retries, backoff_base_seconds, webhook
  timeout, etc.) come from config; no hardcoded constants.

### New for this PRD

- **AC-05-A.** `lithos_cache_lookup` chokepoint: a call missing `query`
  or `source_url` raises `LithosError("missing_lookup_arg")` BEFORE any
  RPC.
- **AC-05-B.** First-sentence helper produces the exact strings expected
  by FR-MCP-3 across a golden-file table of inputs (no abstract → title
  only; abstract with `Mr. Smith went home.` → `Mr` is NOT a sentence
  end because `.` must be followed by whitespace at the end of the
  sentence; actually verify exact rule: `.`, `!`, or `?` followed by
  whitespace OR end-of-string).
- **AC-05-C.** `lithos_write` `duplicate` envelope is treated as hit
  (no error, no second write, item counted in `dedup_skipped`).
- **AC-05-D.** `lithos_write` `slug_collision` is retried once with
  the documented title suffix; second collision → skip + log.
- **AC-05-E.** `lithos_write` `version_conflict` re-reads the note,
  re-applies tag merge + user-notes preservation, retries once;
  second conflict → skip + log.
- **AC-05-F.** `lithos_write` first `content_too_large` → drop
  `## Full Text` and retry. Second `content_too_large`:
  - Create path: skip + count + log; no note persisted.
  - Repair path: trimmed Tier-1-only retry + `influx:repair-needed`
    tag. If THAT also fails: leave note untouched, count, sweep
    continues (no abort, no `updated_at` advance).
  Tests MUST cover all four sub-paths.
- **AC-05-G.** `lithos_agent_register` is called exactly once on
  startup; reconnect after an SSE drop re-registers.
- **AC-05-H.** Feedback: `lithos_list` returns 3 rejection items;
  filter prompt's `negative_examples` block contains exactly those 3
  titles, rendered per FR-FLT-5 format.
- **AC-05-I.** Webhook: real digest POST to a local fake receiver
  contains all FR-NOT-2 keys, `highlights` capped at the
  `notify_immediate`-threshold count.
- **AC-05-J.** With `AGENT_ZERO_WEBHOOK_URL=""`, the webhook step is
  silently skipped.
- **AC-05-K.** With Lithos unreachable, `validate-config` exits
  non-zero with a message naming the SSE endpoint.

## 9. Tests required

- Contract tests for every tool the wrapper calls in this PRD, with
  both happy-path and at least one error-envelope test each:
  `agent_register`, `cache_lookup`, `write`
  (`duplicate`, `invalid_input`, `content_too_large` x4 sub-paths,
  `slug_collision`, `version_conflict`), `list`.
- Integration test exercising the full arXiv → Lithos → webhook path
  against a local fake Lithos and a local fake webhook receiver.
- Unit tests for the dedup-query helper (first-sentence rules) and
  the chokepoint guard.
- Coverage ≥ 80% on `lithos_client.py`, `notifications.py`.

## 10. Definition of Done

- [ ] PRD 04's `lithos_client` and `enrich` stubs are still in place
      ONLY where this PRD does not own the replacement (e.g. `enrich`
      is replaced by PRD 07).
- [ ] PRD 04's `lithos_client` stub is replaced by the real one;
      AC-M1-7/9/10/11 fully pass.
- [ ] All AC-05-A…K satisfied.
- [ ] LCMA tool stubs in `lithos_client.py` raise `LCMAError` and are
      not invoked anywhere in the codebase yet.
- [ ] `validate-config` dry-connects and asserts JSON-mode
      compatibility per FR-CLI-5.
- [x] **v1 public surface is now FROZEN.** A reviewer-style note is
      added to `tasks/prd-influx-v1-index.md` (or this PRD's footer)
      confirming freeze.
- [ ] Ruff, pyright, pytest all green. Coverage ≥ 80% on new modules.

---

## v1 Public Surface Freeze

> **FROZEN as of PRD 05 completion (2026-04-25).**
>
> Per master PRD §14, the v1 public surface is now frozen. The following
> are locked and MUST NOT change in downstream PRDs (06–10):
>
> - **HTTP response shapes** — status codes, JSON envelopes, error codes
> - **Lithos tool call shapes** — `lithos_cache_lookup`, `lithos_write`,
>   `lithos_list`, `lithos_agent_register` argument and return contracts
> - **Note structure** — Tier 1/2/3 section headings, tag format, field names
> - **Config schema** — `[lithos]`, `[models.*]`, `[notifications]`,
>   `[profiles.*.thresholds]` key names and value types
>
> Later PRDs add behaviour (new tool calls via LCMA stubs, repair sweep
> logic, backfill orchestration) but NEVER modify the frozen surface.
> Treat `src/influx/lithos_client.py`'s wrapper API as the canonical
> Lithos surface contract.
