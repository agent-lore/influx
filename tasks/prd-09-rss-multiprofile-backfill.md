# PRD 09 — RSS Fetcher + Multi-Profile Orchestration + Backfill

**Part of:** Influx v1 (see `tasks/prd-influx-v1-index.md`)
**Covers master PRD stories:** S-11 + S-12 + S-13
**Milestone:** M3
**Prerequisites:** PRD 01, 02, 03, 04, 05, 06, 07, 08
**Downstream PRDs that depend on this:** 10

---

## 1. Context

Three M3 stories combine well: they all extend the existing arXiv-only
single-profile pipeline along independent axes (RSS source, multi-
profile merging, backfill kind). After this PRD, the v1 product is
functionally complete; PRD 10 adds OTEL and finishes polish.

## 2. In scope

### 2.1 RSS fetcher (S-11)

- `sources/rss.py` — Atom/RSS parser using `feedparser`.
- Per-feed `source_tag` (`"rss"` or `"blog"`) flows into the canonical
  `source:*` tag and the archive bucket layout (FR-ST-1, AC-M3-4).
- RSS article fetches go through PRD 02's guarded HTTP client.
- Archive layout for RSS:
  `/archive/<feed.source_tag>/{YYYY}/{MM}/{feed-slug}-{YYYY-MM-DD}-{url-hash}.html`
  with `{url-hash}` = first 10 hex chars of SHA-256 of the normalised
  `source_url`.
- Note paths: `articles/rss/{YYYY}/{MM}` or `articles/blog/{YYYY}/{MM}`
  per `source_tag`.
- Source-agnostic dedup: `lithos_cache_lookup` query composition is
  unchanged from PRD 05 (FR-MCP-3) — it works for RSS items because
  it falls back to title alone when no abstract/summary is present.

### 2.2 Multi-profile orchestration (S-12)

- Profile tag merging on shared notes (FR-NOTE-6).
- Per-profile `## Profile Relevance` bullets merged.
- Per-profile rejection authority: `influx:rejected:<profile>` blocks
  re-adding `profile:<profile>` (FR-NOTE-6 + AC-M3-6 — partially
  asserted by PRD 06; this PRD asserts the inverse direction in the
  ingest path).
