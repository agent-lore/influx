---
title: Influx — Requirements Document
version: 0.5.0
date: 2026-04-22
status: draft
tags: [influx, requirements, design, architecture]
supersedes: INFLUX-REQUIREMENTS-0.4.md
---

# Influx — Requirements Document

> [!abstract] Project Summary
> **Influx** is a knowledge ingestion pipeline that monitors arXiv and web sources (blogs, Medium, RSS feeds) for new content matching one or more configurable interest profiles, filters it for relevance using an LLM, and feeds the results into a Lithos knowledge base as structured markdown notes. For each relevant item, Influx extracts clean text from the source (preferring arXiv HTML over PDF where available, using `trafilatura` for web articles), generates a structured summary with key contributions and relevance reasoning, downloads and archives the original source to a local file store, and writes a rich Lithos note linking back to both the canonical source URL and the local file. A feedback mechanism allows the user to mark items as irrelevant, improving future filtering over time via negative few-shot examples. Influx has no UI of its own — a companion project **Lithos Lens** provides a local web UI with feed view and interactive graph visualisation of the knowledge base.

> [!info] Changes since 0.4
> - Logging corrected to stderr (matches Lithos).
> - MCP client/transport pinned (official `mcp` SDK + Streamable HTTP).
> - Frontmatter mapping table added (§9.2) — Lithos-allowed fields only; extensions go in `tags`.
> - Stale-cache update semantics (§9.4) fully specified.
> - LLM JSON-mode + Pydantic schema enforcement pinned (§6, §7).
> - SSRF guard, download size, and timeout caps added (§13).
> - LCMA availability probe, profile-name validation, archive-path sanitisation, webhook/backfill/concurrency/health-endpoint rules all specified.
> - New sections: §18 Testing Strategy, §19 Environment Variables, §20 Reserved Tags, §21 Retention & Secrets. Milestones gained acceptance criteria.

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
- Ingest structured notes into Lithos knowledge base, organised by profile and date
- Use LCMA retrieval and edge tools to surface connections at ingest time (when LCMA is available)
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
> Influx is a headless scheduled pipeline. All human-facing browsing, graph rendering, and feedback UI live in Lithos Lens. Influx exposes only a health endpoint and receives feedback indirectly via notes tagged `influx:rejected` in Lithos.

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
| Deduplication | Lithos `lithos_cache_lookup` by `source_url` | Lithos is the source of truth; no separate state DB |
| Archive storage | Local filesystem shared volume | Simple, human-accessible, easy to back up |
| Lithos communication | Official `mcp` Python SDK, **Streamable HTTP** transport | Canonical; SSE is on a deprecation path |
| Notification | Webhook to Agent Zero, fire-and-forget | Real-time; no polling; no local queue |
| Text extraction | arXiv HTML → PDF fallback → abstract-only | Quality-first with graceful degradation |
| Feedback storage | Lithos notes tagged `influx:rejected` | Lithos is source of truth; LCMA can reason over rejections |
| Config format | TOML (Python 3.12 built-in `tomllib`) | Consistent with Cardinal and other recent projects |
| Interest profiles | Multiple named profiles | Keeps unrelated domains (AI/robotics vs HEMA) cleanly separated |
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
      - LITHOS_URL=${LITHOS_URL:-http://host.docker.internal:8765}
      - LITHOS_MCP_TRANSPORT=${LITHOS_MCP_TRANSPORT:-streamable-http}
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
# description, source list, thresholds, and Lithos path prefix.
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
lcma_edge_similarity = 0.75  # minimum lithos_retrieve score to create related_to edge

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

Profile names are used in tags (`profile:<name>`), archive paths (`/archive/<source>/<name>/...`), and Lithos note paths (`papers/<name>/...`). They must match `^[a-z][a-z0-9-]{0,31}$`. Invalid names cause startup to fail with a clear error — do not silently slugify.

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
| ≥ 7 | Ingest Tier 1 summary note |
| ≥ 8 | Ingest Tier 1 + Tier 2 full text note |
| ≥ 9 | Ingest Tier 1 + Tier 2 + Tier 3 deep extraction |
| ≥ notify_immediate (default 8) | Include in immediate notification |

### 6.6 Multi-Profile Runs

Each run processes all enabled profiles sequentially. A paper may match multiple profiles — it is ingested once per matching profile with separate notes under each profile's path, tagged with the profile name.

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

If any enrichment call fails after retries, the note is still written **without** the failed section. Missing sections are omitted from the body; no placeholder text is inserted. The failure is logged with the note ID and retried on the next scheduled run that re-encounters the paper (rare, since dedup will typically skip).

---

## 8. Storage

### 8.1 Archive Store

**Volume:** `influx-archive` mounted at `/archive`

**Layout:** `/{source}/{profile}/{YYYY}/{MM}/{id}.{ext}`

**Examples:**
```
/archive/arxiv/ai-robotics/2026/03/2603.12939.pdf
/archive/arxiv/hema/2026/03/2603.99999.pdf
/archive/blog/ai-robotics/2026/03/karpathy-2026-03-15.html
/archive/blog/ai-robotics/2026/03/lilianweng-2026-03-10.html
```

**Naming convention:**
- arXiv: use arXiv ID (e.g. `2603.12939`)
- Blog/web: `{feed-name-slug}-{YYYY-MM-DD}`
- Extension: `.pdf` for papers, `.html` for saved web articles

**Path safety:** feed names are slugified (`^[a-z0-9]+(-[a-z0-9]+)*$`, max 40 chars). After constructing the full archive path, Influx must verify `path.resolve().is_relative_to(archive_root)` and reject otherwise. Empty slugs (e.g. feed name of all-whitespace) are rejected at config load.

**On download failure:** Log error, set `local_file: null` on the Lithos note, retry next run.

### 8.2 Lithos Note Paths

Notes are organised by profile, source type, year, and month to avoid directory bloat as the knowledge base grows:

```
papers/{profile}/{YYYY}/{MM}/{id}
```

**Examples:**
```
papers/ai-robotics/2026/03/2603.12939
papers/ai-robotics/2026/03/karpathy-2026-03-15
papers/hema/2026/03/some-hema-article
```

> [!note] Directory Scale Planning
> At 10-20 ingested papers/day, a flat `papers/` directory would accumulate ~5,000 files/year. The `{profile}/{YYYY}/{MM}/` hierarchy caps any single directory at ~200-400 files (one month's intake for one profile), which Linux handles comfortably. At higher ingestion rates, adding `/{DD}/` is a simple config change.

### 8.3 Lithos Note Structure

Frontmatter below uses **only Lithos-allowed fields** (see Lithos SPEC §3.2 and §9.2 of this document). Influx-specific metadata (arxiv ID, categories, text quality, relevance score) is carried via `tags` and reserved keys.

#### Tier 1 — Summary Note (all ingested items)

```markdown
---
title: "{Paper Title}"
source_url: https://arxiv.org/abs/2603.12939
tags:
  - profile:ai-robotics
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
confidence: 0.9              # relevance_score / 10.0
note_type: summary
namespace: influx
---

# {Paper Title}

**Authors:** Author A, Author B
**Published:** 2026-03-16
**Ingested:** 2026-03-16T06:12:34Z
**Local file:** `/archive/arxiv/ai-robotics/2026/03/2603.12939.pdf`

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

## Relevance
{why this was flagged — from filter reason + enrichment}

## Links
- [arXiv page](https://arxiv.org/abs/2603.12939)
- [PDF](https://arxiv.org/pdf/2603.12939)
```

The `agent="influx"` parameter is passed on every `lithos_write` call (see §9.3) — it appears in Lithos's `author`/`contributors` fields automatically and does not need to be in the note body.

#### Tier 2 — Full Text Note (score ≥ full_text threshold)

```markdown
---
title: "{Paper Title} — Full Text"
source_url: https://arxiv.org/abs/2603.12939
tags:
  - profile:ai-robotics
  - source:arxiv
  - arxiv-id:2603.12939
  - full-text
  - ingested-by:influx
  - schema:1
note_type: observation
namespace: influx
# derived_from_ids is set by Influx at write time (list containing the Tier 1
# summary UUID). Lithos auto-projects derived_from_ids into edges.db as
# `derived_from` edges — do NOT manually upsert those edges (see §10.2).
---

# {Paper Title} — Full Text

> Summary note: [[{Paper Title}]]

## Introduction
{extracted text}

## Related Work
{extracted text}

## Methods
{extracted text}

## Experiments / Results
{extracted text}

## Discussion
{extracted text}

## Conclusion
{extracted text}
```

#### Tier 3 — Deep Extraction additions to summary note (score ≥ deep_extract threshold)

Appended sections to the Tier 1 note:

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

---

## 9. Lithos Integration

### 9.1 MCP Client & Transport

Influx uses the **official `mcp` Python SDK** connecting to Lithos via **Streamable HTTP** (`LITHOS_MCP_TRANSPORT=streamable-http`, default). SSE is supported as a fallback for older Lithos deployments but is considered legacy.

- Client wrapper lives at `influx/lithos_client.py`
- Connection is established lazily on first use and kept alive for the duration of a run
- Transport is configurable via env (`LITHOS_MCP_TRANSPORT`) to allow graceful migration
- On startup, the client calls `tools/list` to probe available tools and logs the tool set (see §10.3 for LCMA availability handling)

### 9.2 Frontmatter Mapping (Lithos schema ↔ Influx concepts)

Lithos enforces a defined frontmatter schema (SPEC §3.2). Anything outside it is not guaranteed to round-trip. Influx maps its concepts as follows:

| Influx concept | Lithos field or tag | Notes |
|---|---|---|
| Canonical URL | `source_url` | Dedup key after normalisation |
| Relevance score (0–10) | `confidence` (0.0–1.0) | `confidence = score / 10` |
| Profile name | `tag: profile:<name>` | Reserved prefix (see §20) |
| Source type | `tag: source:arxiv`, `source:rss`, `source:blog` | |
| arXiv ID | `tag: arxiv-id:<id>` | |
| Category | `tag: cat:<category>` (one per category) | |
| Text quality | `tag: text:html` \| `text:pdf` \| `text:abstract-only` | |
| Note authored by Influx | `tag: ingested-by:influx`, `namespace: influx`, and `agent="influx"` on write | |
| Note schema version | `tag: schema:<N>` | Sourced from `influx.note_schema_version` |
| Tier 2 → Tier 1 link | `derived_from_ids: [<tier1-uuid>]` | Lithos auto-projects to `derived_from` edge |
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
# result["hit"] is True        → skip (already have a fresh copy)
# result["stale_exists"]       → update existing note (result["stale_id"]); see §9.4
# otherwise                    → proceed with a fresh ingest
```

**URL normalisation (Influx side, before calling Lithos):**
- lowercase scheme and host
- drop default ports (`:80`, `:443`)
- strip tracking params: `utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `ref`
- remove trailing slash on path
- do NOT remove fragments — they are semantically meaningful for some blog URLs

Lithos further normalises on write; Influx pre-normalises so the fast-path hit rate is high even for duplicates reached via different feeds.

### 9.4 Write Path — Create

```python
result = lithos_write(
    title=title,
    content=tier1_body,
    agent="influx",                      # required on every call, creates and updates
    path=f"papers/{profile}/{yyyy}/{mm}",
    source_url=normalize_url(url),
    tags=build_tags(...),                # see §9.2
    confidence=score / 10.0,
    note_type="summary",
    namespace="influx",
)
# result["status"] ∈ {"created", "updated", "duplicate", "error"}
```

### 9.5 Write Path — Stale Update

When `lithos_cache_lookup` returns `stale_exists: true` with a `stale_id`, Influx replaces the existing note content in a single write:

```python
# 1. Read the current version to capture expected_version for optimistic lock
existing = lithos_read(id=stale_id)
expected_version = existing["metadata"]["version"]

# 2. Replace content + frontmatter-mapped fields; do NOT touch user-edited
#    fields. `agent` is still required.
write = lithos_write(
    id=stale_id,
    title=title,                     # may have been corrected upstream; replace
    content=tier1_body,              # full replacement of the body
    agent="influx",
    source_url=normalize_url(url),   # pass again (normalised may have changed)
    tags=build_tags(...),            # full replacement of Influx-managed tags
    confidence=score / 10.0,
    expected_version=expected_version,
)

# 3. On version_conflict: re-read once, re-apply, retry once. On second
#    conflict, log and skip (the next run will retry).
```

Rules:
- Tier 2 and Tier 3 content is regenerated from scratch on stale update (no merge).
- `ingested_at` (in the body, not frontmatter) is updated to the current run's timestamp.
- Any user-added tags that do not use reserved prefixes (`profile:`, `source:`, `arxiv-id:`, `cat:`, `text:`, `ingested-by:`, `schema:`, `influx:`) are **preserved** — Influx only replaces tags it knows it manages.
- If a Tier 1 note is stale-updated from score X → score Y where Y drops below `full_text` threshold, the Tier 2 note is **not** deleted. Users may choose to delete it in Lens.

### 9.6 Write Path — Error Envelopes

`lithos_write` returns structured status envelopes. Handling per Lithos SPEC §10:

| Status / code | Influx action |
|---|---|
| `created` / `updated` | Proceed |
| `duplicate` | Treat as hit; log info |
| `error: invalid_input` | Log error with payload; skip item |
| `error: content_too_large` | Truncate body at Lithos limit (see `max_content_size_bytes`); retry once |
| `error: slug_collision` | Append short hash to slug; retry once |
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

Lithos LCMA MVP1 and MVP2 tools are used by Influx to seed the knowledge graph at ingest time **when available**. Influx detects LCMA availability on startup and gracefully disables LCMA behaviour when the tools are not present.

### 10.1 Availability Probe

On startup, Influx calls `tools/list` on the Lithos MCP server and stores the set of available tools. If any of `lithos_retrieve`, `lithos_edge_upsert`, `lithos_task_create`, `lithos_task_complete` is missing, LCMA integration is disabled for the run and logged as a warning. The pipeline still functions — it just skips §10.2–§10.4.

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

**Do NOT** manually upsert `derived_from` edges — Lithos's `provenance_projection` reconcile scope projects `derived_from_ids` frontmatter into `edges.db` as `derived_from` edges automatically. Double-writing causes drift.

**Do** manually upsert:

- `builds_on` edges from Tier 3 extraction. For each named prior work, resolve deterministically via `lithos_cache_lookup(source_url=arxiv_abs_url, query=prior_title)` after attempting to extract an arXiv ID from the body. Create an edge only on an exact `source_url` match. Fuzzy title matching is deferred.
- `related_to` edges when `lithos_retrieve` returns a result with `score >= profiles.thresholds.lcma_edge_similarity` (default 0.75).

```python
lithos_edge_upsert(
    from_id=new_note_id,
    to_id=prior_id,
    type="builds_on",
    weight=0.8,
    namespace="influx",
    provenance_actor="influx",
    provenance_type="llm_extraction",
)

lithos_edge_upsert(
    from_id=new_note_id,
    to_id=related_id,
    type="related_to",
    weight=similarity_score,
    namespace="influx",
    provenance_actor="influx",
    provenance_type="semantic_similarity",
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
    cited_nodes=ingested_note_ids,
)
```

`lithos_task_complete` extended parameters (`outcome`, `cited_nodes`, `misleading_nodes`, `receipt_id`) are the canonical surface — verified in the Lithos implementation.

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
        {"title": "...", "similarity": 0.89}
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

Lithos Lens updates the rejected note's tags via `lithos_write` (see `LITHOS-LENS-REQUIREMENTS-0.4.md` §8 for the write-side contract). The resulting note carries the tag `influx:rejected`. Influx does not write feedback itself.

### 12.3 Injecting Negative Examples

At the start of each filter run, per profile — use `lithos_list` (tag-only filtering), **not** `lithos_search` (which requires a free-text `query` argument):

```python
rejected = lithos_list(
    tags=["influx:rejected", f"profile:{profile_name}"],
    limit=config.feedback.negative_examples_per_profile,
)
```

Each returned item is read via `lithos_read(id=...)` to get the title, which is formatted as a single line (see §6.3) and injected into the `NEGATIVE EXAMPLES` block of the filter prompt. Abstracts are intentionally not injected — negatives are for calibration, not re-filtering.

---

## 13. Resilience & Error Handling

### 13.1 Failure Matrix

| Failure | Behaviour |
|---------|----------|
| arXiv API unreachable | Retry 3× with exponential backoff; skip run if all fail |
| arXiv rate limit (429) | Back off 10 seconds; retry up to 3× |
| HTML fetch fails | Fall back to PDF extraction |
| HTML extraction <1000 chars | Treat as failure; fall back to PDF |
| PDF download fails | Store abstract-only note; set `local_file: null`; retry next run |
| Download exceeds `max_download_bytes` | Abort download; log; proceed with abstract-only |
| Download exceeds `download_timeout_seconds` | Same as above |
| LLM call fails | Retry 2×; store note without enrichment fields (see §7.4) |
| LLM returns non-JSON despite JSON mode | Log raw response; attempt regex fallback; if that fails, skip item |
| Lithos unreachable on first call | Retry 3×; abort run; log error; exit non-zero |
| Lithos MCP tool missing at runtime | Log error; skip that item; continue run |
| LCMA tools missing at startup | Disable §10; log warning; continue |
| Duplicate detected | Skip silently |
| `lithos_write` returns `version_conflict` | Re-read, re-apply, retry once; skip on second conflict |
| `lithos_write` returns `slug_collision` | Append short hash to slug; retry once |
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
- **Durable run history** is stored as Lithos notes at `path: "influx/runs"` — queryable, persistent, human-readable
- OTEL (when enabled) provides structured spans and metrics for deeper observability

### 14.4 Health Endpoint

- `GET http://localhost:8080/health` — returns the persisted run-state view

```json
{
  "status": "ok",
  "profiles": {
    "ai-robotics": {
      "status": "ok",
      "last_run_at": "2026-03-16T06:12:34Z",
      "last_run_status": "success",
      "last_run_ingested": 12,
      "next_run_at": "2026-03-17T06:00:00Z"
    }
  }
}
```

Semantics:

- `status` is the worst per-profile status: `ok` if all profiles succeeded on their last run, `degraded` if any last run failed, `starting` if no run has completed yet since container start.
- `last_run_at` is the wall-clock start of the most recent run attempt (success or failure).
- `next_run_at` comes from APScheduler `next_fire_time`.
- On startup, Influx reads `lithos_list(path_prefix="influx/runs", limit=1)` per profile to pre-populate `last_run_*` so `/health` is correct immediately and not just after the first run.

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
- Writes `influx/runs` notes and creates `lithos_task_create`/`complete` tasks tagged `influx:backfill` (not `influx:run`) so dashboards can filter them out
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
| `lithos_cache_lookup(query, source_url?, max_age_hours?, tags?)` | `query` | Deduplication before processing. `query` is REQUIRED even when a `source_url` fast-path hit is expected. |
| `lithos_write(title, content, agent, ...)` | `title`, `content`, `agent` | Write summary, full-text notes. Always pass all three required fields — even on updates. |
| `lithos_read(id)` | `id` | Load rejected-note titles for negative examples; load existing version before stale update. |
| `lithos_retrieve(query, limit, agent_id, task_id, tags)` | `query` | LCMA post-ingestion connection query |
| `lithos_list(path_prefix?, tags?, since?, limit?)` | none | Load negative examples for filter prompt (tag-only; not `lithos_search`) |
| `lithos_edge_upsert(from_id, to_id, type, weight, namespace, ...)` | as named | Create typed edges between notes — NOT `derived_from` (auto-projected) |
| `lithos_task_create(title, agent, tags?)` | `title`, `agent` | Create run coordination task |
| `lithos_task_complete(task_id, agent, outcome?, cited_nodes?)` | `task_id`, `agent` | Complete run task with outcome summary |
| `lithos_agent_register(id, name?, type?)` | `id` | Register on startup (optional — auto-registers otherwise) |

### 16.4 Influx CLI

| Command | Purpose |
|---|---|
| `python -m influx` | Run once, then exit (used by scheduler in container mode after boot) |
| `python -m influx serve` | Start the scheduler + health endpoint (container default) |
| `python -m influx run --profile X` | Trigger a single run for one profile now |
| `python -m influx backfill ...` | See §15 |
| `python -m influx validate-config` | Parse config, dry-connect to Lithos, probe tools, print effective config, exit non-zero if anything is wrong |
| `python -m influx migrate-notes` | Apply schema_version upgrades to existing Influx-authored notes (placeholder in v0.1) |

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
- [ ] MCP client wrapper (`influx/lithos_client.py`) using `mcp` SDK + Streamable HTTP
- [ ] Startup probe: `tools/list`, API-key preflight, LCMA availability
- [ ] Deduplication via `lithos_cache_lookup` (passing both `query` and `source_url`)
- [ ] Tier 1 note writer with profile-based paths + frontmatter mapping (§9.2)
- [ ] Stale-update path (§9.5) with `expected_version` + one retry
- [ ] Archive downloader (`influx/storage.py`) with path-safety check
- [ ] APScheduler setup (`influx/scheduler.py`) with `max_instances=1`, `coalesce=True`
- [ ] Webhook notification to Agent Zero (fire-and-forget, 5s timeout)
- [ ] Health endpoint (`GET /health`) with persisted run-state
- [ ] Structured JSON logging to stderr (`python-json-logger`)
- [ ] `docker-compose.yml` with `.env.dev` / `.env.prod`
- [ ] CLI: `validate-config` and `run --profile X`

**M1 acceptance:** `./run.sh dev up` → scheduled run fires at configured cron → a new arXiv paper that matches `ai-robotics` appears in Lithos with `agent=influx`, correct `source_url`, `profile:ai-robotics` tag, and is absent on the following run (dedup works). Agent Zero webhook receives the digest. `/health` shows `last_run_at` and `next_run_at`.

### Milestone 2 — Full Text, Enrichment & LCMA Edges (v0.2)
*Goal: richer notes + LCMA graph seeding*

- [ ] arXiv HTML fetcher with trafilatura extraction + quality gate (≥1000 chars)
- [ ] PDF text extraction with pymupdf4llm (fallback)
- [ ] HTML sanitisation (strip `<script>` etc. per §13.5)
- [ ] Tier 2 full text note writer (linked via `derived_from_ids`)
- [ ] Tier 1 LLM enrichment with JSON mode + Pydantic schema
- [ ] Tier 3 deep extraction for score ≥ `deep_extract` threshold
- [ ] LCMA availability gate wrapping §10
- [ ] `lithos_retrieve` post-ingestion connection query
- [ ] `lithos_edge_upsert` for `builds_on` (deterministic arXiv-ID resolution) and `related_to` (similarity ≥ threshold)
- [ ] `lithos_task_create` / `lithos_task_complete` per run with `outcome` + `cited_nodes`
- [ ] "Related in your knowledge base" in notifications

**M2 acceptance:** For a paper scoring ≥ 9, a Tier 1 note, Tier 2 full-text note, and Tier 3 sections all appear in Lithos. `lithos_related` on the Tier 1 note shows the Tier 2 note as a `derived_from` edge (auto-projected), plus `builds_on` / `related_to` edges where applicable. Running against a pre-LCMA Lithos produces a clear startup warning and skips §10 cleanly.

### Milestone 3 — Multiple Profiles & RSS (v0.3)
*Goal: multi-profile support + blog/RSS monitoring*

- [ ] Multi-profile pipeline orchestration (sequential, with per-profile locks)
- [ ] Profile-scoped paths, tags, and negative examples
- [ ] RSS feed fetcher with `feedparser`
- [ ] Web article extraction with `trafilatura`
- [ ] Config-driven feed list per profile
- [ ] Backfill CLI (`influx backfill --profile ... --days N` / `--from` / `--to` / `--all-profiles`) with cost-estimate + `--confirm`
- [ ] Feed-name slug validation + archive path-safety enforcement

**M3 acceptance:** Two profiles (e.g. `ai-robotics`, `hema`) both run in one scheduled fire, produce separate notes under their own paths and tags, and RSS items from configured feeds appear alongside arXiv items. Backfill over 7 days completes and never overlaps with a scheduled run.

### Milestone 4 — Observability (v0.4)
*Goal: production-ready telemetry*

- [ ] `influx/telemetry.py` — mirrors Lithos OTEL pattern
- [ ] `@traced` decorator on key pipeline stages with standard attributes (§14.2)
- [ ] OTEL metrics: items fetched, filtered, ingested, errors (per profile)
- [ ] Run history notes in Lithos (`influx/runs` path)
- [ ] Tag rejection rate reporting (per §4 `feedback.recalibrate_after_runs`)

**M4 acceptance:** With `INFLUX_OTEL_ENABLED=true` and a local collector, spans appear in the collector with correct attributes and run-level metrics. With OTEL disabled, nothing changes from M3 behaviour. `influx/runs` notes are queryable in Lens.

---

## 18. Testing Strategy

### 18.1 Test Types

| Layer | Scope | Tooling |
|---|---|---|
| Unit | Config loader, URL normaliser, path-safety, Pydantic schemas, prompt formatters, slugifier | `pytest`, no network |
| Contract | MCP client wrapper against a fake Lithos server | `pytest` + `mcp` SDK test harness |
| Integration | End-to-end against a real Lithos dev container, recorded arXiv responses, and mocked LiteLLM | `pytest` + `docker compose` + VCR.py for arXiv + LiteLLM response fixtures |
| E2E (manual) | One scheduled run against a staging Lithos with real APIs | Checklist in `docs/e2e.md` |

### 18.2 Coverage Target

- Unit: 80%+ for pure modules (config, URL, path, schemas, prompts)
- Contract: every Lithos tool called by Influx has a happy-path + error-envelope test
- Integration: at least one end-to-end arXiv → Lithos flow, one RSS → Lithos flow, one stale-update flow

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
| `LITHOS_URL` | container | url | `http://host.docker.internal:8765` | — | Lithos MCP endpoint |
| `LITHOS_MCP_TRANSPORT` | container | enum | `streamable-http` | — | `streamable-http` \| `sse` |
| `AGENT_ZERO_WEBHOOK_URL` | container | url | empty | `notifications.webhook_url` | Empty disables webhook |
| `OPENROUTER_API_KEY` | container | secret | empty | — | Required if any `models.*` uses `openrouter/*` |
| `OPENAI_API_KEY` | container | secret | empty | — | Required if any `models.*` uses `openai/*` |
| `ANTHROPIC_API_KEY` | container | secret | empty | — | Required if any `models.*` uses `anthropic/*` |

---

## 20. Reserved Tags

Tags with the following prefixes are managed by Influx and must not be used by humans in Lens to mean something else:

| Prefix | Meaning |
|---|---|
| `profile:<name>` | Interest profile assignment |
| `source:<name>` | Source type (`arxiv`, `rss`, `blog`) |
| `arxiv-id:<id>` | arXiv identifier |
| `cat:<category>` | arXiv category |
| `text:<quality>` | Extraction quality (`html`, `pdf`, `abstract-only`) |
| `ingested-by:<agent>` | Always `ingested-by:influx` on Influx-authored notes |
| `schema:<N>` | Note schema version |
| `full-text` | Marks a Tier 2 note (bare tag, not a prefix) |
| `influx:rejected` | User feedback; authored by Lens, consumed by Influx |
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
│   ├── lithos_client.py     # mcp SDK + Streamable HTTP
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
| `mcp` | Official MCP Python SDK (Streamable HTTP client) |
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

**End of Requirements v0.5**
