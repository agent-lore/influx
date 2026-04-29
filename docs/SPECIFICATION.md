# Influx - Specification

Version: 0.1.0
Date: 2026-04-29
Status: Aligned with Implementation

---

## 1. Goals

### 1.1 Primary Goals

1. **Profile-driven ingestion**: Collect research and technical content for configured interest profiles.
2. **Source coverage**: Ingest from arXiv Atom feeds and configured RSS/Atom feeds.
3. **Relevance filtering**: Use configured OpenAI-compatible model slots to score candidates before writing notes.
4. **Tiered enrichment**: Add structured summary, full text, and deep extraction sections according to per-profile score thresholds.
5. **Local archive**: Store source PDFs or HTML under a configured archive directory and link archived paths from notes.
6. **Lithos integration**: Write canonical Markdown notes to Lithos over MCP/SSE, deduplicate by cache lookup, and wire LCMA relationships after writes.
7. **Operator control**: Provide a local admin HTTP API plus CLI commands for validation, serving, manual runs, backfills, and recent run reporting.
8. **Operational safety**: Apply outbound HTTP guardrails, local-admin bind protection, per-profile run locks, and bounded shutdown.

### 1.2 Non-Goals

1. **Standalone knowledge store**: Influx does not persist its own knowledge corpus; Lithos is the note store.
2. **General feed reader UI**: There is no web UI for browsing candidates or notes.
3. **Native non-OpenAI model APIs**: Model calls use OpenAI-compatible `/chat/completions` JSON-mode semantics.
4. **User authentication**: The admin API is designed as a local admin surface, protected by bind-host policy rather than auth.
5. **Distributed scheduling**: Scheduler and coordinator state are in-process; this is a single-process service.
6. **Guaranteed source completeness**: RSS and arXiv fetches are best-effort within configured windows, retries, and provider limits.

### 1.3 Compatibility Policy

1. **Package metadata is authoritative**: Runtime `influx.__version__` is derived from installed package metadata.
2. **Note compatibility matters**: Influx-authored notes use a stable canonical Markdown structure and preserve user notes during rewrites.
3. **Config evolves through typed schema**: Runtime behavior is driven by `influx.toml` validated by Pydantic models.
4. **MCP contracts follow Lithos**: Tool envelopes and retry behavior are implemented in the Lithos client wrapper.

---

## 2. Architecture

### 2.1 Component Overview

```text
+-----------------------------------------------------------------+
|                            Influx                               |
|                                                                 |
|  CLI                                                            |
|  validate-config / serve / run / backfill / migrate-notes        |
|                         |                                       |
|                         v                                       |
|  FastAPI Admin API + InfluxService                              |
|  /live /ready /status /runs /runs/recent /backfills              |
|                         |                                       |
|        +----------------+----------------+                      |
|        v                                 v                      |
|  APScheduler                      ProbeLoop                     |
|  single cron dispatcher           cached Lithos + credentials   |
|        |                                                        |
|        v                                                        |
|  Coordinator + run_profile                                      |
|  per-profile non-overlap, repair, feedback, dedup, write         |
|        |                                                        |
|        v                                                        |
|  Source Providers                                                |
|  arXiv fetch/filter/archive/extract + RSS fetch/filter/archive   |
|        |                                                        |
|        v                                                        |
|  LithosClient (MCP/SSE)                                          |
|  cache_lookup / write / list / task / retrieve / edge_upsert      |
+-----------------------------------------------------------------+
```

### 2.2 Runtime Flow

1. `serve` loads config, validates the local admin bind address, creates the FastAPI app, starts the probe loop, and starts the scheduler.
2. The scheduler registers one `influx-tick` cron dispatcher job. Each tick creates a fresh source provider/cache pair and starts profile runs as background tasks.
3. Manual `POST /runs` and `POST /backfills` requests acquire per-profile locks and spawn background run tasks.
4. `run_profile` creates a Lithos task, runs the repair sweep for non-backfill runs, loads negative feedback examples, composes the filter prompt, calls the source provider, checks Lithos cache, writes notes, runs LCMA post-write hooks, and dispatches configured notifications for non-backfill runs.
5. `run_profile` records start, completion, failure, and basic run counts in the local run ledger.
6. Backfills use the same write path but skip repair sweep and skip already-ingested cache hits.

