# Influx

Influx is a Python 3.12 ingestion pipeline for collecting arXiv papers and RSS
posts, filtering them against interest profiles, enriching high-value items with
LLM calls, archiving source material, and writing notes into Lithos.

Package metadata is the source of truth for the project version. At runtime,
`influx.__version__` is derived from the installed package metadata.

## Getting Started

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp influx.example.toml influx.toml
```

Edit `influx.toml` for your Lithos endpoint, source profiles, model providers,
API-key environment variables, prompt text or prompt paths, and archive storage.
Relative prompt paths are resolved relative to the config file.

## CLI

```bash
uv run influx validate-config
uv run influx serve
uv run influx run --profile ai-robotics
uv run influx backfill --profile ai-robotics --days 7
uv run influx backfill --profile ai-robotics --from 2026-04-01 --to 2026-04-07
uv run influx migrate-notes
```

`validate-config` loads the TOML file, validates prompt variables, performs
JSON-mode model dry-calls for configured slots, and dry-connects to Lithos
(skipped when `lithos.url` is empty).

`serve` starts the scheduler and local admin API. The admin API backs the
`run` and `backfill` commands.

`migrate-notes` prints the configured `influx.note_schema_version` and exits
0. It performs no migration: there is no note-migration logic behind it yet.

## Configuration

Influx reads `influx.toml` from `INFLUX_CONFIG`, the current directory,
`~/.influx/influx.toml`, or `/etc/influx/influx.toml`, in that order. See
[influx.example.toml](influx.example.toml) for a complete annotated template.
For the current system behavior and contracts, use [docs/SPECIFICATION.md](docs/SPECIFICATION.md).

Important sections:

- `[influx]`: `note_schema_version`, stamped on every Influx-authored note.
- `[lithos]`: Lithos MCP/SSE endpoint (`url`, `transport = "sse"`). Overridable via `LITHOS_URL` / `LITHOS_MCP_TRANSPORT`.
- `[schedule]`: cron expression, timezone, misfire and shutdown grace, optional start jitter and inter-profile gap.
- `[[profiles]]`: interest profiles, thresholds, arXiv categories, and RSS feeds.
- `[providers]` and `[models]`: model provider URLs, API-key env vars, and model slots for filtering, enrichment, and extraction.
- `[prompts]`: inline prompt text or prompt file paths. Required variables are validated at startup.
- `[filter]`: filter batch size, minimum score kept in results, and negative-example title truncation.
- `[extraction]`: minimum extracted text lengths, thin-summary suppression (`min_summary_chars`), and stripped HTML tags.
- `[storage]`: archive location, local state directory, download limits, and timeout policy.
- `[storage.archive_policy]`: per-domain archive acquisition modes (`blocked`, `rate_limited`, `skip`, `unsupported`) layered on a built-in default set.
- `[notifications]`: outbound webhook timeout plus typed `[[notifications.webhooks]]` sinks for generic digests, Agent Zero, and OpenClaw.
- `[security]`: outbound network guardrails, including private-IP policy and remote admin bind.
- `[resilience]`, `[feedback]`, `[repair]`: retry/backoff and arXiv pacing, negative-example limits, and repair sweep batch size.
- `[inbox]`: opt-in manual-submission intake via Lithos tasks tagged `influx:inbox` (poll cadence, per-tick cap, agent id, local-PDF root, filter-outage backoff).
- `[telemetry]`: validated but not wired; see Observability below.

`storage.state_dir` defaults to `/state`. It stores the local run ledger used by
`/runs/recent` and the reporting script. This is operational state for the
running deployment, not Lithos knowledge content.

## Observability

OpenTelemetry export is controlled by environment variables only. The
`[telemetry]` TOML section is accepted by the config model but never consulted
when the exporters are wired, so edit the environment, not the file:

- `INFLUX_OTEL_ENABLED=true` turns on trace and metric export.
- `OTEL_EXPORTER_OTLP_ENDPOINT` (or the per-signal `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `..._METRICS_ENDPOINT` / `..._LOGS_ENDPOINT`) selects the OTLP HTTP collector; `OTEL_EXPORTER_OTLP_PROTOCOL` defaults to `http/protobuf`.
- `INFLUX_OTEL_CONSOLE_FALLBACK=true` writes spans and metrics to stdout when no endpoint is configured.
- `OTEL_SERVICE_NAME` (default `influx`) and `INFLUX_ENVIRONMENT` set the `service.name` and `deployment.environment` resource attributes.

The Docker stack forwards all of these from the per-environment `.env.<env>`
file (see Docker below).

Logs are structured JSON by default using Lithos-compatible core fields:
`timestamp`, `level`, `logger`, and `message`. Set `INFLUX_LOG_FORMAT=text` for
plain local logs, and `INFLUX_LOG_LEVEL` to control verbosity.

Notification targets are configured in TOML. Secrets stay in the environment.
Each `[[notifications.webhooks]]` entry defines a typed sink with:

