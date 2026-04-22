---
title: Influx — Requirements Document
version: 0.2.0
date: 2026-03-16
status: draft
tags: [influx, requirements, design, architecture]
---

# Influx — Requirements Document

> [!abstract] Project Summary
> **Influx** is a knowledge ingestion pipeline that monitors arXiv and web sources (blogs, Medium, RSS feeds) for new content matching a defined set of research interests, filters it for relevance using an LLM, and feeds the results into a Lithos knowledge base as structured markdown notes. For each relevant item, Influx extracts clean text from the source (preferring arXiv HTML over PDF where available, using `trafilatura` for web articles), generates a structured summary with key contributions and relevance reasoning, downloads and archives the original PDF to a local file store (`/usr/papers/{source}/{date}/`), and writes a rich lithos note that links back to both the canonical source URL and the local PDF. A companion web UI (**Influx UI**) provides a feed view and interactive graph visualisation of the knowledge base, and a feedback mechanism allows the user to mark papers as irrelevant, improving future filtering over time.

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
- [[#10. Notifications]]
- [[#11. Feedback Mechanism]]
- [[#12. Web UI]]
- [[#13. Resilience & Error Handling]]
- [[#14. Observability]]
- [[#15. Backfill Mode]]
- [[#16. API Reference]]
- [[#17. Implementation Plan]]

---

## 1. Goals & Non-Goals

### Goals

- Monitor arXiv daily for new papers matching a configurable interest profile
- Monitor RSS feeds (blogs, Medium, etc.) for relevant articles
- Filter content for relevance using a cheap/fast LLM
- Improve filtering over time via user feedback (negative few-shot examples)
- Extract clean text from sources (HTML preferred, PDF fallback)
- Archive original PDFs to local filesystem
- Ingest structured notes into Lithos knowledge base
- Notify the user of new relevant content immediately
- Surface connections between new content and existing Lithos knowledge
- Provide a local web UI with feed view and graph visualisation
- Support backfill of historical content
- Run independently of Agent Zero (separate containers, restartable independently)

### Non-Goals

- Full-text search UI (that is Lithos's job)
- Relationship discovery and concept formation (that is LCMA's job)
- Email delivery (v1 scope; may be added later)
- Social media monitoring (out of scope for v1)
- Citation graph construction (out of scope for v1)
- The UI is not Influx-specific — it is a general Lithos knowledge browser

---

## 2. Architecture Overview

### Three-Container Design

```
┌─────────────────────────────────────────────────────────────────┐
│                        DOCKER NETWORK                            │
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌────────────────┐   │
│  │    LITHOS    │◀────│    INFLUX    │     │   INFLUX UI    │   │
│  │              │     │  (ingestion) │     │   (web app)    │   │
│  │  knowledge   │     │              │     │                │   │
│  │  store +     │     │  scheduled   │     │  stateless     │   │
│  │  MCP API     │     │  batch job   │     │  HTTP server   │   │
│  └──────────────┘     └──────────────┘     └───────┬────────┘   │
│          ▲                                          │            │
│          └──────────────────────────────────────────┘           │
│                      Lithos HTTP API only                        │
└─────────────────────────────────────────────────────────────────┘
         │                                    │
         │ HTTP API                           │ :7842 exposed
         ▼                                    ▼
  ┌─────────────┐                     ┌──────────────┐
  │ AGENT ZERO  │                     │   BROWSER    │
  │  (webhook   │                     │  (human UI)  │
  │  notifs)    │                     └──────────────┘
  └─────────────┘
```

> [!important] UI Independence
> **Influx UI has zero runtime dependency on the Influx ingestion container.** It is a pure Lithos client. Run history, paper notes, feedback, and graph data all come from Lithos. The UI and ingestion pipeline can be restarted, updated, or fail independently.

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deployment | Three separate Docker containers | Independent restartability; clean separation of concerns |
| Scheduling | APScheduler (Python, in Influx container) | Configurable, handles missed runs, stays in-process |
| LLM access | LiteLLM | Provider-agnostic; supports OpenRouter, local models, Ollama |
| Deduplication | Lithos `cache_lookup` by `source_url` | Lithos is the source of truth; no separate state DB |
| PDF storage | Local filesystem (shared volume) | Simple, human-accessible, easy to back up |
| Lithos communication | HTTP MCP API | Clean decoupling; Lithos is the authority |
| Notification | Webhook to Agent Zero | Real-time; no polling needed |
| Text extraction | arXiv HTML → PDF fallback → abstract-only | Quality-first with graceful degradation |
| Feedback storage | Lithos notes tagged `influx-rejected` | Lithos is source of truth; LCMA can reason over rejections |
| UI graph rendering | Cytoscape.js | Best for knowledge graphs; handles typed edges; good to ~10K nodes |
| UI frontend | FastAPI + HTMX + Cytoscape.js | No build step; minimal stack; no React/webpack |

---

## 3. Infrastructure & Deployment

### Containers

| Container     | Base image | Purpose |
| ------------- | ------------------ | --------------------------------------------- |
| `lithos`      | lithos image | Knowledge store and MCP API |
| `influx`      | `python:3.12-slim` | Ingestion pipeline, scheduler, PDF downloader |
| `lithos-lens` | `python:3.12-slim` | Web UI, feed view, graph view, feedback API |

### Volumes

| Volume | Influx mount | Influx UI mount | Purpose |
|--------|-------------|----------------|--------|
| `papers` | `/usr/papers` (rw) | `/usr/papers` (ro) | PDF archive |
| `influx-config` | `/etc/influx` (rw) | `/etc/influx` (ro) | Configuration |
| `influx-logs` | `/var/log/influx` (rw) | — | Log files |
| `lithos-data` | — | — | Lithos internal (managed by Lithos) |

### Docker Compose

```yaml
services:
  lithos:
    image: lithos:latest
    volumes:
      - lithos-data:/data
    networks:
      - influx-net

  influx:
    build: ./influx
    volumes:
      - papers:/usr/papers
      - ./config:/etc/influx
      - influx-logs:/var/log/influx
    depends_on: [lithos]
    networks:
      - influx-net
    environment:
      - LITHOS_API_URL=http://lithos:8000
      - AGENT_ZERO_WEBHOOK_URL=http://agent-zero:8000/webhook/influx

  influx-ui:
    build: ./influx-ui
    ports:
      - "7842:8000"
    volumes:
      - papers:/usr/papers:ro
      - ./config:/etc/influx:ro
    depends_on: [lithos]
    networks:
      - influx-net
    environment:
      - LITHOS_API_URL=http://lithos:8000
      - PAPERS_DIR=/usr/papers

volumes:
  lithos-data:
  papers:
  influx-logs:

networks:
  influx-net:
```

### Environment Variables — Influx

```env
# LLM
LITELLM_MODEL_FILTER=openai/gpt-4.1-mini
LITELLM_MODEL_ENRICH=openai/gpt-4.1-mini
OPENROUTER_API_KEY=sk-or-v1-...

# Lithos
LITHOS_API_URL=http://lithos:8000
LITHOS_AGENT_ID=influx

# Agent Zero (notifications)
AGENT_ZERO_WEBHOOK_URL=http://agent-zero:8000/webhook/influx

# Behaviour
INFLUX_RUN_SCHEDULE=0 6 * * *
INFLUX_RELEVANCE_THRESHOLD=7
INFLUX_FULL_TEXT_THRESHOLD=8
INFLUX_DEEP_EXTRACT_THRESHOLD=9
INFLUX_NOTIFY_THRESHOLD=8
```

### Environment Variables — Influx UI

```env
LITHOS_API_URL=http://lithos:8000
PAPERS_DIR=/usr/papers
UI_PORT=8000
```

---

## 4. Configuration

All user-facing configuration lives in `/etc/influx/config.yaml` (shared read-only with UI container):

```yaml
# Influx Configuration

interests:
  profile: |
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

sources:
  arxiv:
    enabled: true
    categories:
      - cs.AI
      - cs.RO
      - cs.MA
      - cs.NE
      - cs.CL
      - cs.LO
    max_results_per_category: 200
    lookback_days: 1

  rss:
    enabled: true
    feeds:
      - name: "Andrej Karpathy"
        url: "https://karpathy.github.io/feed.xml"
      - name: "Lilian Weng (OpenAI)"
        url: "https://lilianweng.github.io/index.xml"

thresholds:
  relevance: 7
  full_text: 8
  deep_extract: 9
  notify_immediate: 8

feedback:
  negative_examples_in_prompt: 20   # how many recent rejections to inject
  recalibrate_after_runs: 7         # recalculate tag weights every N runs

schedule:
  cron: "0 6 * * *"
  timezone: "UTC"

storage:
  papers_dir: "/usr/papers"
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

**Response format:** Atom/XML feed. Each entry contains:

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

The system prompt below is used with the configured filter model. The `INTEREST PROFILE` block is populated from `config.yaml`. The `NEGATIVE EXAMPLES` block is populated at runtime from recent rejections stored in Lithos (see [[#11. Feedback Mechanism]]).

```
You are a research paper relevance filter. Score each paper for relevance
to the following interest profile.

## INTEREST PROFILE

### HIGH INTEREST topics (score 7-10):
- Multi-agent systems: coordination, communication, emergent behaviour, swarms
- Agent memory, knowledge graphs, knowledge representation for AI agents
- Humanoid robotics, social robotics, NAO robots, human-robot interaction
- LLM reasoning and planning: chain-of-thought, world models, structured reasoning
- Emotional models for robots/AI, affective computing in robotics
- Intelligent AI/robot companions, social AI
- Robot environment understanding: spatial reasoning, scene understanding,
  navigation, embodied AI
- Neurosymbolic reasoning: combining neural networks with symbolic/logical reasoning
- Artificial life, emergent complexity, self-organising systems
- Fundamental AI breakthroughs with broad impact

### MEDIUM INTEREST topics (score 4-6):
- LLM agents with tool use, planning, or memory components
- Reinforcement learning for robotics or embodied agents
- Vision-language-action models for robotics
- Cognitive architectures for AI
- Papers that touch on the above topics as a secondary contribution

### LOW INTEREST / EXCLUDE (score 1-3):
- General machine learning methods without agent/robot/reasoning angle
- Computer vision papers without robotics or agent context
- NLP/text processing without reasoning or agent focus
- Federated learning, model compression, quantisation
- Medical imaging, clinical AI
- Autonomous driving (unless strong reasoning/planning angle)
- Benchmarks and datasets with no novel method
- Fine-tuning, RLHF, reward modelling for language tasks

### EXCEPTION: Score 8-10 regardless of topic if:
- The paper appears to introduce a genuinely novel paradigm or architecture
- It could be a landmark paper (like "Attention is All You Need" level impact)
- It challenges fundamental assumptions in AI/ML

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
- Deduplication of results by ID (model occasionally returns duplicates)
- Papers scoring below `thresholds.relevance` are discarded

### 6.3 Threshold Behaviour

| Score | Action |
|-------|--------|
| < 7 | Discard |
| ≥ 7 | Ingest Tier 1 summary note |
| ≥ 8 | Ingest Tier 1 + Tier 2 full text note |
| ≥ 9 | Ingest Tier 1 + Tier 2 + Tier 3 deep extraction |
| ≥ 8 (notify threshold) | Include in immediate notification |

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

```
Given this paper's title and abstract, extract:
1. Key contributions (3-5 bullet points, each ≤ 20 words)
2. Primary method or approach (1-2 sentences)
3. Main result or finding (1-2 sentences)
4. Relevance to: [interest profile summary]

Return as JSON: {"contributions": [...], "method": "...",
                 "result": "...", "relevance": "..."}
```

### 7.3 LLM Enrichment (Tier 3 — papers scoring ≥ 9)

```
From this paper extract:
1. Explicit claims made (list)
2. Datasets or benchmarks used (list)
3. Named prior works this builds on (list)
4. Open questions or future work raised (list)
5. Potential connections to: [interest profile]

Return as JSON.
```

---

## 8. Storage

### 8.1 PDF Archive

**Location:** `/usr/papers/{source}/{YYYY}/{MM}/{id}.pdf`

**Examples:**
```
/usr/papers/arxiv/2026/03/2603.12939.pdf
/usr/papers/blog/2026/03/karpathy-2026-03-15.pdf
```

**Naming convention:**
- arXiv: use arXiv ID (e.g. `2603.12939`)
- Blog/web: `{feed-name}-{YYYY-MM-DD}` slugified

**On download failure:** Log error, set `local_pdf: null` in lithos note, retry next run.

### 8.2 Lithos Note Structure

#### Tier 1 — Summary Note (all ingested papers)

```markdown
---
title: "{Paper Title}"
authors: ["Author A", "Author B"]
published: YYYY-MM-DD
source_url: https://arxiv.org/abs/2603.12939
local_pdf: /usr/papers/arxiv/2026/03/2603.12939.pdf
arxiv_id: 2603.12939
categories: [cs.RO, cs.AI]
relevance_score: 9
tags: [robot-memory, spatio-temporal-reasoning, embodied-ai]
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
{why this was flagged}

## Links
- [arXiv page](https://arxiv.org/abs/2603.12939)
- [PDF](https://arxiv.org/pdf/2603.12939)
- Local PDF: `/usr/papers/arxiv/2026/03/2603.12939.pdf`
```

#### Tier 2 — Full Text Note (papers scoring ≥ 8)

```markdown
---
title: "{Paper Title} — Full Text"
source_url: https://arxiv.org/abs/2603.12939
derived_from_ids: ["{summary-note-uuid}"]
tags: [full-text]
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

#### Tier 3 — Deep Extraction additions to summary note (score ≥ 9)

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

Before processing any item:
```
lithos_cache_lookup(source_url="{url}", max_age_hours=null)
```
- **Hit:** skip item entirely
- **Stale hit:** update existing note
- **Miss:** proceed with full pipeline

### 9.2 Writing Notes

Use `lithos_write` with:
- `agent`: `"influx"`
- `path`: `"papers/arxiv"` or `"papers/blog"`
- `source_url`: canonical URL
- `tags`: from filter output
- `confidence`: relevance score / 10.0

### 9.3 Post-Ingestion Connection Query

After writing each summary note:
```
lithos_semantic(query="{title} {contributions}", limit=5, threshold=0.75)
```
Top matches included in notification digest as "Related in your knowledge base".

### 9.4 Agent Registration

On startup:
```
lithos_agent_register(id="influx", name="Influx Pipeline", type="ingestion-pipeline")
```

---

## 10. Notifications

### 10.1 Immediate Notification

POSTs to Agent Zero webhook after each run:

```json
{
  "type": "influx_digest",
  "run_date": "2026-03-16",
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

### 10.2 Quiet Run Notification

```json
{
  "type": "influx_digest",
  "run_date": "2026-03-16",
  "stats": {"sources_checked": 0, "ingested": 0},
  "message": "No new relevant content found today."
}
```

---

## 11. Feedback Mechanism

### 11.1 Overview

Users can mark any ingested paper as "not relevant" via the web UI or a notification link. Feedback is stored in Lithos and used to improve future filtering via negative few-shot examples injected into the filter prompt.

### 11.2 Storing Feedback in Lithos

When a paper is rejected, the existing summary note is updated:
```
lithos_write(
  id="{existing-note-uuid}",
  tags=[...existing_tags, "influx-rejected"],
  confidence=0.0
)
```

Lithos is the feedback store — no separate database needed. This means:
- LCMA can eventually reason about what the user finds *uninteresting*
- Feedback is auditable and queryable
- Rejections persist across Influx restarts

### 11.3 Injecting Negative Examples into Filter Prompt

At the start of each filter run, Influx loads recent rejections:
```python
rejected = lithos_search(
    tags=["influx-rejected"],
    limit=config.feedback.negative_examples_in_prompt  # default 20
)
```

These are formatted and injected into the `NEGATIVE EXAMPLES` block of the filter prompt:
```
- "Federated Hierarchical Clustering for Distributed Systems" → score 2
  Reason: federated learning, no agent or robotics context
- "RLHF for Code Generation" → score 1
  Reason: fine-tuning/alignment, not reasoning or planning
```

### 11.4 Tag-Level Calibration

Every N runs (configurable, default 7), Influx analyses rejection rates per tag:
- Tags with >80% rejection rate → note in logs, optionally lower effective score for those tags
- Tags with >90% acceptance rate → note in logs for user awareness

This is informational in v1; automatic threshold adjustment is a future enhancement.

### 11.5 Feedback Entry Points

| Entry point | Mechanism |
|-------------|----------|
| Web UI feed view | 👍 / 👎 buttons on each paper card |
| Web UI graph view | 👎 button in node detail panel |
| Notification link | `[not relevant]` link → `POST /api/feedback` on Influx UI |

---

## 12. Web UI

> [!note] Separation of Concerns
> Influx UI is a **pure Lithos client**. It has no runtime dependency on the Influx ingestion container. All data (papers, run history, feedback, graph edges) comes from Lithos. The UI can run, be updated, and be restarted entirely independently of ingestion.

### 12.1 Technology Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Backend | FastAPI | Lightweight, async, already needed for health endpoint |
| Graph rendering | Cytoscape.js | Best for knowledge graphs; typed edges; scales to ~10K nodes |
| Frontend | HTMX + vanilla JS | No build step; no React/webpack; Cytoscape handles complexity |
| Styling | Tailwind CSS (CDN) | Clean, no build step |

### 12.2 Views

#### Feed View (`/`)

A clean reading list of ingested papers, newest first:

```
┌─────────────────────────────────────────────────────┐
│  📥 Influx  [Feed] [Graph] [Settings]   2026-03-16  │
├─────────────────────────────────────────────────────┤
│  Filter: [All dates ▾] [All tags ▾] [Score ≥ 7 ▾]  │
├─────────────────────────────────────────────────────┤
│  ⭐10  RoboStream: Spatio-Temporal Reasoning...      │
│        cs.RO · robot-memory · embodied-ai            │
│        [Abstract ▾]  [Open PDF]  [👍 Keep] [👎 Skip]│
├─────────────────────────────────────────────────────┤
│  ⭐9   Multi-Agent LLM Routing via Ant Colony...     │
│        cs.MA · multi-agent · routing                 │
│        [Abstract ▾]  [Open PDF]  [👍 Keep] [👎 Skip]│
└─────────────────────────────────────────────────────┘
```

- Expandable abstract inline (HTMX)
- Filterable by date, tag, score, source type
- PDF link opens local file directly
- 👎 immediately updates Lithos and removes card from feed

#### Graph View (`/graph`)

Force-directed graph of all ingested papers and their relationships:

- **Nodes:** papers/articles
  - Size: proportional to relevance score
  - Colour: by primary tag cluster
  - Label: short title
- **Edges:**
  - 🔵 `semantic_similar` — same topic area (available immediately)
  - 🟢 `builds_on` — one extends the other (LCMA)
  - 🔴 `contradicts` — conflicting findings (LCMA)
  - 🟡 `uses_method` — shared methodology (LCMA)
  - 🟣 `analogous_to` — structural similarity across domains (LCMA)
- **Click node** → side panel with abstract, contributions, PDF link, feedback buttons
- **Hover edge** → tooltip showing relationship type and strength
- **Filters** → date range, tag, score threshold, edge type, source type
- **Rejected papers** shown as faded nodes (not removed — useful for LCMA)

#### Settings View (`/settings`)

- Display current interest profile (read-only in v1; editable in v2)
- Show configured RSS feeds
- Show threshold values
- Show last run stats and next scheduled run
- Link to run history

### 12.3 API Endpoints

```
GET  /                      → feed view
GET  /graph                 → graph view
GET  /settings              → settings view

GET  /api/papers            → JSON list of papers from Lithos
                              ?date=2026-03-16&tags=robot-memory&score_min=7
GET  /api/paper/{id}        → full paper detail from Lithos
GET  /api/graph             → JSON nodes + edges for Cytoscape
                              ?tags=...&score_min=7&edge_types=semantic,builds_on
POST /api/feedback          → mark paper accepted or rejected
                              body: {"id": "...", "action": "accept"|"reject"}
GET  /api/runs              → run history from Lithos
GET  /health                → {"status": "ok", "lithos": "ok"}
```

### 12.4 Graph Data Source

In v1 (before LCMA), graph edges come from `lithos_semantic` similarity queries:
```python
# For each node, find its top-5 semantic neighbours
for paper in papers:
    neighbours = lithos_semantic(query=paper.title, limit=5, threshold=0.75)
    edges.extend([(paper.id, n.id, "semantic_similar", n.similarity)
                  for n in neighbours])
```

In v2 (with LCMA), edges come from LCMA's typed relationship graph directly.

---

## 13. Resilience & Error Handling

| Failure | Behaviour |
|---------|----------|
| arXiv API unreachable | Retry 3× with exponential backoff; skip run if all fail |
| HTML fetch fails | Fall back to PDF extraction |
| PDF download fails | Store abstract-only note; set `local_pdf: null`; retry next run |
| LLM call fails | Retry 2×; store note without enrichment fields |
| Lithos unreachable | Retry 3×; abort run; log error |
| Duplicate detected | Skip silently |
| Malformed LLM JSON | Log warning; attempt regex extraction; fall back to no-tags |
| arXiv rate limit (429) | Back off 10 seconds; retry |
| Feedback write fails | Log error; show error in UI; do not silently drop |

### Retry Policy

- Max retries: 3
- Backoff: exponential (1s, 2s, 4s)
- Per-item failures do not abort the run
- Run-level failures (Lithos down) abort the run and log

---

## 14. Observability

### Logging

- Structured JSON logs to `/var/log/influx/influx.log`
- Each run produces a summary log entry and a Lithos note at `path: "influx/runs"`:
  ```json
  {"event": "run_complete", "date": "2026-03-16",
   "duration_s": 142, "fetched": 157, "filtered": 55,
   "ingested": 12, "errors": 0, "rejected_loaded": 18}
  ```

### Health Endpoints

- Influx UI: `GET /health` → `{"status": "ok", "lithos": "ok"}`
- Influx: internal health check only (no exposed port needed)

---

## 15. Backfill Mode

```bash
python -m influx backfill --days 30
python -m influx backfill --from 2026-01-01 --to 2026-03-15
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

**Namespaces for XML parsing:**
```python
ns = {
    'atom': 'http://www.w3.org/2005/Atom',
    'arxiv': 'http://arxiv.org/schemas/atom'
}
```

**Rate limit:** 1 request per 3 seconds. No authentication required.

**URL patterns:**
- HTML: `https://arxiv.org/html/{arxiv_id}`
- PDF: `https://arxiv.org/pdf/{arxiv_id}`
- Abstract: `https://arxiv.org/abs/{arxiv_id}`

### 16.2 LiteLLM / OpenRouter

**Base URL:** `https://openrouter.ai/api/v1/chat/completions`

**Authentication:** `Authorization: Bearer {OPENROUTER_API_KEY}`

**Recommended models:**

| Use case | Model | Notes |
|----------|-------|-------|
| Filtering (scoring) | `openai/gpt-4.1-mini` | Fast, cheap, sufficient |
| Enrichment (summarisation) | `openai/gpt-4.1-mini` | Same model fine |
| Deep extraction | `anthropic/claude-sonnet-4.6` | Better for nuanced extraction |
| Local/offline | `ollama/llama3.2` | Via LiteLLM Ollama provider |

### 16.3 Lithos MCP API

| Tool | Used by | Purpose |
|------|---------|---------|
| `lithos_cache_lookup(source_url=...)` | Influx | Deduplication before processing |
| `lithos_write(...)` | Influx | Write summary, full-text, feedback notes |
| `lithos_semantic(query, limit, threshold)` | Influx + UI | Find related notes |
| `lithos_search(tags=[...], limit=N)` | Influx | Load negative examples for filter prompt |
| `lithos_agent_register(id, name, type)` | Influx | Register on startup |
| `lithos_list(path_prefix, tags, since)` | UI | Feed view paper listing |
| `lithos_read(id)` | UI | Paper detail view |
| `lithos_links(id, direction)` | UI | Graph edge data (LCMA phase) |

---

## 17. Implementation Plan

### Milestone 1 — arXiv Pipeline (v0.1)
*Goal: daily arXiv monitoring → Lithos ingestion → notification*

- [ ] Project scaffold: `pyproject.toml`, `Dockerfile`, `config.yaml`
- [ ] arXiv fetcher module (`influx/sources/arxiv.py`)
- [ ] LiteLLM filter module (`influx/filter.py`) with batching
- [ ] Lithos client wrapper (`influx/lithos_client.py`)
- [ ] Deduplication via `lithos_cache_lookup`
- [ ] Tier 1 note writer (summary note)
- [ ] PDF downloader (`influx/storage.py`)
- [ ] APScheduler setup (`influx/scheduler.py`)
- [ ] Webhook notification to Agent Zero
- [ ] Basic logging
- [ ] Docker Compose with Lithos + Influx

### Milestone 2 — Full Text & Enrichment (v0.2)
*Goal: richer notes for high-scoring papers*

- [ ] arXiv HTML fetcher with trafilatura extraction
- [ ] PDF text extraction with pymupdf4llm (fallback)
- [ ] Tier 2 full text note writer (linked to summary note)
- [ ] Tier 1 LLM enrichment (contributions, method, results)
- [ ] Tier 3 deep extraction for score ≥ 9
- [ ] Post-ingestion semantic connection query
- [ ] "Related in your knowledge base" in notifications

### Milestone 3 — Web UI: Feed View + Feedback (v0.3)
*Goal: human-readable feed with feedback mechanism*

- [ ] `influx-ui` container scaffold (FastAPI + HTMX + Tailwind)
- [ ] Feed view: paper list from Lithos, filterable by date/tag/score
- [ ] Expandable abstract inline
- [ ] PDF link (opens local file)
- [ ] 👍 / 👎 feedback buttons → `POST /api/feedback` → Lithos update
- [ ] Negative examples loaded from Lithos and injected into filter prompt
- [ ] Settings view (read-only)
- [ ] Health endpoint
- [ ] Docker Compose updated with `influx-ui` container

### Milestone 4 — Web UI: Graph View (v0.4)
*Goal: visual knowledge graph with semantic edges*

- [ ] Graph view with Cytoscape.js
- [ ] Nodes: papers sized by score, coloured by tag cluster
- [ ] Edges: semantic similarity from `lithos_semantic`
- [ ] Click node → side panel with detail + feedback
- [ ] Filter panel (date, tag, score, edge type)
- [ ] Rejected papers shown as faded nodes

### Milestone 5 — RSS & Web Sources (v0.5)
*Goal: monitor blogs and Medium alongside arXiv*

- [ ] RSS feed fetcher with `feedparser`
- [ ] Web article extraction with `trafilatura`
- [ ] Unified pipeline for arXiv + RSS
- [ ] Config-driven feed list
- [ ] Blog PDF archiving

### Milestone 6 — Backfill & Observability (v0.6)
*Goal: seed Lithos with historical content; operational visibility*

- [ ] Backfill CLI (`influx backfill --days N`)
- [ ] Run history notes in Lithos
- [ ] Tag-level calibration reporting
- [ ] Retry logic and error recovery hardening

### Milestone 7 — LCMA Integration (v0.7)
*Goal: leverage LCMA typed edges in graph view*

- [ ] Graph view updated to use LCMA typed edges
- [ ] Edge colours by type (builds_on, contradicts, uses_method, analogous_to)
- [ ] Contradiction alerts in notifications
- [ ] Analogy-based connection discovery
- [ ] Concept node visualisation

---

## Appendix A — Proposed Directory Structure

```
influx/                          ← monorepo root
├── docker-compose.yml
├── config/
│   └── config.yaml
│
├── influx/                      ← ingestion pipeline container
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── influx/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── scheduler.py
│   │   ├── pipeline.py
│   │   ├── filter.py
│   │   ├── enrichment.py
│   │   ├── lithos_client.py
│   │   ├── notifier.py
│   │   ├── storage.py
│   │   ├── sources/
│   │   │   ├── arxiv.py
│   │   │   └── rss.py
│   │   └── extraction/
│   │       ├── html.py
│   │       └── pdf.py
│   └── tests/
│
└── influx-ui/                   ← web UI container
    ├── Dockerfile
    ├── pyproject.toml
    ├── app/
    │   ├── __init__.py
    │   ├── main.py
    │   ├── lithos_client.py
    │   ├── routers/
    │   │   ├── feed.py
    │   │   ├── graph.py
    │   │   ├── feedback.py
    │   │   └── settings.py
    │   └── templates/
    │       ├── base.html
    │       ├── feed.html
    │       ├── graph.html
    │       └── settings.html
    └── static/
        └── cytoscape.min.js
```

---

## Appendix B — Key Dependencies

### Influx (ingestion)

| Package | Purpose |
|---------|---------|
| `litellm` | LLM provider abstraction |
| `apscheduler` | In-process scheduling |
| `feedparser` | RSS feed parsing |
| `trafilatura` | Web article text extraction |
| `pymupdf4llm` | PDF → markdown extraction |
| `httpx` | Async HTTP client |
| `pyyaml` | Config file parsing |
| `pydantic` | Data validation and settings |

### Influx UI

| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework |
| `uvicorn` | ASGI server |
| `httpx` | Lithos API client |
| `jinja2` | HTML templating |
| `pydantic` | Request/response validation |
| Cytoscape.js (CDN) | Graph visualisation |
| HTMX (CDN) | Dynamic HTML without JS framework |
| Tailwind CSS (CDN) | Styling |