- Cross-profile parallelism allowed; same-profile strict serial
  (already enforced by PRD 03's coordinator).

### 2.3 Backfill (S-13)

- `POST /backfills` real implementation, replacing PRD 03's stub
  estimator.
- Naive cost estimate: `days × len(categories) × max_results_per_category`
  (Q-3).
- `--confirm` gate triggered by `estimated_items > 1000`
  (FR-BF-6 / AC-M3-8).
- `lithos_task_create(tags=["influx:backfill", ...])` (FR-BF-5,
  FR-LCMA-5 backfill arm).
- Backfills DO NOT call the repair sweep (FR-REP-2; PRD 06 already
  enforces this; this PRD ensures `kind="backfill"` is correctly
  propagated from the API down).
- Backfills DO NOT POST webhook (FR-NOT-4; PRD 05 already gates this;
  this PRD ensures `kind="backfill"` reaches the webhook hook).
- Already-ingested items (per `lithos_cache_lookup`) are skipped
  (FR-BF-2).
- Honour arXiv pacing (FR-SRC-3 / FR-BF-3) — already enforced by
  PRD 04's global rate limiter.

## 3. Out of scope

- OTEL spans / metrics — PRD 10.
- Per-tag rejection-rate logging — PRD 10.

## 4. Internal seams permitted

- None remaining beyond PRD 10's polish items.

## 5. Functional Requirements

### 5.1 RSS source (master PRD §6.2)

- **FR-SRC-4.** RSS feeds parsed by an RSS/Atom parser; per-item
  fields: title, URL, published, summary. Every fetched item inherits
  its feed's configured `source_tag` (FR-CFG-9) verbatim; no feed
  contributes items to more than one `source:*` bucket.
- **FR-SRC-5.** RSS article fetches subject to the SSRF guard and
  size/timeout caps (FR-RES-4).

### 5.2 Archive layout for RSS (master PRD §6.5)

Refer to PRD 04's FR-ST-1 for the full rule. RSS-specific clauses:

- RSS/feed items → `source:<feed.source_tag>` per FR-CFG-9, archived
  at `/archive/<feed.source_tag>/{YYYY}/{MM}/{feed-slug}-{YYYY-MM-DD}-{url-hash}.html`.
- `{url-hash}` is the first 10 hex chars of SHA-256(normalised
  `source_url`). MANDATORY per-item disambiguator. Two distinct items
  from the same feed published on the same date MUST map to distinct
  archive filenames; two references to the same normalised
  `source_url` MUST always yield the same filename.
- The archive `{source}` segment, the note's `source:*` tag, and the
  note's storage path (FR-NOTE-2) MUST all agree.

### 5.3 Multi-profile orchestration (master PRD §6.6)

- **FR-NOTE-6.** `profile:*` tags are merged (union), not replaced.
  `## Profile Relevance` entries are merged by profile name. Per-
  profile `influx:rejected:<profile>` is preserved and authoritative:
  when present, MUST NOT re-add `profile:<profile>` or refresh its
  `## Profile Relevance` entry.

### 5.4 Multi-profile execution (master PRD §6.10)

Cross-profile parallelism is allowed (Q-4); already enforced by PRD 03's
coordinator. This PRD ensures the per-source fetch deduplicates across
profiles within a run (R-8) — e.g. two profiles both subscribed to
`cs.AI` fetch `cs.AI` once.

### 5.5 Backfill (master PRD §6.16)

- **FR-BF-1.** Backfill range is specified as either `--days N` or
  `--from YYYY-MM-DD --to YYYY-MM-DD`.
- **FR-BF-2.** Items already ingested (per `lithos_cache_lookup`) are
  skipped.
- **FR-BF-3.** arXiv rate limits honoured. Budget: ~30s per day of
  backfill per profile.
- **FR-BF-4.** Backfills never send webhook notifications.
- **FR-BF-5.** Backfills create `lithos_task_create` tasks tagged
  `influx:backfill`.
- **FR-BF-6.** Confirmation required when the live service's
  pre-submission estimate exceeds 1000 items. Service returns
  `reason="confirm_required"` with `estimated_items` field; CLI
  re-submits with `confirm=true` only after user acknowledgement.

## 6. Files to create / modify

### Create
- `src/influx/sources/rss.py`
- `src/influx/backfill.py` — estimator + run flow
- `tests/unit/test_rss_fetcher.py`
- `tests/unit/test_archive_url_hash.py` — same-feed-same-date
  collision test (AC-M3-4 sub-test 2) + determinism test (sub-test 3)
- `tests/unit/test_multi_profile_merge.py`
- `tests/unit/test_backfill_estimator.py`
- `tests/integration/test_rss_to_lithos.py`
- `tests/integration/test_multi_profile_shared_source.py`
- `tests/integration/test_backfill_arxiv.py`
- `tests/integration/test_profile_rejection_authority.py`
- `tests/fixtures/rss/*.xml` — recorded feed fixtures per type

### Modify
- `src/influx/service.py` — `run_profile` now handles both arXiv and
  RSS sources for a profile in one pass; deduplicate per-source
  fetches across profiles when a scheduled fire runs multiple
  profiles concurrently.
- `src/influx/notes.py` — extend tag-merging tests to cover the
  multi-profile shared-source case.
- `src/influx/http_api.py` — wire real `/backfills` handler with
  estimator.
- `src/influx/cli.py` — `backfill` subcommand flow (estimate handling,
  confirm prompt) is finalised.
- `src/influx/lcma.py` (PRD 08) — task creation tag depends on `kind`:
  `influx:run` for scheduled/manual, `influx:backfill` for backfill.

## 7. Dependencies to add

| Purpose | Package |
|---|---|
| RSS / Atom parsing | `feedparser` |

## 8. Acceptance Criteria

### From master PRD §7.3

- **AC-M3-1.** Two enabled profiles both run in one scheduled fire
  without overlap. (Coordinator enforces; PRD 03's stub run hook is
  now real.)
- **AC-M3-2.** A single arXiv paper that matches both profiles
  produces ONE Lithos note carrying both `profile:*` tags and two
  `## Profile Relevance` bullets.
- **AC-M3-3.** Disjoint matches produce one note per profile, each
  tagged with the single matching `profile:*` tag.
- **AC-M3-4.** Every RSS item inherits its feed's `source_tag`
  (FR-CFG-9) as the canonical `source:*` tag. For `source_tag = "rss"`,
  note carries `source:rss`, stored at `articles/rss/{YYYY}/{MM}`,
  archived at `/archive/rss/{YYYY}/{MM}/{feed-slug}-{YYYY-MM-DD}-{url-hash}.html`.
  For `source_tag = "blog"`, note carries `source:blog`, stored at
  `articles/blog/{YYYY}/{MM}`, archived at
  `/archive/blog/{YYYY}/{MM}/{feed-slug}-{YYYY-MM-DD}-{url-hash}.html`.

  Three test sub-requirements:
  1. Config test: exercise both `source_tag` values; assert one-to-
     one correspondence between `source_tag`, the `source:*` tag,
     the note path, and the archive path.
  2. Collision test: two distinct items from the same feed with the
     same published date but different normalised `source_url`s
     produce distinct archive filenames; neither overwrites the
     other.
  3. Determinism test: ingesting the same item twice yields the
     same archive filename (same `{url-hash}`).

- **AC-M3-5.** Rejected notes: adding `influx:rejected:<profile>` to
  an Influx-authored note in Lithos causes its title to appear in
  the next run's filter prompt in `NEGATIVE EXAMPLES`. (Already
  partially asserted by PRD 05; this PRD's integration test
  exercises end-to-end with a multi-profile run.)
