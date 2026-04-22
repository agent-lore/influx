---
title: Influx — Requirements Document
version: 0.4.0
date: 2026-04-22
status: draft
tags: [influx, requirements, design, architecture]
---

# Influx — Requirements Document

> [!abstract] Project Summary
> **Influx** is a knowledge ingestion pipeline that monitors arXiv and web sources (blogs, Medium, RSS feeds) for new content matching one or more configurable interest profiles, filters it for relevance using an LLM, and feeds the results into a Lithos knowledge base as structured markdown notes. For each relevant item, Influx extracts clean text from the source (preferring arXiv HTML over PDF where available, using `trafilatura` for web articles), generates a structured summary with key contributions and relevance reasoning, downloads and archives the original source to a local file store, and writes a rich Lithos note linking back to both the canonical source URL and the local file. A feedback mechanism allows the user to mark items as irrelevant, improving future filtering over time via negative few-shot examples. Influx has no UI of its own — a companion project **Lithos Lens** provides a local web UI with feed view and interactive graph visualisation of the knowledge base.

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
- Use LCMA retrieval and edge tools to surface connections at ingest time
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
│  │    LITHOS    │◀────│    INFLUX    │     │  LITHOS-LENS    │   │
│  │              │     │  (ingestion) │     │   (web UI)      │   │
│  │  knowledge   │     │              │     │                 │   │
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

> [!important] Influx has no UI
> Influx is a headless scheduled pipeline. All human-facing browsing, graph rendering, and feedback UI live in Lithos Lens. Influx exposes only a health endpoint and receives feedback indirectly via notes tagged `influx:rejected` in Lithos.

### Repository Structure

Two separate repositories:

| Repo | Purpose |
|------|---------|
| `influx` | Ingestion pipeline — arXiv/RSS monitoring, LLM filtering, Lithos ingestion |
| `lithos-lens` | Web UI — documented separately (see `LITHOS-LENS-REQUIREMENTS-0.4.md`) |

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deployment | Separate Docker container | Independent restartability; clean separation of concerns |
| Scheduling | APScheduler (Python, in Influx container) | Configurable, handles missed runs, stays in-process |
| LLM access | LiteLLM | Provider-agnostic; supports OpenRouter, local models, Ollama |
| Deduplication | Lithos `lithos_cache_lookup` by `source_url` | Lithos is the source of truth; no separate state DB |
| Archive storage | Local filesystem shared volume | Simple, human-accessible, easy to back up |
| Lithos communication | MCP API (SSE transport) | Clean decoupling; Lithos is the authority |
| Notification | Webhook to Agent Zero | Real-time; no polling needed |
| Text extraction | arXiv HTML → PDF fallback → abstract-only | Quality-first with graceful degradation |
| Feedback storage | Lithos notes tagged `influx:rejected` | Lithos is source of truth; LCMA can reason over rejections |
| Config format | TOML (Python 3.12 built-in `tomllib`) | Consistent with Cardinal and other recent projects |
| Interest profiles | Multiple named profiles | Keeps unrelated domains (AI/robotics vs HEMA) cleanly separated |
| OTEL | Opt-in, additive, optional packages | Consistent with Lithos conventions |
| Environments | `.env.dev` / `.env.prod` per service | Consistent with Lithos conventions |

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
- API keys and secrets are **not** in the env files — they are injected separately (e.g. via `.a0proj/secrets.env` or a secrets manager)

**`.env.dev`:**
```env
INFLUX_ENVIRONMENT=dev
INFLUX_ARCHIVE_PATH=./archive
INFLUX_HOST_PORT=8080
INFLUX_CONTAINER_NAME=influx
INFLUX_OTEL_ENABLED=false
OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318
```

**`.env.prod`:**
```env
INFLUX_ENVIRONMENT=production
INFLUX_ARCHIVE_PATH=/home/user/projects/influx/archive
INFLUX_HOST_PORT=8080
INFLUX_CONTAINER_NAME=influx
INFLUX_OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

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
      - LITHOS_URL=${LITHOS_URL:-http://host.docker.internal:8765}
      - INFLUX_AGENT_ID=${INFLUX_AGENT_ID:-influx}
      - AGENT_ZERO_WEBHOOK_URL=${AGENT_ZERO_WEBHOOK_URL:-}
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
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

Configuration uses **TOML** format (Python 3.12 built-in `tomllib` for reading; `tomli-w` for writing if needed). The config file lives at `/etc/influx/config.toml` and is shared read-only with Lithos Lens (for settings views).

```toml
# Influx Configuration
# /etc/influx/config.toml

