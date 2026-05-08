# Citation Graph — Issue Drafts

Date: 2026-05-08
Status: Drafted, not yet filed
Source plan: `docs/plans/citation-graph.md` (v1.0.0)

These eight tracer-bullet issues implement Phase 1+2 of the citation
graph extension.  All are AFK — the design has been pinned in the
parent plan; no further architectural calls are needed.

---

## Filing instructions (for later)

- **Repo:** `agent-lore/influx`
- **Labels (all issues):** `enhancement`, `ready-for-agent`, `needs-triage`
- **Dependency order:** file in the order TB-1 → TB-2 → … → TB-8 so
  each "Blocked by" reference can use the real GitHub issue number of
  the prior slice.
- **Prerequisite already filed:** `agent-lore/influx#99` blocks TB-6.
- **One-liner per issue:** use `gh issue create --repo agent-lore/influx
  --title "<title>" --label "enhancement,ready-for-agent,needs-triage"
  --body "$(cat <<'EOF'
  <body>
  EOF
  )"` (HEREDOC body matches the format below).

After filing, replace `<TB-N>` placeholders in subsequent `Blocked by`
lines with the real issue numbers.

---

## TB-1 — S2AG foundation: enrichment writes frontmatter and tags on arXiv notes

**Type:** AFK
**Blocked by:** None — can start immediately

### What to build

Stand up the S2AG enrichment pipe end-to-end so that every arXiv-source
note Influx writes picks up paper-side S2AG state in its frontmatter
and tags.  This is the foundation slice — every subsequent issue
extends behaviours of the same pipe.

Touches: a new `src/influx/s2ag.py` module (async client + probe), the
existing `Cascade` (new injection point between Tier 2 and Tier 1, gate
`score >= thresholds.relevance`, applicability gated to arXiv-source
candidates only — RSS items pass `arxiv_id=None` and short-circuit),
the per-tick `FetchCache` (reuse `get_or_fetch` keyed by
`("s2ag", paperId)`), the renderer / note-writer (frontmatter +
tag emission), config (`[s2ag]` section, `[resilience]
s2ag_429_backoff_seconds`), and metrics (`influx_s2ag_calls_total`,
`influx_s2ag_skip_total`).

Out of scope for this slice: edges (TB-6), TLDR fallback (TB-4),
concept tags (TB-2), degraded path / probe-latched skip (TB-3),
paper-not-in-S2 handling (TB-5), repair sweep (TB-7).

### Acceptance criteria

- [ ] `src/influx/s2ag.py` exposes `S2agClient` with `paper_batch(ids)`
      and `paper(id, paper_id_type="ARXIV")` methods, using the
      existing guarded HTTP path.
- [ ] Probe loop runs `GET /graph/v1/paper/search?query=test&limit=1`
      every 60 s; failures surface in `/status` but do not affect
      ingest in this slice (degraded path lands in TB-3).
- [ ] `Cascade` gains an injection point `s2ag_enricher: S2agEnricher | None`
      that runs after Tier 2 and before Tier 1, gated by
      `score >= thresholds.relevance` AND `arxiv_id is not None`.
- [ ] `EnrichedSections` carries new fields `s2ag: S2agEnrichment | None`
      and `s2ag_attempted: bool`.
- [ ] `S2agEnrichment` contains at minimum `s2_paper_id` (40-char hex
      or empty), `s2_doi` (string or empty), `s2_external_ids` (dict),
      `s2_citations_seen` (int), `s2_reference_count`,
      `s2_citation_count`, `s2_influential_citation_count`, `s2_tldr`
      (string or empty), `s2_fields_of_study` (list — populated but
      not yet emitted as tags; TB-2 covers tag emission).
- [ ] `s2_references` is populated on the cascade output but not
      persisted to frontmatter (in-memory only — TB-6 consumes it).
- [ ] Note frontmatter on arXiv-source notes includes `s2_paper_id`,
      `s2_doi`, `s2_citations_seen`, `s2_fields_of_study`.  Empty
      strings / empty lists when enrichment was skipped.
- [ ] Tag emission: when `s2_paper_id` is non-empty, the note carries
      a `s2-paper-id:<hex>` tag.
- [ ] RSS-source notes are unchanged: no `s2_*` frontmatter, no
      `s2-paper-id:*` tag, no S2AG calls (verifiable via metrics).
- [ ] Per-tick `FetchCache` is reused — concurrent ingestion of the
      same `paperId` in two profiles produces exactly one S2AG call.
