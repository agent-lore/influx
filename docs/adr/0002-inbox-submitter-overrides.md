# Inbox submitters may force ingestion and request the full-text tier

Inbox v1 deliberately gave submitters no way past the Filter
(`docs/plans/inbox.md` §1.2 non-goal 2, §3.2 "no `force` / `bypass_filter`
field"): an inbox item was scored against every Profile, dropped below
threshold, and enriched only as far as its score allowed, exactly like a feed
item. That is right for feed-style submitters (the briefing bots) but wrong for
a deliberate, curated submission: the submitter has already decided the item
belongs in Lithos and usually wants the full text. The Morrow literature work
(Lithos task `6880a2cc`, 2026-10-07) hit both limits: a 2017 RL paper filtered
at a top score of 6, and a sibling paper ingested at 7 as a Tier-1 note holding
only the arXiv abstract page. The Profiles exist to screen daily feed volume, so
the fix is not to retune them; an explicit submission should not depend on them.

The InboxTask therefore accepts two optional metadata fields, both off by
default so existing submitters see no change:

- **`force: true`**: when no Profile clears its threshold, the item is still
  written, hosted on the **top-scoring real Profile** (ties go to config order;
  if the model scored it for no Profile, the first Profile whose filter call
  worked hosts it at score 0). Every Profile is still scored and `per_profile`
  still records each verdict. The note is tagged `influx:forced` and its
  Profile Relevance reason begins `Forced by submitter <id> (…)`, so it does not
  read as a pass on merit. A synthetic `profile:<source_tag>` pseudo-Profile was
  rejected: every ingestion is a real single-Profile Run, so a pseudo-Profile
  would need a synthetic `ProfileConfig` in the Run, ledger, lock and metrics,
  and the repair sweep, which iterates configured Profiles, would never visit
  those notes.
- **`tier: "full"`**: the dispatch runs with its tier gates lowered to the
  item's score, so it gets Tier 1, Tier 2 full text, and Tier 3 deep extraction
  whenever full text was obtained. For an arXiv URL the item is acquired the way
  the scheduled arXiv Source does it instead of scraping the abs page:
  title and abstract from the export API, the PDF archived, full text from the
  HTML → PDF cascade, plus `arxiv-id:` and `text:*` tags. `tier` alone does not
  stop an item being filtered out; combine it with `force` for that.

Overrides are reported in `inbox_result.override`
(`force_requested`, `forced`, `forced_profile`, `tier_requested`,
`tier_achieved`) and in the outcome string (`; forced: <profile> (…)`,
`; tier full achieved: full | full_text | summary`). A malformed value is a
terminal `invalid_override` error rather than a silent fallback.

## Consequences

- An override never applies to an existing note (cache hit): complement
  profiles a replay dispatches run at their own gates, so a resubmission cannot
  rewrite the note with extra enrichment. Nor does it bypass the #292
  filter-unavailable deferral: with no verdict at all, there is no ranking to
  host a forced item on, and enrichment would need the same LLM slot.
- A `tier: "full"` arXiv acquisition whose text cascade fails is tagged
  `influx:repair-needed`, so the sweep's source-agnostic re-extraction recovers
  the full text from the archived PDF (for scores at or above the Profile's
  own `full_text` gate; see below).
- Resubmitting an already-ingested URL with the flags does **not** upgrade the
  note to full text; that is follow-up work.
- `notify_immediate` is never lowered, so a forced low-score note does not
  trigger an immediate notification.
- The repair sweep gates its Tier 2 / Tier 3 retries on the Profile's own
  thresholds (`repair.select_stages`), so a failed Tier 2/3 on a forced
  sub-threshold note is not retried. Nothing is stripped either: the clearing
  check waives those stages for scores below the gates.
- Inbox notes still have no archive-download resolver in the repair sweep
  (task `73dc153d`, GH #248); forced arXiv notes archive the PDF at ingest, so
  this matters only when that first download fails.
- A forced note sits in its host Profile's views with a below-threshold score;
  `influx:forced` is how to filter those out.