- `name`, `type`, `url`
- `enabled`, `notify_on`, `event_mode`, `min_score`
- `auth_token_env` for bearer-token auth
- target-specific fields such as `context` for `agent_zero_message_async`, `rfc_module` / `rfc_function` / `rfc_password_env` for `agent_zero_rfc_message`, and `deliver` / `wake_mode` / `channel` / `sender_name` for `openclaw_agent`

Supported webhook types:

- `generic_digest`
- `agent_zero_message_async`
- `agent_zero_notification_create`
- `agent_zero_rfc_message`
- `openclaw_agent`

`event_mode = "digest"` sends one run summary per matching run. `event_mode = "article"` sends one notification per ingested article that meets `min_score`.

Agent Zero's normal HTTP API endpoints remain session-authenticated when login is enabled. For machine-to-machine delivery, prefer `agent_zero_rfc_message`; see [docs/AGENT_ZERO_RFC.md](docs/AGENT_ZERO_RFC.md). Direct `agent_zero_message_async` and `agent_zero_notification_create` sinks are still available for deployments that intentionally use session auth or no auth.

Webhook delivery is considered successful only on HTTP `2xx`. Redirects such as `302 /login` are logged as failures. Webhook failures never fail the Influx run itself.

## Development

Format, lint, type-check, and test:

```bash
make fmt
make lint
make typecheck
make test
make check
```

Useful direct commands:

```bash
uv run ruff check .
uv run pyright src/
uv run pytest tests/ -q
```

After changing code or models, regenerate the architecture docs (component
diagram, domain model, metrics, and per-component pages under
[`docs/generated/`](docs/generated/README.md)):

```bash
make diagrams   # = pytest tests/guardrail/ -q; commit the result
```

`make test` runs the guardrail tests too, so a normal test run also rewrites
`docs/generated/`. CI's `diagrams` job fails if the committed views drift from the
code.

## Model evals

`evals/` measures a model, slot-setting or prompt change on the exact
requests production sends (filter, Tier 1, Tier 3) before it ships, and
keeps a human-labelled answer key for the filter:

```bash
uv run python -m evals --help   # build-dataset, run, compare, label, grade
```

The operator guide, [docs/operations/model-evals.md](docs/operations/model-evals.md),
covers comparing a new model against production, labelling the filter set
and grading runs against it. [docs/plans/model-evals.md](docs/plans/model-evals.md)
has the background and the GPT-4.1-mini → GPT-6 Luna results.

## Docker

Build and run with the project Docker helpers:

```bash
make docker-build
cp docker/.env.example docker/.env.dev
./docker/run.sh dev up
./docker/run.sh dev logs
./docker/run.sh dev down
```

The Docker stack is configured through per-environment `.env.<env>` files.
Each environment can set:

- `INFLUX_ENVIRONMENT`: environment label, forwarded into the container (also the OTEL `deployment.environment`).
- `INFLUX_CONTAINER_NAME`: container name; must be unique per environment.
- `INFLUX_DATA_PATH`: host directory mounted at `/data`; contains `influx.toml`.
- `INFLUX_ARCHIVE_PATH`: host directory mounted at `/archive`; stores fetched source material.
- `INFLUX_STATE_PATH`: host directory mounted at `/state`; stores the local run ledger.
- `INFLUX_ADMIN_HOST_PORT`: host port used to reach the admin API.
- `INFLUX_OTEL_ENABLED`, `INFLUX_OTEL_CONSOLE_FALLBACK`, `OTEL_EXPORTER_OTLP_*`, `OTEL_SERVICE_NAME`: telemetry controls, forwarded into the container.
- `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `OPENCLAW_TOKEN`, `AGENT_ZERO_RFC_PASSWORD`: provider and webhook secrets, forwarded into the container.

The container reads its config from `/data/influx.toml` (`INFLUX_CONFIG` is
set to that path in the image and the compose file), i.e.
`$INFLUX_DATA_PATH/influx.toml` on the host. The compose file does not forward
`LITHOS_URL`, so the Lithos endpoint must be set in that file's `[lithos]`
section.

`INFLUX_LOG_FORMAT` and `INFLUX_LOG_LEVEL` are read by the process but are not
forwarded by `docker/docker-compose.yml`; setting them in `.env.<env>` has no
effect on the container. Add them to the service `environment:` block (for
example via a compose override file) if you need non-default logging in Docker.

## Operator Report

Use the report script to summarize a running Influx environment:

```bash
scripts/influx-report.py --env staging --limit 10
```

`--env <name>` maps to `docker/.env.<name>` (default: `staging`). The script
reads that file to find the admin URL, archive path, and state path, then
calls `/status` and `/runs/recent`.

Useful options:

```bash
scripts/influx-report.py --env dev
scripts/influx-report.py --env staging --limit 50
scripts/influx-report.py --env staging --base-url http://127.0.0.1:18080
```

The report includes service readiness, package version, per-profile scheduler
state, active and recent runs from the run ledger, archive file counts, inferred
Lithos article-note counts, and the path to `runs.jsonl`.