- [ ] Config `[s2ag]` section as in §5.9 of the plan: `enabled`,
      `base_url`, `api_key_env`, `request_timeout`, `batch_size`.
      `[resilience].s2ag_429_backoff_seconds`.
- [ ] Metrics `influx_s2ag_calls_total{endpoint, status}` and
      `influx_s2ag_skip_total{reason=non_arxiv_source}` emit.
- [ ] Unit tests with a fake S2AG transport: happy path, RSS skip,
      cache hit-collapse, malformed response handling.
- [ ] One smoke test against a live S2AG endpoint (or marked
      `pytest.mark.network`) that ingests a known arXiv paper and
      asserts the frontmatter on disk.

### Blocked by

None — can start immediately.

---

## TB-2 — Concept tags from `s2FieldsOfStudy`

**Type:** AFK
**Blocked by:** TB-1

### What to build

Emit `field:<slug>` tags for the top three categories returned by
S2AG's `s2FieldsOfStudy` payload, in the order S2AG returns them.

### Acceptance criteria

- [ ] Take the first three entries of
      `S2agEnrichment.s2_fields_of_study` as returned by S2AG (do not
      reorder).
- [ ] Slug normaliser converts `"Computer Science"` → `"computer-science"`
      (lowercase, single non-alphanumeric run collapses to `-`,
      trim leading/trailing `-`).
- [ ] Each of the (up to) three categories produces a `field:<slug>`
      tag on the note.
- [ ] Cap at 3 tags even when S2AG returns more.
- [ ] No top-level allow/deny list.  Reasoning lives in §5.5 of the
      plan.
- [ ] Unit tests cover: 0 categories, 1 category, 5 categories
      (asserts cap), unicode-bearing category names (e.g.
      `"Pharmacology & Toxicology"`).

### Blocked by

- TB-1

---

## TB-3 — Degraded path: S2AG outage tolerated

**Type:** AFK
**Blocked by:** TB-1

### What to build

S2AG outage must never fail ingest.  Three consecutive probe failures
latch `s2ag_unavailable`; the cascade detects the latch and skips the
stage cleanly; the run ledger records `s2ag_unavailable` in
`degraded_reasons`.  `[s2ag].enabled = false` is a manual kill switch
with the same effect.

### Acceptance criteria

- [ ] `ProbeLoop` gains a `_probe_s2ag` probe; latch flips after 3
      consecutive failures and clears on the first success.
- [ ] When the latch is set, `Cascade.enrich` skips the s2ag stage
      without raising and without bumping any counter.
- [ ] When `[s2ag].enabled = false`, the cascade behaves identically
      to a latched probe.
- [ ] Affected runs append `s2ag_unavailable` to
      `RunLedger.degraded_reasons` once per run (not per item; mirrors
      `filter_error` semantics).
- [ ] arXiv-source notes ingested during an outage are written without
      `s2_*` frontmatter or `s2-paper-id:*` tag, but are otherwise
      complete.
- [ ] Integration test: blocking the S2AG host at the network level
      causes ingest to complete with the run flagged
      `s2ag_unavailable`; no stack traces in logs above WARNING.
- [ ] Unit test for the probe latch state machine: 2 fails → no
      latch; 3 fails → latch; success → clear.

### Blocked by

- TB-1

---

## TB-4 — TLDR fallback for failed Tier 1

**Type:** AFK
**Blocked by:** TB-1

### What to build

When Tier 1 fails (transient or counted) AND the cascade has captured
a non-empty `s2_tldr`, the renderer substitutes the TLDR for the
`## Summary` section and tags the note `summary:s2ag-tldr`.  When
Tier 1 fails AND the TLDR is empty, the renderer omits the section
entirely and tags `summary:tier1-failed-no-tldr`.

This is purely a renderer / tag-emission concern.  No new S2AG calls;
it consumes `S2agEnrichment.s2_tldr` produced by TB-1.

### Acceptance criteria

- [ ] Renderer implements the FSM in §5.4 of the plan:

  | Tier 1 | `s2_tldr` | Output | Tags |
  |---|---|---|---|
  | OK | * | Tier 1 prose | (no s2ag-tldr) |
  | Failed | non-empty | TLDR + trailing parenthetical attribution `_(summary auto-generated by Semantic Scholar)_` | `summary:s2ag-tldr` |
  | Failed | empty | (no `## Summary` section) | `summary:tier1-failed-no-tldr` |
  | Skipped (`score < relevance`) | * | (no `## Summary` section) | (no S2AG tags) |

