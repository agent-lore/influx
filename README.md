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
```

`validate-config` loads the TOML file, validates prompt variables, performs
JSON-mode model dry-calls for configured slots, and dry-connects to Lithos.

`serve` starts the scheduler and local admin API. The admin API backs the
`run` and `backfill` commands.

## Configuration

Influx reads `influx.toml` from `INFLUX_CONFIG`, the current directory,
`~/.influx/influx.toml`, or `/etc/influx/influx.toml`, in that order. See
[influx.example.toml](influx.example.toml) for a complete annotated template.
For the current system behavior and contracts, use [docs/SPECIFICATION.md](docs/SPECIFICATION.md).

Important sections:

- `[lithos]`: Lithos MCP/SSE endpoint.
- `[[profiles]]`: interest profiles, thresholds, arXiv categories, and RSS feeds.
- `[providers]` and `[models]`: model provider URLs, API-key env vars, and model slots for filtering, enrichment, and extraction.
- `[prompts]`: inline prompt text or prompt file paths. Required variables are validated at startup.
- `[storage]`: archive location, local state directory, download limits, and timeout policy.
- `[notifications]`: outbound webhook timeout plus typed `[[notifications.webhooks]]` sinks for generic digests, Agent Zero, and OpenClaw.
- `[security]`: outbound network guardrails, including private-IP policy.
- `[resilience]`, `[feedback]`, `[repair]`, `[telemetry]`: retry, negative-example, repair, and observability settings.

`storage.state_dir` defaults to `/state`. It stores the local run ledger used by
`/runs/recent` and the reporting script. This is operational state for the
running deployment, not Lithos knowledge content.

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

- `INFLUX_DATA_PATH`: host directory mounted at `/data`; contains `influx.toml`.
- `INFLUX_ARCHIVE_PATH`: host directory mounted at `/archive`; stores fetched source material.
- `INFLUX_STATE_PATH`: host directory mounted at `/state`; stores the local run ledger.
- `INFLUX_ADMIN_HOST_PORT`: host port used to reach the admin API.
- `INFLUX_LOG_FORMAT`: `json` by default; set `text` for local plain-text logs.
- `INFLUX_LOG_LEVEL`: Python logging level, default `INFO`.

## Operator Report

Use the report script to summarize a running Influx environment:

```bash
scripts/influx-report.py staging --limit 10
```

The positional environment name maps to `docker/.env.<env>`. The script reads
that file to find the admin URL, archive path, and state path, then calls
`/status` and `/runs/recent`.

Useful options:

```bash
scripts/influx-report.py dev
scripts/influx-report.py staging --limit 50
scripts/influx-report.py staging --base-url http://127.0.0.1:18080
```

The report includes service readiness, package version, per-profile scheduler
state, active and recent runs from the run ledger, archive file counts, inferred
Lithos article-note counts, and the path to `runs.jsonl`.
