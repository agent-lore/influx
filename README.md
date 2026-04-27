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

Important sections:

- `[lithos]`: Lithos MCP/SSE endpoint.
- `[[profiles]]`: interest profiles, thresholds, arXiv categories, and RSS feeds.
- `[providers]` and `[models]`: model provider URLs, API-key env vars, and model slots for filtering, enrichment, and extraction.
- `[prompts]`: inline prompt text or prompt file paths. Required variables are validated at startup.
- `[storage]`: archive location, download limits, and timeout policy.
- `[security]`: outbound network guardrails, including private-IP policy.
- `[resilience]`, `[feedback]`, `[repair]`, `[telemetry]`: retry, negative-example, repair, and observability settings.

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
