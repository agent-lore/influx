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
> **Influx** is a knowledge ingestion pipeline that monitors arXiv and web sources (blogs, Medium, RSS feeds) for new content matching one or more configurable interest profiles, filters it for relevance using an LLM, and feeds the results into a Lithos knowledge base as structured markdown notes. For each relevant item, Influx extracts clean text from the source (preferring arXiv HTML over PDF where available, using `trafilatura` for web articles), generates a structured summary with key contributions and relevance reasoning, downloads and archives the original source to a local file store, and writes exactly one canonical Lithos note per source. Profile membership is represented by tags on that canonical note rather than by duplicate notes. A feedback mechanism allows the user to mark items as irrelevant per profile, improving future filtering over time via negative few-shot examples. Influx has no UI of its own — a companion project **Lithos Lens** provides a local web UI with feed view and interactive graph visualisation of the knowledge base.

> [!info] Changes Since 0.5
> - Canonical note model pinned: exactly one Lithos note per source; profile membership is expressed by tags, not duplicate notes.
> - Lithos transport aligned to current canonical behavior: official `mcp` SDK over SSE.
> - Generic stale-update semantics replaced with explicit repair/upgrade semantics aligned to current `lithos_write`.
> - Full-text and deep-extraction content now extend the canonical note instead of creating tier-specific sibling notes.
> - LCMA is now a required dependency for Influx v0.7; startup fails fast if required LCMA tools are absent.
> - arXiv exact lookup is pinned to canonical `source_url` plus `arxiv-id:<id>` tags rather than filename assumptions.
> - Feedback tags are now profile-scoped (`influx:rejected:<profile>`) so one canonical note can be rejected for one profile without poisoning another.

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
- [[#21. Retention & Secrets]]

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
> Influx is a headless scheduled pipeline. All human-facing browsing, graph rendering, and feedback UI live in Lithos Lens. Influx exposes only a health endpoint and receives feedback indirectly via notes tagged `influx:rejected:<profile>` in Lithos.

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
| Scheduling | APScheduler (Python, in Influx container) | Configurable, handles missed runs, stays in-process |
| LLM access | LiteLLM, JSON mode + Pydantic schema | Provider-agnostic; enforces structured output |
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
| Health endpoint | Live readiness checks + current scheduler state | Avoids coupling health to persisted run history |
| OTEL | Opt-in, additive, optional packages | Consistent with Lithos conventions |
| Environments | `.env.dev` / `.env.prod` per service | Consistent with Lithos conventions |
| Logging | JSON to **stderr** (not stdout) | Matches Lithos; captured by `docker logs` |

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

### Environment Files & run.sh

Follows the Lithos convention exactly:

- Each service has `.env.dev` and `.env.prod` (and optionally `.env.staging`) files
- **No `env_file:` directive in `docker-compose.yml`** — instead, `run.sh` passes `--env-file .env.<env>` to `docker compose`, making those vars available for interpolation in the compose file
- The `environment:` section in compose passes specific vars into the container using `${VAR:-default}` syntax
- API keys and secrets are **not** in the env files — they are injected separately; see §21.2
- Config changes require a container restart (`./run.sh <env> restart`). There is no hot-reload.

See §19 for the canonical environment variable table.

### `docker-compose.yml`

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
      - INFLUX_CONFIG=/etc/influx/config.toml
      - LITHOS_URL=${LITHOS_URL:-http://host.docker.internal:8765/sse}
      - LITHOS_MCP_TRANSPORT=${LITHOS_MCP_TRANSPORT:-sse}
      - INFLUX_AGENT_ID=${INFLUX_AGENT_ID:-influx}
      - AGENT_ZERO_WEBHOOK_URL=${AGENT_ZERO_WEBHOOK_URL:-}
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - INFLUX_OTEL_ENABLED=${INFLUX_OTEL_ENABLED:-false}
      - OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT:-http://host.docker.internal:4318}
      - INFLUX_LOG_LEVEL=${INFLUX_LOG_LEVEL:-INFO}
      - INFLUX_DRY_RUN=${INFLUX_DRY_RUN:-false}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
    extra_hosts:
      - "host.docker.internal:host-gateway"

volumes:
  influx-archive:
```

### `run.sh`

Ships the same `run.sh` pattern as Lithos, adapted for `influx`:

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
    logs)    docker compose "${compose_args[@]}" logs -f 2>&1 | grep -v 'GET /health' ;;
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

When `INFLUX_CONFIG` is not set, Influx looks for `config.toml` in this order:

1. `./config.toml` (current working directory)
2. `~/.influx/config.toml` (user home)
3. `/etc/influx/config.toml` (system / container default)

First file found wins. The Docker image sets `INFLUX_CONFIG=/etc/influx/config.toml`.

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
# Models
# Each model slot accepts either a bare string (shorthand) or a table with
# per-model tuning. Shorthand `filter = "openai/gpt-4.1-mini"` is equivalent
# to `[models.filter]\nmodel = "openai/gpt-4.1-mini"`.
# ---------------------------------------------------------------------------

[models.filter]
model = "openai/gpt-4.1-mini"
temperature = 0.0
max_tokens = 2048

[models.enrich]
model = "openai/gpt-4.1-mini"
temperature = 0.2

[models.extract]
model = "anthropic/claude-sonnet-4.6"
temperature = 0.2

[models.litellm]
request_timeout = 30
max_retries = 2
json_mode = true              # enforce response_format={"type": "json_object"}

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

**Rate limiting:** Minimum **3 seconds** between successive requests to `export.arxiv.org` (arXiv courtesy guideline). This is separate from 429 backoff handling (see §13).

**arXiv HTML availability:** Most papers from 2020 onwards. Check with a HEAD request before full fetch; fall through to PDF on 404.

### 5.2 RSS Feeds

- Parse with `feedparser`
- Extract: title, URL, published date, summary
- Fetch full article text with `trafilatura`
- Deduplication by article URL via `lithos_cache_lookup`
- URL fetches are subject to the SSRF guard and size/timeout caps (see §13.4)

---

## 6. Relevance Filtering

### 6.1 Filter Prompt

The system prompt below is used with the configured `models.filter` model via LiteLLM **with JSON mode enabled** and responses validated against a Pydantic schema. The `INTEREST PROFILE` block is populated from the active profile's `description`. The `NEGATIVE EXAMPLES` block is populated at runtime from recent rejections stored in Lithos.

```
You are a research paper relevance filter. Score each paper for relevance
to the following interest profile.

## INTEREST PROFILE
{profile.description}

## NEGATIVE EXAMPLES
The following were previously marked as NOT interesting by the user.
Use them to calibrate your scoring (titles only, no abstracts):

{injected_negative_examples}

## OUTPUT FORMAT
Return a JSON object with a single key "results" whose value is an array.
For each paper with score >= 6 include:
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

Titles longer than 200 chars are truncated. Limit: `feedback.negative_examples_per_profile` (default 20).

### 6.4 Batching

- Papers sent to filter model in batches of 25
- Each batch contains: `ID`, `Title`, `Abstract` (full abstract for the batch items only)
- JSON mode enforced via `response_format={"type": "json_object"}` with the `FilterResponse` schema
- On JSON-mode parse failure: log the raw response, attempt a one-shot regex fallback to recover obvious results, and if that also fails the batch is skipped (papers requeue on the next run via `lithos_cache_lookup` miss)

### 6.5 Threshold Behaviour

| Score | Action |
|-------|--------|
| < relevance threshold (default 7) | Discard |
| ≥ 7 | Ingest or update the canonical note with Tier 1 sections |
| ≥ 8 | Canonical note includes Tier 2 full-text sections |
| ≥ 9 | Canonical note includes Tier 3 deep-extraction sections |
| ≥ notify_immediate (default 8) | Include in immediate notification |

### 6.6 Multi-Profile Runs

Each run processes all enabled profiles sequentially. A source may match multiple profiles. Influx writes one canonical note per source, unions the matching `profile:<name>` tags onto that note, stores the note under a source/date path, and records per-profile relevance inside the managed body. The note-wide `confidence` field is set to the maximum matched profile score divided by 10. Full-text and deep-extraction upgrades are triggered if any matched profile crosses the corresponding threshold.

---

## 7. Content Enrichment

### 7.1 Text Extraction Strategy

```
For arXiv papers:
  1. HEAD https://arxiv.org/html/{id} → if 200, GET and extract with trafilatura
     - Reject if extracted text < 1000 chars (likely nav/boilerplate); fall through.
  2. Fallback: download PDF → extract with pymupdf4llm → markdown
  3. Fallback: use abstract only, tag note `text:abstract-only`

For RSS/web articles:
  1. Fetch article URL (subject to SSRF guard + size/timeout caps)
  2. Extract with trafilatura → markdown
     - Reject if extracted text < 500 chars; fall through.
  3. Fallback: use feed summary only
```

### 7.2 LLM Enrichment (Tier 1 — all papers ≥ relevance threshold)

Uses `models.enrich` with JSON mode. Single LLM call from title + abstract:

```
Given this paper's title and abstract, extract:
1. Key contributions (3-5 bullet points, each ≤ 20 words)
2. Primary method or approach (1-2 sentences)
3. Main result or finding (1-2 sentences)
4. Relevance to: {one-line profile summary}

Return JSON: {"contributions": [...], "method": "...",
              "result": "...", "relevance": "..."}
```

Pydantic schema:

```python
class Tier1Enrichment(BaseModel):
    contributions: list[str] = Field(min_length=1, max_length=6)
    method: str
    result: str
    relevance: str
```

### 7.3 LLM Enrichment (Tier 3 — papers scoring ≥ deep_extract threshold)

Uses `models.extract` with JSON mode on the full text:

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
> Influx controls only the directory path passed to `lithos_write`. Current Lithos derives the filename from the note title slug. Influx MUST NOT assume caller-controlled basenames in v0.7.

> [!note] arXiv Exact Lookup
> Current Lithos does not let Influx choose a separate slug or filename for arXiv IDs. Influx therefore keeps the human paper title in `title` and relies on exact `source_url = https://arxiv.org/abs/<id>` plus `arxiv-id:<id>` tags for deterministic machine lookup. A future Lithos enhancement may add caller-specified filenames; that is out of scope for Influx v0.7.

### 8.3 Lithos Note Structure

Influx writes exactly **one canonical note per source**. Frontmatter below uses **only Lithos-allowed fields** (see Lithos SPEC §3.2 and §9.2 of this document). Influx-specific metadata (arXiv ID, categories, text quality, repair state, and stage-completion state) is carried via `tags`. The note-wide `confidence` field is the maximum matched profile score divided by `10.0`; per-profile reasoning is stored in the managed body.

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

<!-- INFLUX-MANAGED-START -->
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
<!-- INFLUX-MANAGED-END -->

## User Notes
```

The `agent="influx"` parameter is passed on every `lithos_write` call (see §9.3) — it appears in Lithos's `author`/`contributors` fields automatically and does not need to be in the note body.

Body ownership rules:

- Influx fully owns the content between `<!-- INFLUX-MANAGED-START -->` and `<!-- INFLUX-MANAGED-END -->`.
- Content outside the managed markers is preserved byte-for-byte on repair and upgrade writes.
- If an Influx-authored note is missing the managed markers, Influx skips automatic repair rather than risking user-content loss.

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

Appended sections inside the managed block:

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
- `LITHOS_URL` points to the Lithos SSE endpoint (for example `http://host.docker.internal:8765/sse`)
- Transport is fixed to `sse` for v0.7
- On startup, the client calls `tools/list` to probe available tools and logs the tool set (see §10.1 for LCMA availability handling)

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

# 2. Merge Influx-owned tags + replace only the managed body block.
merged_tags = merge_note_tags(
    existing_tags=existing["metadata"]["tags"],
    managed_tags=build_managed_tags(...),
)
merged_content = replace_managed_block(
    existing_content=existing["content"],
    managed_block=render_managed_block(...),
)

# 3. Write back the merged document. `agent` is still required.
write = lithos_write(
    id=doc_id,
    title=title,                     # may have been corrected upstream; replace
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
- Influx replaces only the body block between `<!-- INFLUX-MANAGED-START -->` and `<!-- INFLUX-MANAGED-END -->`.
- If those markers are missing, Influx logs a warning and skips automatic repair to avoid clobbering user edits.
- `profile:*` tags and `## Profile Relevance` entries are merged by profile name: newly matched profiles are added, currently processed profiles are refreshed, and unrelated existing profiles are preserved.
- `influx:rejected:<profile>` is authoritative for that profile while it remains on the note. When that tag is present, Influx MUST NOT re-add `profile:<profile>` if it is absent, and MUST NOT refresh or create the corresponding `## Profile Relevance` entry on subsequent repair/upgrade passes.
- Influx replaces the rest of the note tags it owns: `source:*`, `arxiv-id:*`, `cat:*`, `text:*`, `ingested-by:*`, `schema:*`, `full-text`, `influx:repair-needed`, and `influx:deep-extracted`.
- All other note tags are preserved, including `influx:rejected:<profile>`.
- `merged_confidence` is `max(existing_confidence, current_max_score / 10.0)` so a repair pass for one profile does not erase a higher historical match from another profile.
- `expires_at` is unset for complete notes. Incomplete notes may set `expires_at` to the next scheduled retry boundary, but Influx does not rely on `stale_exists` as its only repair trigger.
- If a note already contains richer sections than current thresholds require, Influx leaves them in place. It does not delete full-text or deep-extraction content automatically.

### 9.6 Write Path — Error Envelopes

`lithos_write` returns structured status envelopes. Handling per Lithos SPEC §10:

| Status / code | Influx action |
|---|---|
| `created` / `updated` | Proceed |
| `duplicate` | Treat as hit; log info |
| `error: invalid_input` | Log error with payload; skip item |
| `error: content_too_large` | Trim full-text sections first, then retry once |
| `error: slug_collision` | Retry once with a disambiguated title suffix (arXiv: ` [arXiv <id>]`; web: ` [<host>]`) |
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

Influx v0.7 requires a Lithos deployment with LCMA tools enabled. Startup fails fast if the required LCMA surface is absent. Where the older published Lithos spec lags, the current Lithos implementation is treated as canonical for Influx.

### 10.1 Availability Probe

On startup, Influx calls `tools/list` on the Lithos MCP server and stores the set of available tools. The following tools are required:

- `lithos_retrieve`
- `lithos_edge_upsert`
- `lithos_edge_list`
- `lithos_task_create`
- `lithos_task_complete`

If any are missing, `validate-config` and service startup fail fast with a clear error. After a successful probe, Influx calls `lithos_edge_list()` once to force lazy creation of `edges.db` before later edge writes.

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

Each pipeline run creates a Lithos task for LCMA coordination and audit:

```python
task = lithos_task_create(
    title=f"Influx run {date}",
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

Backfill runs use `influx:backfill` in place of `influx:run`.

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

Each returned item is read via `lithos_read(id=...)` to get the title, which is formatted as a single line (see §6.3) and injected into the `NEGATIVE EXAMPLES` block of the filter prompt. Abstracts are intentionally not injected — negatives are for calibration, not re-filtering.

Semantics:

- `influx:rejected:<profile>` suppresses that profile assignment even on a shared canonical note; it does not affect other profile tags on the same note.
- Lens profile-scoped views SHOULD exclude notes carrying `influx:rejected:<profile>` for the active profile by default.
- If the rejection tag is later removed, Influx may add `profile:<profile>` again on a future run if the source still matches.

---

## 13. Resilience & Error Handling

### 13.1 Failure Matrix

| Failure | Behaviour |
|---------|----------|
| arXiv API unreachable | Retry 3× with exponential backoff; skip run if all fail |
| arXiv rate limit (429) | Back off 10 seconds; retry up to 3× |
| HTML fetch fails | Fall back to PDF extraction |
| HTML extraction <1000 chars | Treat as failure; fall back to PDF |
| PDF download fails | Store canonical note with `text:abstract-only`, add `influx:repair-needed`, retry next run |
| Download exceeds `max_download_bytes` | Abort download; log; proceed with abstract-only |
| Download exceeds `download_timeout_seconds` | Same as above |
| LLM call fails | Retry 2×; store note without the failed sections, add `influx:repair-needed` (see §7.4) |
| LLM returns non-JSON despite JSON mode | Log raw response; attempt regex fallback; if that fails, skip item |
| Lithos unreachable on first call | Retry 3×; abort run; log error; exit non-zero |
| Lithos MCP tool missing at runtime | Abort the current run; log error; exit non-zero |
| LCMA tools missing at startup | Fail startup / `validate-config`; exit non-zero |
| Duplicate detected | Treat as hit; if repair/upgrade is needed, enter the merge path |
| `lithos_write` returns `version_conflict` | Re-read, re-apply, retry once; skip on second conflict |
| `lithos_write` returns `slug_collision` | Retry once with a disambiguated title suffix |
| `lithos_write` returns `content_too_large` | Truncate body; retry once |
| `lithos_edge_upsert` fails | Log warning; continue — edges are enrichment, not critical path |
| Webhook POST fails | Log warning; no retry (see §11.1) |

### 13.2 Retry Policy

- Max retries: 3
- Backoff: exponential (1s, 2s, 4s) unless otherwise specified
- Per-item failures do not abort the run
- Run-level failures (Lithos fully unreachable) abort the run and log; exit code 2

### 13.3 Run Concurrency

APScheduler is configured with `max_instances=1` per job, `coalesce=True`, and `misfire_grace_time=schedule.misfire_grace_seconds`. In addition, an in-process `asyncio.Lock` per profile prevents a manual `python -m influx run --profile X` from overlapping a scheduled run for the same profile.

### 13.4 SSRF & Download Safety

All outbound HTTP fetches (arXiv API, RSS feeds, article URLs, PDF downloads) go through a guarded HTTP client that enforces:

- Scheme is `http` or `https` only
- Resolved IPs are not in any of: loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`), private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`), or multicast. Override via `[security] allow_private_ips = true` for dev-only use.
- Max response size: `storage.max_download_bytes` (default 50 MB). Streams and aborts on overflow.
- Connect + read timeout: `storage.download_timeout_seconds` (default 30s)
- Response content-type must match the expected family (HTML, PDF, XML/Atom) — reject otherwise

### 13.5 Content Sanitisation

Extracted HTML (via `trafilatura`) may contain prompt-injection payloads. Influx does not execute extracted content, but downstream LLM consumers reading Influx-authored notes should treat content with the `ingested-by:influx` tag as **untrusted**. Influx additionally:

- Strips `<script>`, `<iframe>`, `<object>`, `<embed>` tags before conversion to markdown
- Does not preserve HTML fragments in markdown output

### 13.6 CLI Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success — including zero-result runs |
| 1 | Partial failure — one or more profiles failed but others succeeded |
| 2 | Total failure — e.g. Lithos unreachable, config invalid |
| 64 | Usage error — bad CLI arguments |

---

## 14. Observability

### 14.1 OTEL — Opt-In, Additive

Follows the same conventions as Lithos:

- OTEL is **opt-in** — `INFLUX_OTEL_ENABLED=true` enables it
- OTEL is **additive** — `docker logs influx` works exactly as before
- OTEL packages are **optional** — `uv sync --extra otel` installs them; Influx runs fine without
- **Console fallback** — `INFLUX_OTEL_CONSOLE_FALLBACK=true` prints spans to stdout (dev without collector)
- Uses `@traced` decorator pattern from `influx/telemetry.py` (mirrors `lithos/telemetry.py`)

**`pyproject.toml` optional dependency:**
```toml
[project.optional-dependencies]
otel = [
    "opentelemetry-sdk>=1.28.0",
    "opentelemetry-api>=1.28.0",
    "opentelemetry-exporter-otlp-proto-http>=1.28.0",
]
```

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

Follows the Lithos pattern — **stderr only, no log files**:

- All log output goes to stderr → captured by `docker logs influx`
- `INFLUX_LOG_LEVEL` controls verbosity (`DEBUG` in dev, `INFO` in prod)
- Structured JSON format via `python-json-logger`
- OTEL (when enabled) provides structured spans and metrics for deeper observability
- Influx does **not** persist run logs or run-history notes in Lithos; durable operational telemetry belongs in the OTEL collector and standard container logs.

### 14.4 Health Endpoint

- `GET http://localhost:8080/health` — returns live liveness/readiness state plus current scheduler state

```json
{
  "status": "ok",
  "ready": true,
  "checks": {
    "config": "ok",
    "scheduler": "ok",
    "lithos": "ok",
    "lithos_tools": "ok",
    "llm_credentials": "ok"
  },
  "profiles": {
    "ai-robotics": {
      "scheduled": true,
      "next_run_at": "2026-03-17T06:00:00Z"
    }
  }
}
```

Semantics:

- `status` is the overall service state: `ok` if all required readiness checks pass, `degraded` if the process is alive but one or more readiness checks fail, `starting` before the first readiness evaluation completes.
- `ready` is `true` only when Influx can perform a scheduled ingestion cycle immediately.
- HTTP status is `200 OK` when `ready=true` and `503 Service Unavailable` otherwise. Docker/container health checks MUST treat `/health` as a readiness endpoint.
- `checks.config` means config loaded and validated successfully.
- `checks.scheduler` means APScheduler is running and the expected jobs are registered.
- `checks.lithos` means the Lithos SSE endpoint is reachable.
- `checks.lithos_tools` means the required Lithos tools are present: `lithos_cache_lookup`, `lithos_write`, `lithos_read`, `lithos_retrieve`, `lithos_edge_upsert`, `lithos_edge_list`, `lithos_task_create`, and `lithos_task_complete`.
- `checks.llm_credentials` means the configured LLM provider key is present and basic client construction succeeds; it does not require a paid completion call on every health probe.
- `profiles.<name>.scheduled` indicates whether a scheduler job is currently registered for that profile.
- `profiles.<name>.next_run_at` comes from APScheduler `next_fire_time`; it is `null` if the profile is disabled or no next fire time is currently known.
- `/health` is computed live from in-memory state and dependency probes. It does not depend on persisted run history, Lithos notes, or previous run outcomes.

---

## 15. Backfill Mode

```bash
python -m influx backfill --profile ai-robotics --days 30
python -m influx backfill --profile ai-robotics --from 2026-01-01 --to 2026-03-15
python -m influx backfill --all-profiles --days 7
```

- Fetches papers day by day for the specified range
- Respects arXiv rate limits (3s between requests; plan for ~30s per day of backfill per profile)
- Skips already-ingested papers via `lithos_cache_lookup`
- **Does not send notifications** during backfill
- Creates `lithos_task_create`/`complete` tasks tagged `influx:backfill` so dashboards can filter them out
- Logs progress to stderr
- Prints an estimated LLM cost at start; requires `--confirm` when expected item count > 1000
- Respects the same concurrency locks as scheduled runs (see §13.3)

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

### 16.2 LiteLLM

**Recommended models:**

| Use case | Config key | Default model | Notes |
|----------|-----------|--------------|-------|
| Filtering | `models.filter` | `openai/gpt-4.1-mini` | Fast, cheap, sufficient |
| Enrichment | `models.enrich` | `openai/gpt-4.1-mini` | Same model fine |
| Deep extraction | `models.extract` | `anthropic/claude-sonnet-4.6` | Better for nuanced extraction |
| Local/offline | any | `ollama/llama3.2` | Via LiteLLM Ollama provider |

JSON mode (`response_format={"type": "json_object"}`) is required for all filter/enrichment calls. Models must be tested for JSON-mode compatibility at `python -m influx validate-config` time (see §16.4).

### 16.3 Lithos MCP API — Influx Usage

| Tool | Required args | Purpose |
|------|---------------|---------|
| `lithos_cache_lookup(query, source_url?, max_age_hours?, tags?)` | `query` | Deduplication and repair/upgrade decisioning before processing. `query` is REQUIRED even when a `source_url` fast-path hit is expected. |
| `lithos_write(title, content, agent, ...)` | `title`, `content`, `agent` | Write the single canonical note for a source. Always pass all three required fields — even on updates. |
| `lithos_read(id)` | `id` | Load rejected-note titles for negative examples; load the existing document before repair/upgrade writes. |
| `lithos_retrieve(query, limit, agent_id, task_id, tags)` | `query` | LCMA post-ingestion connection query |
| `lithos_list(path_prefix?, tags?, since?, limit?)` | none | Load negative examples and exact arXiv tag hits |
| `lithos_edge_list(from_id?, to_id?, type?, namespace?)` | none | Bootstrap `edges.db` on startup and inspect LCMA state |
| `lithos_edge_upsert(from_id, to_id, type, weight, namespace, ...)` | as named | Create typed semantic edges between notes |
| `lithos_task_create(title, agent, tags?)` | `title`, `agent` | Create scheduled-run or backfill coordination task |
| `lithos_task_complete(task_id, agent, outcome?)` | `task_id`, `agent` | Complete scheduled-run or backfill task with outcome summary; Influx v0.7 does not send automated retrieval-feedback fields |
| `lithos_agent_register(id, name?, type?)` | `id` | Register on startup (optional — auto-registers otherwise) |

### 16.4 Influx CLI

| Command | Purpose |
|---|---|
| `python -m influx` | Run once, then exit (used by scheduler in container mode after boot) |
| `python -m influx serve` | Start the scheduler + health endpoint (container default) |
| `python -m influx run --profile X` | Trigger a single run for one profile now |
| `python -m influx backfill ...` | See §15 |
| `python -m influx validate-config` | Parse config, dry-connect to Lithos, probe tools, print effective config, exit non-zero if anything is wrong |
| `python -m influx migrate-notes` | Apply future schema upgrades to existing Influx-authored notes, including 0.5-style note-shape migrations if needed |

---

## 17. Implementation Plan

### Milestone 1 — arXiv Pipeline (v0.1)
*Goal: daily arXiv monitoring → Lithos ingestion → notification*

- [ ] Project scaffold: `pyproject.toml`, `Dockerfile`, `config.toml`
- [ ] TOML config loader with env var overrides (§19)
- [ ] Profile name validator (§4.4)
- [ ] Guarded HTTP client with SSRF + size + timeout caps (§13.4)
- [ ] arXiv fetcher module (`influx/sources/arxiv.py`)
- [ ] LiteLLM filter module (`influx/filter.py`) with batching + JSON mode + Pydantic schema
- [ ] MCP client wrapper (`influx/lithos_client.py`) using `mcp` SDK + SSE
- [ ] Startup probe: `tools/list`, API-key preflight, required LCMA tools, and `lithos_edge_list()` bootstrap
- [ ] Deduplication via `lithos_cache_lookup` (passing both `query` and `source_url`)
- [ ] Canonical note writer with source/date paths + frontmatter mapping (§9.2)
- [ ] Managed-body repair/upgrade path (§9.5) with `expected_version` + one retry
- [ ] Archive downloader (`influx/storage.py`) with path-safety check
- [ ] APScheduler setup (`influx/scheduler.py`) with `max_instances=1`, `coalesce=True`
- [ ] Webhook notification to Agent Zero (fire-and-forget, 5s timeout)
- [ ] Health endpoint (`GET /health`) with live readiness checks and scheduler state
- [ ] Structured JSON logging to stderr (`python-json-logger`)
- [ ] `docker-compose.yml` with `.env.dev` / `.env.prod`
- [ ] CLI: `validate-config` and `run --profile X`

**M1 acceptance:** `./run.sh dev up` → scheduled run fires at configured cron → a new arXiv paper that matches `ai-robotics` appears in Lithos with `agent=influx`, correct `source_url`, `arxiv-id:...` tag, source/date path, and is absent on the following run (dedup works). Agent Zero webhook receives the digest. `/health` reports `ready=true`, passing dependency checks, and a non-null `next_run_at` for the scheduled profile.

### Milestone 2 — Full Text, Enrichment & LCMA Edges (v0.2)
*Goal: richer notes + LCMA graph seeding*

- [ ] arXiv HTML fetcher with trafilatura extraction + quality gate (≥1000 chars)
- [ ] PDF text extraction with pymupdf4llm (fallback)
- [ ] HTML sanitisation (strip `<script>` etc. per §13.5)
- [ ] Tier 2 full-text section writer on the canonical note
- [ ] Tier 1 LLM enrichment with JSON mode + Pydantic schema
- [ ] Tier 3 deep extraction for score ≥ `deep_extract` threshold
- [ ] LCMA-required startup validation wrapping §10
- [ ] `lithos_retrieve` post-ingestion connection query
- [ ] `lithos_edge_upsert` for `builds_on` (deterministic arXiv-ID resolution) and `related_to` (score ≥ threshold)
- [ ] `lithos_task_create` / `lithos_task_complete` per run with `outcome`
- [ ] "Related in your knowledge base" in notifications

**M2 acceptance:** For a paper scoring ≥ 9, one canonical note exists in Lithos and includes Tier 1, Tier 2, and Tier 3 sections in the managed body. `lithos_related` on that note shows `builds_on` / `related_to` edges where applicable. If required LCMA tools are absent, startup fails fast.

### Milestone 3 — Multiple Profiles & RSS (v0.3)
*Goal: multi-profile support + blog/RSS monitoring*

- [ ] Multi-profile pipeline orchestration (sequential, with per-profile locks)
- [ ] Profile-union note tagging, profile-scoped rejection tags, and per-profile negative examples
- [ ] RSS feed fetcher with `feedparser`
- [ ] Web article extraction with `trafilatura`
- [ ] Config-driven feed list per profile
- [ ] Backfill CLI (`influx backfill --profile ... --days N` / `--from` / `--to` / `--all-profiles`) with cost-estimate + `--confirm`
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
| Unit | Config loader, URL normaliser, path-safety, managed-body merge logic, Pydantic schemas, prompt formatters, slugifier | `pytest`, no network |
| Contract | MCP client wrapper against a fake Lithos server | `pytest` + `mcp` SDK test harness |
| Integration | End-to-end against a real Lithos dev container, recorded arXiv responses, and mocked LiteLLM | `pytest` + `docker compose` + VCR.py for arXiv + LiteLLM response fixtures |
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
| `INFLUX_CONFIG` | container | path | `/etc/influx/config.toml` | — | Path to the TOML config |
| `INFLUX_ENVIRONMENT` | host+container | string | `dev` | — | Label for logs/telemetry |
| `INFLUX_ARCHIVE_PATH` | host | path | `./archive` | — | Host bind mount for archive volume |
| `INFLUX_ARCHIVE_DIR` | container | path | `/archive` | `storage.archive_dir` | |
| `INFLUX_HOST_PORT` | host | int | `8080` | — | Host port for health endpoint |
| `INFLUX_CONTAINER_NAME` | host | string | `influx` | — | |
| `INFLUX_UID` / `INFLUX_GID` | host | int | `1000` / `1000` | — | Container run-as user |
| `INFLUX_AGENT_ID` | container | string | `influx` | — | Agent identity for Lithos calls |
| `INFLUX_LOG_LEVEL` | container | enum | `INFO` | — | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `INFLUX_DRY_RUN` | container | bool | `false` | — | When `true`: fetch + filter but do not write to Lithos or webhook |
| `INFLUX_OTEL_ENABLED` | container | bool | `false` | `telemetry.enabled` | |
| `INFLUX_OTEL_CONSOLE_FALLBACK` | container | bool | `false` | `telemetry.console_fallback` | |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | container | url | `http://host.docker.internal:4318` | — | OTLP collector |
| `LITHOS_URL` | container | url | `http://host.docker.internal:8765/sse` | — | Lithos SSE endpoint |
| `LITHOS_MCP_TRANSPORT` | container | enum | `sse` | — | Fixed to `sse` in v0.7 |
| `AGENT_ZERO_WEBHOOK_URL` | container | url | empty | `notifications.webhook_url` | Empty disables webhook |
| `OPENROUTER_API_KEY` | container | secret | empty | — | Required if any `models.*` uses `openrouter/*` |
| `OPENAI_API_KEY` | container | secret | empty | — | Required if any `models.*` uses `openai/*` |
| `ANTHROPIC_API_KEY` | container | secret | empty | — | Required if any `models.*` uses `anthropic/*` |

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

## 21. Retention & Secrets

### 21.1 Retention

- `storage.retain_days` defaults to `3650` (~10 years) — effectively "keep forever by default".
- Influx does **not** currently delete archive files or notes; retention is advisory and intended as a hook for a future `python -m influx gc` command.
- At typical volumes (10–20 papers/day × ~5 MB) the archive grows ~30 GB/year. Users running multiple profiles should size the `influx-archive` volume accordingly.
- Lens may offer a "prune by tag" view in a later release — out of scope for Influx v1.

### 21.2 Secrets

API keys are **never** written to `.env.dev` / `.env.prod`. They are injected separately — typical options:

- A sibling `.a0proj/secrets.env` file sourced by `run.sh` before invoking `docker compose`
- An OS secret manager (e.g. `pass`, `1password-cli`)
- A CI secret store for production deploys

Influx fails fast on startup if any configured model's required API key env var is empty. The error message names the missing variable and the model that needs it. This prevents silent failures deep inside a scheduled run.

---

## Appendix A — Directory Structure

```
influx/
├── Dockerfile
├── docker-compose.yml
├── .env.dev
├── .env.prod
├── pyproject.toml
├── README.md
├── run.sh
├── config/
│   └── config.toml
├── influx/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py            # TOML loader + Pydantic models + profile-name validation
│   ├── http_client.py       # Guarded HTTP client (SSRF, size, timeout)
│   ├── scheduler.py
│   ├── pipeline.py
│   ├── filter.py            # LiteLLM + JSON mode + Pydantic schemas
│   ├── enrichment.py
│   ├── lithos_client.py     # mcp SDK + SSE
│   ├── notifier.py
│   ├── storage.py           # Archive downloader + path-safety
│   ├── telemetry.py         # mirrors lithos/telemetry.py
│   ├── sources/
│   │   ├── arxiv.py
│   │   └── rss.py
│   └── extraction/
│       ├── html.py
│       └── pdf.py
└── tests/
    ├── unit/
    ├── contract/
    ├── integration/
    └── fixtures/
```

---

## Appendix B — Key Dependencies

| Package | Purpose |
|---------|---------|
| `mcp` | Official MCP Python SDK (SSE client) |
| `litellm` | LLM provider abstraction, JSON mode |
| `pydantic` | Data validation, settings, and LLM response schemas |
| `apscheduler` | In-process scheduling |
| `feedparser` | RSS feed parsing |
| `trafilatura` | Web article text extraction |
| `pymupdf4llm` | PDF → markdown extraction |
| `httpx` | Async HTTP client (wrapped with SSRF guard) |
| `fastapi` + `uvicorn` | Health endpoint |
| `python-json-logger` | Structured JSON logging to stderr |
| `tomli-w` | TOML writing (if config needs updating at runtime) |
| `opentelemetry-*` | OTEL (optional extra: `uv sync --extra otel`) |

---

**End of Requirements v0.7**