[schedule]
cron = "0 6 * * *"      # daily at 06:00
timezone = "UTC"

[storage]
archive_dir = "/archive"

[notifications]
webhook_url = ""        # set via env var AGENT_ZERO_WEBHOOK_URL

# ---------------------------------------------------------------------------
# Interest Profiles
# Multiple profiles are supported. Each profile has its own interest
# description, source list, thresholds, and Lithos path prefix.
# Papers are tagged with the profile name and stored under separate paths,
# keeping unrelated domains cleanly separated in the knowledge base.
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
relevance = 7           # minimum score to ingest
full_text = 8           # minimum score to fetch and store full text
deep_extract = 9        # minimum score for deep structured extraction
notify_immediate = 8    # minimum score for immediate notification

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
# All model references use LiteLLM format: "provider/model-name"
# ---------------------------------------------------------------------------

[models]
filter   = "openai/gpt-4.1-mini"          # cheap scoring of title+abstract
enrich   = "openai/gpt-4.1-mini"          # tier-1 summarisation
extract  = "anthropic/claude-sonnet-4.6"  # tier-3 deep extraction

[models.litellm]
request_timeout = 30
max_retries = 2

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

### Config Loading

```python
import tomllib
from pathlib import Path

def load_config(path: Path = Path("/etc/influx/config.toml")) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    # Env vars override config file values
    # e.g. INFLUX_OTEL_ENABLED overrides telemetry.enabled
    return Config.model_validate(data)
```

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

**Date filtering:** Filter by `<published>` date in Python after fetching.

**Rate limiting:** 3-second delay between API calls required.

**arXiv HTML availability:** Most papers from 2020 onwards. Check with HEAD request before fetch.

### 5.2 RSS Feeds

- Parse with `feedparser`
- Extract: title, URL, published date, summary
- Fetch full article text with `trafilatura`
- Deduplication by article URL via `lithos_cache_lookup`

---

## 6. Relevance Filtering

### 6.1 Filter Prompt

The system prompt below is used with the configured `models.filter` model via LiteLLM. The `INTEREST PROFILE` block is populated from the active profile's `description` field in `config.toml`. The `NEGATIVE EXAMPLES` block is populated at runtime from recent rejections for that profile stored in Lithos.

```
You are a research paper relevance filter. Score each paper for relevance
to the following interest profile.

## INTEREST PROFILE
{profile.description}

## NEGATIVE EXAMPLES
The following were previously marked as NOT interesting by the user.
Use them to calibrate your scoring:

{injected_negative_examples}

## OUTPUT FORMAT
Return ONLY a valid JSON array. For each paper with score >= 6 include:
- "id": the arXiv ID or article URL string
- "score": integer 1-10
- "tags": list of 2-5 short keyword tags
- "reason": one sentence explaining the score

If no papers meet the threshold, return []. Return ONLY the JSON array,
no other text.
```

### 6.2 Batching

- Papers sent to filter model in batches of 25
- Each batch contains: `ID`, `Title`, `Abstract`
- Results parsed from JSON array response
- Deduplication of results by ID
- Papers scoring below `profile.thresholds.relevance` are discarded

### 6.3 Threshold Behaviour

| Score | Action |
|-------|--------|
| < relevance threshold (default 7) | Discard |
| ≥ 7 | Ingest Tier 1 summary note |
| ≥ 8 | Ingest Tier 1 + Tier 2 full text note |
| ≥ 9 | Ingest Tier 1 + Tier 2 + Tier 3 deep extraction |
| ≥ notify_immediate (default 8) | Include in immediate notification |

### 6.4 Multi-Profile Runs

Each run processes all enabled profiles sequentially. A paper may match multiple profiles — it is ingested once per matching profile with separate notes under each profile's path, tagged with the profile name.

---

## 7. Content Enrichment

### 7.1 Text Extraction Strategy

```
For arXiv papers:
  1. Attempt fetch of https://arxiv.org/html/{id}
     → If available: extract with trafilatura → markdown
  2. Fallback: download PDF → extract with pymupdf4llm → markdown
  3. Fallback: use abstract only, flag note as "abstract-only"

For RSS/web articles:
  1. Fetch article URL
  2. Extract with trafilatura → markdown
  3. Fallback: use feed summary only
```

