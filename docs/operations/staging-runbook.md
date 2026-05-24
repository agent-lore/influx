# Influx Staging Operator Runbook

**Audience:** anyone diagnosing a failed or partial scheduled run on
`influx-staging` without reading the source.

**Bar:** find the failure in under ten minutes, decide whether to
intervene or wait for the next sweep.

**Tooling:** `scripts/influx-diagnose.py` wraps every recipe in this
document. Run `./scripts/influx-diagnose.py --help` for the full
subcommand list.

---

## 1. Environment quick-reference

| Item                  | Where it lives                                                        |
| --------------------- | --------------------------------------------------------------------- |
| Container name        | `influx-staging` (set via `INFLUX_CONTAINER_NAME` in `docker/.env.staging`) |
| Run ledger (history)  | `${INFLUX_STATE_PATH}/runs.jsonl` — append-only JSONL.                |
| Active runs           | `${INFLUX_STATE_PATH}/active-runs.json` — keyed by `run_id`.          |
| Admin HTTP API        | `http://${INFLUX_ADMIN_BIND_HOST}:${INFLUX_ADMIN_HOST_PORT}` (default `127.0.0.1:18080`). |
| Logs                  | `docker logs influx-staging` — JSON-per-line via `InfluxJsonFormatter`. |

`scripts/influx-diagnose.py` reads `docker/.env.<env>` for these
values, so substituting environments is `--env dev` / `--env staging`.

## 2. Decide if there is a problem

Three quick signals, all read-only:

```
./scripts/influx-diagnose.py --env staging recent --limit 5
./scripts/influx-diagnose.py --env staging failures
./scripts/influx-report.py --env staging
```

- **`recent`** lists the last terminal runs with status, profile, kind,
  duration, and source-acquisition errors. Active runs (if any) appear
  above the list.
