# Citation Graph — Issue Drafts

Date: 2026-05-10
Status: Drafted, not yet filed
Source plan: `docs/plans/citation-graph.md` (v1.1.0)

These six tracer-bullet issues implement the simplified Phase 1+2
citation graph extension. All are AFK — the design has been pinned in
the parent plan; no further architectural calls are needed.

---

## Filing instructions (for later)

- **Repo:** `agent-lore/influx`
- **Labels (all issues):** `enhancement`, `ready-for-agent`, `needs-triage`
- **Dependency order:** file in the order TB-1 → TB-2 → … → TB-6 so
  each "Blocked by" reference can use the real GitHub issue number of
  the prior slice.
- **Prerequisite already filed:** `agent-lore/influx#99` blocks TB-3.
- **One-liner per issue:** use `gh issue create --repo agent-lore/influx
  --title "<title>" --label "enhancement,ready-for-agent,needs-triage"
  --body "$(cat <<'EOF'
  <body>
  EOF
  )"` (HEREDOC body matches the format below).

After filing, replace `<TB-N>` placeholders in subsequent `Blocked by`
lines with the real issue numbers.

---

## TB-1 — S2AG foundation: enrichment writes stable IDs and raw fields on arXiv notes

**Type:** AFK
**Blocked by:** None — can start immediately

### What to build

Stand up the S2AG enrichment pipe end-to-end so that every arXiv-source
note Influx writes picks up stable S2 state in its frontmatter and
in-memory references for later edge fan-out.

Touches: a new `src/influx/s2ag.py` module (async client), the existing
`Cascade` (new injection point between Tier 2 and Tier 1, gate
`score >= thresholds.relevance`, applicability gated to arXiv-source
candidates only — RSS items pass `arxiv_id=None` and short-circuit),
the per-tick `FetchCache` (reuse `get_or_fetch` keyed by
`("s2ag", paperId)`), the renderer / note-writer (frontmatter + tag
emission), config (`[s2ag]` section, `[resilience]
s2ag_429_backoff_seconds`), and metrics (`influx_s2ag_calls_total`,
`influx_s2ag_skip_total`).

Out of scope for this slice: concept tags (TB-2), edges (TB-3),
paper-not-in-S2 handling (TB-4), repair sweep (TB-5).

### Acceptance criteria

- [ ] `src/influx/s2ag.py` exposes `S2agClient` with
      `paper_batch(ids)` and `references(id)` methods, using the
      existing guarded HTTP path.
- [ ] `Cascade` gains an injection point
      `s2ag_enricher: S2agEnricher | None` that runs after Tier 2 and
      before Tier 1, gated by `score >= thresholds.relevance` AND
      `arxiv_id is not None`.
- [ ] `EnrichedSections` carries new fields
      `s2ag: S2agEnrichment | None` and `s2ag_attempted: bool`.
- [ ] `S2agEnrichment` contains at minimum `s2_paper_id`
      (40-char hex or empty), `s2_fields_of_study` (list),
      `s2_reference_count`, `s2_citation_count`, and `s2_references`.
- [ ] `s2_references` is populated on the cascade output but not
      persisted to frontmatter (in-memory only — TB-3 consumes it).
- [ ] Note frontmatter on arXiv-source notes includes `s2_paper_id`
      and `s2_fields_of_study`. `s2_paper_id` is present and set to the
      empty string when enrichment was skipped or failed.
- [ ] Tag emission: when `s2_paper_id` is non-empty, the note carries
      a `s2-paper-id:<hex>` tag.
- [ ] RSS-source notes are unchanged: no `s2_*` frontmatter, no
      `s2-paper-id:*` tag, no S2AG calls (verifiable via metrics).
- [ ] Per-tick `FetchCache` is reused — concurrent ingestion of the
      same `paperId` in two profiles produces exactly one S2AG call.
- [ ] Config `[s2ag]` section as in §5.7 of the plan: `enabled`,
      `base_url`, `api_key_env`, `request_timeout`, `batch_size`.
      `[resilience].s2ag_429_backoff_seconds`.
- [ ] Metrics `influx_s2ag_calls_total{endpoint, status}` and
      `influx_s2ag_skip_total{reason=non_arxiv_source|disabled}` emit.
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
- [ ] Slug normaliser converts `"Computer Science"` →
      `"computer-science"` (lowercase, single non-alphanumeric run
      collapses to `-`, trim leading/trailing `-`).
- [ ] Each of the (up to) three categories produces a `field:<slug>`
      tag on the note.
- [ ] Cap at 3 tags even when S2AG returns more.
- [ ] No top-level allow/deny list. Reasoning lives in §5.4 of the
      plan.
- [ ] Unit tests cover: 0 categories, 1 category, 5 categories
      (asserts cap), unicode-bearing category names (e.g.
      `"Pharmacology & Toxicology"`).

### Blocked by

- TB-1

---

## TB-3 — Reference fan-out emits `cites` edges

**Type:** AFK
**Blocked by:** `agent-lore/influx#99`, TB-1

### What to build

After `lithos_write` succeeds for an arXiv-source note that has a
non-empty `s2_references` cascade payload, fan out `cites` edges into
Lithos. This is the headline behaviour of Phase 2.

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
      - `type = "cites"`
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
      `influx:repair-needed`. TB-5 will own re-runs.
- [ ] `influx_s2ag_edges_emitted_total{profile}` and
      `influx_s2ag_cache_lookup_total{result=tag_hit|url_hit|miss}`
      emit.
- [ ] The existing `arXiv:<id>` resolver (§10.5 of spec, code in
      `lcma.py:resolve_builds_on`) is removed or no-op'd. Tier 3's
      `builds_on` field stays in the note for human readability but is
      no longer load-bearing for edge creation.
