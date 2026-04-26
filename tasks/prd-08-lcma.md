# PRD 08 — LCMA Integration: `lithos_retrieve`, `lithos_edge_upsert`, Tasks

**Part of:** Influx v1 (see `tasks/prd-influx-v1-index.md`)
**Covers master PRD story:** S-10
**Milestone:** M2
**Prerequisites:** PRD 01, 02, 03, 04, 05, 06, 07
**Downstream PRDs that depend on this:** 09 (multi-profile shares the
LCMA call surface), 10.

---

## 1. Context

LCMA (Lithos Capability Memory & Analytics) is a Lithos-side service that
exposes `lithos_retrieve`, `lithos_edge_upsert`, `lithos_task_create`, and
`lithos_task_complete`. PRD 05 stubbed these in `lithos_client.py` to raise
`LCMAError("not implemented")`. This PRD replaces the stubs with real
calls and wires them into the per-profile run flow.

Influx does not enumerate LCMA capabilities at startup — LCMA is a
deployment prerequisite (FR-LCMA-1). Misconfigured deployments are
discovered at first use and abort the run.

This PRD also fills in the `related_in_lithos` field on the webhook
digest from PRD 05 (FR-NOT-6) using the high-scoring `lithos_retrieve`
results.

## 2. In scope

- `lithos_task_create` / `lithos_task_complete` bracketing every
  per-profile run with `tags=["influx:run", f"profile:{profile}"]`.
  (Backfill task tagging — `influx:backfill` — is in PRD 09 since
  backfill flow lives there.)
- After every canonical note write, call `lithos_retrieve` with
  composed query, `agent_id="influx"`, `task_id=run_task_id`,
  `tags=[f"profile:{profile}"]`, `limit=5`.
- For each retrieved result with `score >= thresholds.lcma_edge_score`
  (default 0.75), call `lithos_edge_upsert(type="related_to", ...)`
  with the receipt's `score` and `receipt_id` in `evidence`.
- For each Tier 3 `builds_on` item containing a recognisable arXiv ID,
  attempt `lithos_cache_lookup(query=prior_title,
  source_url=https://arxiv.org/abs/<id>)`; on exact `source_url` match,
  upsert `type="builds_on"` with
  `evidence={"kind": "tier3_builds_on_extraction"}`. No fuzzy
  matching.
- `lithos_retrieve` query composition helper (FR-LCMA-2) implemented
  in a dedicated function with golden-file tests.
- `LCMAError("unknown_tool")` aborts the current run, logs deployment
  error, marks readiness degraded (FR-LCMA-6 + FR-RES-3 path).
- Wire `related_in_lithos` into the webhook digest (FR-NOT-6).

## 3. Out of scope

- Multi-profile orchestration — PRD 09. (This PRD's tests run against
  one profile at a time.)
- RSS — PRD 09.
- Backfill task tagging — PRD 09.
- OTEL spans for LCMA calls — PRD 10.

## 4. Internal seams permitted

- Backfill run flow does not call `lithos_task_create` with
  `influx:backfill` yet — PRD 09 does that. This PRD only touches
  scheduled and manual `POST /runs` flows (which use `influx:run`).

## 5. Functional Requirements (master PRD §6.8)

### 5.1 Deployment prerequisite

- **FR-LCMA-1.** LCMA is a deployment prerequisite. Influx does NOT
  enumerate capabilities or bootstrap Lithos internals at startup.

### 5.2 Retrieve

- **FR-LCMA-2.** After each canonical note write call `lithos_retrieve(
  query=<query>, limit=5, agent_id="influx", task_id=run_task_id,
  tags=[f"profile:{profile}"])`. `<query>` is composed deterministically:
  1. Start with the normalised `title`.
  2. If the note has a `Tier1Enrichment` result, append the first
     **up to 3** `contributions` elements in original list order, each
     trimmed of leading/trailing whitespace, each skipped if empty
     after trimming, joined with the single separator `" | "`.
     Fewer than 3 non-empty elements is fine; field may be absent
     (e.g. enrichment failure), in which case only the title is used.
  3. Collapse internal whitespace runs to a single space and truncate
     to **500 characters** (no partial-word re-wrap; simple character
     slice).

  Implemented in a single helper so contract tests can golden-file
  assert the exact query string for any `(title, contributions)` input.

### 5.3 Related-to edges

- **FR-LCMA-3.** For each retrieved result with `score >=
  thresholds.lcma_edge_score` (default 0.75), call
  `lithos_edge_upsert(type="related_to", ...)` with `evidence`
  containing the retrieve receipt's `score` and `receipt_id`.

### 5.4 Builds-on edges

- **FR-LCMA-4.** For each Tier 3 `builds_on` item, attempt arXiv-ID
  extraction; if present, resolve via
  `lithos_cache_lookup(source_url=https://arxiv.org/abs/<id>,
  query=prior_title)` and upsert `type="builds_on"` only on an exact
  `source_url` match. The lookup MUST pass BOTH `query` and
  `source_url` (FR-MCP-3, R-7). `<prior_title>` is the textual
  prefix of the `builds_on` item preceding the arXiv ID, falling back
  to the arXiv ID itself when no prefix is present. Fuzzy title
  matching is deferred — anything other than an exact `source_url`
  match is silently skipped.

### 5.5 Tasks