### 2.3 In-Process State

- `Coordinator`: tracks busy profiles and prevents same-profile overlap.
- `ProbeLoop`: caches Lithos reachability and provider credential status for `/ready` and `/status`.
- `FetchCache`: deduplicates shared source fetches within a scheduled fire or HTTP-triggered run scope.
- `active_tasks`: tracks scheduler and HTTP background tasks for graceful shutdown.

### 2.4 Local Persistent State

- `RunLedger`: stores local operational run history under `storage.state_dir`.
- The ledger uses `active-runs.json` for in-flight runs and `runs.jsonl` for terminal run history.
- On service startup, any leftover active runs from a previous process are marked `abandoned`.
- The ledger is intentionally not stored in Lithos. It is service operational state rather than knowledge content.

---

## 3. Configuration

### 3.1 Discovery

Influx loads TOML config from:

1. `INFLUX_CONFIG`
2. `./influx.toml`
3. `~/.influx/influx.toml`
4. `/etc/influx/influx.toml`

Environment overrides are applied for selected runtime values. The complete annotated template is `influx.example.toml`.

### 3.2 Sections

- `[influx]`: `note_schema_version`, stamped as `schema:<version>` on Influx-authored notes.
- `[lithos]`: Lithos MCP endpoint and transport. Only `transport = "sse"` is supported.
- `[schedule]`: cron expression, timezone, misfire grace, and shutdown grace.
- `[storage]`: archive directory, local state directory, retention setting, max download size, and download timeout.
- `[notifications]`: outbound timeout plus typed `[[notifications.webhooks]]` sinks. `notifications.webhook_url` remains as a legacy single-sink generic-digest fallback.
- `[security]`: outbound/private-IP and remote-admin bind policy.
- `[[profiles]]`: profile name, description, thresholds, and source configuration.
- `[providers.*]`: OpenAI-compatible provider base URLs, API-key environment variables, and extra headers.
- `[models.*]`: model slots for `filter`, `enrich`, and `extract`.
- `[prompts.*]`: inline prompt text or prompt file paths for `filter`, `tier1_enrich`, and `tier3_extract`.
- `[filter]`: batch size and negative-example tuning.
- `[extraction]`: minimum extracted text lengths and stripped HTML tags.
- `[resilience]`: retry, backoff, arXiv pacing, and Lithos write-conflict settings.
- `[feedback]`: negative feedback example limits.
- `[repair]`: repair sweep batch limit.
- `[telemetry]`: optional OpenTelemetry setup.

### 3.3 Validation Rules

- Profile names must match `^[a-z][a-z0-9-]{0,31}$`.
- Notification webhook names must match `^[a-z0-9][a-z0-9-]{0,63}$` and be unique within `notifications.webhooks`.
- Notification webhook URLs must use `http` or `https`.
- `generic_digest` only supports `event_mode = "digest"`.
- `agent_zero_notification_create` only supports `event_mode = "article"`.
- `agent_zero_message_async` requires a non-empty `context`.
- RSS URLs must use `http` or `https`; RSS `source_tag` is `rss` or `blog`.
- Prompt entries must specify exactly one of `text` or `path`.
- Relative prompt paths are resolved relative to the config file.
- Prompt variables are validated at load time:
  - `filter`: `profile_description`, `negative_examples`, `min_score_in_results`
  - `tier1_enrich`: `title`, `abstract`, `profile_summary`
  - `tier3_extract`: `title`, `full_text`
- Provider API keys are checked during normal config loading when `api_key_env` is set.
- `validate-config` additionally dry-calls JSON-mode model slots and dry-connects to Lithos when a Lithos URL is configured.

---

## 4. CLI

### 4.1 Commands

```bash
influx validate-config
influx serve
influx run --profile <profile>
influx backfill --profile <profile> --days <n> [--confirm]
influx backfill --profile <profile> --from <YYYY-MM-DD> --to <YYYY-MM-DD> [--confirm]
influx migrate-notes
```

### 4.2 Behavior