- [ ] Unit tests with a fake Lithos transport: happy fan-out, mixed
      hit/miss/transient-fail, cap-50 ordering with ties,
      evidence-payload shape.
- [ ] Smoke test against staging Lithos verifies the resulting
      `edges.db` row carries the expected `type`, `provenance_actor`,
      `provenance_type`, and `evidence` JSON.

### Blocked by

- `agent-lore/influx#99` (edge_upsert wrapper bug)
- TB-1

---

## TB-4 — Paper-not-in-S2-corpus → counted-once + terminal

**Type:** AFK
**Blocked by:** TB-1

### What to build

Distinguish "S2AG returned 404 / empty body for a known arXiv ID" from
generic counted failures. Retrying achieves nothing because S2AG's
corpus state is not going to change for a paper it does not know.
Treat as a single counted attempt that immediately latches
`influx:s2ag-terminal`.

### Acceptance criteria

- [ ] `S2agClient` distinguishes "paper not in S2" (HTTP 404 from
      `/paper/...` for a well-formed arXiv ID, or empty `data` from
      `/paper/batch`) from generic counted failures.
- [ ] On paper-not-in-S2: cascade increments `s2ag_attempts` once
      (via the partial counter machinery TB-5 will own; for now record
      on the note's `## Repair` block as `s2ag_attempts: 1`,
      `s2ag_last_stage: "enrich"`,
      `s2ag_last_error: "not_in_s2_corpus"`).
- [ ] For arXiv-source notes, `s2_paper_id: ""` remains present in
      frontmatter so repair has a stable selector even when there is no
      payload.
- [ ] Note immediately tagged `influx:s2ag-terminal`.
- [ ] Repair sweep (when TB-5 lands) must respect
      `influx:s2ag-terminal` and not retry. This slice provides the
      tag; TB-5 honours it.
- [ ] `influx_s2ag_skip_total{reason=not_in_s2_corpus}` emits.
- [ ] Unit test: fake transport returns 404 → terminal tag, one
      counted attempt recorded.

### Blocked by

- TB-1

---

## TB-5 — Repair sweep `s2ag` stage with counters and retries

**Type:** AFK
**Blocked by:** TB-1, TB-3

### What to build

Add s2ag as a new repair-sweep stage, ordered last (after
`tier3_extract`). Re-runs enrichment when `s2_paper_id` is empty;
re-runs reference fan-out when the previous fan-out reported a partial
failure. Honours `influx:s2ag-terminal` (set by TB-4 or by exhaustion
of the counted-failure cap).

### Acceptance criteria

- [ ] `RepairCounters` (`src/influx/repair_counters.py`) grows fields
      `s2ag_attempts`, `s2ag_last_stage`, `s2ag_last_error`, including
      render/parse round-trip via the `## Repair` markdown section.
- [ ] `CountedStage` Literal grows `"s2ag"`. `bump_s2ag(stage=, error=)`
      method added; `attempts_for("s2ag")` mapping added.
- [ ] `Stages` decision struct grows `s2ag_retry: bool`. Set true
      when:
      - `not influx:s2ag-terminal` AND
      - `s2ag_attempts < REPAIR_COUNTED_CAP` AND
      - (`frontmatter.s2_paper_id == ""` OR
        `s2ag_last_stage == "edges"`)
- [ ] New repair hook is the LAST stage in the sweep order (after
      `tier3_extract`).
- [ ] Behaviour:
      1. If `s2_paper_id == ""`: re-run `/paper/batch` enrichment.
         Success → update frontmatter and tags. Counted failure →
         `bump_s2ag(stage="enrich", ...)`. Transient failure → leave
         for next sweep.
      2. After step 1, if `s2_paper_id` is non-empty AND not terminal:
         re-fetch `/paper/{paperId}/references` and re-run the §5.3
         fan-out from TB-3. Idempotent edge upsert means already-
         emitted edges no-op cleanly. Per-edge failure →
         `bump_s2ag(stage="edges", ...)`.
- [ ] `s2ag_attempts >= REPAIR_COUNTED_CAP` flips
      `influx:s2ag-terminal`; sweep skips it on subsequent passes.
- [ ] Successful retry clears `influx:repair-needed` only when no other
      stage is also failing.
- [ ] Unit tests: counter round-trip, retry-then-succeed, retry-then-
      cap-then-terminal, partial fan-out recovery on second pass,
      terminal-respect.

### Blocked by

- TB-1
- TB-3

---

## TB-6 — Final observability: enrichment-duration histogram and ledger fields

**Type:** AFK
**Blocked by:** TB-1, TB-3

### What to build

Close out the metrics surface with the histogram and run-ledger fields
the operator dashboard will consume. Trivially extends the metric
wiring already present in TB-1 and TB-3.

### Acceptance criteria

- [ ] `influx_s2ag_enrichment_duration_seconds{profile}` histogram
      emits per cascade s2ag-stage execution (full duration including
      any retries inside the stage).
- [ ] Run ledger gains additive fields `s2ag_calls` (int) and
      `s2ag_edges_written` (int); absent / 0 for runs where s2ag did
      nothing.
- [ ] `runs/recent` API surfaces both fields without breaking backward
      compatibility for clients that do not know about them.
- [ ] One smoke test verifies that a real ingest writes non-zero
      `s2ag_calls` and `s2ag_edges_written` to the ledger row.
- [ ] One Grafana / SigNoz panel mock-up (markdown only, no live
      dashboard) showing the `skip_total` →
      `calls_total{status}` → `cache_lookup_total{result}` →
      `edges_emitted_total` / `edge_upsert_failed_total` chain.

### Blocked by

- TB-1
- TB-3