### 7.2 LLM Enrichment (Tier 1 — all papers ≥ threshold)

Uses `models.enrich`. Single LLM call from title + abstract:

```
Given this paper's title and abstract, extract:
1. Key contributions (3-5 bullet points, each ≤ 20 words)
2. Primary method or approach (1-2 sentences)
3. Main result or finding (1-2 sentences)
4. Relevance to: {profile.description summary}

Return as JSON: {"contributions": [...], "method": "...",
                 "result": "...", "relevance": "..."}
```

### 7.3 LLM Enrichment (Tier 3 — papers scoring ≥ deep_extract threshold)

Uses `models.extract`. Additional structured extraction from full text:

```
From this paper extract:
1. Explicit claims made (list)
2. Datasets or benchmarks used (list)
3. Named prior works this builds on (list)
4. Open questions or future work raised (list)
5. Potential connections to: {profile.description}

Return as JSON.
```

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

**On download failure:** Log error, set `local_file: null` in lithos note, retry next run.

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

#### Tier 1 — Summary Note (all ingested items)

```markdown
---
title: "{Paper Title}"
authors: ["Author A", "Author B"]
published: YYYY-MM-DD
source_url: https://arxiv.org/abs/2603.12939
local_file: /archive/arxiv/ai-robotics/2026/03/2603.12939.pdf
arxiv_id: 2603.12939
categories: [cs.RO, cs.AI]
relevance_score: 9
tags: [robot-memory, spatio-temporal-reasoning, embodied-ai, profile:ai-robotics]
source_type: arxiv
ingested_by: influx
ingested_at: 2026-03-16T06:12:34Z
text_quality: html
---

# {Paper Title}

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
- Local: `/archive/arxiv/ai-robotics/2026/03/2603.12939.pdf`
```

#### Tier 2 — Full Text Note (score ≥ full_text threshold)

```markdown
---
title: "{Paper Title} — Full Text"
source_url: https://arxiv.org/abs/2603.12939
derived_from_ids: ["{summary-note-uuid}"]
tags: [full-text, profile:ai-robotics]
ingested_by: influx
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

### 9.1 Deduplication

Before processing any item, use `lithos_cache_lookup` with both `query` (required) and `source_url` (fast path):

```python
# query is REQUIRED — use a short descriptor; source_url is the fast-path dedup key
result = lithos_cache_lookup(
    query=f"{title} {abstract_first_sentence}",
    source_url=url,
    max_age_hours=None,
)
# result["hit"] is True   → skip (already have fresh copy)
# result["stale_exists"]  → update existing note (result["stale_id"])
# otherwise               → proceed with fresh ingest
```

### 9.2 Writing Notes

`lithos_write` requires `title`, `content`, and `agent` on every call (even updates — omitted optional fields preserve existing values, but the required trio is never optional at the MCP boundary). For Influx:

- `agent`: `"influx"`
- `path`: `"papers/{profile}/{YYYY}/{MM}"`
- `source_url`: canonical URL (acts as dedup key after normalisation)
- `tags`: filter tags + `profile:{name}` tag
- `confidence`: `relevance_score / 10.0`
- `note_type`: `"summary"` for Tier 1 notes, `"observation"` for full-text notes
- `derived_from_ids`: for Tier 2 notes, the UUID of the Tier 1 summary note

### 9.3 Agent Registration

On startup:
```python
lithos_agent_register(id="influx", name="Influx Pipeline", type="ingestion-pipeline")
```

Registration is optional (agents auto-register on first use), but doing so explicitly sets a human-readable name and type.

---

## 10. LCMA Integration

Lithos LCMA MVP1 and MVP2 tools are available and should be used by Influx to seed the knowledge graph at ingest time.

### 10.1 Post-Ingestion Retrieval

After writing each summary note, use `lithos_retrieve` (LCMA MVP1) instead of basic semantic search. This runs seven parallel scouts with reranking and produces an audit receipt:

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

### 10.2 Explicit Edge Creation

For Tier 3 papers (score ≥ deep_extract), after extracting "Builds On" prior works, create typed edges:

```python
# If a named prior work is found in Lithos
for prior_id in resolved_prior_work_ids:
    lithos_edge_upsert(
        from_id=new_note_id,
        to_id=prior_id,
        type="builds_on",
        weight=0.8,
        namespace="influx",
        provenance_actor="influx",
        provenance_type="llm_extraction",
    )
```

For high-scoring papers with strong semantic similarity to existing notes:
```python
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