- **`failures`** filters to `failed`, `abandoned`, and `degraded` runs
  in one go. A `degraded` run completed but is flagged for at least
  one structured reason in `degraded_reasons`:
  - `source_acquisition` (issue #20) — at least one source-fetch
    failure was swallowed; the body of the run still landed.
  - `ingestion_stall` (issue #36) — this and the prior scheduled
    run for the same profile both inspected items but ingested
    zero; typical cause is every candidate hitting `slug_collision`
    / `duplicate`, all writes being cache-merge with no new content,
    or an upstream content shift.  The diagnose row prints the
    reason list so you can triage without parsing the JSON.
  - `fetch_stall` (issue #50) — this and the prior scheduled run
    for the same profile both saw `fetched_total == 0` (no source
    returned any items at all) despite historical non-zero fetches
    in the recent ledger window; typical cause is too-narrow
    `lookback_days` (nothing reached the fetch path) or an
    upstream feed shape change.  Brand-new profiles are not
    flagged.
  - `filter_stall` (issue #85) — this and the prior scheduled run
    for the same profile both saw `fetched_total > 0` but
    `sources_checked == 0` (sources fetched normally and the filter
    ran cleanly but rejected every candidate) despite historical
    non-zero inspections in the recent ledger window; typical cause
    is profile description drift, filter prompt regression, or
    `min_score_in_results` set too high.  Distinct from
    `fetch_stall`: routes operator attention to the filter, not the
    fetch path.  Distinct from `filter_error`: the scorer ran cleanly
    here.  Brand-new profiles are not flagged.
  - `filter_error` (issue #85, post-review) — the LLM filter scorer
    raised `FilterScorerError` at least once during this run
    (transport, parse, or provider error).  Single-run signal — no
    consecutive-runs gate, no `kind=scheduled` gate.  Operator triage:
    check `[providers]` configuration, model availability for the
    `[models.filter]` slot, and any recent prompt/response schema
    changes.  Do **not** investigate profile description, filter
    prompt, or `min_score_in_results` — those are the `filter_stall`
    signals; this one is purely about filter execution.  Mutually
    exclusive with `filter_stall`.

  The three stall reasons (`ingestion_stall`, `fetch_stall`,
  `filter_stall`) are mutually exclusive on any single run.
  `filter_error` is mutually exclusive with `filter_stall` (same
  shape, different cause) but orthogonal to the rest.

  ### Degradation severity (issue #164)

  On top of the flat `degraded_reasons` list, every completed run
  carries a `degradation_severity` bucket so dashboards / paging /
  triage can separate release-noise from real breakage without
  parsing the reason list. The bucket also appears as `severity=` in
  the `run completed` log line and as the `severity` label on the
  `run_completions` metric (always present, including on `skipped`
  and `failure` outcomes — see below).

  | Severity | Triggers | Operator action |
  |----------|----------|-----------------|
  | `success` | `degraded_reasons` empty | None |
  | `expected_lossy` | only `source_acquisition`, `source_cooldown_skip`, `archive_acquisition`, `invalid_url_rejections` present | Glance; investigate only if frequency spikes. These are tolerated upstream / policy-driven outcomes. |
  | `unexpected_failure` | any of `invalid_note_state`, `invalid_url_stall`, `ingestion_stall`, `fetch_stall`, `filter_stall`, `filter_error` present | Triage — actionable regression signal. |
  | `not_applicable` | `outcome=skipped` (circuit breaker fired) or `outcome=failure` (body raised before reasons were computed) | Read the existing `outcome` label — `severity` is set to a constant so the metric series shape stays uniform across all completions. |

  When unexpected and lossy reasons co-occur on the same run (the
  staging shape `reasons=archive_acquisition,invalid_note_state`),
  the run escalates to `unexpected_failure`. The flat
  `degraded_reasons` list still carries both entries so operators see
  the full picture; only the severity bucket is single-valued.
  A `skipped` run (#40) means the Lithos circuit breaker fired:
  ProbeLoop saw 3+ consecutive `degraded` Lithos probes, so the
  scheduler short-circuited to avoid burning LLM tokens against a
  write path that would fail.  Look at `/ready` and `/status` to
  confirm Lithos health; the breaker closes automatically on the
  first `ok` probe.
- **`influx-report.py`** queries `/status` + `/runs/recent` over HTTP
  for an at-a-glance view; useful when the container is unreachable
  from the ledger path (e.g. running on a remote host).

If everything is `completed` and not `degraded`, you are done. Otherwise
pick a `run_id` and dig in.

## 3. Drill into one run

```
./scripts/influx-diagnose.py run <run_id>
```

This prints:

1. The full ledger entry (status, started/completed timestamps,
   `degraded`, `source_acquisition_errors`).
2. Every JSON log record that carries `run_id == <run_id>`, in order.
   Default window is `--since 24h --tail 20000`; widen with
   `--since 7d` if the run is older.

If you want all WARNINGs / ERRORs for the same window without filtering
on `run_id`, use:

```
./scripts/influx-diagnose.py warnings --since 24h
./scripts/influx-diagnose.py warnings --contains "lithos_write returned"
./scripts/influx-diagnose.py warnings --run-id <run_id>
```

## 4. Common log shapes

All shapes below are emitted as JSON via `src/influx/logging_config.py:InfluxJsonFormatter`.
The `extra=` fields hang directly off the top-level record.

### 4.1 `lithos_write returned non-success`

| Field            | Meaning                                                              |
| ---------------- | -------------------------------------------------------------------- |
| `lithos_status`  | The top-level `status` from the Lithos response envelope.            |
| `source_url`     | The URL Influx tried to attach to the note.                          |
| `detail`         | Server-supplied diagnostic, when present.                            |
| `body_excerpt`   | First 500 chars of the raw JSON body when `detail` was empty — the breadcrumb that prevents 2026-04-30 style mysteries. |

**Status values worth knowing** (each is a top-level `status`, not
`status="error"` with a sub-`code`):

- `slug_collision` — Influx retries automatically with `[arXiv <id>]`
  suffix; expect a follow-up `created` for the same source URL.
- `version_conflict` — Influx re-reads, merges tags + user notes,
  retries once. A second `version_conflict` hard-aborts the run with
  `SweepWriteError` (sweep) or skips that article (initial write).
- `content_too_large` — Influx trims `## Full Text` then `## Tier 3`
  sections and retries. A third `content_too_large` is logged as
  chronic and the existing note is left untouched.
- `invalid_input` — Influx logs and skips. The raw body excerpt tells
  you which field Lithos rejected.

### 4.2 `article write skipped`

Emitted from `run.py`'s Ingest stage when the per-article write
fell through without producing a Lithos hit. Carries:

| Field        | Meaning                                                 |
| ------------ | ------------------------------------------------------- |
| `profile`    | Profile that produced the item.                         |
| `source_url` | Canonical source URL.                                   |
| `title`      | Title that was attempted.                               |
| `status`     | The `status` that came back (mirrors `lithos_status`).  |
| `detail`     | Diagnostic from the underlying envelope.                |
| `tags`       | The full tag set that was about to be written.          |
| `cache_hit`  | Always `false` here.                                    |

### 4.3 `sweep: <stage> failed for <note_id>`

Emitted from `repair._log_stage_failure` when an injected hook raises.
Stage is one of: `archive_download`, `text_extraction`, `tier2_enrichment`,
`tier3_extraction`, plus `parse_note` for unparseable notes.

| Field          | Meaning                                                       |
| -------------- | ------------------------------------------------------------- |
| `sweep_stage`  | The hook that failed.                                         |
| `note_id`      | Lithos note UUID.                                             |
| `profile`      | Profile that owns the sweep.                                  |
| `run_id`       | Current run.                                                  |
| `exc_type`     | Class name of the raised exception.                           |
| `model`        | LCMA model slot, when the failure is from `LCMAError`.        |
| `stage`        | Lower-level stage from `ExtractionError`/`LCMAError`.         |
| `detail`       | Free-form diagnostic from the exception.                      |
| `url`          | The URL the hook was working on, when relevant.               |

`stage` is the input to `repair.classify_failure`. Anything in
`{parse, validate, oversize}` is **counted** (advances the per-stage
attempt counter); everything else is **transient** (no counter bump).

### 4.4 Terminal-flip events

When a per-stage counter reaches `REPAIR_COUNTED_CAP=3`, the sweep adds
`influx:<stage>-terminal` and emits a WARNING with one of these
`sweep_stage` values:

- `archive_terminal_flip` (carries `archive_attempts`, `kind`, `detail`)
- `tier2_terminal_flip` (carries `tier2_attempts`, `stage`, `detail`)
- `tier3_terminal_flip` (carries `tier3_attempts`, `stage`, `detail`)

```
./scripts/influx-diagnose.py terminal-flips --since 7d
```

groups them by stage and lists the notes that flipped.

### 4.5 `notification webhook ...`

`notifications.py` emits structured WARNINGs when a webhook is skipped
or returns a non-2xx. `extra` carries `webhook_name`, `webhook_url`,
`status_code` (when the request actually went out).

## 5. Trigger or abort a run

### 5.1 Manual run

```
curl -fsS -X POST -H 'content-type: application/json' \
     -d '{"profile": "staging-ai"}' \
     http://127.0.0.1:18080/runs
```

`POST /runs` accepts `{"profile": "<name>"}` or `{"all_profiles": true}`,
not both. A `409 Conflict` with `reason="profile_busy"` means the
profile is already running — wait, or restart the container to clear
it. The successful response is `202` with the new `request_id`.

### 5.2 Abort an in-flight run

There is no `/runs/cancel` endpoint. To stop a stuck run, restart the
container — the active ledger entry will be marked `abandoned` on the
next start (`run_ledger.abandon_active`), and the next sweep starts
clean.

```
./scripts/influx-diagnose.py cancel
```

prints the exact restart command for the current environment.

## 6. Operator escape hatches

Influx never clears `influx:*-terminal` tags by itself. To re-arm a
note after fixing the underlying cause:

| Tag                          | Cap counter (`## Repair`) | Re-arm steps                                                                                       |
| ---------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------- |
| `influx:archive-terminal`    | `archive_attempts`        | Remove the tag in Lithos. Optionally also delete the `## Repair` block. Next sweep retries from 0. |
| `influx:tier2-terminal`      | `tier2_attempts`          | Same — remove the tag, optionally clear the counter, next sweep retries Tier 2.                    |
| `influx:tier3-terminal`      | `tier3_attempts`          | Same — remove the tag, optionally clear the counter, next sweep retries Tier 3.                    |
| `influx:text-terminal`       | _n/a_ (set explicitly when abstract-only re-extraction returns TERMINAL) | Remove the tag — abstract-only re-extraction will run next sweep. |

The full per-stage cap contract lives in
[`docs/SPECIFICATION.md` §11.1](../SPECIFICATION.md#111-per-stage-cap-and-self-repair).

## 7. Reading metrics from the OTEL backend

When the staging deployment has `INFLUX_OTEL_ENABLED=true` and an OTLP
endpoint configured, Influx exports metrics alongside spans. Use these
to answer "is the run progressing?" without `docker logs`.

| Question | Instrument |
| -------- | ---------- |
| Is anything actually running right now? | `sum(influx_active_runs)` per `profile`. |
| When did the last run start / finish? | `rate(influx_run_starts_total[15m])` and `rate(influx_run_completions_total[15m])` filtered by `outcome`. |
| How long are runs taking? | `histogram_quantile(0.95, influx_run_duration_seconds_bucket)` by `profile`. |
| Are runs degrading? | `sum by (profile, outcome) (rate(influx_run_completions_total[1h]))` — non-zero `outcome="degraded"` means swallowed source-fetch errors. |
| Is the source funnel narrowing as expected? | `rate(influx_source_candidates_fetched_total[1h])` → `rate(influx_articles_filtered_total{decision="pass"}[1h])` → `rate(influx_articles_inspected_total[1h])` → `rate(influx_lithos_writes_total{status=~"created\\|updated"}[1h])`. |
| Are writes failing silently? | `rate(influx_lithos_writes_total{status!~"created\\|updated"}[15m])` by `status`. |
| Is the LLM pipeline degrading? | `rate(influx_llm_validation_failures_total[1h])` by `tier`. |
| Is the repair sweep stuck on one stage? | `rate(influx_repair_candidates_total[1h])` by `kind`. |
| Are upstream sources flapping? | `rate(influx_source_acquisition_errors_total[1h])` by `source, kind`. |

Resource attributes on every metric: `service.name=influx` plus
`deployment.environment=<INFLUX_ENVIRONMENT>` (e.g. `staging`). Filter
by these on the collector if multiple environments share a backend.

### Reading logs in the OTEL backend

The same `INFLUX_OTEL_ENABLED=true` toggle that wires traces and metrics
also forwards Influx's structured log records to the OTEL backend
(issue #28). Each `logger.info / warning / exception` call is exported
as an OTEL log record alongside the existing stderr JSON stream — so
`docker logs` and `scripts/influx-diagnose.py` keep working unchanged,
and the OTEL backend becomes a second, queryable view of the same
records.

| Question | Query shape (LogQL-like) |
| -------- | ------------------------ |
| Why did this run degrade? | Filter by `run_id=<the one from the run_completions metric>` to see every log line emitted under that run. |
| What article writes were skipped? | Filter for body matching `article write skipped`; group by `status` (e.g. `duplicate`, `slug_collision`). |
| Which sources are throwing acquisition errors? | Filter for body matching `source acquisition error`; group by `source` and `kind`. |
| What is the repair sweep doing right now? | Filter for `sweep_stage` attribute presence; group by `sweep_stage`. |
| Did this exception fire during a specific run? | Severity `ERROR` filtered by `run_id` and `profile`. |

Every OTEL log record carries the same structured fields the JSON
formatter writes to stderr (`run_id`, `profile`, `source_url`, `status`,
`detail`, `note_id`, `sweep_stage`, `tags`, `cache_hit`, `exc_type`, …)
as OTEL log attributes — anything passed to `logger.<level>(...,
extra={...})` survives the trip to the collector. Resource attributes
on every record match the metric set: `service.name=influx` plus
`deployment.environment=<INFLUX_ENVIRONMENT>`.

When OTEL is disabled, no log handler is constructed and no records are
exported — the disabled path follows the same AC-10-A discipline as
spans and metrics.

## 8. Cleaning up slug-collision squatters

Influx now self-heals most slug collisions automatically.  When
``lithos_write`` returns ``slug_collision``, the client reads the
squatting doc and dispatches:

| Squatter shape | Action | Metric |
|---|---|---|
| Same paper (matching `arxiv-id` or `source_url`) | Treat as `duplicate` (Lithos's URL-dedup missed it) | `influx_slug_collision_dedup_recovery_total` |
| Empty residue (no tags, no `source_url`, empty body) | Delete + re-issue original write | `influx_slug_collision_reclaimed_total` |
| Different paper, same slug | Fall back to AC-05-D `[arXiv <id>]` suffix retry; if that also collides and the suffixed-slug squatter is also reclaimable, delete + retry once more | (no metric on the retry itself) |

Anything still `slug_collision` after the chain is appended to
`${INFLUX_STATE_PATH}/unresolved-slug-collisions.jsonl` and surfaced
via:

```bash
./scripts/influx-diagnose.py --env staging slug-collision-backlog
```

For the small minority of collisions that still need manual cleanup
(e.g. an operator must decide whether to delete a non-Influx note
that happens to share a slug), the existing `squatters` subcommand
still applies:

The ``squatters`` subcommand surfaces them and offers a confirmed
deletion path:

```
# 1. Read-only scan — list every squatter from the WARNING stream.
./scripts/influx-diagnose.py squatters --since 7d

# 2. Inspect the squatting doc body (recommended before deletion).
#    The output of step 1 prints `doc_id=<uuid>`; pass it to lithos_read
#    via your usual MCP client / influx admin path.

# 3. Delete one squatter, with audit trail.
./scripts/influx-diagnose.py squatters --apply --yes <doc-id>

# 4. Or wipe every squatter the scan found (use with care).
./scripts/influx-diagnose.py squatters --apply --yes-to-all
```

Safety properties:
- Default mode is a pure log scan — no Lithos connection.
- ``--apply`` requires either ``--yes <doc-id>`` (per-id confirmation,
  repeatable) or ``--yes-to-all``; passing ``--apply`` alone aborts.
- Before deleting, the script reads the doc and refuses unless its
  tags include ``ingested-by:influx``.  Pass
  ``--no-require-influx-authored`` to override (use only after manual
  review).
- Each delete is recorded in Lithos with ``agent=influx-diagnose``
  (override with ``--agent <name>``).

Today only the **second** collision (the suffix retry) appears in the
WARNING ``detail`` field, so cleaning the listed squatter unblocks
*one* of the two squatted slugs.  The next sweep typically exposes
the unsuffixed-slug squatter, which can be removed the same way.
[#32](https://github.com/agent-lore/influx/issues/32) tracks
surfacing both squatters in a single WARNING so they can be cleaned
in one pass.

## 8b. Cleaning up invalid-source-metadata notes (issue #162)

After an `invalid_source_metadata` repair incident (#150), notes whose
`source:*` metadata was empty/garbled and whose URL/path/id provided no
inference fallback were terminalised in-band — they carry
`influx:source-invalid` + `influx:text-terminal`, and the sweep no
longer loops on them.  The bad-state notes themselves still need
operator cleanup; the `invalid-source` subcommand surfaces them and
applies the recommended action.

When to run it:
- After a repair-related incident that mentioned `invalid_source_metadata`
  in WARNINGs, the related notes will carry `influx:source-invalid`.
- Periodically as a hygiene check (the tag has a clear semantic so a
  zero-result scan is the expected steady state).

```bash
# 1. Read-only audit — list every note tagged influx:source-invalid
#    along with the recommended action (RECONSTRUCT vs TOMBSTONE).
./scripts/influx-diagnose.py invalid-source

# 2. Inspect one specific note's classification without scanning the
#    whole index.
./scripts/influx-diagnose.py invalid-source --id <doc-id>

# 3. Apply the recommended action to one note.
./scripts/influx-diagnose.py invalid-source --apply --yes <doc-id>

# 4. Apply to every note in the audit (use after reviewing output).
./scripts/influx-diagnose.py invalid-source --apply --yes-to-all
```

Action semantics:
- **RECONSTRUCT** (recoverable note: URL/path/id implies a source).
  Backfills the `source:*` tag, drops `influx:source-invalid` and
  `influx:text-terminal`, re-arms `influx:repair-needed`.  The next
  scheduled sweep picks the note up and re-runs text extraction with
  the repaired metadata.
- **TOMBSTONE** (unrecoverable: no inference fallback exists).
  Adds `influx:tombstone` on top of the existing terminal state and
  drops `influx:repair-needed`.  The in-band terminal flags
  (`influx:source-invalid`, `influx:text-terminal`) are preserved as
  audit history.  Operators can later filter `influx:tombstone` in
  dashboards / search to exclude operator-cleaned notes.

Safety properties:
- Default mode is read-only.
- `--apply` requires either `--yes <doc-id>` (repeatable),
  `--yes-to-all`, or `--id <doc-id>`.  Passing `--apply` alone aborts.
- **`--apply` refuses any note that does not actually carry
  `influx:source-invalid`** even when the operator names it via
  `--id`.  This prevents an off-target id from rewriting an
  unrelated note's tags.  The read-only audit flags such notes
  upfront with an `INELIGIBLE` banner.
- Each rewrite is recorded in Lithos with `agent=influx-diagnose`
  (override with `--agent <name>`).
- The subcommand never deletes notes — use the `squatters` subcommand
  when actual removal is required.

## 8c. Promotion gate (issue #165)

A configurable gate that consumes the `degradation_severity` split
from #164 and produces a single PASS/FAIL verdict suitable for a
staging-promotion CI step.

```bash
./scripts/influx-diagnose.py promotion-gate [--window N] [--max-lossy-ratio R] [--min-runs M]
```

Policy:

- **Hard fail** on any `degradation_severity=unexpected_failure`
  run in the window (write-time data integrity, stall ratchets,
  scorer failures).  The acceptance criterion is "fail immediately"
  — even one such run trips the gate.
- **Soft fail** when the fraction of `expected_lossy` runs in the
  window is **strictly greater than** `--max-lossy-ratio` (default
  `0.5`).  A ratio exactly equal to the threshold passes — only
  ratios above it fail — so an operator setting `0.5` does not see
  the gate flap on a clean 50/50 split.  Tolerated upstream noise
  is fine in small doses but becomes a quality signal if it
  dominates.
- **Insufficient runs** when fewer than `--min-runs` (default 5)
  scheduled completed runs are present — prevents spurious
  PASS/FAIL on a freshly-started deployment.
- **PASS** otherwise.

Output is a stable, line-oriented summary with:
- the verdict + machine-readable `reason` code
- runs evaluated vs. configured window/min
- severity bucket counts
- the expected-lossy ratio vs. threshold
- top driver reasons / profiles (top 5 each)
- on hard-fail, the specific failing `run_id`s with their reasons

Exit codes: `0` on PASS, `1` on FAIL (any reason).  Suitable for
direct use in a CI step:

```bash
./scripts/influx-diagnose.py --env staging promotion-gate \
    --window 24 --max-lossy-ratio 0.4 \
    || { echo "Staging quality gate failed"; exit 1; }
```

Tuning the knobs:

- `--window`: roughly aligned with your scheduled cadence.  At one
  run per hour, `24` = last day.  Higher values trade
  responsiveness (a recent regression takes longer to clear) for
  stability (one bad run does not dominate the verdict).
- `--max-lossy-ratio`: depends on upstream noise floor.  Start
  near `0.5` and tighten as you trust the steady-state.
- `--min-runs`: bump to your usual daily run count if you only run
  the gate after a 24h window has filled.

The gate is read-only: no Lithos connection, no docker exec, no
ledger writes.  It only reads `${INFLUX_STATE_PATH}/runs.jsonl`.

## 8d. Thin-summary suppression (issue #166)

When an RSS or arXiv archive fetch fails for any reason, Influx
previously wrote a Lithos note built from the feed-provided
`<summary>` / arXiv `<abstract>` plus the title.  Many of those
summary-only notes are operationally garbage — Hacker News pointers
(`Discussion (47 points)`), title repetitions, generic teasers
(`Read the full article at example.com…`) — accumulating in Lithos
with no recovery path.

Issue #166 adds a forward-only **thin-summary suppression** rule.
When the archive fetch did not deliver a body **and** the feed
summary is "thin" by a structural test, the source adapter drops the
item entirely — no Lithos note, no `influx:archive-missing` tag, no
repair-sweep entry.  Existing notes already in Lithos are not
touched; only new items are affected.

The rule fires on `not archive_result.ok` for **any** failure kind —
including `non_html_source` (#160) and `unsupported` (#161) — so the
trigger scope is broader than the `archive_missing` flag alone.

### The rule (any-of)

| Rule | Fires when |
| ---- | ---------- |
| `length` | The trimmed summary is shorter than `min_summary_chars` (default 80).  Setting `min_summary_chars = 0` disables this rule only. |
| `title_equality` | The summary equals the title after normalisation (lowercase, strip Unicode punctuation, collapse whitespace). |
| `boilerplate` | The trimmed summary matches one of the patterns below. |

Order matters — the cheapest rule is evaluated first.  The rule that
fires is logged so an operator can decide which knob to tune.

### Boilerplate patterns

The pattern list ships in
`src/influx/thin_summary.py::BOILERPLATE_PATTERNS` as a single
module-level constant of `(regex, rationale)` pairs.  Current entries:

| Pattern | Rationale |
| ------- | --------- |
| `^Discussion \(\d+ points?\)$` (case-insensitive) | Hacker News pointer summary — the feed surfaces only the comment-thread points count, not the article body. |
| `^Comments(\.|\s|$)` (case-insensitive) | HN / Reddit pointer summary — the feed item is just a link to the comment thread. |
| `^Read (the |this )?(full |entire )?(article|post|story|entry) (at|on|via)\s` (case-insensitive) | Generic teaser — the feed publisher truncates the body to a "read the full article at…" pointer with no real content. |
| `^Continue reading\b` (case-insensitive) | WordPress / Substack-style truncation marker with no extra content beyond the title. |
| `^Read more\b` (case-insensitive) | Generic "Read more" truncation marker — the feed body is effectively empty. |
| `^[…\.\s\[\]]*$` | Empty-ish marker — the summary is only ellipsis / bracket characters / whitespace, no real text at all. |

To propose a new pattern: confirm the boilerplate shape on a real feed
sample, add the regex + rationale to the tuple, write a parametrised
case in `tests/unit/test_thin_summary.py::TestBoilerplateRule`, and
open a PR.  Patterns are deliberately anchored to the *start* of the
trimmed summary so an article that mentions "continue reading"
mid-body is not affected — only summaries that *are* the boilerplate.

### Tuning

Set in `influx.toml`:

```toml
[extraction]
min_summary_chars = 80   # default; raise to suppress more aggressively
```

Reasonable values:

- **0** — disables the length rule only.  Title-equality and
  boilerplate-match continue to fire.  Use this when an operator
  has audited a specific feed and wants to keep its terse-but-real
  summaries; tag-shape and pointer-style drops are still on.
- **80** (default) — drops the obvious pointer-shape garbage without
  affecting realistic feed summaries.
- **200+** — aggressive; will drop many feeds whose summaries are
  genuine first-sentence teasers.  Only tighten when an operator has
  measured the suppression impact on the run logs (see telemetry
  below).

### Telemetry

| Surface | Where | Notes |
| ------- | ----- | ----- |
| Per-drop INFO log | `influx.sources.rss` / `influx.sources.arxiv` | One line per dropped item: `thin-summary drop source=… profile=… url=… failure_kind=… rule=…`.  `rule` names the first rule that fired (`length` / `title_equality` / `boilerplate`). |
| Per-run INFO summary | `influx.run_service` | `run summary_thin_drops profile=… kind=… run_id=… dropped_items=N` when N > 0.  Distinct from the unsupported-policy summary above it. |
| OTel counter | `influx_summary_thin_drops_total` | Labels: `profile`, `source`, `failure_kind`, `rule`.  Distinct from `influx_archive_missing_total` and `influx_archive_policy_failures_total` so dashboards can pivot the suppression rate independently. |
| Ledger entry field | `summary_thin_drops_total` on each `runs.jsonl` entry | Per-run total; `None` on legacy entries written before #166 landed. |

The drop is deliberately **excluded** from:

- `archive_failures_total` (the item never received `influx:archive-missing`).
- The `archive_acquisition` degraded reason.
- `source_acquisition_errors`.
- The `degradation_severity` classification (a drop is a quality
  choice, not a degradation).

So a run whose only "issue" is thin-summary suppression remains
classified `success` with no degraded reasons, even if `dropped_items`
is large.  An operator sees the suppression in the dedicated INFO
line and the dedicated counter; the run-completion verdict is
unaffected.

### Out of scope

- **No retrospective pruning.**  Existing summary-only notes already
  in Lithos keep their current state.  A separate cleanup pass
  against Lithos would be needed to remove them and is intentionally
  out of scope here.
- **No per-profile threshold.**  A single global `min_summary_chars`
  applies to every profile; per-profile overrides can be added later
  if operator need emerges.
- **No repair-sweep re-evaluation.**  The thin-summary rule fires
  at write-time only.  A note tagged `influx:archive-missing` before
  #166 landed is not retroactively dropped on a later sweep.

## 8e. URL validation rules (issues #131 / #177)

Influx validates every article URL pre-acquire so a misbehaving feed
cannot persist garbage URLs into Lithos. The check happens in
`classify_article_url` (`src/influx/urls.py`) and runs against every
`<link>` parsed out of an RSS entry. Rejected entries are counted
toward the run's `invalid_url_rejections_total` and never reach the
LLM filter or the write path.

### What gets rejected

| Reason            | Examples that trip it                                          |
| ----------------- | -------------------------------------------------------------- |
| `malformed`       | unparseable URL string, missing scheme                         |
| `scheme`          | non-`http(s)` schemes (`file://`, `javascript:`, `data:`, …)   |
| `no_host`         | URL with empty host part                                       |
| `loopback`        | `localhost`, `127.0.0.0/8`, `::1` (the Sourcegraph 5174 case)  |
| `link_local`      | `169.254.0.0/16`, `fe80::/10`                                  |
| `private`         | RFC1918 (`10/8`, `172.16/12`, `192.168/16`) and IPv6 equivalents |
| `multicast`       | `224.0.0.0/4` and IPv6 equivalents                             |

Each rejection logs a WARNING with `profile`, `feed`, `url`, `reason`,
and `title` so operators can drill via:

```
./scripts/influx-diagnose.py warnings --contains "rss item rejected pre-acquire"
```

### Operator-facing severity

Two degraded reasons surface URL-validation activity at the run level
(see §2 degradation-severity table for placement):

- **`invalid_url_stall`** (unexpected_failure, single-run) — every
  fetched item was URL-rejected, so nothing reached the filter.
  Single bad-feed shape: typically a feed-shape regression upstream.
- **`invalid_url_rejections`** (expected_lossy, single-run, #177) —
  rejection count meets or exceeds the burst threshold (currently
  `10`, see `_INVALID_URL_REJECTIONS_BURST_THRESHOLD` in
  `run_ledger.py`) while some good items still reached the filter.
  Canonical incident: the Sourcegraph blog feed shipped
  `http://localhost:5174/...` links for 5 days in May 2026, rejecting
  ~30 URLs/run silently. The burst signal fires immediately so the
  next operator review surfaces it.

### Cleaning up after a leak

If bad URLs slipped into Lithos before the validation was tightened
(or before the burst signal was added), use the recipe from §8 plus
the `--no-require-influx-authored` flag — the bad-URL squatters may
have lost their `ingested-by:influx` tag through the repair sweep:

```
# Enumerate by host, e.g. localhost:5174:
LITHOS_URL=...
./scripts/influx-diagnose.py --env staging squatters --apply \
  --no-require-influx-authored \
  --id <doc-id-1> --id <doc-id-2> ...
```

Then drop the stale backlog entries (defensive backup first):

```
cp ${INFLUX_STATE_PATH}/unresolved-slug-collisions.jsonl \
   ${INFLUX_STATE_PATH}/unresolved-slug-collisions.jsonl.bak.$(date +%s)
# Filter out the bad host:
grep -v '"source_url": "http://<bad-host>/' \
  ${INFLUX_STATE_PATH}/unresolved-slug-collisions.jsonl.bak.* \
  > ${INFLUX_STATE_PATH}/unresolved-slug-collisions.jsonl
```

## 9. Scheduling stagger (issue #87)

Profiles share a single global `[schedule].cron` expression. Within
each tick, profiles run **sequentially in declared `[[profiles]]`
order**. Two `[schedule]` knobs shape that fan-out:

| Setting | Effect |
| ------- | ------ |
| `initial_jitter_seconds` | Sleep a uniform random `[0, N]` seconds at tick start, before any profile runs. Defaults to `0`. |
| `inter_profile_gap_seconds` | Sleep `N` seconds between consecutive profiles in the same tick. Not applied before the first profile or after the last. Defaults to `0`. |

Why this exists: when multiple profiles fire on the hour (e.g. `0 *
* * *`), they hit shared upstreams (arXiv categories, RSS feeds) at
exactly `:00:NN` of every hour and cluster with every other on-the-hour
arXiv consumer — easy 429 territory. `initial_jitter_seconds` walks the
tick off the boundary; `inter_profile_gap_seconds` separates profile-A
and profile-B requests within the tick.

Recommended starting values for staging:

```toml
[schedule]
cron = "0 * * * *"
initial_jitter_seconds = 30
inter_profile_gap_seconds = 1800   # 30 minutes
```

What this does **not** give you:

- Independent cadence per profile (e.g. profile-A every 6h, profile-B
  every 12h). All profiles share one cron expression. True per-profile
  cron is tracked separately under issue #90.

Verification after a config change:

```
./scripts/influx-diagnose.py recent --limit 10
./scripts/influx-diagnose.py warnings --since 24h --contains "429"
```

### arXiv retry / backoff hardening (#129)

The arXiv fetch path absorbs transient 429 and timeout conditions before
the run degrades, in addition to the staggering above:

- **Progressive 429 backoff.** Each successive 429 in a single fetch
  waits twice the previous delay (default `30, 60, 120, ...`), capped
  at `resilience.arxiv_429_backoff_max_seconds`. `Retry-After` headers
  override the computed delay but are clamped to the same cap.
- **Separate 429 retry budget.** 429 retries use
  `resilience.arxiv_429_max_retries` (default `5`), distinct from the
  `resilience.max_retries` budget that covers network / 5xx failures.
- **Cross-fetch pacing.** All arXiv fetches in the same process wait
  `resilience.arxiv_request_min_interval_seconds` since the previous
  fetch — so two profiles hitting arXiv in the same scheduled tick are
  paced even when `inter_profile_gap_seconds = 0`.
- **Recovered-retry diagnostics.** Every retry decision is recorded on
  the run-ledger entry as `source_retry_counts`. Operators can confirm
  hardening worked by looking for runs that show `source_retries`
  *without* the `degraded` flag — those are runs where the hardening
  absorbed a transient failure.

Acceptable residual degradation: a run can still finish `degraded` with
`source_acquisition` if arXiv is unreachable for longer than the
configured budget allows. With the defaults that is roughly
`(arxiv_429_max_retries + 1) * arxiv_429_backoff_max_seconds` ≈
`12 minutes` of sustained 429 pressure on a single fetch. Such runs are
expected production behaviour, not bugs.

Verification:

```
./scripts/influx-diagnose.py recent --limit 20
# Look for `source_retries: source=arxiv rate_limit=N` lines on
# non-degraded runs — those are recovered transient 429s.
```

If degradation still recurs after staggering and the new defaults, the
next levers are operator-side: raise `arxiv_429_max_retries`, raise
`arxiv_429_backoff_max_seconds`, or widen `inter_profile_gap_seconds`.

## 10. Reference

- Run ledger schema: `src/influx/run_ledger.py` (`RunEntry` TypedDict).
- Admin endpoints: `src/influx/http_api.py` (`/live`, `/ready`,
  `/status`, `/runs/recent`, `POST /runs`, `POST /backfills`).
- Structured log fields: each `logger.warning(..., extra={...})` call
  in `src/influx/`. The `terminal-flips` and `warnings` subcommands
  pull these structured fields without forcing operators to remember
  the JSON keys.
- Metric instruments: `src/influx/metrics.py` (helper per instrument)
  and `docs/SPECIFICATION.md` §13.2 for the label contract.
- Master spec for the sweep: `docs/SPECIFICATION.md` §11.
- Terminal cap rationale and prior incident notes: PR #11 (initial
  data layer), PR #15 (archive cap), PR #25 (archive_download hook),
  PR #26 (text_extraction_retry hook).
