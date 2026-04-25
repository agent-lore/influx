# PRD 07 — Content Extraction (HTML/PDF/article) + Tier 1 & Tier 3 Enrichment

**Part of:** Influx v1 (see `tasks/prd-influx-v1-index.md`)
**Covers master PRD stories:** S-8 + S-9
**Milestone:** M2
**Prerequisites:** PRD 01, 02, 03, 04, 05, 06
**Downstream PRDs that depend on this:** 08, 09, 10

---

## 1. Context

PRDs 04 and 05 ship arXiv → filter → archive → write with `text:abstract-only`
notes only and stub Tier 1 enrichment. PRD 06 ships the repair sweep with
hooks that call `re_extract_archive` / `tier2_enrich` / `tier3_extract`.

This PRD fills in those hooks for real:

- HTML extraction (`arxiv.org/html/{id}` and generic web articles).
- PDF extraction (arXiv PDF fallback).
- Tier selection (`text:html` / `text:pdf` / `text:abstract-only`) on
  initial write — replacing the always-`text:abstract-only` stub
  behaviour from PRD 04.
- Tier 1 enrichment (replaces PRD 04's stub) — produces the
  `## Summary` section.
- Tier 3 deep extraction — produces `## Claims`,
  `## Datasets & Benchmarks`, `## Builds On`, `## Open Questions`.
- Tier 2 (`## Full Text`) section is the extracted full text itself,
  not a separate LLM call.

After this PRD, notes that score above the relevant thresholds are
written WITH their tier sections on first pass — eliminating the
PRD 04 seam that wrote the threshold tag without the section.

## 2. In scope

- `extraction/html.py` — generic article extraction (e.g. via
  `trafilatura` or `readability-lxml`) using PRD 02's guarded HTTP
  client.
- `extraction/pdf.py` — PDF → text extraction (e.g. via `pypdf` /
  `pdfminer.six`).
- arXiv-specific extraction order (FR-ENR-1): HTML
  (`arxiv.org/html/{id}`) → PDF → abstract-only.
- Generic web extraction order (FR-ENR-3): article extraction → feed
  summary fallback. (RSS fetcher is PRD 09; this PRD ensures the
  extractor is callable and tested, even if the only caller in this
  PRD is the arXiv flow.)
- Min-length gates: `extraction.min_html_chars` (default 1000) and
  `extraction.min_web_chars` (default 500).
- HTML sanitisation: strip tags from `extraction.strip_tags` (default
  `["script", "iframe", "object", "embed"]`), no HTML fragments in the
  markdown output (FR-RES-5).
- `enrich.tier1_enrich(...)` real implementation using `models.enrich`
  in JSON mode with `prompts.tier1_enrich`. Replaces PRD 04's stub.
- `enrich.tier3_extract(...)` real implementation using `models.extract`
  in JSON mode with `prompts.tier3_extract`.
- `Tier1Enrichment` and `Tier3Extraction` Pydantic schemas.
- Wire Tier 2 (the `## Full Text` body) and Tier 3 (the four
  Tier-3-only sections) into the canonical note renderer in `notes.py`.
- Replace PRD 06's `re_extract_archive`, `tier2_enrich`,
  `tier3_extract` hooks with real implementations. The hook
  signatures defined by PRD 06 are unchanged.
- FR-ENR-6: enrichment failure → write the note WITHOUT the failed
  section, tag `influx:repair-needed`, no placeholder text. PRD 06's
  sweep retries on a later run.

## 3. Out of scope

- LCMA (`lithos_retrieve`, edges, tasks) — PRD 08.
- RSS fetcher itself — PRD 09. This PRD's web-article extractor is
  exercised against fixture HTML, not against a live RSS source.
- Multi-profile orchestration — PRD 09.

## 4. Internal seams permitted

- None. This PRD removes the last enrichment / extraction stubs.
  Anything left stubbed at the end of this PRD belongs to PRD 08
  (LCMA) or later.

## 5. Functional Requirements

### 5.1 Content extraction (master PRD §6.4)

- **FR-ENR-1.** arXiv text extraction order: HTML (via
  `arxiv.org/html/{id}`) → PDF → abstract-only (tagged
  `text:abstract-only`).
- **FR-ENR-2.** HTML extraction is rejected when extracted text length
  is below `extraction.min_html_chars` (default 1000); fall through to
  PDF.
- **FR-ENR-3.** Web-article extraction is rejected when text length is
  below `extraction.min_web_chars` (default 500); fall through to feed
  summary.

### 5.2 Tier 1 enrichment

- **FR-ENR-4.** Tier 1 uses `models.enrich` in JSON mode with the
  prompt at `prompts.tier1_enrich`. Required template variables
  (exactly): `{title}`, `{abstract}`, `{profile_summary}`. Response
  schema:

  ```
  Tier1Enrichment = {
    contributions: list[str] length∈[1,6],
    method:        str,
    result:        str,
    relevance:     str,
  }
  ```

  Render into the note's `## Summary` section as a structured block
  (one `### Contributions` bullet list, then `### Method`,
  `### Result`, `### Relevance` paragraphs).

### 5.3 Tier 3 deep extraction

- **FR-ENR-5.** Tier 3 uses `models.extract` in JSON mode with the
  prompt at `prompts.tier3_extract`. Required template variables
  (exactly): `{title}`, `{full_text}`. Response schema:

  ```
  Tier3Extraction = {
    claims:                list[str] length∈[1, 10],
    datasets:              list[str] length∈[0, 10],
    builds_on:             list[str] length∈[0, 10],
    open_questions:        list[str] length∈[0, 10],
    potential_connections: list[str] length∈[0, 10],
  }
  ```

  Each string element is non-empty and has ≤ 500 characters after
  trimming. Elements exceeding 500 characters MUST be truncated to
  500 on ingest. Responses violating list-length bounds MUST fail
  validation and be handled per FR-ENR-6.

  Render into the note's `## Claims`, `## Datasets & Benchmarks`,
  `## Builds On`, `## Open Questions` sections (one bullet per item).
  `potential_connections` is consumed by PRD 08 (LCMA) — it is NOT
  rendered into a body section.

### 5.4 Failure handling

- **FR-ENR-6.** If any enrichment call fails after retries, the note
  is written **without** the failed section, tagged
  `influx:repair-needed`. No placeholder text is inserted. Retry on
  a subsequent run is driven by PRD 06's repair sweep.

### 5.5 HTML stripping

- **FR-RES-5.** Extracted HTML has `extraction.strip_tags` removed
  (default `["script", "iframe", "object", "embed"]`). HTML fragments
  are NOT preserved in the markdown output. The extraction layer
  produces clean text; the renderer produces clean markdown.

### 5.6 Tier selection on initial write

The PRD 04 seam ("write the threshold tag without the section") is
**removed** here. With this PRD landed:

- A note that crosses the `relevance` threshold gets `## Summary`
  populated by Tier 1 enrichment (or `influx:repair-needed` on
  enrichment failure, with no `## Summary` section).
- A note that crosses the `full_text` threshold gets `## Full Text`
  populated by the extracted text (or `influx:repair-needed` on
  extraction failure).
- A note that crosses the `deep_extract` threshold gets the four
  Tier-3 sections populated by Tier 3 extraction (or
  `influx:repair-needed` on Tier 3 failure).

Independence: each tier failure is independent. A note may have a
successful Tier 1 + successful archive + failed Tier 3 — the note is
written with `## Summary` + `## Archive`, NO Tier 3 sections, and
`influx:repair-needed`.

## 6. Files to create / modify

### Create
- `src/influx/extraction/__init__.py`
- `src/influx/extraction/html.py`
- `src/influx/extraction/pdf.py`
- `src/influx/extraction/article.py` — generic web article path
- `src/influx/enrich.py` — replaces PRD 04 stub with real Tier 1 +
  Tier 3 callers and schemas
- `tests/unit/test_html_extraction.py`
- `tests/unit/test_pdf_extraction.py`
- `tests/unit/test_article_extraction.py`
- `tests/unit/test_tier1_schema.py`
- `tests/unit/test_tier3_schema.py`
- `tests/unit/test_tier3_truncation.py`
- `tests/integration/test_arxiv_html_to_full_text.py`
- `tests/integration/test_arxiv_pdf_fallback.py`
- `tests/integration/test_arxiv_abstract_only_initial_write.py`
- `tests/fixtures/extraction/*.html`
- `tests/fixtures/extraction/*.pdf`

### Modify
- `src/influx/notes.py` — render Tier 2 (`## Full Text`) and the four
  Tier 3 sections; section ordering per FR-NOTE-9 + master PRD §8.1.
- `src/influx/schemas.py` — add `Tier1Enrichment`, `Tier3Extraction`.
- `src/influx/repair.py` (PRD 06) — replace test-injected hooks with
  real ones imported from `extraction` and `enrich` modules.
- `src/influx/sources/arxiv.py` — call into `extraction` then `enrich`
  on initial write so notes ship with their tier sections populated.

## 7. Dependencies to add

| Purpose | Package |
|---|---|
| HTML extraction | `trafilatura` (or `readability-lxml`) |
| PDF text extraction | `pypdf` (or `pdfminer.six`) |

## 8. Acceptance Criteria

### From master PRD §7.2

- **AC-M2-1.** For an arXiv paper where `arxiv.org/html/{id}` returns
  200 and the extracted text is ≥ `extraction.min_html_chars`, the
  canonical note carries `text:html` and the `## Full Text` section
  when score ≥ `full_text` threshold.
- **AC-M2-2.** When HTML fetch fails or yields < `min_html_chars`,
  Influx falls back to PDF and tags `text:pdf`.
- **AC-M2-3** (full). When both HTML and PDF extraction fail, the
  note is written with `text:abstract-only` and no Tier 2/3 sections.
  `influx:text-terminal` is NOT added at this initial-write point —
  terminality is established only by the subsequent successful
  re-extraction in PRD 06's sweep.
- **AC-M2-4.** For a paper scoring ≥ `deep_extract`, the note
  contains `## Claims`, `## Datasets & Benchmarks`, `## Builds On`,
  `## Open Questions` BEFORE `## User Notes`, and tag
  `influx:deep-extracted`.

### From master PRD §7.5

- **AC-X-1** (extended). All extraction tunables
  (`extraction.min_html_chars`, `extraction.min_web_chars`,
  `extraction.strip_tags`) are config-driven; no hardcoded constants.

### New for this PRD

- **AC-07-A.** A Tier 1 enrichment call returning JSON with
  `contributions: []` (length 0) fails Pydantic validation; the note
  is written WITHOUT `## Summary` and tagged `influx:repair-needed`
  per FR-ENR-6.
- **AC-07-B.** A Tier 3 response with a 600-character claim is
  truncated to 500 characters on ingest (no validation failure).
- **AC-07-C.** A Tier 3 response with `claims: []` (length 0, below
  bound 1) fails validation; the note is written WITHOUT Tier 3
  sections and tagged `influx:repair-needed`.
- **AC-07-D.** Tier 1 success + Tier 3 failure produces a note with
  `## Summary` populated, no Tier 3 sections, and
  `influx:repair-needed`. Re-running PRD 06's sweep retries Tier 3
  only.
- **AC-07-E.** HTML extraction yielding 800 chars (below
  `min_html_chars`=1000) falls through to PDF.
- **AC-07-F.** Stripped tags: an HTML fixture containing `<script>`,
  `<iframe>`, `<object>`, `<embed>` produces text output with NONE
  of those tags' contents present, and no HTML fragments anywhere
  in the markdown.
- **AC-07-G.** PRD 06's hooks now call real implementations: an
  abstract-only note with archive path stored, on the next sweep,
  re-extracts using `extraction.html` or `extraction.pdf` against the
  archive file (NOT a live HTTP fetch — the archive file is the
  source of truth for re-extraction).

## 9. Tests required

- Recorded HTML and PDF fixtures drive deterministic extraction
  tests.
- Tier 1 / Tier 3 schemas have positive tests + negative tests for
  every bound (length 0 below bound 1; length above the upper
  bound; non-empty after trim).
- The truncation behaviour (500-char cap) is unit tested.
- Integration tests:
  - HTML success path (`text:html` + `## Full Text` + score ≥
    `full_text` → tag `full-text` present, body populated)
  - PDF fallback path
  - abstract-only initial-write path (no `influx:text-terminal`)
  - Tier 3 success + render
  - per-tier failure independence (AC-07-D)
- Coverage ≥ 80% on `extraction/*.py`, `enrich.py`.

## 10. Definition of Done

- [ ] All AC-M2-1…4, AC-X-1 (extraction part), AC-07-A…G satisfied.
- [ ] The PRD 04 seam ("threshold tag without section") is gone:
      `full-text` is set iff `## Full Text` body is non-empty;
      `influx:deep-extracted` is set iff Tier 3 sections exist.
- [ ] PRD 06 hooks are wired to real extraction/enrichment;
      PRD 06's tests still pass with the real callables.
- [ ] No remaining stub modules in the codebase except those owned
      by PRDs 08, 09, 10.
- [ ] Ruff, pyright, pytest all green. Coverage ≥ 80% on new modules.