- [ ] When a subsequent re-write succeeds at Tier 1, the renderer
      always prefers Tier 1; both `summary:s2ag-tldr` and
      `summary:tier1-failed-no-tldr` tags are removed in the same
      write.
- [ ] `influx_s2ag_tldr_fallback_total{profile, tier="tier1"}` metric
      emits when the fallback is applied.
- [ ] Unit tests for each FSM cell.
- [ ] Unit test asserting tag removal on Tier 1 success in a re-write.

### Blocked by

- TB-1

---

## TB-5 — Paper-not-in-S2-corpus → counted-once + terminal

**Type:** AFK
**Blocked by:** TB-1

### What to build

Distinguish "S2AG returned 404 / empty body for a known arXiv ID" from
generic counted failures.  Retrying achieves nothing because S2AG's
corpus state isn't going to change for a paper it doesn't know.  Treat
as a single counted attempt that immediately latches
`influx:s2ag-terminal` and tags `summary:s2ag-miss`.

### Acceptance criteria

- [ ] `S2agClient` distinguishes "paper not in S2" (HTTP 404 from
      `/paper/...` for a well-formed arXiv ID, or empty `data` from
      `/paper/batch`) from generic counted failures.
- [ ] On paper-not-in-S2: cascade increments `s2ag_attempts` once
      (via the partial counter machinery TB-7 will own; for now
      record on the note's `## Repair` block as `s2ag_attempts: 1`,
      `s2ag_last_stage: "enrich"`, `s2ag_last_error: "not_in_s2_corpus"`).
- [ ] Note immediately tagged `influx:s2ag-terminal` AND
      `summary:s2ag-miss`.
- [ ] No `s2_*` frontmatter on the note (frontmatter fields are
      empty / absent because there is no payload).
- [ ] Repair sweep (when TB-7 lands) must respect `influx:s2ag-terminal`
      and not retry.  This slice provides the tag; TB-7 honours it.
- [ ] `influx_s2ag_skip_total{reason=not_in_s2_corpus}` emits.
- [ ] Unit test: fake transport returns 404 → terminal tag + miss tag,
      one counted attempt recorded.

### Blocked by

- TB-1

---

## TB-6 — Reference fan-out emits typed citation edges

**Type:** AFK
**Blocked by:** `agent-lore/influx#99`, TB-1

### What to build

After `lithos_write` succeeds for an arXiv-source note that has a
non-empty `s2_references` cascade payload, fan out typed citation
edges into Lithos.  This is the headline behaviour of Phase 2.

Critically depends on `agent-lore/influx#99` (LithosClient.edge_upsert
wrapper signature) being merged first — without that fix every edge
upsert call would fail.

### Acceptance criteria

- [ ] References are sorted by
      `(influentialCitationCount DESC, year DESC, paperId ASC)` and
      capped at 50 BEFORE any `lithos_cache_lookup` is issued.
- [ ] Resolution chain per reference:
      1. `lithos_cache_lookup(tags=["s2-paper-id:<target_hex>"])` for
         exact-match on S2 canonical ID.
      2. If miss AND `externalIds.ArXiv` present:
         `lithos_cache_lookup(source_url="https://arxiv.org/abs/<arxiv_id>")`.
      3. If still miss: silently drop (no counter, no warning, no tag).
- [ ] On hit, `lithos_edge_upsert` is called with:
      - `from_id` = source note id, `to_id` = target note id
      - `type` derived from S2AG `intents` (extension→`extends`,
        methodology→`applies_method_of`, result→`compares_with`,
        background→`cites_background`, none-or-multiple→`cites`)
      - `weight = 1.0`
      - `namespace = "influx"`
      - `provenance_actor = "influx-s2ag"`
      - `provenance_type = "s2ag_reference"`
      - `evidence = {"s2_paper_id": <target_hex>, "intents": [...],
        "contexts": [<≤1 short snippet>], "source_paper_id": <source_hex>}`
      - `conflict_state = None`
- [ ] Best-effort fan-out: per-reference errors logged + counted via
      `influx_s2ag_edge_upsert_failed_total{profile, reason}`, do
      NOT fail the parent note write.
- [ ] If ≥1 reference fails during a fan-out: bump `s2ag_attempts`
      ONCE (not per-edge), set `s2ag_last_stage="edges"`, tag
      `influx:repair-needed`.  TB-7 will own re-runs.
