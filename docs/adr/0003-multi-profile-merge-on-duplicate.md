# A second Profile merges into the existing note on `duplicate`; the richer body wins

FR-NOTE-6 says that when a second Profile ingests an item already in Lithos,
Influx merges into the existing note: `profile:*` tags are unioned, the
`## Profile Relevance` entries are unioned, and `## User Notes` / `## Repair`
carry over. Influx built that merge on a `version_conflict` reply carrying
the existing note's id. Real Lithos never sends one. A create-path write for a
`source_url` it already indexes comes back `duplicate`, naming the note in
`duplicate_of.id`. `version_conflict` comes only from an update whose
`expected_version` is stale, and it carries no id. The integration test passed
only because its fake invented the reply. So the merge never ran: a prod scan
on 2026-10-08 found 0 of 4674 Influx notes carrying more than one `profile:*`
tag (Lithos task `c1196e30`).

Measuring prod logs on 2026-10-09 (497 distinct profile/URL cache hits) showed
the other half of the cost. 466 (94%) were a Profile re-seeing a note it had
already written; 31 (6%) were a different Profile whose tag and Profile
Relevance entry were dropped. Every one of the 497 was acquired (download,
text extraction, Tier 1–3 LLM calls) before Lithos replied `duplicate`.

## Decision

- **Merge on `duplicate`.** `LithosClient.write_note` reads the note
  `duplicate_of.id` names and merges into it with `influx.note_merge`. It
  writes the result back as an update by `id` with `expected_version` and the
  existing title. `path`, `source_url`, `note_type` and `namespace` are left
  out, so Lithos keeps the note's own and the file never moves. On
  `version_conflict` it re-reads and merges once more, then gives up (logged).
  A note read without a `version` is not merged, since the update would be an
  unguarded overwrite. The hook covers every create-path write, including the
  `content_too_large` trim retry (Lithos checks size before its `source_url`
  index) and a slug-collision recovery that identifies the item's note,
  either by the `source_url` pre-check or by squatter inspection (matching
  `arxiv-id` or canonical URL, e.g. a note stored under an older http URL).
- **The richer body wins.** A body ranks by whether it has `## Full Text` and
  whether it has Tier 3 content. The incoming body replaces the existing one
  only when it ranks strictly higher and does not drop the existing note's
  archived copy (`## Archive` path); it then takes the existing `## User
  Notes` and `## Repair`, and its Influx-owned tags replace the old ones (the
  usual `merge_tags` contract). Otherwise the existing body stays, together
  with the Influx-owned tags that describe it (`full-text`, `text:*`, terminal
  markers), and only the profile tag and Profile Relevance entry are added.
  Profiles run in config order, so without this a later, lower-scoring
  Profile's summary-only write would replace an earlier Profile's full text
  (the fan-out concern raised in the PR #300 review). Dispatching the highest
  score last was rejected: scheduled Profiles are separate Runs at least half an hour apart,
  and the rank rule also protects inbox fan-out and re-ingestion.
  A tie keeps the existing body, which avoids churn.
- **Nothing to merge, nothing written.** When the merge adds no Profile and
  no richer body, `note_merge` returns `None` and the outcome stays
  `duplicate`. The pre-acquire dedup applies the same test earlier: a cache
  hit whose note already carries `profile:<profile>` or
  `influx:rejected:<profile>` is skipped (`action=skip-present`), like a
  backfill hit. That removes the 94% of wasted acquires.
- **Only Influx's notes.** A note without `ingested-by:influx` is another
  agent's; the merge leaves it alone. A body with none of the canonical
  sections is never ranked or replaced, though tags still merge.
- **The repair flag survives.** `influx:repair-needed` stays on the merged
  note if either side had it. The repair sweep only visits flagged notes; it
  re-derives what is missing from the merged note (the added Profile's score
  may now require Tier 2 or Tier 3) and clears the flag once nothing is.
  Dropping it would strand that enrichment for good, because later runs skip
  the Profile as `skip-present` (found in the PR #304 review: Profile B scores
  9, its Tier 3 call fails, its full-text body ties with Profile A's, and A's
  body is kept).
- **Confidence** is the higher of the two (FR-NOTE-8).

## Consequences

- Second-Profile writes now report `updated`, count as ingested for that
  Profile, appear in its digest, and get LCMA wiring against the shared note.
- `lithos_writes{status="duplicate"}` and the ledger's duplicate write
  outcomes drop sharply, because settled hits no longer reach the write.
  `cache_hits` still counts them.
- Profile tags lost before this change are not backfilled. The notes don't
  record which other Profiles scored them. Items still inside a Profile's
  fetch window merge on that Profile's next Run; older ones stay
  single-Profile. A backfill can't recover them either, because backfills
  skip every cache hit (FR-BF-2).
- A merge that fails (a second `version_conflict`, `invalid_input`, or a
  body still too large after falling back to the existing one, reported as
  `content_too_large_skipped`) leaves the note untouched and is logged by the
  run as `article write skipped … cache_hit=true`.
- The same machinery is what an upgrade-on-resubmission needs (Lithos task
  `d1be2959`): a same-Profile write with a richer body would upgrade the note
  through this path, but nothing sends one today. Scheduled runs skip a
  Profile already on the note before acquire, and the inbox never dispatches
  one.
