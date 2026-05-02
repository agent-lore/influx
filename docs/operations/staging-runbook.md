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
./scripts/influx-diagnose.py recent --limit 5
./scripts/influx-diagnose.py failures
./scripts/influx-report.py staging
```

- **`recent`** lists the last terminal runs with status, profile, kind,
  duration, and source-acquisition errors. Active runs (if any) appear
  above the list.
- **`failures`** filters to `failed`, `abandoned`, and `degraded` runs
  in one go. A `degraded` run completed but had at least one swallowed
  source-fetch failure (issue #20); the body of the run still landed.
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

Emitted from `scheduler.py` when the per-article write fell through
without producing a Lithos hit. Carries:

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

OTEL log export is **not** wired in this revision; the runbook still
relies on `docker logs` (and `influx-diagnose.py`) for log inspection.

## 8. Cleaning up slug-collision squatters

When ``lithos_write`` returns ``slug_collision`` and the suffix retry
also fails, Influx logs a WARNING but otherwise drops the article.
The same paper will hit the same collision on every subsequent sweep
until the squatting Lithos document is removed (see
[#31](https://github.com/agent-lore/influx/issues/31) for the planned
self-healing path).

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

## 9. Reference

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