- [ ] `influx_s2ag_edges_emitted_total{profile, edge_type}` and
      `influx_s2ag_cache_lookup_total{result=tag_hit|url_hit|miss}`
      emit.
- [ ] The existing `arXiv:<id>` resolver (§10.5 of spec, code in
      `lcma.py:resolve_builds_on`) is removed or no-op'd.  Tier 3's
      `builds_on` field stays in the note for human readability but
      is no longer load-bearing for edge creation.
- [ ] Unit tests with a fake Lithos transport: happy fan-out, mixed
      hit/miss/transient-fail, intent-to-type mapping, cap-50
      ordering with ties, evidence-payload shape.
- [ ] Smoke test against staging Lithos verifies the resulting
      `edges.db` row carries the expected `provenance_actor`,
      `provenance_type`, and `evidence` JSON.

### Blocked by

- `agent-lore/influx#99` (edge_upsert wrapper bug)
- TB-1

---

## TB-7 — Repair sweep `s2ag` stage with counters and retries

**Type:** AFK
**Blocked by:** TB-1, TB-6

### What to build

Add s2ag as a new repair-sweep stage, ordered last (after
`tier3_extract`).  Re-runs enrichment when `s2_paper_id` is empty;
re-runs reference fan-out when the previous fan-out reported a
partial failure.  Honours `influx:s2ag-terminal` (set by TB-5 or by
exhaustion of the counted-failure cap).

### Acceptance criteria

- [ ] `RepairCounters` (`src/influx/repair_counters.py`) grows fields
      `s2ag_attempts`, `s2ag_last_stage`, `s2ag_last_error`, including
      render/parse round-trip via the `## Repair` markdown section.
- [ ] `CountedStage` Literal grows `"s2ag"`.  `bump_s2ag(stage=, error=)`
      method added; `attempts_for("s2ag")` mapping added.
- [ ] `Stages` decision struct grows `s2ag_retry: bool`.  Set true
      when:
      - `not influx:s2ag-terminal` AND
      - `s2ag_attempts < REPAIR_COUNTED_CAP` AND
      - (`frontmatter.s2_paper_id == ""` OR `s2ag_last_stage == "edges"`)
- [ ] New repair hook is the LAST stage in the sweep order (after
      `tier3_extract`).
- [ ] Behaviour:
      1. If `s2_paper_id == ""`: re-run `/paper/batch` enrichment.
         Success → update frontmatter and tags.  Counted failure →
         `bump_s2ag(stage="enrich", ...)`.  Transient failure →
         leave for next sweep.
      2. After step 1, if `s2_paper_id` is non-empty AND not terminal:
         re-fetch `/paper/{paperId}/references` and re-run the §5.3
         fan-out from TB-6.  Idempotent edge upsert means already-
         emitted edges no-op cleanly.  Per-edge failure →
         `bump_s2ag(stage="edges", ...)`.
- [ ] `s2ag_attempts >= REPAIR_COUNTED_CAP` flips
      `influx:s2ag-terminal`; sweep skips it on subsequent passes.
- [ ] Successful retry clears `influx:repair-needed` only when no
      other stage is also failing.
- [ ] Unit tests: counter round-trip, retry-then-succeed, retry-then-
      cap-then-terminal, partial fan-out recovery on second pass,
      terminal-respect.

### Blocked by

- TB-1
- TB-6

---

## TB-8 — Final observability: enrichment-duration histogram and ledger fields

**Type:** AFK
**Blocked by:** TB-1, TB-6

### What to build

Close out the metrics surface with the histogram and run-ledger
fields the operator dashboard will consume.  Trivially extends the
metric wiring already present in TB-1 and TB-6.

### Acceptance criteria

- [ ] `influx_s2ag_enrichment_duration_seconds{profile}` histogram
      emits per cascade s2ag-stage execution (full duration
      including any retries inside the stage).
- [ ] Run ledger gains additive fields `s2ag_calls` (int) and
      `s2ag_edges_written` (int); absent / 0 for runs where s2ag
      did nothing.
- [ ] `runs/recent` API surfaces both fields without breaking
      backward compatibility for clients that don't know about them.
- [ ] One smoke test verifies that a real ingest writes non-zero
      `s2ag_calls` and `s2ag_edges_written` to the ledger row.
- [ ] One Grafana / SigNoz panel mock-up (markdown only, no live
      dashboard) showing the `skip_total` →
      `calls_total{status}` → `cache_lookup_total{result}` →
      `edges_emitted_total` / `edge_upsert_failed_total` chain.

### Blocked by

- TB-1
- TB-6
