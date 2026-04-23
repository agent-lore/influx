---
title: Influx — Requirements Document
version: 0.7.0
date: 2026-04-22
status: draft
tags:
  - influx
  - requirements
  - design
  - architecture
supersedes: INFLUX-REQUIREMENTS-0.6.md
---

# Influx — Requirements Document

> [!abstract] Project Summary
> **Influx** is a knowledge ingestion pipeline that monitors arXiv and web sources (blogs, Medium, RSS feeds) for new content matching one or more configurable interest profiles, filters it for relevance using an LLM, and feeds the results into a Lithos knowledge base as structured markdown notes. For each relevant item, Influx extracts clean text from the source (preferring arXiv HTML over PDF where available), generates a structured summary with key contributions and relevance reasoning, downloads and archives the original source to a local file store, and writes exactly one canonical Lithos note per source. Profile membership is represented by tags on that canonical note rather than by duplicate notes. A feedback mechanism allows the user to mark items as irrelevant per profile, improving future filtering over time via negative few-shot examples. Influx has no UI of its own — a companion project **Lithos Lens** provides a local web UI with feed view and interactive graph visualisation of the knowledge base.

> [!info] Changes Since 0.5
> - Canonical note model pinned: exactly one Lithos note per source; profile membership is expressed by tags, not duplicate notes.
> - Lithos transport aligned to current canonical behavior: official `mcp` SDK over SSE.
> - Generic stale-update semantics replaced with explicit repair/upgrade semantics aligned to current `lithos_write`.
> - Full-text and deep-extraction content now extend the canonical note instead of creating tier-specific sibling notes.
> - LCMA is now a required dependency for Influx v0.7; Lithos must be deployed with LCMA enabled, but Influx no longer bootstraps or enumerates LCMA internals at startup.
> - arXiv exact lookup is pinned to canonical `source_url` plus `arxiv-id:<id>` tags rather than filename assumptions.
> - Feedback tags are now profile-scoped (`influx:rejected:<profile>`) so one canonical note can be rejected for one profile without poisoning another.
> - LLM provider is now independently configurable from model name (e.g. route Anthropic models via OpenRouter); see §4.2 `[providers.*]`.
> - All tunable values (thresholds, batch sizes, min-text-length gates, retry/backoff timings, strip-tags list) are explicit config keys, not hardcoded constants.
> - All LLM prompts (filter, tier-1 enrichment, tier-3 extraction) are configurable via `[prompts.*]` — inline text or file path.

---

## Table of Contents