- `validate-config`: prints the effective config JSON, checks JSON-mode model slots, and verifies Lithos SSE connectivity.
- `serve`: runs the FastAPI admin API and scheduler under uvicorn.
- `run`: posts a manual run request to the local admin API.
- `backfill`: posts a backfill request to the local admin API. If the server returns `confirm_required`, the CLI retries only when `--confirm` was supplied.
- `migrate-notes`: prints the current configured `note_schema_version`.

### 4.3 Exit Codes

- `0`: success
- `1`: partial/conflict class, including busy profile responses
- `2`: runtime failure
- `64`: usage/configuration error

---

## 5. Admin HTTP API

The admin API is intended to bind to loopback by default. Non-loopback bind hosts require `security.allow_remote_admin = true`.

### 5.1 Environment

- `INFLUX_ADMIN_BIND_HOST`: bind host, default `127.0.0.1`
- `INFLUX_ADMIN_PORT`: bind port, default `8080`

### 5.2 Endpoints

- `GET /live`: always returns `200 {"live": true}` while the process is alive.
- `GET /ready`: returns `200` when cached probes are ready, otherwise `503`.
- `GET /status`: returns process status, readiness, version, dependency probe states, and per-profile scheduler, busy, and last-run state.
- `GET /runs/recent`: returns active runs and recent terminal run ledger entries. Query parameters are `limit` and `profile`.
- `POST /runs`: accepts `{"profile": "<name>"}` or `{"all_profiles": true}`.
- `POST /backfills`: accepts `{"profile": "<name>", "days": n}` or `{"profile": "<name>", "from": "...", "to": "..."}`; `all_profiles` is also supported.

### 5.3 Run and Backfill Semantics

- Unknown profiles return `422`.
- Busy profiles return `409` with `status="conflict"`, `error="profile_busy"`, the profile name, request ID, and any known active-run summary.
- Accepted jobs return `202` with `request_id`, `kind`, `scope`, and `submitted_at`.
- `all_profiles` acquires all profile locks before starting work. If any profile is busy, already-acquired locks are released and the request returns `409`.
- Backfill estimates are calculated as `days * categories * max_results_per_category`; estimates above 1000 require `confirm`.

### 5.4 Error Responses

Admin API errors use compact structured JSON, aligned with Lithos' `error`/`message` convention where it maps cleanly:

```json
{
  "status": "invalid_request",
  "error": "unknown_profile",
  "message": "Unknown profile: 'example'",
  "reason": "unknown_profile"
}
```

Validation errors include `detail`; conflict responses include `active_run` when the local run ledger can identify the conflicting run. Existing CLI compatibility aliases such as `reason` are retained.

---

## 6. Source Processing

### 6.1 arXiv

The arXiv provider:

1. Builds category queries against `https://export.arxiv.org/api/query`.
2. Applies configured `max_results_per_category`, lookback windows, and backfill date ranges.
3. Fetches Atom feeds via guarded HTTP.
4. Retries transient failures with configured backoff and handles HTTP 429 with the configured arXiv backoff.
5. Parses entries into `ArxivItem` records.
6. Scores candidates with the batched filter scorer.
7. Drops candidates missing from the filter response or below the profile relevance threshold.
8. Archives the PDF under `archive_dir/arxiv/YYYY/MM/<arxiv-id>.pdf`.
9. Builds canonical notes with score-gated enrichment.

Archive failures do not abort note creation. The note is tagged `influx:archive-missing` and `influx:repair-needed`, and the Archive section is left empty.

### 6.2 RSS and Atom Feeds

The RSS provider:

1. Fetches configured feed URLs via guarded HTTP.
2. Parses RSS/Atom entries with `feedparser`.
3. Preserves each feed's configured `source_tag`.
4. Scores feed items with the configured filter model.
5. Drops items missing from the filter response or below relevance.
6. Archives article HTML under `archive_dir/<source_tag>/YYYY/MM/<feed-slug>-YYYY-MM-DD-<url-hash>.html`.
7. Extracts article text with fallback to feed summary.
8. Builds canonical notes with score-gated enrichment.

### 6.3 Fetch Deduplication

Within a run scope, shared arXiv category/window fetches and shared RSS feed bytes are cached so multiple profiles do not refetch the same upstream source. Scheduled ticks use a fresh cache per tick.