- **FR-LCMA-5.** One Lithos task per profile per run, created with
  `lithos_task_create(title=f"Influx run {profile} {date}",
  agent="influx", tags=["influx:run", f"profile:{profile}"])`.
  Completed with `lithos_task_complete(task_id, agent="influx",
  outcome=...)`. Backfills use `influx:backfill` instead — PRD 09.

### 5.6 Failure handling

- **FR-LCMA-6.** When an LCMA-dependent call fails with "unknown
  tool" (or equivalent), Influx aborts the current run, logs a
  deployment error, marks readiness degraded.

## 6. Files to create / modify

### Create
- `src/influx/lcma.py` — composed query helper, retrieve+edge wiring,
  builds_on resolver, task bracketing.
- `tests/contract/test_lcma_calls.py` — happy-path + unknown-tool
  envelope tests for each LCMA tool.
- `tests/unit/test_lcma_query_composition.py` — golden-file table for
  FR-LCMA-2 query composition.
- `tests/unit/test_builds_on_arxiv_id_extraction.py`
- `tests/integration/test_lcma_edges_end_to_end.py`

### Modify
- `src/influx/lithos_client.py` — replace `LCMAError`-raising stubs
  with real wrappers around the four LCMA tools.
- `src/influx/service.py` — bracket each per-profile run with
  `task_create` / `task_complete`.
- `src/influx/sources/arxiv.py` (and the post-write hook) — call
  `lcma.after_write(note, run_task_id, profile)` after each successful
  `lithos_write`.
- `src/influx/notifications.py` — fill `related_in_lithos` on
  highlights using the `lithos_retrieve` results captured during the
  run.

## 7. Dependencies to add

None.

## 8. Acceptance Criteria

### From master PRD §7.2

- **AC-M2-5.** After every canonical note write Influx calls
  `lithos_retrieve` with `agent_id="influx"`, `task_id=<run-task-id>`,
  `tags=["profile:<name>"]`.
- **AC-M2-6.** Retrieved results with `score ≥ lcma_edge_score`
  produce a `lithos_edge_upsert(type="related_to")` call whose
  `evidence` contains `{"kind": "lithos_retrieve", "score": ...,
  "receipt_id": ...}`.
- **AC-M2-7.** When a Tier 3 `builds_on` item contains a recognisable
  arXiv ID and `lithos_cache_lookup(query=<prior_title>,
  source_url=https://arxiv.org/abs/<id>)` hits, Influx upserts
  `type="builds_on"` with
  `evidence={"kind": "tier3_builds_on_extraction"}`. The lookup call
  MUST pass BOTH `query` and `source_url`. `<prior_title>` is the
  textual prefix preceding the arXiv ID, falling back to the arXiv
  ID itself when no prefix is present.
- **AC-M2-8.** Tier 3 `builds_on` items without a matching
  `source_url` do NOT create an edge.
- **AC-M2-9.** If Lithos returns an "unknown tool"-style error on
  `lithos_retrieve` / `lithos_edge_upsert`, the run aborts with a
  clear deployment-error log and readiness becomes degraded.
- **AC-M2-10.** Each profile run is bracketed by
  `lithos_task_create` / `lithos_task_complete` with matching
  `influx:run` tag.

### New for this PRD

- **AC-08-A.** Query composition golden test: at least 5 input cases
  including:
  - title only (no contributions)
  - title + 1 contribution
  - title + 3 contributions
  - title + 5 contributions (only first 3 used)
  - 600-character title (truncated to 500 chars)
- **AC-08-B.** Composed query collapses runs of whitespace (e.g.
  double spaces, newlines) to a single space.
- **AC-08-C.** `prior_title` extraction from a Tier 3 `builds_on`
  item like `"FooNet (arXiv:2412.12345)"` yields `prior_title="FooNet"`
  and `arxiv_id="2412.12345"`.
- **AC-08-D.** A `builds_on` item with only an arXiv ID (no prior
  title text) uses the arXiv ID as `prior_title` for the lookup.
- **AC-08-E.** `lithos_retrieve` failure with `LCMAError("unknown_tool")`
  aborts the run mid-way; the partial run does NOT produce edges,
  but already-written notes remain.
- **AC-08-F.** Webhook digest from PRD 05 now includes
  `related_in_lithos` with title + score for high-scoring retrieve
  results.

## 9. Tests required

- Golden-file query composition tests (AC-08-A, AC-08-B).
- arXiv ID extraction tests for various `builds_on` item shapes.
- Contract tests: every LCMA tool happy-path + at least one
  envelope test (`unknown_tool` for retrieve, edge_upsert).
- Integration: end-to-end run with at least one related_to edge
  upsert visible in the fake-Lithos recorder.
- Coverage ≥ 80% on `lcma.py`.

## 10. Definition of Done

- [ ] All AC-M2-5…10, AC-08-A…F satisfied.
- [ ] `lithos_client.py` no longer has any `NotImplementedError` or
      `LCMAError("not implemented")` stubs.
- [ ] `validate-config` (PRD 05) is unchanged — LCMA is a runtime
      prerequisite, not a startup probe (FR-LCMA-1).
- [ ] Webhook digest highlights carry `related_in_lithos` field.
- [ ] Ruff, pyright, pytest all green. Coverage ≥ 80% on new modules.