- [[#1. Goals & Non-Goals]]
- [[#2. Architecture Overview]]
- [[#3. Infrastructure & Deployment]]
- [[#4. Configuration]]
- [[#5. Source Monitoring]]
- [[#6. Relevance Filtering]]
- [[#7. Content Enrichment]]
- [[#8. Storage]]
- [[#9. Lithos Integration]]
- [[#10. LCMA Integration]]
- [[#11. Notifications]]
- [[#12. Feedback Mechanism]]
- [[#13. Resilience & Error Handling]]
- [[#14. Observability]]
- [[#15. Backfill Mode]]
- [[#16. API Reference]]
- [[#17. Implementation Plan]]
- [[#18. Testing Strategy]]
- [[#19. Environment Variables]]
- [[#20. Reserved Tags]]
- [[#21. Retention]]

---

## 1. Goals & Non-Goals

### Goals

- Monitor arXiv daily for new papers matching one or more configurable interest profiles
- Monitor RSS feeds (blogs, Medium, etc.) for relevant articles per profile
- Filter content for relevance using a cheap/fast LLM
- Improve filtering over time via user feedback (negative few-shot examples)
- Extract clean text from sources (HTML preferred, PDF fallback)
- Archive original PDFs and web articles to local filesystem
- Ingest structured notes into Lithos knowledge base, organised by source and date
- Use LCMA retrieval and edge tools to surface connections at ingest time via an LCMA-enabled Lithos deployment
- Notify the user of new relevant content immediately
- Support backfill of historical content
- Run independently of Agent Zero (separate container, restartable independently)

### Non-Goals

- Full-text search UI — that is Lithos's job (surfaced by Lithos Lens)
- Relationship discovery and concept formation — that is LCMA's job
- Email delivery — out of v1 scope; may be added later
- Social media monitoring — out of scope for v1
- Citation graph construction — out of scope for v1
- Any UI — all browsing lives in Lithos Lens

---

## 2. Architecture Overview

### Three-Container Design

```
┌──────────────────────────────────────────────────────────────────┐
│                         DOCKER NETWORK                            │
│                                                                   │
│  ┌──────────────┐     ┌──────────────┐     ┌─────────────────┐   │
│  │    LITHOS    │────▶│    INFLUX    │     │  LITHOS-LENS    │   │
│  │              │     │  (ingestion) │     │   (web UI)      │   │
│  │  knowledge   │◀────│              │     │                 │   │
│  │  store +     │     │  scheduled   │     │  stateless      │   │
│  │  MCP API     │     │  batch job   │     │  HTTP server    │   │
│  └──────────────┘     └──────────────┘     └────────┬────────┘   │
│          ▲                                           │            │
│          └───────────────────────────────────────────┘            │
│                       Lithos MCP API only                         │
└──────────────────────────────────────────────────────────────────┘
         │                                     │
         │ MCP API                             │ :7843 exposed to host
         ▼                                     ▼
  ┌─────────────┐                      ┌──────────────┐
  │ AGENT ZERO  │                      │   BROWSER    │
  │  (webhook   │                      │  (human UI)  │
  │  notifs)    │                      └──────────────┘
  └─────────────┘
```

Influx is an **MCP client** of Lithos. All dependencies flow Influx → Lithos.

> [!important] Influx has no UI
> Influx is a headless scheduled pipeline. All human-facing browsing, graph rendering, and feedback UI live in Lithos Lens. Influx exposes a small operational HTTP API (`/live`, `/ready`, `/status`, plus local/admin run-submission endpoints) and receives feedback indirectly via notes tagged `influx:rejected:<profile>` in Lithos.

### Repository Structure

Two separate repositories:

| Repo | Purpose |
|------|---------|
| `influx` | Ingestion pipeline — arXiv/RSS monitoring, LLM filtering, Lithos ingestion |
| `lithos-lens` | Web UI — documented separately (see `LITHOS-LENS-REQUIREMENTS-0.4.md` §8 for the feedback-write contract) |

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deployment | Separate Docker container | Independent restartability; clean separation of concerns |
| Scheduling | In-process scheduler | Configurable, handles missed runs, stays with the long-running service |
| LLM access | Provider-configurable abstraction with structured output validation | Keeps providers swappable while enforcing machine-readable responses |
| Canonical note model | One Lithos note per source | Avoids duplicate notes across profiles; simplifies repair and upgrades |
| Deduplication | Lithos `lithos_cache_lookup` by `source_url` | Lithos is the source of truth; no separate state DB |
| Archive storage | Local filesystem shared volume | Simple, human-accessible, easy to back up |
| Lithos communication | Official `mcp` Python SDK, **SSE** transport | Matches current Lithos transport surface and deployment target |
| Notification | Webhook to Agent Zero, fire-and-forget | Real-time; no polling; no local queue |
| Text extraction | arXiv HTML → PDF fallback → abstract-only | Quality-first with graceful degradation |
| Feedback storage | Lithos notes tagged `influx:rejected:<profile>` | Supports profile-specific rejection on a shared canonical note |
| Config format | TOML (Python 3.12 built-in `tomllib`) | Consistent with Cardinal and other recent projects |
| Interest profiles | Multiple named profiles | Keeps unrelated domains (AI/robotics vs HEMA) cleanly separated |
| Multi-profile merge | Union profile tags; use max score for note-wide confidence | Preserves one note per source while keeping per-profile relevance |
| Operational HTTP API | `/live`, `/ready`, `/status`, plus manual run/backfill submission endpoints | Keeps container probes simple while letting operators trigger work through the live service |
| OTEL | Opt-in, additive, optional packages | Consistent with Lithos conventions |
| Environments | `.env.dev` / `.env.prod` per service | Consistent with Lithos conventions |
| Logging | JSON to **stdout** | Container-friendly default; captured by `docker logs` |

---

## 3. Infrastructure & Deployment

### Container

| Container | Base image | Purpose |
|-----------|-----------|--------|
| `influx` | `python:3.12-slim` | Ingestion pipeline, scheduler, archive downloader |

Lithos runs in its own container (`lithos`) and is a dependency, not a sub-component of Influx.

### Shared Volume: Archive Store

The archive volume is named **`influx-archive`** (generic — holds PDFs, saved web pages, and any other downloaded source material, not just papers). It is owned by Influx but mounted read-only by Lithos Lens.

| Volume | Influx mount | Purpose |
|--------|-------------|--------|
| `influx-archive` | `/archive` (rw) | Downloaded source files |
| `influx-config` | `/etc/influx` (rw) | Shared config (TOML) — mounted read-only by Lens |

### Local Deployment Convention (Illustrative)

The following `.env`, `docker compose`, and `run.sh` pattern is an **illustrative local-development convention**, not a hard requirement. Implementations may use different deployment tooling so long as the container/runtime contract in this document is preserved.

- Each service has `.env.dev` and `.env.prod` (and optionally `.env.staging`) files
- One acceptable pattern is to pass environment files to `docker compose` for interpolation and then pass specific variables into the container via the `environment:` section
- Service endpoints (Lithos, OTEL collector, local model gateways, etc.) are supplied via environment/config values. The requirements do not pin specific hostnames for container-to-container communication.
- Secrets (provider API keys, webhook URLs) live in the `.env.<env>` files alongside non-secret configuration. The `.env.*` files MUST be gitignored and MUST NOT be committed. Influx fails fast on startup if any configured provider's `api_key_env` variable is empty.
- Config changes require the running service to be restarted. There is no hot-reload.

See §19 for the canonical environment variable table.

### Example `docker-compose.yml` (Informative)

```yaml
# Influx — ingestion pipeline
services:
  influx:
    image: ${INFLUX_IMAGE:-influx:local}
    pull_policy: never
    build:
      context: .
      dockerfile: Dockerfile
    container_name: ${INFLUX_CONTAINER_NAME:-influx}
    user: "${INFLUX_UID:-1000}:${INFLUX_GID:-1000}"
    restart: unless-stopped
    volumes:
      - ${INFLUX_ARCHIVE_PATH:-./archive}:/archive
      - ./config:/etc/influx:ro
    ports:
      - "${INFLUX_HOST_PORT:-8080}:8080"
    environment:
      - INFLUX_ENVIRONMENT=${INFLUX_ENVIRONMENT:-dev}
      - INFLUX_ARCHIVE_DIR=/archive
      - INFLUX_CONFIG=/etc/influx/influx.toml
      - LITHOS_URL=${LITHOS_URL:-}
      - LITHOS_MCP_TRANSPORT=${LITHOS_MCP_TRANSPORT:-sse}
      - INFLUX_AGENT_ID=${INFLUX_AGENT_ID:-influx}
      - AGENT_ZERO_WEBHOOK_URL=${AGENT_ZERO_WEBHOOK_URL:-}
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - INFLUX_OTEL_ENABLED=${INFLUX_OTEL_ENABLED:-false}
      - OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT:-}
      - INFLUX_LOG_LEVEL=${INFLUX_LOG_LEVEL:-INFO}
      - INFLUX_DRY_RUN=${INFLUX_DRY_RUN:-false}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/live"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s

volumes:
  influx-archive:
```

### Example `run.sh` (Informative)

One acceptable local-ops wrapper script is:

```bash
#!/usr/bin/env bash
# Launch/manage an Influx stack for a given environment.
#
# Usage:
#   ./run.sh <env> [action]
#
#   env     One of: dev, prod, staging (matches .env.<env>)
#   action  up      Build and start the stack in detached mode (default)
#           down    Stop and remove the stack
#           logs    Tail container logs (Ctrl-C to detach)
#           status  Show running containers for this project
#           restart Shortcut for down + up

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

env_name="${1:-}"
action="${2:-up}"

if [[ -z "${env_name}" ]]; then
    echo "Error: environment name is required" >&2
    echo "Usage: $0 <dev|prod|staging> [up|down|logs|status|restart]" >&2
    exit 1
fi

env_file="${SCRIPT_DIR}/.env.${env_name}"
if [[ ! -f "${env_file}" ]]; then
    echo "Error: env file not found: ${env_file}" >&2
    exit 1
fi

project_name="influx-${env_name}"
compose_args=(-p "${project_name}" --env-file "${env_file}")

cd "${SCRIPT_DIR}"

case "${action}" in
    up)      docker compose "${compose_args[@]}" up -d --build ;;
    down)    docker compose "${compose_args[@]}" down ;;
    restart) docker compose "${compose_args[@]}" down && docker compose "${compose_args[@]}" up -d --build ;;
    logs)    docker compose "${compose_args[@]}" logs -f 2>&1 | grep -v 'GET /live' ;;
    status)  docker compose "${compose_args[@]}" ps ;;
    *)
        echo "Error: unknown action '${action}'" >&2
        echo "Valid actions: up, down, restart, logs, status" >&2
        exit 1
        ;;
esac
```

---

## 4. Configuration

Configuration uses **TOML** format (Python 3.12 built-in `tomllib` for reading; `tomli-w` for writing if needed).

### 4.1 Config File Discovery

When `INFLUX_CONFIG` is not set, Influx looks for `influx.toml` in this order:

1. `./influx.toml` (current working directory)
2. `~/.influx/influx.toml` (user home)
3. `/etc/influx/influx.toml` (system / container default)

First file found wins. The Docker image sets `INFLUX_CONFIG=/etc/influx/influx.toml`.

### 4.2 Example Config

```toml
# Influx Configuration

[influx]
note_schema_version = 1         # stamped on every Influx-authored note (see §9.2)

[schedule]
cron = "0 6 * * *"              # daily at 06:00
timezone = "UTC"
misfire_grace_seconds = 3600    # tolerate missed fires within an hour

[storage]
archive_dir = "/archive"
retain_days = 3650              # ~10 years; effectively "keep forever" by default
max_download_bytes = 52_428_800 # 50 MB per file
download_timeout_seconds = 30

[notifications]
webhook_url = ""                # set via env var AGENT_ZERO_WEBHOOK_URL
timeout_seconds = 5             # fire-and-forget; no retry

[security]
# SSRF guard applied to all outbound fetches (RSS feeds, articles, PDFs).
# Private/loopback/link-local IPs are rejected. Only http/https are allowed.
allow_private_ips = false

# ---------------------------------------------------------------------------
# Interest Profiles
# Multiple profiles are supported. Each profile has its own interest
# description, source list, and thresholds. Notes remain source-scoped;
# profile membership is represented by `profile:<name>` tags, not paths.
# Profile names MUST match regex: ^[a-z][a-z0-9-]{0,31}$
# ---------------------------------------------------------------------------

[[profiles]]
name = "ai-robotics"
description = """
  HIGH INTEREST: Multi-agent systems, agent memory and knowledge graphs,
  humanoid and social robotics, LLM reasoning and planning, emotional models
  for robots, intelligent AI companions, robot environment understanding and
  navigation, neurosymbolic reasoning, artificial life and emergent complexity,
  fundamental AI breakthroughs.

  MEDIUM INTEREST: LLM agents with tool use or memory, reinforcement learning
  for robotics, vision-language-action models, cognitive architectures.

  LOW INTEREST / EXCLUDE: General ML without agent/robot angle, CV without
  robotics context, federated learning, model compression, medical imaging,
  benchmarks-only papers, fine-tuning and RLHF for language tasks.

  EXCEPTION: Score 8-10 regardless of topic if the paper appears to introduce
  a genuinely novel paradigm or architecture (transformer-level impact).
  """

[profiles.thresholds]
relevance = 7                # minimum score to ingest
full_text = 8                # minimum score to fetch and store full text
deep_extract = 9             # minimum score for deep structured extraction
notify_immediate = 8         # minimum score for immediate notification
lcma_edge_score = 0.75       # minimum lithos_retrieve composite score to create related_to edge

[profiles.sources.arxiv]
enabled = true
categories = ["cs.AI", "cs.RO", "cs.MA", "cs.NE", "cs.CL", "cs.LO"]
max_results_per_category = 200
lookback_days = 1

[[profiles.sources.rss]]
name = "Andrej Karpathy"
url = "https://karpathy.github.io/feed.xml"

[[profiles.sources.rss]]
name = "Lilian Weng"
url = "https://lilianweng.github.io/index.xml"

# ---------------------------------------------------------------------------
# Providers
# Providers are independent of model names. Each `[providers.*]` entry gives
# the connection information for a provider. The example config below uses
# provider/model strings that are convenient for the current implementation,
# but the requirement is simply that provider selection remains configurable.
# ---------------------------------------------------------------------------

[providers.openai]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[providers.anthropic]
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
# Optional headers forwarded to OpenRouter (e.g. referer/app-name).
extra_headers = { "HTTP-Referer" = "https://example.invalid/influx", "X-Title" = "Influx" }

# [providers.ollama]
# base_url = "http://<configured-ollama-endpoint>"
# api_key_env = ""                 # Ollama has no API key

# ---------------------------------------------------------------------------
# Models
# Each model slot names a provider defined in `[providers.*]` and a model
# string. Implementations may interpret model strings using any internal
# routing layer, provided provider/model selection remains configuration-driven.
# ---------------------------------------------------------------------------

[models.filter]
provider = "openai"
model = "openai/gpt-4.1-mini"
temperature = 0.0
max_tokens = 2048
request_timeout = 30
max_retries = 2
json_mode = true              # enforce response_format={"type": "json_object"}

[models.enrich]
provider = "openai"
model = "openai/gpt-4.1-mini"
temperature = 0.2
request_timeout = 30
max_retries = 2
json_mode = true

[models.extract]
provider = "anthropic"
model = "anthropic/claude-sonnet-4.6"
temperature = 0.2
request_timeout = 60
max_retries = 2
json_mode = true

# Example: the same extract slot routed via OpenRouter instead of direct
# Anthropic — useful for unified billing across mixed providers.
# [models.extract]
# provider = "openrouter"
# model = "openrouter/anthropic/claude-sonnet-4"
# temperature = 0.2

# ---------------------------------------------------------------------------
# Prompts
# Every LLM prompt is configurable. Each entry specifies exactly one of
# `text` (inline string) or `path` (relative to the config file directory).
# Prompts are Python `str.format()` templates. Template variables are
# documented per-entry; an unknown variable raises at startup via
# `validate-config`. The paths below are example defaults, not a normative
# requirement to ship prompts in that exact location.
# ---------------------------------------------------------------------------

[prompts.filter]
# Variables: {profile_description}, {negative_examples}, {min_score_in_results}
path = "./prompts/filter.md"

[prompts.tier1_enrich]
# Variables: {title}, {abstract}, {profile_summary}
path = "./prompts/tier1_enrich.md"

[prompts.tier3_extract]
# Variables: {title}, {full_text}
path = "./prompts/tier3_extract.md"

# ---------------------------------------------------------------------------
# Filter tuning
# ---------------------------------------------------------------------------

[filter]
batch_size = 25                      # items per filter LLM call
min_score_in_results = 6             # model returns only items with score >= this
negative_example_max_title_chars = 200

# ---------------------------------------------------------------------------
# Extraction tuning
# ---------------------------------------------------------------------------

[extraction]
min_html_chars = 1000                # below this, HTML extraction is treated as failure
min_web_chars = 500                  # below this, web-article extraction is treated as failure
strip_tags = ["script", "iframe", "object", "embed"]

# ---------------------------------------------------------------------------
# Resilience tuning
# ---------------------------------------------------------------------------

[resilience]
max_retries = 3
backoff_base_seconds = 1             # exponential: base, base*2, base*4, ...
arxiv_request_min_interval_seconds = 3
arxiv_429_backoff_seconds = 10
lithos_write_conflict_max_retries = 1

# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

[feedback]
negative_examples_per_profile = 20   # recent rejections to inject per profile
recalibrate_after_runs = 7           # log tag rejection rates every N runs

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

[telemetry]
enabled = false                       # set true via env INFLUX_OTEL_ENABLED
console_fallback = false              # print spans to stdout (dev without collector)
service_name = "influx"
export_interval_ms = 30000
```

### 4.3 Config Loading

```python
import tomllib
from pathlib import Path

def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    # Env vars override config file values (see §19 for the full table).
    return Config.model_validate(data)
```

### 4.4 Profile Name Validation

Profile names are used in tags (`profile:<name>`, `influx:rejected:<profile>`) and task tags. They must match `^[a-z][a-z0-9-]{0,31}$`. Invalid names cause startup to fail with a clear error — do not silently slugify.

---

## 5. Source Monitoring

### 5.1 arXiv

**API endpoint:**
```
GET https://export.arxiv.org/api/query
  ?search_query=cat:cs.AI+OR+cat:cs.RO+OR+cat:cs.MA
  &sortBy=submittedDate
  &sortOrder=descending
  &start=0
  &max_results=200
```

**Response format:** Atom/XML. Each entry contains:

| Field | XML element | Notes |
|-------|------------|-------|
| arXiv ID | `<id>` | e.g. `http://arxiv.org/abs/2603.12939v1` |
| Title | `<title>` | Full title |
| Abstract | `<summary>` | Complete abstract — sufficient for filtering |
| Authors | `<author><name>` | All authors |
| Published | `<published>` | ISO 8601 timestamp |
| Updated | `<updated>` | Last revision timestamp |
| PDF URL | `<link title="pdf">` | Direct PDF download URL |
| HTML URL | Derived: `arxiv.org/html/{id}` | Clean HTML (preferred for extraction) |
| Categories | `<category term="...">` | All cross-listed categories |
| Primary category | `<arxiv:primary_category>` | Author's primary classification |

**XML namespaces:**
```python
ns = {
    'atom': 'http://www.w3.org/2005/Atom',
    'arxiv': 'http://arxiv.org/schemas/atom'
}
```

**Date filtering:** Filter by `<published>` date in Python after fetching. Published timestamps are UTC; `lookback_days` is interpreted in UTC.

**Rate limiting:** Minimum `resilience.arxiv_request_min_interval_seconds` (default 3s) between successive requests to `export.arxiv.org` (arXiv courtesy guideline). This is separate from 429 backoff handling (see §13).

**arXiv HTML availability:** Most papers from 2020 onwards. Check with a HEAD request before full fetch; fall through to PDF on 404.

### 5.2 RSS Feeds

- Parse with an RSS / Atom parser
- Extract: title, URL, published date, summary
- Fetch full article text with the configured article-extraction path
- Deduplication by article URL via `lithos_cache_lookup`
- URL fetches are subject to the SSRF guard and size/timeout caps (see §13.4)

---

## 6. Relevance Filtering

### 6.1 Filter Prompt

The filter prompt is loaded from config key `prompts.filter` (see §4.2). It is used with the configured `models.filter` model **with JSON mode enabled** and responses validated against a Pydantic schema. The template has three variables:

- `{profile_description}` — the active profile's `description` field.
- `{negative_examples}` — recent rejections, formatted per §6.3.
- `{min_score_in_results}` — value of `filter.min_score_in_results` (default 6); the model returns only items scoring at or above this threshold.

An illustrative default prompt is:

```
You are a research paper relevance filter. Score each paper for relevance
to the following interest profile.

## INTEREST PROFILE
{profile_description}

## NEGATIVE EXAMPLES
The following were previously marked as NOT interesting by the user.
Use them to calibrate your scoring (titles only, no abstracts):

{negative_examples}

## OUTPUT FORMAT
Return a JSON object with a single key "results" whose value is an array.
For each paper with score >= {min_score_in_results} include:
- "id": the arXiv ID or article URL string
- "score": integer 1-10
- "tags": list of 2-5 short keyword tags
- "reason": one sentence explaining the score

If no papers meet the threshold, return {"results": []}.
```

### 6.2 Pydantic Response Schema

```python
class FilterResult(BaseModel):
    id: str
    score: int = Field(ge=1, le=10)
    tags: list[str] = Field(min_length=0, max_length=5)
    reason: str

class FilterResponse(BaseModel):
    results: list[FilterResult]
```

### 6.3 Negative Example Formatting

Each negative example is rendered as a **single line** — no abstract — to keep the filter prompt within cheap-model context budgets:

```
- "{title}" (rejected)
```

Titles longer than `filter.negative_example_max_title_chars` (default 200) are truncated. Limit: `feedback.negative_examples_per_profile` (default 20).

The exact prompt wording is not normative. The contract is the available template variables, the required structured response shape, and the scoring semantics.

### 6.4 Batching

- Papers are sent to the filter model in batches of `filter.batch_size` (default 25)
- Each batch contains: `ID`, `Title`, `Abstract` (full abstract for the batch items only)
- JSON mode enforced via `response_format={"type": "json_object"}` with the `FilterResponse` schema
- On JSON-mode parse failure: log the raw response, count the attempt as a failed filter call, and retry within the normal model retry budget. If all retries fail, the batch is skipped (papers requeue on the next run via `lithos_cache_lookup` miss). Influx does **not** attempt regex salvage of malformed LLM output.

### 6.5 Threshold Behaviour

| Score | Action |
|-------|--------|
| < relevance threshold (default 7) | Discard |
| ≥ 7 | Ingest or update the canonical note with Tier 1 sections |
| ≥ 8 | Canonical note includes Tier 2 full-text sections |
| ≥ 9 | Canonical note includes Tier 3 deep-extraction sections |
| ≥ notify_immediate (default 8) | Include in immediate notification |

### 6.6 Multi-Profile Runs

Each run processes all enabled profiles sequentially. A source may match multiple profiles. Influx writes one canonical note per source, unions the matching `profile:<name>` tags onto that note, stores the note under a source/date path, and records per-profile relevance inside the canonical body ahead of `## User Notes`. The note-wide `confidence` field is set to the maximum matched profile score divided by 10. Full-text and deep-extraction upgrades are triggered if any matched profile crosses the corresponding threshold.

---

## 7. Content Enrichment

### 7.1 Text Extraction Strategy

```
For arXiv papers:
  1. HEAD https://arxiv.org/html/{id} → if 200, GET and extract with the preferred HTML/article extractor
     - Reject if extracted text < extraction.min_html_chars (default 1000, likely nav/boilerplate); fall through.
  2. Fallback: download PDF → extract with the configured PDF-to-text/markdown path
  3. Fallback: use abstract only, tag note `text:abstract-only`

For RSS/web articles:
  1. Fetch article URL (subject to SSRF guard + size/timeout caps)
  2. Extract with the preferred HTML/article extractor → markdown
     - Reject if extracted text < extraction.min_web_chars (default 500); fall through.
  3. Fallback: use feed summary only
```

### 7.2 LLM Enrichment (Tier 1 — all papers ≥ relevance threshold)

Uses `models.enrich` with JSON mode. Single LLM call from title + abstract. The prompt is loaded from config key `prompts.tier1_enrich` (see §4.2). Template variables: `{title}`, `{abstract}`, `{profile_summary}`.

An illustrative default prompt is:

```
Given this paper's title and abstract, extract:
1. Key contributions (3-5 bullet points, each ≤ 20 words)
2. Primary method or approach (1-2 sentences)
3. Main result or finding (1-2 sentences)
4. Relevance to: {profile_summary}

Title: {title}

Abstract:
{abstract}

Return JSON: {"contributions": [...], "method": "...",
              "result": "...", "relevance": "..."}
```

The exact wording and formatting of the default prompt are illustrative. The contract is the input variables and the validated response schema.

Pydantic schema:

```python
class Tier1Enrichment(BaseModel):
    contributions: list[str] = Field(min_length=1, max_length=6)
    method: str
    result: str
    relevance: str
```

### 7.3 LLM Enrichment (Tier 3 — papers scoring ≥ deep_extract threshold)

Uses `models.extract` with JSON mode on the full text. The prompt is loaded from config key `prompts.tier3_extract` (see §4.2). Template variables: `{title}`, `{full_text}`. The response is validated against:

```python
class Tier3Extraction(BaseModel):
    claims: list[str]
    datasets: list[str]
    builds_on: list[str]          # free-text names of prior works
    open_questions: list[str]
    potential_connections: list[str]
```

### 7.4 LLM Enrichment Failure Policy

If any enrichment call fails after retries, the note is still written **without** the failed section. Missing sections are omitted from the body; no placeholder text is inserted. The note is tagged `influx:repair-needed`, and later dedup hits on that note enter the repair/upgrade path instead of being skipped.

---

## 8. Storage

### 8.1 Archive Store

**Volume:** `influx-archive` mounted at `/archive`

**Layout:** `/{source}/{YYYY}/{MM}/{id}.{ext}`

**Examples:**
```
/archive/arxiv/2026/03/2603.12939.pdf
/archive/arxiv/2026/03/2603.99999.pdf
/archive/blog/2026/03/karpathy-2026-03-15.html
/archive/blog/2026/03/lilianweng-2026-03-10.html
```

**Naming convention:**
- arXiv: use arXiv ID (e.g. `2603.12939`)
- Blog/web: `{feed-name-slug}-{YYYY-MM-DD}`
- Extension: `.pdf` for papers, `.html` for saved web articles

**Path safety:** feed names are slugified (`^[a-z0-9]+(-[a-z0-9]+)*$`, max 40 chars). After constructing the full archive path, Influx must verify `path.resolve().is_relative_to(archive_root)` and reject otherwise. Empty slugs (e.g. feed name of all-whitespace) are rejected at config load.

**On download failure:** Log error, leave the canonical note without a local-file path, tag it `influx:repair-needed`, and retry on a later run.

### 8.2 Lithos Note Paths

Notes are organised by source type, year, and month to avoid directory bloat as the knowledge base grows:

```
papers/arxiv/{YYYY}/{MM}
articles/rss/{YYYY}/{MM}
articles/blog/{YYYY}/{MM}
```

**Examples:**
```
papers/arxiv/2026/03
articles/rss/2026/03
articles/blog/2026/03
```

> [!note] Directory Scale Planning
> At 10-20 ingested papers/day, a flat `papers/` directory would accumulate ~5,000 files/year. The `{source}/{YYYY}/{MM}/` hierarchy keeps any single directory comfortably sized without encoding profile membership in the path.

> [!important] Lithos Filename Behavior
> Influx controls only the directory path passed to `lithos_write`. Current Lithos derives the filename from the note title slug. Influx MUST NOT assume caller-controlled basenames in v0.7. To avoid filename churn, normal repair/upgrade writes preserve the existing title rather than rewriting it opportunistically.

> [!note] arXiv Exact Lookup
> Current Lithos does not let Influx choose a separate slug or filename for arXiv IDs. Influx therefore treats filenames as opaque, keeps the human paper title in `title`, and relies on exact `source_url = https://arxiv.org/abs/<id>` plus `arxiv-id:<id>` tags for deterministic machine lookup. A future Lithos enhancement may add caller-specified filenames; that is out of scope for Influx v0.7.

### 8.3 Lithos Note Structure

Influx writes exactly **one canonical note per source**. Frontmatter below uses **only Lithos-allowed fields** (see Lithos SPEC §3.2 and §9.2 of this document). Influx-specific metadata (arXiv ID, categories, text quality, repair state, and stage-completion state) is carried via `tags`. The note-wide `confidence` field is the maximum matched profile score divided by `10.0`; per-profile reasoning is stored in the canonical body ahead of the reserved `## User Notes` section.

#### Canonical Note (all ingested items)

```markdown
---
title: "{Paper Title}"
source_url: https://arxiv.org/abs/2603.12939
tags:
  - profile:ai-robotics
  - profile:agents
  - source:arxiv
  - arxiv-id:2603.12939
  - cat:cs.RO
  - cat:cs.AI
  - text:html
  - ingested-by:influx
  - schema:1
  - robot-memory
  - spatio-temporal-reasoning
  - embodied-ai
confidence: 0.9              # max(profile_scores) / 10.0
note_type: summary
namespace: influx
---

# {Paper Title}

**arXiv ID:** 2603.12939
**Authors:** Author A, Author B
**Published:** 2026-03-16
**Ingested:** 2026-03-16T06:12:34Z
**Local file:** `/archive/arxiv/2026/03/2603.12939.pdf`

## Abstract
{original abstract text}

## Key Contributions
- {contribution 1}
- {contribution 2}
- {contribution 3}

## Method
{1-2 sentence summary of approach}

## Results
{1-2 sentence summary of findings}

## Profile Relevance
- `ai-robotics` (score 9): {why this was flagged for ai-robotics}
- `agents` (score 7): {why this was flagged for agents}

## Links
- [arXiv page](https://arxiv.org/abs/2603.12939)
- [PDF](https://arxiv.org/pdf/2603.12939)

## User Notes
```

The `agent="influx"` parameter is passed on every `lithos_write` call (see §9.3) — it appears in Lithos's `author`/`contributors` fields automatically and does not need to be in the note body.

The example above is illustrative. The stable contract is: one canonical note per source, required frontmatter semantics, canonical content before `## User Notes`, and verbatim preservation of `## User Notes` and everything beneath it. Exact headings, wording, and presentation above that boundary are implementation choices unless another integration explicitly depends on them.

Body ownership rules:

- Influx fully owns the note frontmatter and all body content before `## User Notes`.
- The `## User Notes` section and everything beneath it are reserved for user-authored content and are preserved verbatim on repair and upgrade writes.
- If an Influx-authored note is missing `## User Notes`, Influx appends an empty `## User Notes` section on rewrite and treats the rest of the body as Influx-owned.

#### Tier 2 — Full Text Additions To The Canonical Note (score ≥ full_text threshold)

```markdown
## Full Text

### Introduction
{extracted text}

### Related Work
{extracted text}

### Methods
{extracted text}

### Experiments / Results
{extracted text}

### Discussion
{extracted text}

### Conclusion
{extracted text}
```

When these sections are present, the canonical note also carries the `full-text` tag.

#### Tier 3 — Deep Extraction Additions To The Canonical Note (score ≥ deep_extract threshold)

Appended sections before `## User Notes`:

```markdown
## Claims
- {explicit claim 1}

## Datasets & Benchmarks
- {dataset 1}

## Builds On
- {prior work 1}

## Open Questions
- {question 1}
```

When these sections are present, the canonical note also carries the `influx:deep-extracted` tag. If any Influx-owned stage is incomplete or needs a later retry, the note carries `influx:repair-needed`.

---

## 9. Lithos Integration

### 9.1 MCP Client & Transport

Influx uses the **official `mcp` Python SDK** connecting to Lithos via **SSE** (`LITHOS_MCP_TRANSPORT=sse`, default). Influx follows current Lithos code behavior as canonical when it differs from older published documentation.

- Client wrapper lives at `influx/lithos_client.py`
- Connection is established lazily on first use and kept alive for the duration of a run
- `LITHOS_URL` points to the Lithos SSE endpoint supplied by environment/config
- Transport is fixed to `sse` for v0.7
- Influx does not enumerate or bootstrap Lithos internals at startup; LCMA support is treated as a deployment prerequisite (see §10.1)

### 9.2 Frontmatter Mapping (Lithos schema ↔ Influx concepts)

Lithos enforces a defined frontmatter schema (SPEC §3.2). Anything outside it is not guaranteed to round-trip. Influx maps its concepts as follows:

| Influx concept | Lithos field or tag | Notes |
|---|---|---|
| Canonical URL | `source_url` | Dedup key after normalisation |
| Note-wide max relevance score (0–10) | `confidence` (0.0–1.0) | `confidence = max(profile_scores) / 10` |
| Profile name | `tag: profile:<name>` | One or more per note; reserved prefix (see §20) |
| Source type | `tag: source:arxiv`, `source:rss`, `source:blog` | |
| arXiv exact identifier | `tag: arxiv-id:<id>` | Deterministic exact lookup key; preserve the dotted arXiv ID verbatim |
| Category | `tag: cat:<category>` (one per category) | |
| Text quality | `tag: text:html` \| `text:pdf` \| `text:abstract-only` | |
| Note authored by Influx | `tag: ingested-by:influx`, `namespace: influx`, and `agent="influx"` on write | |
| Note schema version | `tag: schema:<N>` | Sourced from `influx.note_schema_version` |
| Full-text sections present | `tag: full-text` | Present on the canonical note when Tier 2 sections exist |
| Deep extraction present | `tag: influx:deep-extracted` | Present on the canonical note when Tier 3 sections exist |
| Repair pending | `tag: influx:repair-needed` | Set when any Influx-owned stage is incomplete or needs a retry |
| Keyword tags from filter | Bare tags | Not prefixed |

All tags with a colon use the `prefix:value` convention. The reserved prefixes are listed in §20.

### 9.3 Deduplication

Before processing any item, use `lithos_cache_lookup` with both `query` (required) and `source_url` (fast path):

```python
result = lithos_cache_lookup(
    query=f"{title} {abstract_first_sentence}",
    source_url=normalize_url(url),
    max_age_hours=None,
)
# result["hit"] is True and no repair/upgrade is needed  → skip
# result["hit"] is True and repair/upgrade is needed     → read/merge/update existing note; see §9.5
# result["stale_exists"]                                 → secondary repair signal; see §9.5
# otherwise                                              → proceed with a fresh ingest
```

**URL normalisation (Influx side, before calling Lithos):**
- lowercase scheme and host
- drop default ports (`:80`, `:443`)
- strip tracking params: `utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `ref`
- remove trailing slash on path
- do NOT remove fragments — they are semantically meaningful for some blog URLs

Lithos further normalises on write; Influx pre-normalises so the fast-path hit rate is high even for duplicates reached via different feeds.

**Exact arXiv lookup:**
- Influx MUST set `source_url=https://arxiv.org/abs/{id}` on every arXiv note.
- Influx MUST tag every arXiv note with `arxiv-id:{id}`.
- Machine clients resolving an arXiv paper SHOULD use exact `source_url` lookup first and MAY use `lithos_list(tags=[f"arxiv-id:{id}"], limit=1)` as a deterministic secondary lookup or migration helper.
- Influx does **not** distort the human title solely to influence the current Lithos title-derived filename.

### 9.4 Write Path — Create

```python
result = lithos_write(
    title=title,
    content=canonical_body,
    agent="influx",                      # required on every call, creates and updates
    path=build_note_path(item),          # e.g. papers/arxiv/2026/03
    source_url=normalize_url(url),
    tags=build_tags(...),                # union of matched profiles + stage-state tags
    confidence=max_score / 10.0,
    note_type="summary",
    namespace="influx",
    expires_at=next_retry_at if repair_needed else None,
)
# result["status"] ∈ {"created", "updated", "duplicate", "error"}
```

### 9.5 Write Path — Repair & Upgrade

When a cache hit requires repair or upgrade, Influx re-reads the current note, preserves user-owned content, and rewrites the canonical note using optimistic locking:

```python
# 1. Read the current version to capture expected_version for optimistic lock
existing = lithos_read(id=doc_id)
expected_version = existing["metadata"]["version"]

# 2. Merge Influx-owned tags + preserve only the user-notes section.
merged_tags = merge_note_tags(
    existing_tags=existing["metadata"]["tags"],
    managed_tags=build_managed_tags(...),
)
user_notes = extract_user_notes_section(existing["content"])
merged_content = render_canonical_note(
    title=existing["metadata"]["title"],
    managed_body=render_managed_sections(...),
    user_notes=user_notes,
)

# 3. Write back the merged document. Preserve the existing title to avoid
#    filename/slug churn during normal repair and upgrade writes.
write = lithos_write(
    id=doc_id,
    title=existing["metadata"]["title"],
    content=merged_content,
    agent="influx",
    source_url=normalize_url(url),
    tags=merged_tags,
    confidence=merged_confidence,
    expected_version=expected_version,
    expires_at=next_retry_at if repair_needed else "",
)

# 4. On version_conflict: re-read once, re-apply, retry once. On second
#    conflict, log and skip (the next run will retry).
```

Rules:
- Repair/upgrade is triggered when a cache hit carries `influx:repair-needed`, when one or more currently matched `profile:*` tags are not yet present on the note, when the note is missing `full-text` and the current max score reaches `full_text`, when the note is missing `influx:deep-extracted` and the current max score reaches `deep_extract`, or when `lithos_cache_lookup` returns `stale_exists=true`.
- Influx regenerates the full canonical body above `## User Notes` from current source data and preserved per-note state.
- If `## User Notes` is absent, Influx appends an empty `## User Notes` section on rewrite.
- `profile:*` tags and `## Profile Relevance` entries are merged by profile name: newly matched profiles are added, currently processed profiles are refreshed, and unrelated existing profiles are preserved.
- `influx:rejected:<profile>` is authoritative for that profile while it remains on the note. When that tag is present, Influx MUST NOT re-add `profile:<profile>` if it is absent, and MUST NOT refresh or create the corresponding `## Profile Relevance` entry on subsequent repair/upgrade passes.
- Influx replaces the rest of the note tags it owns: `source:*`, `arxiv-id:*`, `cat:*`, `text:*`, `ingested-by:*`, `schema:*`, `full-text`, `influx:repair-needed`, and `influx:deep-extracted`.
- All other note tags are preserved, including `influx:rejected:<profile>`.
- `merged_confidence` is `max(existing_confidence, current_max_score / 10.0)` so a repair pass for one profile does not erase a higher historical match from another profile.
- `expires_at` is unset for complete notes. Incomplete notes may set `expires_at` to the next scheduled retry boundary, but Influx does not rely on `stale_exists` as its only repair trigger.
- Normal repair/upgrade writes preserve the existing title. Explicit title refreshes, if ever needed, happen through migration tooling rather than routine ingest.
- If a note already contains richer sections than current thresholds require, Influx leaves them in place. It does not delete full-text or deep-extraction content automatically.

### 9.6 Write Path — Error Envelopes

`lithos_write` returns structured status envelopes. Handling per Lithos SPEC §10:

| Status / code | Influx action |
|---|---|
| `created` / `updated` | Proceed |
| `duplicate` | Treat as hit; log info |
| `error: invalid_input` | Log error with payload; skip item |
| `error: content_too_large` | Trim full-text sections first, then retry once |
| `error: slug_collision` | Retry once on create with a disambiguated title suffix (arXiv: ` [arXiv <id>]`; web: ` [<host>]`) |
| `error: version_conflict` | Re-read, re-apply, retry once |

All other Lithos tool errors are logged and the item is skipped — they do not abort the run (see §13).

### 9.7 Agent Registration

On startup:

```python
lithos_agent_register(id="influx", name="Influx Pipeline", type="ingestion-pipeline")
```

Registration is optional (agents auto-register on first use), but doing so explicitly sets a human-readable name and type.

---

## 10. LCMA Integration

Influx v0.7 requires a Lithos deployment with LCMA enabled. This is a deployment prerequisite rather than a startup introspection step. Where the older published Lithos spec lags, the current Lithos implementation is treated as canonical for Influx.

### 10.1 Deployment Requirement

Operators MUST run Lithos with LCMA enabled before starting Influx.

- Influx does **not** call `tools/list` to enumerate LCMA capabilities at startup.
- Influx does **not** call `lithos_edge_list()` or any other tool purely to bootstrap Lithos internals.
- `python -m influx validate-config` MAY verify that `LITHOS_URL` is reachable and that basic configuration is coherent, but it does not preflight the full LCMA tool surface.
- If an LCMA-dependent call such as `lithos_retrieve`, `lithos_edge_upsert`, or `lithos_task_create` later fails with an unknown-tool / unsupported-capability style error, Influx treats that as a deployment/configuration error: abort the current run, log the problem clearly, and mark service readiness degraded until the dependency is corrected.

### 10.2 Post-Ingestion Retrieval

After writing each summary note, use `lithos_retrieve` (LCMA MVP1). This runs seven parallel scouts with reranking and produces an audit receipt:

```python
related = lithos_retrieve(
    query=f"{title} {contributions}",
    limit=5,
    agent_id="influx",
    task_id=run_task_id,
    tags=[f"profile:{profile_name}"],
)
```

Top results are included in the notification digest as "Related in your knowledge base".

### 10.3 Explicit Edge Creation

Influx manually upserts only the semantic edges it authors at ingest time:

- `builds_on` edges from Tier 3 extraction. For each named prior work, resolve deterministically via `lithos_cache_lookup(source_url=arxiv_abs_url, query=prior_title)` after attempting to extract an arXiv ID from the body. Create an edge only on an exact `source_url` match. Fuzzy title matching is deferred.
- `related_to` edges when `lithos_retrieve` returns a result with `score >= profiles.thresholds.lcma_edge_score` (default 0.75).

```python
lithos_edge_upsert(
    from_id=new_note_id,
    to_id=prior_id,
    type="builds_on",
    weight=0.8,
    namespace="influx",
    provenance_actor="influx",
    provenance_type="agent",
    evidence={"kind": "tier3_builds_on_extraction"},
)

lithos_edge_upsert(
    from_id=new_note_id,
    to_id=related_id,
    type="related_to",
    weight=related_score,
    namespace="influx",
    provenance_actor="influx",
    provenance_type="agent",
    evidence={"kind": "lithos_retrieve", "score": related_score, "receipt_id": receipt_id},
)
```

### 10.4 Run Task Coordination

Each scheduled, manual, or backfill execution of a **single profile** creates one Lithos task for LCMA coordination and audit:

```python
task = lithos_task_create(
    title=f"Influx run {profile_name} {date}",
    agent="influx",
    tags=["influx:run", f"profile:{profile_name}"],
)
# ... run pipeline ...
lithos_task_complete(
    task_id=task["task_id"],
    agent="influx",
    outcome=f"Ingested {count} items from {profile_name}",
)
```

`lithos_task_complete` currently supports the extended parameters (`outcome`, `cited_nodes`, `misleading_nodes`, `receipt_id`) in Lithos code. Influx v0.7 uses `outcome`, but it does **not** automatically send `cited_nodes` or `misleading_nodes`: in current Lithos behavior those fields are interpreted as feedback about nodes returned by a retrieval receipt, not about newly ingested note IDs.

Backfill profile-runs use `influx:backfill` in place of `influx:run`.

---

## 11. Notifications

### 11.1 Immediate Notification

POSTs to Agent Zero webhook after each profile run. **Fire-and-forget**: 5-second timeout, no retry. If the webhook call fails, log at `warning` level and move on — the next run's digest will still include everything that matters.

```json
{
  "type": "influx_digest",
  "run_date": "2026-03-16",
  "profile": "ai-robotics",
  "stats": {
    "sources_checked": 157,
    "ingested": 12,
    "high_relevance": 4
  },
  "highlights": [
    {
      "id": "2603.12939",
      "title": "RoboStream: ...",
      "score": 10,
      "tags": ["robot-memory", "embodied-ai"],
      "reason": "...",
      "url": "https://arxiv.org/abs/2603.12939",
	      "related_in_lithos": [
	        {"title": "...", "score": 0.89}
	      ]
	    }
	  ],
  "all_ingested": [...]
}
```

### 11.2 Quiet Run Notification

```json
{
  "type": "influx_digest",
  "run_date": "2026-03-16",
  "profile": "ai-robotics",
  "stats": {"sources_checked": 0, "ingested": 0},
  "message": "No new relevant content found today."
}
```

### 11.3 Backfill Mode

No webhook calls during backfill (see §15).

---

## 12. Feedback Mechanism

### 12.1 Overview

Feedback is authored in Lithos Lens. From Influx's perspective, feedback is data it reads from Lithos at the start of each run.

### 12.2 How Feedback Arrives

Lithos Lens updates the rejected note's tags via `lithos_write` (see `LITHOS-LENS-REQUIREMENTS-0.4.md` §8 for the write-side contract). The resulting note carries the profile-scoped tag `influx:rejected:<profile>`. Lens SHOULD also remove the matching `profile:<profile>` tag when the user rejects that profile. Influx does not write feedback itself.

### 12.3 Injecting Negative Examples

At the start of each filter run, per profile — use `lithos_list` (tag-only filtering), **not** `lithos_search` (which requires a free-text `query` argument):

```python
rejected = lithos_list(
    tags=[f"influx:rejected:{profile_name}"],
    limit=config.feedback.negative_examples_per_profile,
)
```

Influx SHOULD use the title returned by `lithos_list` when available. Only when a list item lacks title metadata does Influx call `lithos_read(id=...)` for that item. The title is formatted as a single line (see §6.3) and injected into the `NEGATIVE EXAMPLES` block of the filter prompt. Abstracts are intentionally not injected — negatives are for calibration, not re-filtering.

Semantics:

- `influx:rejected:<profile>` suppresses that profile assignment even on a shared canonical note; it does not affect other profile tags on the same note.
- Lens profile-scoped views SHOULD exclude notes carrying `influx:rejected:<profile>` for the active profile by default.
- If the rejection tag is later removed, Influx may add `profile:<profile>` again on a future run if the source still matches.

---

## 13. Resilience & Error Handling

### 13.1 Failure Matrix

| Failure | Behaviour |
|---------|----------|
| arXiv API unreachable | Retry `resilience.max_retries` times with exponential backoff; skip run if all fail |
| arXiv rate limit (429) | Back off `resilience.arxiv_429_backoff_seconds` (default 10s); retry up to `resilience.max_retries` times |
| HTML fetch fails | Fall back to PDF extraction |
| HTML extraction < `extraction.min_html_chars` | Treat as failure; fall back to PDF |
| PDF download fails | Store canonical note with `text:abstract-only`, add `influx:repair-needed`, retry next run |
| Download exceeds `storage.max_download_bytes` | Abort download; log; proceed with abstract-only |
| Download exceeds `storage.download_timeout_seconds` | Same as above |
| LLM call fails | Retry `models.<slot>.max_retries` times; store note without the failed sections, add `influx:repair-needed` (see §7.4) |
| LLM returns non-JSON despite JSON mode | Log raw response; treat as a failed model call; retry within the normal retry budget and skip the batch if retries are exhausted |
| Lithos unreachable on first call | Retry 3×; abort the current run; log error; mark readiness degraded; keep the service alive |
| Required Lithos / LCMA call unavailable at runtime | Abort the current run; log a clear deployment/configuration error; mark readiness degraded |
| Duplicate detected | Treat as hit; if repair/upgrade is needed, enter the merge path |
| `lithos_write` returns `version_conflict` | Re-read, re-apply, retry once; skip on second conflict |
| `lithos_write` returns `slug_collision` | Retry once on create with a disambiguated title suffix |
| `lithos_write` returns `content_too_large` | Truncate body; retry once |
| `lithos_edge_upsert` fails | Log warning; continue — edges are enrichment, not critical path |
| Webhook POST fails | Log warning; no retry (see §11.1) |

### 13.2 Retry Policy

- Max retries: `resilience.max_retries` (default 3)
- Backoff: exponential from `resilience.backoff_base_seconds` (default 1s → 1s, 2s, 4s, …) unless otherwise specified
- Per-item failures do not abort the run
- Run-level failures (Lithos fully unreachable) abort the current run and mark readiness degraded. One-shot CLI commands return exit code 2 in this case; `serve` stays alive.

### 13.3 Run Concurrency

The in-process scheduler is configured with `max_instances=1` per job, `coalesce=True`, and `misfire_grace_time=schedule.misfire_grace_seconds`. Manual runs and backfills are submitted to the already-running Influx service, not executed in a separate pipeline process. The live service owns a single in-process execution coordinator that enforces non-overlap for the same profile across scheduled, manual, and backfill work.

### 13.4 SSRF & Download Safety

All outbound HTTP fetches (arXiv API, RSS feeds, article URLs, PDF downloads) go through a guarded HTTP client that enforces:

- Scheme is `http` or `https` only
- Resolved IPs are not in any of: loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`), private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`), or multicast. Override via `[security] allow_private_ips = true` for dev-only use.
- Max response size: `storage.max_download_bytes` (default 50 MB). Streams and aborts on overflow.
- Connect + read timeout: `storage.download_timeout_seconds` (default 30s)
- Response content-type must match the expected family (HTML, PDF, XML/Atom) — reject otherwise

### 13.5 Content Sanitisation

Extracted HTML may contain prompt-injection payloads. Influx does not execute extracted content, but downstream LLM consumers reading Influx-authored notes should treat content with the `ingested-by:influx` tag as **untrusted**. Influx additionally:

- Strips tags listed in `extraction.strip_tags` (default `["script", "iframe", "object", "embed"]`) before conversion to markdown
- Does not preserve HTML fragments in markdown output

### 13.6 CLI Exit Codes (one-shot commands)

| Code | Meaning |
|---|---|
| 0 | Success — including zero-result runs |
| 1 | Partial failure — one or more profiles failed but others succeeded |
| 2 | Total failure — e.g. Lithos unreachable, config invalid |
| 64 | Usage error — bad CLI arguments |

---

## 14. Observability

### 14.1 OTEL — Opt-In, Additive

Follows the same high-level conventions as Lithos:

- OTEL is **opt-in** — `INFLUX_OTEL_ENABLED=true` enables it
- OTEL is **additive** — `docker logs influx` works exactly as before
- OTEL packages are **optional** — Influx runs fine without them
- **Console fallback** — `INFLUX_OTEL_CONSOLE_FALLBACK=true` prints spans to stdout (dev without collector)
- Package names, helper patterns, and exact dependency versions are implementation choices.

### 14.2 Spans & Attributes

**Key spans:**

| Span | Description |
|------|-------------|
| `influx.run` | Full pipeline run (per profile) |
| `influx.fetch.arxiv` | arXiv API fetch |
| `influx.fetch.rss` | RSS feed fetch |
| `influx.filter` | LLM relevance scoring batch |
| `influx.enrich.tier1` | Tier 1 LLM enrichment |
| `influx.enrich.tier2` | Full text extraction |
| `influx.enrich.tier3` | Deep extraction |
| `influx.lithos.write` | Lithos note write |
| `influx.lithos.retrieve` | LCMA retrieval call |
| `influx.archive.download` | PDF/HTML download |

**Standard span attributes** (attached to all spans where applicable):

| Attribute | Example |
|---|---|
| `influx.profile` | `ai-robotics` |
| `influx.run_id` | UUID generated at run start |
| `influx.run_type` | `scheduled` \| `manual` \| `backfill` |
| `influx.source` | `arxiv` \| `rss` |
| `influx.item_count` | integer |

### 14.3 Logging

Follows the container logging pattern — **stdout only, no log files**:

- All log output goes to stdout → captured by `docker logs influx`
- `INFLUX_LOG_LEVEL` controls verbosity (`DEBUG` in dev, `INFO` in prod)
- Structured JSON format via the configured logging stack
- OTEL (when enabled) provides structured spans and metrics for deeper observability
- Influx does **not** persist run logs or run-history notes in Lithos; durable operational telemetry belongs in the OTEL collector and standard container logs.

### 14.4 Live, Ready, and Status Endpoints

- `GET http://localhost:8080/live` — liveness probe for Docker/container health checks
- `GET http://localhost:8080/ready` — readiness probe for operators or orchestrators
- `GET http://localhost:8080/status` — detailed operator-facing status

Example `GET /status` response:

```json
{
  "status": "ok",
  "ready": true,
  "started_at": "2026-03-16T05:59:58Z",
  "checks": {
    "config": "ok",
    "scheduler": "ok"
  },
  "dependencies": {
    "lithos": {
      "status": "ok",
      "last_checked_at": "2026-03-16T06:00:05Z"
    },
    "llm_credentials": {
      "status": "ok",
      "last_checked_at": "2026-03-16T06:00:02Z"
    }
  },
  "profiles": {
    "ai-robotics": {
      "scheduled": true,
      "currently_running": false,
      "next_run_at": "2026-03-17T06:00:00Z",
      "last_run_started_at": "2026-03-16T06:00:00Z",
      "last_run_finished_at": "2026-03-16T06:03:12Z",
      "last_run_outcome": "success",
      "consecutive_failures": 0
    }
  }
}
```

Semantics:

- `/live` answers only the liveness question: the process has booted, the HTTP server is running, and the scheduler/event loop is not in a terminal failed state.
- `/live` returns `200 OK` when alive and `503 Service Unavailable` otherwise. Docker/container health checks MUST use `/live`.
- `/ready` answers the readiness question: Influx is currently able to perform a scheduled ingestion cycle using the latest cached startup/dependency state.
- `/ready` returns `200 OK` when ready and `503 Service Unavailable` otherwise. It may return a compact JSON body such as `{"ready": true, "status": "ok"}`.
- `/status` is the detailed diagnostic endpoint for humans and dashboards. It is not used as the Docker health check.
- `/status` returns `200 OK` whenever the process can serve the request; callers inspect the JSON body for `status` (`ok`, `degraded`, `starting`) and `ready`.
- `status` is the overall service state: `ok` if all required readiness checks pass, `degraded` if the process is alive but one or more readiness checks fail, `starting` before the first readiness evaluation completes.
- `checks.config` means config loaded and validated successfully.
- `checks.scheduler` means the in-process scheduler is running and the expected jobs are registered.
- `dependencies.lithos.status` means the Lithos SSE endpoint is reachable according to the latest background probe result.
- `dependencies.llm_credentials.status` means the configured LLM provider key is present and basic client construction succeeds; it does not require a paid completion call.
- `profiles.<name>.scheduled` indicates whether a scheduler job is currently registered for that profile.
- `profiles.<name>.currently_running` indicates whether the execution coordinator currently has an active scheduled, manual, or backfill run for that profile.
- `profiles.<name>.next_run_at` comes from the scheduler's next-fire-time state; it is `null` if the profile is disabled or no next fire time is currently known.
- `profiles.<name>.last_run_*` and `consecutive_failures` come from in-memory run bookkeeping maintained by the service and reset on process restart.
- Dependency probes are performed in the background on a fixed interval and cached in memory with timestamps. `/ready` and `/status` read that cached state; they do **not** perform fresh outbound probes on every request.
- These endpoints do not depend on persisted run history, Lithos notes, or previous run outcomes stored outside the process.

---

## 15. Backfill Mode

```bash
python -m influx backfill --profile ai-robotics --days 30
python -m influx backfill --profile ai-robotics --from 2026-01-01 --to 2026-03-15
python -m influx backfill --all-profiles --days 7
```

- The CLI submits the backfill request to the already-running Influx service; the service executes the job in-process
- Fetches papers day by day for the specified range
- Respects arXiv rate limits (3s between requests; plan for ~30s per day of backfill per profile)
- Skips already-ingested papers via `lithos_cache_lookup`
- **Does not send notifications** during backfill
- Creates `lithos_task_create`/`complete` tasks tagged `influx:backfill` so dashboards can filter them out
- The service logs progress to stdout; the submitting CLI exits once the request is accepted
- The CLI asks the service for an estimated cost before submission and requires `--confirm` when expected item count > 1000
- Runs inside the same execution coordinator as scheduled and manual work, so incompatible work never overlaps (see §13.3)

---

## 16. API Reference

### 16.1 arXiv API

**Base URL:** `https://export.arxiv.org/api/query`

| Parameter | Type | Description |
|-----------|------|-------------|
| `search_query` | string | e.g. `cat:cs.AI+OR+cat:cs.RO` |
| `sortBy` | string | `submittedDate` \| `relevance` \| `lastUpdatedDate` |
| `sortOrder` | string | `descending` \| `ascending` |
| `start` | int | Pagination offset |
| `max_results` | int | Results per page (max 2000, recommend ≤ 200) |

**Rate limit:** minimum 3 seconds between successive requests from the same client. No authentication required.

**URL patterns:**
- HTML: `https://arxiv.org/html/{arxiv_id}`
- PDF: `https://arxiv.org/pdf/{arxiv_id}`
- Abstract: `https://arxiv.org/abs/{arxiv_id}`

### 16.2 LLM Provider / Model Configuration

**Providers:** Every `[models.*]` slot names a provider defined in `[providers.*]` (see §4.2). The provider entry carries the base URL, the API-key env var to read, and any extra headers to attach. OpenAI, Anthropic, and OpenRouter are documented as first-class examples; additional providers may be added by defining a new `[providers.<name>]` block.

**Recommended models (defaults):**

| Use case | Config key | Default provider | Default model | Notes |
|----------|-----------|-----------------|--------------|-------|
| Filtering | `models.filter` | `openai` | `openai/gpt-4.1-mini` | Fast, cheap, sufficient |
| Enrichment | `models.enrich` | `openai` | `openai/gpt-4.1-mini` | Same model fine |
| Deep extraction | `models.extract` | `anthropic` | `anthropic/claude-sonnet-4.6` | Better for nuanced extraction |
| Unified billing | any | `openrouter` | e.g. `openrouter/anthropic/claude-sonnet-4` | Route any upstream model through OpenRouter |
| Local/offline | any | `ollama` | e.g. `ollama/llama3.2` | Define `[providers.ollama]` with your local base URL |

The model/provider table above is illustrative. The stable contract is that provider choice, model selection, timeouts, retries, and structured-output mode are all configuration-driven. JSON mode (`response_format={"type": "json_object"}`) is controlled per-slot via `models.<slot>.json_mode`. Models must be tested for JSON-mode compatibility at `python -m influx validate-config` time (see §16.4).

### 16.3 Lithos MCP API — Influx Usage

| Tool | Required args | Purpose |
|------|---------------|---------|
| `lithos_cache_lookup(query, source_url?, max_age_hours?, tags?)` | `query` | Deduplication and repair/upgrade decisioning before processing. `query` is REQUIRED even when a `source_url` fast-path hit is expected. |
| `lithos_write(title, content, agent, ...)` | `title`, `content`, `agent` | Write the single canonical note for a source. Always pass all three required fields — even on updates. |
| `lithos_read(id)` | `id` | Load rejected-note titles when `lithos_list` does not return them; load the existing document before repair/upgrade writes. |
| `lithos_retrieve(query, limit, agent_id, task_id, tags)` | `query` | LCMA post-ingestion connection query |
| `lithos_list(path_prefix?, tags?, since?, limit?)` | none | Load negative examples and exact arXiv tag hits; prefer returned summary metadata when available |
| `lithos_edge_upsert(from_id, to_id, type, weight, namespace, ...)` | as named | Create typed semantic edges between notes |
| `lithos_task_create(title, agent, tags?)` | `title`, `agent` | Create scheduled-run or backfill coordination task |
| `lithos_task_complete(task_id, agent, outcome?)` | `task_id`, `agent` | Complete scheduled-run or backfill task with outcome summary; Influx v0.7 does not send automated retrieval-feedback fields |
| `lithos_agent_register(id, name?, type?)` | `id` | Register on startup (optional — auto-registers otherwise) |

### 16.4 Influx CLI

| Command | Purpose |
|---|---|
| `python -m influx` | Print CLI help and exit non-zero; operators MUST pick an explicit subcommand. Influx is a long-running service — use `serve` for the scheduler |
| `python -m influx serve` | Start the scheduler + operational HTTP API and block (container default; this is the normal long-running mode) |
| `python -m influx run --profile X` | Submit a manual run request to the already-running local Influx service and exit once the request is accepted |
| `python -m influx backfill ...` | Submit a backfill request to the already-running local Influx service; see §15 |
| `python -m influx validate-config` | Parse config, validate prompts/model settings, dry-connect to Lithos, print effective config, exit non-zero if anything is obviously wrong |
| `python -m influx migrate-notes` | Apply future schema upgrades to existing Influx-authored notes, including 0.5-style note-shape migrations if needed |

### 16.5 Influx Service HTTP API

In v1, `POST /runs` and `POST /backfills` are **local admin endpoints only**. They are intended for loopback / same-host use by the local CLI and MUST NOT be treated as a general remote-control API. A conforming v1 deployment SHOULD bind them only on localhost by default or otherwise protect them from remote access.

| Endpoint | Purpose |
|---|---|
| `GET /live` | Liveness probe for Docker/container health checks |
| `GET /ready` | Readiness probe from cached dependency state |
| `GET /status` | Detailed operator-facing service and scheduler status |
| `POST /runs` | Submit a manual run to the live service. Body includes exactly one of `profile` or `all_profiles`. Returns `202` on acceptance and `409` if conflicting work is already active. Local admin endpoint only in v1. |
| `POST /backfills` | Submit a backfill job to the live service. Body includes profile/range arguments plus `confirm` when required. Returns `202` on acceptance and `409` if conflicting work is already active. Local admin endpoint only in v1. |

Response conventions for `POST /runs` and `POST /backfills`:

- v1 does **not** define a full job-resource API. The response contract is intentionally small: enough for CLI/output correlation, not a general remote queue-management interface.
- Accepted requests return `202 Accepted` with a compact JSON body containing:
  - `status: "accepted"`
  - `request_id`: an opaque identifier for correlating CLI output and service logs
  - `kind`: `run` or `backfill`
  - `scope`: the accepted profile/range payload
  - `submitted_at`: server timestamp in UTC
- Conflicting requests return `409 Conflict` with a JSON body containing:
  - `status: "conflict"`
  - `reason`: stable machine-readable code such as `profile_busy`
  - `request_id`
  - `active_run`: compact information about the conflicting active work (`kind`, `profile`, `started_at`)
- Validation failures return `400 Bad Request` or `422 Unprocessable Entity` with a JSON body containing:
  - `status: "invalid_request"`
  - `reason`: stable machine-readable code such as `confirm_required`, `bad_date_range`, or `missing_profile`
  - `message`: short human-readable explanation
  - optional structured fields relevant to the error (for example `estimated_items` when `confirm_required`)

Illustrative `202 Accepted` body for `POST /runs`:

```json
{
  "status": "accepted",
  "request_id": "01HXYZ...",
  "kind": "run",
  "scope": {
    "profile": "ai-robotics"
  },
  "submitted_at": "2026-04-23T12:34:56Z"
}
```

Illustrative `202 Accepted` body for `POST /backfills`:

```json
{
  "status": "accepted",
  "request_id": "01HXYZ...",
  "kind": "backfill",
  "scope": {
    "profile": "ai-robotics",
    "from": "2026-01-01",
    "to": "2026-03-15"
  },
  "submitted_at": "2026-04-23T12:34:56Z"
}
```

Illustrative `409 Conflict` body:

```json
{
  "status": "conflict",
  "reason": "profile_busy",
  "request_id": "01HXYZ...",
  "active_run": {
    "kind": "scheduled",
    "profile": "ai-robotics",
    "started_at": "2026-04-23T12:30:00Z"
  }
}
```

Illustrative validation-error body:

```json
{
  "status": "invalid_request",
  "reason": "confirm_required",
  "message": "Backfill exceeds confirmation threshold.",
  "estimated_items": 1432
}
```

---

## 17. Implementation Plan

This section is informative rather than normative. It captures one reasonable delivery sequence and module split, but implementations may reorder milestones, rename modules, or substitute equivalent tooling.

### Milestone 1 — arXiv Pipeline (v0.1)
*Goal: daily arXiv monitoring → Lithos ingestion → notification*

- [ ] Project scaffold: `pyproject.toml`, `Dockerfile`, `influx.toml`
- [ ] TOML config loader with env var overrides (§19)
- [ ] Profile name validator (§4.4)
- [ ] Guarded HTTP client with SSRF + size + timeout caps (§13.4)
- [ ] arXiv fetcher module (`influx/sources/arxiv.py`)
- [ ] Filter module (`influx/filter.py`) with batching + JSON mode + Pydantic schema
- [ ] MCP client wrapper (`influx/lithos_client.py`) using `mcp` SDK + SSE
- [ ] Startup checks: config load, API-key preflight, and Lithos connectivity check
- [ ] Deduplication via `lithos_cache_lookup` (passing both `query` and `source_url`)
- [ ] Canonical note writer with source/date paths + frontmatter mapping (§9.2)
- [ ] Canonical-note repair/upgrade path (§9.5) with `## User Notes` preservation, `expected_version`, and one retry
- [ ] Archive downloader (`influx/storage.py`) with path-safety check
- [ ] In-process scheduler setup (`influx/scheduler.py`) with `max_instances=1`, `coalesce=True`
- [ ] Webhook notification to Agent Zero (fire-and-forget, 5s timeout)
- [ ] Endpoints: `GET /live`, `GET /ready`, `GET /status`, `POST /runs`, and `POST /backfills`
- [ ] Structured JSON logging to stdout
- [ ] `docker-compose.yml` with `.env.dev` / `.env.prod`
- [ ] CLI thin clients: `validate-config`, `run --profile X`, and `backfill ...`

**M1 acceptance:** `./run.sh dev up` → scheduled run fires at configured cron → a new arXiv paper that matches `ai-robotics` appears in Lithos with `agent=influx`, correct `source_url`, `arxiv-id:...` tag, source/date path, and is absent on the following run (dedup works). Agent Zero webhook receives the digest. `/live` returns `200`, `/ready` returns `200` with `ready=true`, and `/status` reports healthy cached dependency checks plus a non-null `next_run_at` for the scheduled profile.

### Milestone 2 — Full Text, Enrichment & LCMA Edges (v0.2)
*Goal: richer notes + LCMA graph seeding*

- [ ] arXiv HTML extraction path + quality gate (≥1000 chars)
- [ ] PDF text extraction fallback
- [ ] HTML sanitisation (strip `<script>` etc. per §13.5)
- [ ] Tier 2 full-text section writer on the canonical note
- [ ] Tier 1 LLM enrichment with JSON mode + Pydantic schema
- [ ] Tier 3 deep extraction for score ≥ `deep_extract` threshold
- [ ] LCMA deployment contract + runtime error handling per §10
- [ ] `lithos_retrieve` post-ingestion connection query
- [ ] `lithos_edge_upsert` for `builds_on` (deterministic arXiv-ID resolution) and `related_to` (score ≥ threshold)
- [ ] `lithos_task_create` / `lithos_task_complete` per run with `outcome`
- [ ] "Related in your knowledge base" in notifications

**M2 acceptance:** For a paper scoring ≥ 9, one canonical note exists in Lithos and includes Tier 1, Tier 2, and Tier 3 sections ahead of `## User Notes`. `lithos_related` on that note shows `builds_on` / `related_to` edges where applicable. If the connected Lithos deployment is not LCMA-enabled, the run fails with a clear deployment/configuration error.

### Milestone 3 — Multiple Profiles & RSS (v0.3)
*Goal: multi-profile support + blog/RSS monitoring*

- [ ] Multi-profile pipeline orchestration (sequential, with per-profile locks)
- [ ] Profile-union note tagging, profile-scoped rejection tags, and per-profile negative examples
- [ ] RSS feed fetcher
- [ ] Web article extraction
- [ ] Config-driven feed list per profile
- [ ] Backfill CLI/API (`influx backfill --profile ... --days N` / `--from` / `--to` / `--all-profiles` → `POST /backfills`) with cost-estimate + `--confirm`
- [ ] Feed-name slug validation + archive path-safety enforcement

**M3 acceptance:** Two profiles (e.g. `ai-robotics`, `hema`) both run in one scheduled fire. If the same source matches both profiles, Lithos contains one canonical note carrying both profile tags; if different sources match, each is written once under the source/date path. RSS items from configured feeds appear alongside arXiv items. Backfill over 7 days completes and never overlaps with a scheduled run.

### Milestone 4 — Observability (v0.4)
*Goal: production-ready telemetry*

- [ ] `influx/telemetry.py` — mirrors Lithos OTEL pattern
- [ ] `@traced` decorator on key pipeline stages with standard attributes (§14.2)
- [ ] OTEL metrics: items fetched, filtered, ingested, errors (per profile)
- [ ] Tag rejection rate reporting (per §4 `feedback.recalibrate_after_runs`)

**M4 acceptance:** With `INFLUX_OTEL_ENABLED=true` and a local collector, spans appear in the collector with correct attributes and run-level metrics. With OTEL disabled, nothing changes from M3 behaviour.

---

## 18. Testing Strategy

### 18.1 Test Types

| Layer | Scope | Tooling |
|---|---|---|
| Unit | Config loader, URL normaliser, path-safety, user-notes preservation logic, Pydantic schemas, prompt formatters, slugifier | `pytest`, no network |
| Contract | MCP client wrapper against a fake Lithos server | `pytest` + `mcp` SDK test harness |
| Integration | End-to-end against a real Lithos dev container, recorded arXiv responses, and a mocked LLM client | `pytest` + `docker compose` + VCR.py for arXiv + response fixtures |
| E2E (manual) | One scheduled run against a staging Lithos with real APIs | Checklist in `docs/e2e.md` |

### 18.2 Coverage Target

- Unit: 80%+ for pure modules (config, URL, path, schemas, prompts)
- Contract: every Lithos tool called by Influx has a happy-path + error-envelope test
- Integration: at least one end-to-end arXiv → Lithos flow, one RSS → Lithos flow, one repair/upgrade merge flow

### 18.3 Fixtures

- `tests/fixtures/arxiv/` — real Atom/XML snapshots for 3–5 dates
- `tests/fixtures/rss/` — RSS feed snapshots for each configured feed type
- `tests/fixtures/litellm/` — pre-recorded JSON-mode responses keyed by input hash
- `tests/fixtures/lithos/` — seeded knowledge base for integration tests

---

## 19. Environment Variables

| Variable | Scope | Type | Default | Overrides config key | Notes |
|---|---|---|---|---|---|
| `INFLUX_CONFIG` | container | path | `/etc/influx/influx.toml` | — | Path to the TOML config |
| `INFLUX_ENVIRONMENT` | host+container | string | `dev` | — | Label for logs/telemetry |
| `INFLUX_ARCHIVE_PATH` | host | path | `./archive` | — | Host bind mount for archive volume |
| `INFLUX_ARCHIVE_DIR` | container | path | `/archive` | `storage.archive_dir` | |
| `INFLUX_HOST_PORT` | host | int | `8080` | — | Host port for the Influx HTTP API |
| `INFLUX_CONTAINER_NAME` | host | string | `influx` | — | |
| `INFLUX_UID` / `INFLUX_GID` | host | int | `1000` / `1000` | — | Container run-as user |
| `INFLUX_AGENT_ID` | container | string | `influx` | — | Agent identity for Lithos calls |
| `INFLUX_LOG_LEVEL` | container | enum | `INFO` | — | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `INFLUX_DRY_RUN` | container | bool | `false` | — | When `true`: fetch + filter but do not write to Lithos or webhook |
| `INFLUX_OTEL_ENABLED` | container | bool | `false` | `telemetry.enabled` | |
| `INFLUX_OTEL_CONSOLE_FALLBACK` | container | bool | `false` | `telemetry.console_fallback` | |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | container | url | empty | — | OTLP collector endpoint; set explicitly for your deployment |
| `LITHOS_URL` | container | url | empty | — | Lithos SSE endpoint; set explicitly for your deployment |
| `LITHOS_MCP_TRANSPORT` | container | enum | `sse` | — | Fixed to `sse` in v0.7 |
| `AGENT_ZERO_WEBHOOK_URL` | container | url | empty | `notifications.webhook_url` | Empty disables webhook |
| `OPENAI_API_KEY` | container | secret | empty | — | Read when any model uses the `openai` provider (default `providers.openai.api_key_env`) |
| `ANTHROPIC_API_KEY` | container | secret | empty | — | Read when any model uses the `anthropic` provider (default `providers.anthropic.api_key_env`) |
| `OPENROUTER_API_KEY` | container | secret | empty | — | Read when any model uses the `openrouter` provider (default `providers.openrouter.api_key_env`) |

Provider API-key env var names are themselves config-driven via `[providers.<name>].api_key_env`. The variables above are defaults. Adding a new provider (e.g. `[providers.together]`) introduces a new env var name without any code change — only the config needs updating. Provider base URLs are likewise config-driven (`[providers.<name>].base_url`) rather than env-driven.

---

## 20. Reserved Tags

Tags with the following prefixes or literals are reserved for the Influx/Lens protocol and must not be repurposed by humans in Lens:

| Prefix | Meaning |
|---|---|
| `profile:<name>` | Interest profile assignment |
| `source:<name>` | Source type (`arxiv`, `rss`, `blog`) |
| `arxiv-id:<id>` | arXiv identifier |
| `cat:<category>` | arXiv category |
| `text:<quality>` | Extraction quality (`html`, `pdf`, `abstract-only`) |
| `ingested-by:<agent>` | Always `ingested-by:influx` on Influx-authored notes |
| `schema:<N>` | Note schema version |
| `full-text` | Canonical note contains Tier 2 full-text sections (bare tag, not a prefix) |
| `influx:deep-extracted` | Canonical note contains Tier 3 deep-extraction sections |
| `influx:repair-needed` | Canonical note requires a future repair or upgrade pass |
| `influx:rejected:<profile>` | User feedback for a specific profile; authored by Lens, consumed by Influx |
| `influx:run` | Scheduled run task marker |
| `influx:backfill` | Backfill run task marker |

---

## 21. Retention

- `storage.retain_days` defaults to `3650` (~10 years) — effectively "keep forever by default".
- Influx does **not** currently delete archive files or notes; retention is advisory and intended as a hook for a future `python -m influx gc` command.
- At typical volumes (10–20 papers/day × ~5 MB) the archive grows ~30 GB/year. Users running multiple profiles should size the `influx-archive` volume accordingly.
- Lens may offer a "prune by tag" view in a later release — out of scope for Influx v1.

---

## Appendix A — Illustrative Dependencies

This appendix is informative. It lists one plausible stack for implementing the requirements; it is not a mandatory dependency set.

| Package Family / Example | Purpose |
|---------|---------|
| MCP client SDK | Lithos SSE client |
| Provider-routing / LLM client library | LLM provider abstraction and structured-output calls |
| Data validation library | Settings and LLM response schemas |
| In-process scheduler | Scheduled ingestion cycles |
| RSS / Atom parser | Feed parsing |
| HTML/article extractor | Web article text extraction |
| PDF text extraction library | PDF → text/markdown extraction |
| HTTP client library | Outbound HTTP with SSRF/timeout/size controls |
| Web framework + ASGI server | Operational HTTP API (`/live`, `/ready`, `/status`, `POST /runs`, `POST /backfills`) |
| Structured logging library | JSON logging to stdout |
| TOML writer (optional) | TOML writing if config needs updating at runtime |
| OpenTelemetry packages (optional) | OTEL export |

---

**End of Requirements v0.7**