---

## 7. Filtering and Enrichment

### 7.1 Filter Model

The filter model uses the `models.filter` slot and expects JSON content matching:

```json
{
  "results": [
    {
      "id": "candidate-id",
      "score": 1,
      "tags": ["tag"],
      "reason": "short reason"
    }
  ]
}
```

Scores must be 1 through 10. Tags are limited to five per result. Failed filter batches are skipped rather than ingested with a default score.

### 7.2 Tier 1 Enrichment

For candidates with `score >= thresholds.relevance`, Influx attempts Tier 1 enrichment with `models.enrich`. The JSON content must validate as:

```json
{
  "contributions": ["..."],
  "method": "...",
  "result": "...",
  "relevance": "..."
}
```

`contributions` must contain 1 to 6 items.

### 7.3 Tier 2 Full Text

For candidates with `score >= thresholds.full_text`, Influx attempts full-text extraction:

- arXiv: HTML/PDF extraction cascade via the arXiv extraction pipeline.
- RSS/blog: article extraction via the web article extractor.

When extraction succeeds, notes include `full-text` and a `## Full Text` section. When extraction fails, notes remain summary/abstract-only and may be tagged for repair.

### 7.4 Tier 3 Deep Extraction

For candidates with `score >= thresholds.deep_extract` and available full text, Influx calls `models.extract`. The JSON content must validate as:

```json
{
  "claims": ["..."],
  "datasets": ["..."],
  "builds_on": ["..."],
  "open_questions": ["..."],
  "potential_connections": ["..."]
}
```

`claims` must contain 1 to 10 items. Other lists contain 0 to 10 items. Each item is trimmed and truncated to 500 characters. Notes render claims, datasets, builds-on, and open-questions sections; `potential_connections` is consumed by LCMA-related logic and is not rendered in the note.

---

## 8. Archive Storage

### 8.1 Layout

Archive paths are relative POSIX paths rendered in the note's `## Archive` section.

```text
<archive_dir>/
+-- arxiv/YYYY/MM/<arxiv-id>.pdf
+-- rss/YYYY/MM/<feed-slug>-YYYY-MM-DD-<url-hash>.html
+-- blog/YYYY/MM/<feed-slug>-YYYY-MM-DD-<url-hash>.html
```

### 8.2 Safety

- Archive source names must be valid slugs.
- Item IDs are rejected if they contain path traversal or absolute-path components.
- Resolved paths must remain under `archive_dir`.
- Downloads use the guarded HTTP client with scheme checks, DNS/private-IP checks, timeouts, content-type checks, and max response size enforcement.

---

## 8A. Local Run Ledger

### 8A.1 Layout

The run ledger is written under `storage.state_dir`, which defaults to `/state`.

```text
<state_dir>/
+-- active-runs.json
+-- runs.jsonl
```

`active-runs.json` is a JSON object keyed by `run_id`. `runs.jsonl` is append-only JSON Lines history for terminal runs.

### 8A.2 Entry Fields

Ledger entries include:

- `run_id`
- `profile`
- `kind`: `scheduled`, `manual`, or `backfill`
- `status`: `running`, `completed`, `failed`, or `abandoned`
- `run_range`
- `started_at`
- `completed_at`
- `duration_seconds`
- `sources_checked`
- `ingested`
- `error`

### 8A.3 Semantics

- Manual single-profile runs use the HTTP `request_id` as the ledger `run_id`.
- Multi-profile manual and backfill requests derive per-profile ledger IDs from the parent request ID.
- Scheduled runs generate a UUID.
- Completed runs record `sources_checked` and `ingested` when available.
- Failed runs record the exception type and message.
- Active runs left behind by a previous process are marked `abandoned` on startup.

---

## 9. Canonical Note Format

Influx writes canonical Markdown notes through Lithos.

### 9.1 Frontmatter

```yaml
---
note_type: summary
namespace: influx
source_url: <normalised source URL>
tags:
  - profile:<profile>
  - source:<source>
  - ingested-by:influx
  - schema:<note_schema_version>
confidence: <float>
---
```

Influx-authored notes must include `ingested-by:influx`. Notes with an archive path must not also carry `influx:archive-missing`.