### 10.3 Run Task Coordination

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

> [!note] `lithos_task_complete` extended parameters
> The implementation accepts `outcome`, `cited_nodes`, `misleading_nodes`, and `receipt_id` even though the public SPECIFICATION.md only lists `task_id` and `agent`. These extended parameters are the canonical surface — the spec text lags behind the code.

---

## 11. Notifications

### 11.1 Immediate Notification

POSTs to Agent Zero webhook after each profile run:

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

---

## 12. Feedback Mechanism

### 12.1 Overview

Feedback is authored in Lithos Lens. From Influx's perspective, feedback is data it reads from Lithos at the start of each run.

### 12.2 How Feedback Arrives

Lithos Lens updates the rejected note's tags via `lithos_write` (see Lens requirements §8). The resulting note carries the tag `influx:rejected`. Influx does not write feedback itself.

### 12.3 Injecting Negative Examples

At the start of each filter run, per profile — use `lithos_list` (tag-only filtering), **not** `lithos_search` (which requires a free-text `query` argument):

```python
rejected = lithos_list(
    tags=["influx:rejected", f"profile:{profile_name}"],
    limit=config.feedback.negative_examples_per_profile,
)
```

Each returned item is read via `lithos_read(id=...)` to get title+abstract, then formatted and injected into the `NEGATIVE EXAMPLES` block of the filter prompt.

---

## 13. Resilience & Error Handling

| Failure | Behaviour |
|---------|----------|
| arXiv API unreachable | Retry 3× with exponential backoff; skip run if all fail |
| HTML fetch fails | Fall back to PDF extraction |
| PDF download fails | Store abstract-only note; set `local_file: null`; retry next run |
| LLM call fails | Retry 2×; store note without enrichment fields |
| Lithos unreachable | Retry 3×; abort run; log error |
| Duplicate detected | Skip silently |
| Malformed LLM JSON | Log warning; attempt regex extraction; fall back to no-tags |
| arXiv rate limit (429) | Back off 10 seconds; retry |
| LCMA edge upsert fails | Log warning; continue — edges are enrichment, not critical path |
| `lithos_write` returns `status=version_conflict` | Re-read note, merge changes, retry once |
| `lithos_write` returns `status=slug_collision` | Log error; append short hash to slug; retry |

### Retry Policy

- Max retries: 3
- Backoff: exponential (1s, 2s, 4s)
- Per-item failures do not abort the run
- Run-level failures (Lithos down) abort the run and log

---

## 14. Observability

### OTEL — Opt-In, Additive

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

### Logging

Follows the Lithos pattern — **stdout only, no log files**:

- All log output goes to stdout → captured by `docker logs influx`
- `INFLUX_LOG_LEVEL` controls verbosity (`DEBUG` in dev, `INFO` in prod)
- Structured JSON format via `python-json-logger`
- **Durable run history** is stored as Lithos notes at `path: "influx/runs"` — queryable, persistent, human-readable
- OTEL (when enabled) provides structured spans and metrics for deeper observability

### Health Endpoint

- `GET http://localhost:8080/health` → `{"status": "ok", "last_run": "...", "next_run": "..."}`

---

## 15. Backfill Mode

```bash
python -m influx backfill --profile ai-robotics --days 30
python -m influx backfill --profile ai-robotics --from 2026-01-01 --to 2026-03-15
python -m influx backfill --all-profiles --days 7
```

- Fetches papers day by day for the specified range
- Respects arXiv rate limits (3s between requests)
- Skips already-ingested papers via `lithos_cache_lookup`
- Does not send notifications during backfill
- Logs progress to stdout

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

**Rate limit:** 1 request per 3 seconds. No authentication required.

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

### 16.3 Lithos MCP API — Influx Usage

| Tool | Required args | Purpose |
|------|---------------|---------|
| `lithos_cache_lookup(query, source_url?, max_age_hours?, tags?)` | `query` | Deduplication before processing. `query` is REQUIRED even when a `source_url` fast-path hit is expected. |
| `lithos_write(title, content, agent, ...)` | `title`, `content`, `agent` | Write summary, full-text notes. Always pass all three required fields — even on updates. |
| `lithos_read(id)` | `id` | Load rejected-note title/content when building negative examples |
| `lithos_retrieve(query, limit, agent_id, task_id, tags)` | `query` | LCMA post-ingestion connection query |
| `lithos_list(path_prefix?, tags?, since?, limit?)` | none | Load negative examples for filter prompt (tag-only; not `lithos_search`) |
| `lithos_edge_upsert(from_id, to_id, type, weight, namespace, ...)` | as named | Create typed edges between notes |
| `lithos_task_create(title, agent, tags?)` | `title`, `agent` | Create run coordination task |
| `lithos_task_complete(task_id, agent, outcome?, cited_nodes?)` | `task_id`, `agent` | Complete run task with outcome summary |
| `lithos_agent_register(id, name?, type?)` | `id` | Register on startup (optional — auto-registers otherwise) |