- **AC-M3-6.** A repair pass on a note with
  `influx:rejected:<profile>` does NOT re-add `profile:<profile>`
  even if the source matches that profile again. (PRD 06 owns the
  sweep; this PRD asserts the same invariant on the ingest path.)
- **AC-M3-7.** `python -m influx backfill --profile X --days 7`
  completes and never overlaps with a scheduled run for the same
  profile.
- **AC-M3-8.** `python -m influx backfill --profile X --days 365`
  without `--confirm` returns `reason="confirm_required"` with an
  `estimated_items` count. Re-submitting with `--confirm` is
  accepted.

### New for this PRD

- **AC-09-A.** RSS feed with `source_tag = "blog"` produces notes
  at `articles/blog/{YYYY}/{MM}` with tag `source:blog`, archived
  in `/archive/blog/...`. The same feed re-tagged `source_tag =
  "rss"` would produce different paths and a different `source:*`
  tag. (Cross-checks AC-M3-4 sub-test 1.)
- **AC-09-B.** Same-feed-same-date collision: two items from
  `feed-X` published on `2026-04-23` with URLs
  `https://feed-x.example/post-a` and `https://feed-x.example/post-b`
  produce two distinct archive files; both exist on disk after the
  run.
- **AC-09-C.** Determinism: re-ingesting `https://feed-x.example/post-a`
  yields the same archive filename (verified by hashing `{url-hash}`
  twice).
- **AC-09-D.** `cs.AI` fetched once when two profiles both subscribe
  to it within the same scheduled fire (R-8 mitigation).
- **AC-09-E.** Backfill with `--days 7` and a profile whose estimate
  is 35 items (well below 1000) is accepted without `--confirm`.
- **AC-09-F.** Backfill creates a Lithos task with tag
  `influx:backfill`, NOT `influx:run`.
- **AC-09-G.** Backfill does NOT POST a webhook.
- **AC-09-H.** Backfill does NOT call the repair sweep
  (`lithos_list(tags=["influx:repair-needed", ...])` is not invoked
  during backfill).
- **AC-09-I.** A backfill `--days N` whose estimate exceeds 1000
  returns `reason="confirm_required"`; submitting again with
  `--confirm` runs to completion.
- **AC-09-J.** Web-article extraction from an RSS feed item where
  the article body is < `extraction.min_web_chars` falls back to
  the feed `<summary>` (FR-ENR-3, exercised end-to-end now that
  PRD 07's article extractor exists).
- **AC-09-K.** A rejected-then-rescored item: a note with
  `influx:rejected:<profile-A>` that the filter scores ≥
  `relevance` for `profile-A` again does NOT re-acquire
  `profile:profile-A`; if `profile-B` separately scores it ≥
  `relevance`, `profile:profile-B` IS added.

## 9. Tests required

- Recorded RSS fixtures per feed type.
- Hash determinism + collision tests on archive filenames.
- Multi-profile orchestration: shared source, disjoint sources,
  rejection-authority preservation.
- Backfill: estimate, confirm gate, task tagging,
  no-webhook/no-sweep verification.
- Coverage ≥ 80% on `sources/rss.py`, `backfill.py`, multi-profile
  orchestration code paths in `service.py`.

## 10. Definition of Done

- [ ] All AC-M3-1…8, AC-09-A…K satisfied.
- [ ] No remaining stubs in the codebase except OTEL (PRD 10) and
      rejection-rate logging (PRD 10).
- [ ] The `run_profile` hook left by PRD 03 is now a single
      well-typed function that handles arXiv + RSS + scheduled +
      manual + backfill cases.
- [ ] Ruff, pyright, pytest all green. Coverage ≥ 80% on new modules.