### 9.2 Body Shape

```markdown
# <Title>

## Archive
path: <relative/archive/path>

## Summary
...

## Full Text
...

## Claims
...

## Datasets & Benchmarks
...

## Builds On
...

## Open Questions
...

## Profile Relevance
### <profile>
Score: <score>/10
<reason>

## User Notes
```

Omitted sections:

- `## Summary` is omitted when neither Tier 1 enrichment nor plain summary is available.
- `## Full Text` is omitted when full text is unavailable.
- Tier 3 sections are omitted when Tier 3 extraction is unavailable.
- `## Profile Relevance` is always emitted, even when empty.
- `## User Notes` is always appended and is preserved byte-exactly on rewrites.

### 9.3 Tags

Common tags include:

- `profile:<profile>`
- `source:arxiv`, `source:rss`, or `source:blog`
- `arxiv-id:<id>` for arXiv notes
- `feed-slug:<slug>` for RSS/blog notes
- `ingested-by:influx`
- `schema:<note_schema_version>`
- `text:abstract-only`, `text:html`, or `text:pdf`
- `full-text`
- `influx:deep-extracted`
- `influx:archive-missing`
- `influx:repair-needed`
- `influx:text-terminal`

---

## 10. Lithos Integration

### 10.1 Transport and Identity

Influx uses MCP over SSE. On every new connection it calls:

```json
{
  "id": "influx",
  "name": "Influx Pipeline",
  "type": "ingestion-pipeline"
}
```

Only SSE transport is supported.

### 10.2 Tools Used

Influx calls these Lithos tools through `LithosClient`:

- `lithos_agent_register`
- `lithos_cache_lookup`
- `lithos_write`
- `lithos_read`
- `lithos_list`
- `lithos_task_create`
- `lithos_task_complete`
- `lithos_retrieve`
- `lithos_edge_upsert`

### 10.3 Deduplication

Before writing, Influx calls `lithos_cache_lookup` using:

- `source_url`
- a composed query from title plus the first sentence of the abstract or summary

Backfills skip cache hits entirely. Scheduled/manual runs still attempt a write on cache hits so multi-profile note metadata can merge.

### 10.4 Write Envelope Handling

`lithos_write` results are parsed into `WriteResult` statuses:

- `created` / `updated`: success; note ID is used for LCMA hooks.
- `duplicate`: treated as already-ingested.
- `invalid_input`: logged and skipped.
- `slug_collision`: retried once with a disambiguating title suffix.
- `version_conflict`: re-read existing note, merge tags, preserve `## User Notes`, merge Profile Relevance, then retry once.
- `content_too_large`: drop `## Full Text` and retry. On a second failure, create-path writes are skipped; repair-path writes retry with Tier 1 only and `influx:repair-needed`.

### 10.5 LCMA Hooks

After successful writes, Influx:

1. Calls `lithos_retrieve` with a query composed from title plus up to three contribution bullets.
2. Upserts `related_to` edges for retrieved notes whose score meets `thresholds.lcma_edge_score`.
3. Resolves Tier 3 `builds_on` entries that contain `arXiv:<id>` through `lithos_cache_lookup`.
4. Upserts `builds_on` edges only when the resolved source URL matches exactly.

Unknown LCMA tools are treated as deployment errors and latch degraded readiness.

---

## 11. Repair Sweep

Scheduled and manual runs begin with a repair sweep. Backfills skip repair.

The sweep lists `influx:repair-needed` notes for the profile and attempts stage-specific recovery based on tags and archive state. It can retry archive/text extraction, Tier 2 full text, and Tier 3 deep extraction paths.

Failure behavior:

- Terminal Lithos write failure aborts the run and latches degraded readiness.
- Chronic `content_too_large` on repair leaves the existing note untouched and continues to the next candidate.
- Successful sweep clears the repair-write readiness latch.

---

## 12. Notifications

Influx can fan out notifications after scheduled and manual runs. Backfills do not emit notifications.

Notification configuration:

