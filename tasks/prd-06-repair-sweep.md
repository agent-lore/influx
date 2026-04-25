# PRD 06 — Repair Sweep + `influx:text-terminal` Classifier + Retry-Order Advancement

**Part of:** Influx v1 (see `tasks/prd-influx-v1-index.md`)
**Covers master PRD story:** S-7b (split from the master PRD's S-7)
**Milestone:** M1
**Prerequisites:** PRD 01, 02, 03, 04, 05
**Downstream PRDs that depend on this:** 07 (extraction provides the
abstract-only re-extraction worker the sweep calls), 09 (multi-profile
shared sweep), 10 (final stub removal).

---

## 1. Context

Influx keeps **no** local queue, run-history store, or dedup database.
Durable retry for failed enrichment, archive download, and abstract-only
re-extraction is driven entirely by tags in Lithos, using the repair sweep
defined in master PRD §6.18.

This is the trickiest invariant in the entire v1 product. The sweep:

1. Picks up to `repair.max_items_per_run` notes per profile per run,
   ordered by oldest `updated_at` first.
2. Independently selects retry stages based on which Influx-owned tags
   are present.
3. Rewrites EVERY visited note (regardless of whether repair succeeded,
   partially succeeded, or failed) so `updated_at` advances and the
   cursor moves on. This is the **retry-order advancement mechanism**
   that gives the sweep its fairness guarantee.
4. Has exactly one exemption: chronic `content_too_large` on the repair
   path (handled by PRD 05's wrapper) leaves a note's `updated_at`
   unchanged but does NOT abort the run.

The new tag `influx:text-terminal` is introduced and owned by this PRD.
It marks `text:abstract-only` notes whose archive has been
**successfully re-extracted** and still yielded no better text.
Exhausting the extractor's retry budget is **not** terminal — it leaves
the note in the sweep for a later run.

## 2. In scope

- `lithos_list(tags=["influx:repair-needed", f"profile:{profile}"],
  limit=repair.max_items_per_run, order_by="updated_at",
  order="asc")` consumed by every scheduled and manual `POST /runs` for
  a profile (NOT backfills — FR-REP-2).
- Per-note stage selection logic (archive download retry, text
  extraction retry, abstract-only re-extraction, Tier 2 retry, Tier 3
  retry) based on the tag set, per FR-REP-1.
- The `influx:text-terminal` tag and its three-outcome semantics
  (Upgrade / Terminal / Transient failure).
- The clearing rules for `influx:archive-missing` and
  `influx:repair-needed`, including the high-score terminal exemption
  (AC-X-7 high-score clearing).
- Rewrite-on-every-visit invariant + the
  chronic-`content_too_large`-on-repair exemption (`updated_at` does
  NOT advance, run does NOT abort, sweep continues).
- Honour the execution coordinator (FR-REP-4): the sweep runs inside
  the same per-profile serialised slot as the rest of that profile's
  run, never opens a second concurrent slot.

## 3. Out of scope

- The actual extraction worker (HTML/PDF/article) — PRD 07. This PRD
  imports a `re_extract_archive(note, archive_path)` hook and calls
  it; PRD 07 supplies the real implementation. For this PRD's tests,
  the hook is parameterised so tests can drive each of the three
  outcomes.
- Tier 2 / Tier 3 enrichment workers — PRD 07. Same hook pattern.
- Backfill — backfills explicitly skip the sweep (FR-REP-2). Backfill
  flow itself lives in PRD 09.

## 4. Internal seams permitted

- Hooks for `re_extract_archive`, `tier2_enrich`, `tier3_extract` are
  test-injectable callables that PRD 07 wires to real implementations.
- The sweep's worker selection uses these hooks as opaque callables
  returning either a success result or raising `ExtractionError` /
  `LithosError`.

## 5. Functional Requirements (master PRD §6.18 + §9.7 repair clause)

### 5.1 Sweep entry point

- **FR-REP-1.** At the start of every scheduled run and every manual
  `POST /runs` for a profile, Influx calls
  `lithos_list(tags=["influx:repair-needed", f"profile:{profile}"],
  limit=repair.max_items_per_run, order_by="updated_at", order="asc")`.
  - `repair.max_items_per_run` is a **dedicated** config knob
    (default 100). MUST NOT be aliased to
    `feedback.negative_examples_per_profile`.
  - Ordering MUST be oldest `updated_at` first.

### 5.2 Per-note stage selection

For each returned note, re-read via `lithos_read`, then select stages
**independently** by inspecting current Influx-owned tags. Any subset
may be outstanding:

- **Archive retry.** `influx:archive-missing` present → retry archive
  download (delegate to PRD 04's `storage.archive_download`). Selected
  even when a valid `text:*` tag is already on the note.
- **Text extraction retry.** Missing any `text:*` tag → retry text
  extraction. If extraction depends on the archived file and the
  archive stage was selected this pass, the archive stage MUST run
  first.
- **Abstract-only re-extraction.** `text:abstract-only` present AND
  `influx:text-terminal` absent AND (archive stage succeeded this
  pass OR a non-empty `path:` line is stored in `## Archive`) →
  retry text extraction against the available archive. Three outcomes
  (PRD 07's worker returns a discriminator):
  - **Upgrade.** Yields `text:html` or `text:pdf`. Replace
    `text:abstract-only` with the upgraded tag. Do NOT add
    `influx:text-terminal`.
  - **Terminal.** Extraction completes successfully and still yields
    abstract-quality text. Keep `text:abstract-only`, add
    `influx:text-terminal`.
  - **Transient failure.** Extraction fails after its retry budget
    this pass. Keep `text:abstract-only` and `influx:repair-needed`,
    DO NOT add `influx:text-terminal`. Note re-enters the sweep on a
    later run.
- **Tier 2 retry.** Missing `full-text` AND current max profile score
  ≥ `thresholds.full_text` AND `influx:text-terminal` absent → retry
  Tier 2 enrichment. Terminal abstract-only notes are NOT candidates
  for this stage.
- **Tier 3 retry.** Missing `influx:deep-extracted` AND current max
  profile score ≥ `thresholds.deep_extract` AND `influx:text-terminal`
  absent → retry Tier 3. Terminal abstract-only notes are NOT
  candidates for this stage.

### 5.3 Tag-removal rules on rewrite

After all selected stages have run, rewrite the note per
`docs/REQUIREMENTS.md` §9.5. Apply these rules:

- `influx:archive-missing` is removed **iff** a non-empty `path:` line
  is stored in `## Archive` on this pass per FR-NOTE-9.
- `influx:repair-needed` is removed **iff** ALL of:
  (a) the note stores a non-empty `path:` line in `## Archive`;
  (b) the note carries a `text:*` tag that is either `text:html`,
      `text:pdf`, OR `text:abstract-only` accompanied by
      `influx:text-terminal`;
  (c) if max profile score ≥ `thresholds.full_text` AND
      `influx:text-terminal` is absent, the note carries `full-text`;
  (d) if max profile score ≥ `thresholds.deep_extract` AND
      `influx:text-terminal` is absent, the note carries
      `influx:deep-extracted`.

  A note with `influx:text-terminal` is treated as a **complete
  terminal state** for Tier 2 and Tier 3: those tiers are not selected
  above and not required for clearing here. This is the explicit
  high-score terminal exemption (AC-X-7 high-score clearing).

  A `text:abstract-only` note WITHOUT `influx:text-terminal` is
  **never cleared**, regardless of which other stages succeed.

- A partially-completed repair keeps `influx:repair-needed` and every
  unsatisfied stage-specific tag in place.

### 5.4 Retry-order advancement (the central invariant)

Every note visited by the sweep — whether its repair succeeded,
partially succeeded, or failed within this pass — MUST be rewritten via
`lithos_write` before the sweep moves to the next candidate. Even a
repair that made no forward progress MUST re-emit the Influx-owned tag
set so Lithos bumps `updated_at`.

Two distinct failure modes:

1. **Write failure that aborts the run.** A visited note's
   `lithos_write` fails after its own retry budget on a generic write
   error (unresolved `version_conflict`, transport failure under
   FR-RES-3). Run aborts, readiness becomes degraded, `updated_at`
   does not advance. Next run revisits the same head of the list.
2. **Chronic `content_too_large` on the repair path.** This is the
   sole exemption. The wrapper from PRD 05 already implements the
   logic: leave the existing note untouched, log + count in
   `content_too_large_skipped`, sweep proceeds to the next candidate
   without aborting. The note's `updated_at` does NOT advance; it
   stays at the head of the `updated_at asc` list until either a
   future trimmed write succeeds or an operator intervenes. The
   fairness guarantee below excludes notes in this state.

### 5.5 Fairness guarantee

For a stable backlog of size B with throughput
`repair.max_items_per_run = M` and no chronic-oversize notes:

- Every persistently-failing note is visited at least once every
  `ceil(B / M)` runs.
- v1 does NOT guarantee single-run exhaustion. The sweep is
  best-effort first-N per run.

### 5.6 Coordinator

- **FR-REP-4.** The sweep MUST run inside the same per-profile
  serialised slot as the rest of that profile's run. Never opens a
  second concurrent slot.

### 5.7 Backfill exclusion

- **FR-REP-2.** Backfills (`POST /backfills`) do NOT run the repair
  sweep. The sweep is a property of normal scheduled and manual runs
  only. (Backfill flow lives in PRD 09; this PRD ensures the sweep
  entry point checks `kind != "backfill"`.)

### 5.8 Reserved tags introduced by this PRD

- **FR-TAG-1 (delta).** Two tags that the master PRD attributes to
  this PRD's slice:
  - `influx:archive-missing` — already written by PRD 04's storage
    layer; this PRD owns the **clearing** logic.
  - `influx:text-terminal` — entirely owned here. Set ONLY when the
    abstract-only re-extraction outcome is Terminal (successful
    extraction yielded no better text). Never set on initial write
    of a note (per AC-M2-3).

## 6. Files to create / modify

### Create
- `src/influx/repair.py` — sweep entry point, stage selection, tag
  clearing rules, retry-order advancement enforcement.
- `tests/unit/test_repair_stage_selection.py`
- `tests/unit/test_repair_clearing_rules.py`
- `tests/unit/test_text_terminal.py`
- `tests/integration/test_repair_sweep_archive_only.py`
- `tests/integration/test_repair_sweep_abstract_only.py` — covers all
  three AC-X-7 outcomes
- `tests/integration/test_repair_sweep_high_score_terminal.py`
- `tests/integration/test_repair_sweep_advancement.py` — AC-X-8
  rewrite-on-every-visit invariant
- `tests/integration/test_repair_sweep_chronic_oversize.py` — AC-X-8
  chronic exemption

### Modify
- `src/influx/service.py` — call `repair.sweep(profile)` at the start
  of every scheduled and manual `POST /runs` for that profile, before
  the normal source fetch + filter pass.
- `src/influx/lithos_client.py` — ensure `lithos_read` and the
  `lithos_list` ordering (`order_by`, `order`) are exposed.

## 7. Dependencies to add

None. (All dependencies came in earlier PRDs.)

## 8. Acceptance Criteria

### From master PRD §7.5

- **AC-X-4** (full). Oversize abort + repair: tagged
  `influx:repair-needed` + `influx:archive-missing` initially with
  empty `## Archive`; the next run's sweep retries the archive
  download independently of the `text:*` tag, writes the `path:` line
  on success, and removes `influx:archive-missing` (and
  `influx:repair-needed` if no other outstanding-stage tag remains).
  Tests MUST assert exact `## Archive` body in both states.

- **AC-X-7.** Abstract-only re-extraction. A note that starts with
  `text:abstract-only`, `influx:repair-needed`, and a non-empty
  `path:` line in `## Archive` is picked up by the next run's sweep.
  Three possible outcomes — tests MUST exercise all three:
  (a) **Upgrade** → `text:html`/`text:pdf`, `influx:text-terminal`
      NOT added.
  (b) **Terminal** → keep `text:abstract-only`, ADD
      `influx:text-terminal`.
  (c) **Transient failure** → keep `text:abstract-only` and
      `influx:repair-needed` WITHOUT `influx:text-terminal`.

  **High-score terminal clearing.** A note reaching outcome (b) with
  max profile score ≥ `thresholds.deep_extract` MUST be cleared from
  the sweep on a run where no other outstanding-stage tag remains,
  EVEN THOUGH it carries neither `full-text` nor
  `influx:deep-extracted`. A test MUST seed score=9, terminal
  abstract-only, archive path stored, and assert the note carries no
  `influx:repair-needed` after one repair pass and is not re-selected
  by the next sweep.

- **AC-X-8.** Retry-order advancement. With B >
  `repair.max_items_per_run` persistently-failing notes (where
  "persistently-failing" means repair did not make progress but
  `lithos_write` itself succeeded — chronic-oversize-on-repair does
  NOT apply), two successive runs together visit ≥ `min(B, 2 × M)`
  distinct notes, and no note visited in run K is in the
  first-N oldest-by-`updated_at` slice of run K+1 unless every other
  repair-needed note has already been visited in the intervening
  runs. A test MUST assert the rewrite-on-every-visit invariant.

  A separate test MUST cover the chronic-oversize exemption: seed one
  note hitting a second `content_too_large` on the repair path;
  across ≥ 2 runs assert (i) the run does NOT abort, (ii) the note's
  `updated_at` does NOT advance, (iii) it stays at the head of the
  `updated_at asc` list, (iv) other repair-needed notes still make
  progress.

### From master PRD §7.3

- **AC-M3-6.** A repair pass on a note with
  `influx:rejected:<profile>` does NOT re-add `profile:<profile>`
  even if the source matches that profile again. (Note: the actual
  filter logic lives in PRD 09's multi-profile orchestration; this
  PRD's responsibility is to ensure the sweep's tag-merge step
  honours the rejection authority defined in FR-NOTE-6.)

### New for this PRD

- **AC-06-A.** Sweep stage selection: a note tagged `text:html` AND
  `influx:archive-missing` triggers the **archive** stage but NOT
  the text-extraction stage (the bug class FR-ST-4 closed).
- **AC-06-B.** Sweep clearing: a note that successfully completes
  archive download + abstract-only-Upgrade in one pass clears both
  `influx:archive-missing` and `influx:repair-needed` (assuming no
  other outstanding-stage tag remains).
- **AC-06-C.** A `text:abstract-only` note WITHOUT
  `influx:text-terminal` is NEVER cleared from `influx:repair-needed`,
  even if archive + Tier 2 + Tier 3 all succeed in this pass — the
  abstract-only state must first be resolved via Upgrade or Terminal.
- **AC-06-D.** Backfill (`POST /backfills`) does NOT call the sweep
  entry point.
- **AC-06-E.** Two profiles' sweeps run inside their respective
  per-profile coordinator slots; no second slot is opened. (Wires
  via PRD 03's coordinator; the test asserts the lock is held for
  the union of sweep + normal fetch, not held twice.)
- **AC-06-F.** A `lithos_write` `version_conflict` during the sweep
  rewrite triggers re-read + re-merge + retry once per FR-MCP-7;
  second conflict aborts the run per FR-RES-3 (failure mode 1 of the
  retry-order advancement clause).

## 9. Tests required

- Unit tests for stage selection over a matrix of tag combinations
  (cover at minimum: archive-missing only; text:abstract-only only;
  archive-missing + text:abstract-only; high-score deep-extract eligible;
  text-terminal present).
- Unit tests for clearing rules over the same matrix.
- Integration tests for all five AC-X-4 / AC-X-7 / AC-X-8 cases listed
  in §13 of the master PRD's testing strategy:
  - archive-only repair flow
  - abstract-only re-extraction (all three outcomes)
  - high-score-terminal clearing
  - retry-order advancement
  - chronic `content_too_large` exemption
- Coverage ≥ 80% on `repair.py`.

## 10. Definition of Done

- [ ] All AC-X-4, AC-X-7, AC-X-8, AC-M3-6, AC-06-A…F satisfied.
- [ ] The `re_extract_archive` / `tier2_enrich` / `tier3_extract` hook
      signatures are documented and stable; PRD 07 fills them in
      without changing the interface.
- [ ] Backfill flow does NOT trigger the sweep (verified by test).
- [ ] Coordinator slot is held exactly once per profile run, including
      the sweep portion.
- [ ] Ruff, pyright, pytest all green. Coverage ≥ 80% on new modules.