---

## 17. Implementation Plan

### Milestone 1 — arXiv Pipeline (v0.1)
*Goal: daily arXiv monitoring → Lithos ingestion → notification*

- [ ] Project scaffold: `pyproject.toml`, `Dockerfile`, `config.toml`
- [ ] TOML config loader with env var overrides
- [ ] arXiv fetcher module (`influx/sources/arxiv.py`)
- [ ] LiteLLM filter module (`influx/filter.py`) with batching
- [ ] Lithos client wrapper (`influx/lithos_client.py`)
- [ ] Deduplication via `lithos_cache_lookup` (passing both `query` and `source_url`)
- [ ] Tier 1 note writer with profile-based paths
- [ ] Archive downloader (`influx/storage.py`)
- [ ] APScheduler setup (`influx/scheduler.py`)
- [ ] Webhook notification to Agent Zero
- [ ] Health endpoint (`GET /health`)
- [ ] Structured JSON logging to stdout (`python-json-logger`)
- [ ] `docker-compose.yml` with `.env.dev` / `.env.prod`

### Milestone 2 — Full Text, Enrichment & LCMA Edges (v0.2)
*Goal: richer notes + LCMA graph seeding*

- [ ] arXiv HTML fetcher with trafilatura extraction
- [ ] PDF text extraction with pymupdf4llm (fallback)
- [ ] Tier 2 full text note writer (linked via `derived_from_ids`)
- [ ] Tier 1 LLM enrichment (contributions, method, results)
- [ ] Tier 3 deep extraction for score ≥ deep_extract threshold
- [ ] `lithos_retrieve` post-ingestion connection query
- [ ] `lithos_edge_upsert` for `builds_on` and `related_to` edges
- [ ] `lithos_task_create` / `lithos_task_complete` per run
- [ ] "Related in your knowledge base" in notifications

### Milestone 3 — Multiple Profiles & RSS (v0.3)
*Goal: multi-profile support + blog/RSS monitoring*

- [ ] Multi-profile pipeline orchestration
- [ ] Profile-scoped paths, tags, and negative examples
- [ ] RSS feed fetcher with `feedparser`
- [ ] Web article extraction with `trafilatura`
- [ ] Config-driven feed list per profile
- [ ] Backfill CLI (`influx backfill --profile ... --days N`)

### Milestone 4 — Observability (v0.4)
*Goal: production-ready telemetry*

- [ ] `influx/telemetry.py` — mirrors Lithos OTEL pattern
- [ ] `@traced` decorator on key pipeline stages
- [ ] OTEL metrics: items fetched, filtered, ingested, errors (per profile)
- [ ] Run history notes in Lithos
- [ ] Tag rejection rate reporting

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
│   ├── config.py            # TOML loader + Pydantic models
│   ├── scheduler.py
│   ├── pipeline.py
│   ├── filter.py
│   ├── enrichment.py
│   ├── lithos_client.py
│   ├── notifier.py
│   ├── storage.py
│   ├── telemetry.py         # mirrors lithos/telemetry.py
│   ├── sources/
│   │   ├── arxiv.py
│   │   └── rss.py
│   └── extraction/
│       ├── html.py
│       └── pdf.py
└── tests/
```

---

## Appendix B — Key Dependencies

| Package | Purpose |
|---------|---------|
| `litellm` | LLM provider abstraction |
| `apscheduler` | In-process scheduling |
| `feedparser` | RSS feed parsing |
| `trafilatura` | Web article text extraction |
| `pymupdf4llm` | PDF → markdown extraction |
| `httpx` | Async HTTP client |
| `pydantic` | Data validation and settings |
| `fastapi` + `uvicorn` | Health endpoint |
| `python-json-logger` | Structured JSON logging |
| `tomli-w` | TOML writing (if config needs updating at runtime) |
| `opentelemetry-*` | OTEL (optional extra: `uv sync --extra otel`) |