- `timeout_seconds` applies to every outbound notification call.
- `[[notifications.webhooks]]` defines typed sinks with:
  - `name`
  - `type`
  - `url`
  - `enabled`
  - `notify_on`
  - `event_mode`
  - optional `min_score`
  - optional `auth_token_env`
  - target-specific fields such as `context`, `deliver`, `channel`, and `sender_name`
- `notifications.webhook_url` remains as a legacy compatibility path. When set and `notifications.webhooks` is empty, Influx sends one `generic_digest` notification to that URL.

Supported types:

- `generic_digest`
- `agent_zero_message_async`
- `agent_zero_notification_create`
- `openclaw_agent`

Supported event modes:

- `digest`: one notification per run
- `article`: one notification per ingested article that meets `min_score`

`generic_digest` payloads:

- Zero-ingest run: quiet digest with message and stats.
- Non-zero run: includes `highlights`, `all_ingested`, and `stats.high_relevance`.

Highlights are items with `score >= thresholds.notify_immediate`.

Typed target behavior:

- `agent_zero_message_async` sends `{"text", "context"}`.
- `agent_zero_notification_create` sends one toast-style payload per matching article.
- `openclaw_agent` sends `{"message", "name", "deliver"}` and includes `channel` when configured.

Bearer-token auth is supported via `auth_token_env`; missing tokens cause the sink to be skipped with a warning. Delivery uses the guarded POST client. Empty legacy webhook URLs are a no-op. Delivery failures are logged and do not fail the run.

---

## 13. Observability

### 13.1 Readiness

`ProbeLoop` checks:

- Lithos SSE endpoint reachability by opening an SSE HTTP connection.
- Presence of configured provider API-key environment variables.

`/ready` and `/status` read cached probe state and do not perform fresh probes.

Readiness is degraded when:

- Lithos probe fails.
- Required provider credentials are missing.
- Cached probes are stale.
- Repair sweep terminal write failure is latched.
- LCMA unknown-tool failure is latched.

### 13.2 Telemetry

When telemetry is enabled, spans are emitted for run, source fetch, archive download, enrichment, Lithos write, and Lithos retrieve operations. The service name and export interval are configured under `[telemetry]`.

### 13.3 Logging

Influx logs to stdout/stderr through Python logging. By default logs are single-line JSON objects using the same core field names as Lithos:

- `timestamp`
- `level`
- `logger`
- `message`

Caller-provided `extra` fields are preserved, including OTEL trace-correlation fields when present. Set `INFLUX_LOG_FORMAT=text` for local plain-text logs. `INFLUX_LOG_LEVEL` controls verbosity.

### 13.4 Rejection Rate Logging

Filter result tags are recorded per profile. At run completion Influx records rejection-rate data through the configured Lithos client path.

---

## 14. Security and Network Guardrails

### 14.1 Outbound HTTP

Guarded fetch and guarded JSON POST paths enforce:

- `http` and `https` schemes only.
- DNS resolution and private-IP blocking unless `security.allow_private_ips = true`.
- Streaming response-size limits.
- Configured timeouts.
- Content-type family checks for downloads where applicable.
- No redirect following in guarded POST.

### 14.2 Admin Bind Guard

The service refuses to bind the admin API to a non-loopback host unless:

```toml
[security]
allow_remote_admin = true
```

---

## 15. Development and Verification

Primary checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright src/
uv run pytest tests/ -q
```

Current implementation verification at the time of this specification:

- Ruff lint: passing
- Ruff format check: passing
- Pyright: passing
- Pytest: `1373 passed`

Known warnings in the suite:

- `websockets.legacy` deprecation from dependencies.
- One `AsyncMock` runtime warning in telemetry wiring tests.

---

## 16. Current Constraints

1. Model providers must expose an OpenAI-compatible chat completions API.
2. Only Lithos SSE transport is supported.
3. The scheduler/coordinator are in-process and not safe for multi-process active/active deployment.
4. Admin API authentication is not implemented; bind policy is the safety boundary.
5. The repair system relies on note tags and Lithos state rather than a separate Influx database.
6. Archive retention is configured but no retention-pruning worker is implemented in the current code.
7. RSS filtering requires a configured `models.filter` slot and provider; failed RSS filter batches are skipped.
8. Source archives and Lithos notes can diverge when archive download fails; repair tags mark those notes for later recovery.
