# Model evals: operator guide

How to measure a model change for Influx's three LLM slots — the filter
scorer, Tier-1 enrichment and Tier-3 extraction — before it reaches
production, and how to keep a human answer key for the filter.

Background, the metric rationale and the GPT-4.1-mini → GPT-6 Luna results
live in [`docs/plans/model-evals.md`](../plans/model-evals.md). This page is
the how-to.

Run the harness from the repo root with `uv run python -m evals <command>`;
`--help` on any command lists every flag.

## 1. When to run it

- **A model swap** for any slot: a new hosted model, a provider retiring
  one, an on-prem model.
- **A slot setting change** that alters output: `reasoning_effort`,
  `json_schema_strict`, `temperature`, `max_tokens`.
- **A prompt change** in `[prompts.*]`. Prompts are rendered from the config
  at run time, so run the baseline model with the old and new config and
  compare the two runs.

## 2. The commands

| command | what it does | costs money? |
|---|---|---|
| `build-dataset` | snapshots live arXiv/RSS candidates per profile (filter + Tier-1 cases) and mines Tier-3 cases from prod notes | no (feeds only) |
| `run` | sends a dataset to one model with production prompts and request shape | yes (the model) |
| `compare` | compares two runs; with `--judge-model`, an LLM judge arbitrates | judge only |
| `label` | you label the filter candidates yourself — the answer key | no |
| `grade` | grades any number of runs' filter decisions against your labels | no |

Everything is written under `evals/` (or `--root`), and all of it is
git-ignored because it holds snapshotted paper text and raw model output:

```text
evals/
├── datasets/<dataset>/
│   ├── filter.jsonl  tier1.jsonl  tier3.jsonl  manifest.json
│   └── labels.jsonl          ← your labels; back this file up
├── runs/<dataset>/
│   ├── <run label>/          spec.json, <slot>.jsonl, summary.md, judge-*.jsonl
│   ├── compare-<cand>-vs-<base>.md
│   └── grade.md
└── .cache/                   responses keyed by the exact request body
```

The cache makes re-running a finished run free: an identical request is
replayed from disk with its originally measured latency and cost. Change
anything in the request (model, prompt, flags) and it is a fresh call.

## 3. Prerequisites

```bash
cd ~/projects/lithos/code/influx
PROD=~/projects/lithos/data/prod
CONFIG=$PROD/config/influx.toml          # prompts, profiles, thresholds, providers
ENV=docker/.env.prod                     # holds OPENROUTER_API_KEY
```

The harness only reads the config; it never writes to it or to Lithos. A
full baseline + candidate + judge pass over `oct15` cost about $12 in
October 2026, most of it the judge on Tier 3.

## 4. Comparing a new model against production

This is the usual job. The `oct15` dataset and the `luna-strict` run
(production's current settings) are already on disk, so only the candidate
and the judge cost anything.

**1. Check what the baseline ran with.** A run overrides only the flags you
pass and inherits every other `[models.<slot>]` setting from `--config`, for
all three slots at once:

```bash
cat evals/runs/oct15/luna-strict/spec.json
```

**2. Smoke-test the candidate on a few cases** to catch auth, schema or
timeout problems cheaply:

```bash
uv run python -m evals run --dataset oct15 --config $CONFIG --env-file $ENV \
    --label newmodel-smoke --model <provider model id> --provider openrouter \
    --strict --max-tokens 8192 --timeout 120 --limit 3
```

**3. Run the full dataset** with a label that names the model and settings:

```bash
uv run python -m evals run --dataset oct15 --config $CONFIG --env-file $ENV \
    --label newmodel --model <provider model id> --provider openrouter \
    --strict --max-tokens 8192 --timeout 120
```

Mind the inherited settings. The prod config now sets `reasoning_effort =
"none"` and `json_schema_strict = true` on every slot:

- A model that rejects `none` (GPT-6 Astra, GPT-6.1 Sol) needs
  `--reasoning-effort low` or similar. Anything but `none` drops
  `temperature` from the request automatically.
- A server without OpenAI structured outputs needs `--no-strict`.
- A model that rejects `reasoning_effort` outright needs a copy of the
  config with that line removed from each `[models.*]` slot, passed as
  `--config` (the CLI can override the value but not clear it).

**4. Compare, with a judge:**

```bash
uv run python -m evals compare --dataset oct15 --config $CONFIG --env-file $ENV \
    --baseline luna-strict --candidate newmodel \
    --judge-model anthropic/claude-sonnet-4.6 --judge-provider openrouter
```

Pick a judge stronger than both models and from a different family than
the candidate: a judge rating its own family's output is biased. To redo
one slot's judging only, add `--judge-slots tier3` (or `filter`, `tier1`).

The report lands in `evals/runs/oct15/compare-newmodel-vs-luna-strict.md`.

## 5. Reading the compare report

Read the sections in this order:

1. **Validity, latency and cost.** Validity below 98%, or many *salvaged*
   results (JSON cut out of surrounding text), means repair sweeps and
   terminal notes in production. Check p95 latency against the slot's
   `request_timeout` (keep 50% headroom) and compare cost per case.
2. **Filter: score agreement.** This is agreement with the *baseline*, not
   correctness. The threshold table shows agreement at each score
   threshold any profile uses; the flips table lists every item whose
   ingest decision changed at *its own profile's* `relevance` threshold
   (they differ: `retro-computing` ingests at 6, the rest at 7). The judge
   arbitrates exactly these flips.
3. **Filter: graded against your labels** (present once `labels.jsonl`
   exists). This is correctness — see §7.
4. **tier1 / tier3: output shape.** Look for structural regressions: empty
   lists, fewer `arXiv:` ids in `builds_on`, bloated text fields.
5. **Judge.** Pairwise wins/losses per slot, judged in both orders (a
   disagreement between the orders counts as a tie). With labels, the
   *filter flips* line is followed by **judge vs your labels**: how often
   the judge backed the model your labels say was right.

The rule for adopting a candidate is in
[plan §5](../plans/model-evals.md#5-decision-rule-for-a-slot-swap). Two
amendments since:

- Filter agreement is now measured against Luna, not GPT-4.1-mini (plan §7.5).
- Once labels exist they outrank both agreement and the judge on the
  filter. A candidate should match the baseline's ingest-gate F1 against
  your labels, without a big precision-for-recall trade you don't want.

## 6. Labelling the filter set

Without labels, every filter number is relative to another model: two
models that wrongly pass the same irrelevant paper look perfect. Labels
are your own call on each candidate, which makes them a fixed answer key
every future run can be graded against.

```bash
uv run python -m evals label --dataset oct15 --config $CONFIG
```

The tool shows one profile's description, then its candidates one at a
time — title, link and abstract — and never the models' scores, so your
labels stay independent. Answer on the pipeline's own scale:

| key | tier | means | model equivalent |
|---|---|---|---|
| `0` | skip | not relevant to this profile; should not be ingested | score < `relevance` |
| `1` | ingest | relevant: worth a note with a short summary | score ≥ `relevance` |
| `2` | full text | clearly relevant: worth fetching the full text | score ≥ `full_text` |
| `3` | deep extract | core to the profile: worth a deep Tier-3 extraction | score ≥ `deep_extract` |

You can add a note after the digit (`0 hardware, not agents`). Other keys:
`s` skips the item for now, `b` goes back one, `p` reprints the profile,
`?` shows help and `q` quits.

- **Resumable.** Each label is appended to `labels.jsonl` as you make it.
  Quit, Ctrl-C or close the terminal at any point; the next `label` run
  starts at the first unlabelled candidate.
- **Labels are per profile.** The same paper fetched for two profiles is
  labelled twice, because relevance depends on the profile.
- **Changing your mind.** `b` goes back within a session.
  `--relabel` walks already-labelled items again and shows each current
  label. Add `--profiles ai-agents` to do one profile. A new label
  supersedes the old one, and the file keeps the history.
- **Label the paper, not the abstract.** If the abstract is too thin to
  decide (RSS posts often have none), open the link. The answer key should
  say what you *want* ingested, even where a model couldn't tell.
- **Several labellers.** `--labeller <name>` (default: your login) is
  recorded on each line. The latest label still wins; there is no
  inter-rater merge.
- **Effort.** `oct15` has 230 candidates across 8 profiles. Most are
  obvious skips, so expect about an hour. Do it in sittings with
  `--profiles` if that helps.
- **Validity.** Labels are tied to the dataset's candidates and to the
  profile descriptions as they were when you labelled. If a profile's
  description changes materially, relabel that profile:
  `--relabel --profiles <name>`.

`labels.jsonl` is the only thing in `evals/` you cannot regenerate, and it
is git-ignored with the rest of the dataset. Back it up, e.g. alongside the
prod config backups.

## 7. Grading runs against your labels

```bash
uv run python -m evals grade --dataset oct15 --config $CONFIG \
    --runs gpt41mini,luna-strict,newmodel
```

This writes `evals/runs/oct15/grade.md` and makes no model calls. It
contains:

- **Label mix:** how many candidates you put in each tier.
- **Per-run, per-gate table.** Each run's score is mapped to a tier with
  each profile's own thresholds, then compared with your label at the
  three gates:
  - **ingest** — precision is the share of what the model let in that you
    wanted (noise in the knowledge base); recall is the share of what you
    wanted that it let in (missed papers). This is the gate that matters
    most.
  - **full text / deep extract** — the same at the costlier gates. Errors
    here cost money and depth, not inclusion.
  - Also F1, accuracy, kappa and the raw TP/FP/FN/TN. A rate that is
    undefined (say, no item you labelled `3` and none the model put
    there) shows `n/a`.
  - Gates are cumulative, as in the pipeline: a score below `relevance`
    is tier 0 whatever `full_text` says.
- **Exact tier:** how often the model's tier equals yours.
- **Ingest misses per run:** every candidate whose ingest decision
  contradicts your label, with its score and which way the model went:
  `ingested (FP)` is noise it let in, `rejected (FN)` a paper it missed.
  `omitted` means the scorer left the item out of its reply, normally
  because it scored below `min_score_in_results`. Read these; they show
  *how* a model is wrong.

Only candidates in batches the run scored are graded (an item that sits in
two batches is graded on the first one scored). A failed batch is a validity
problem and is reported in the run summary instead, so check that `graded`
matches across runs before comparing their rates.

**Mind the sample size.** With a few dozen relevant items per dataset, one
item is several points of recall. Treat small differences as noise and
read the misses. More labelled datasets over time tighten the numbers.

`compare` adds the same table for its two runs automatically whenever
`labels.jsonl` exists. Use **judge vs your labels** to decide how much to
trust the judge on the slots that have no key (Tier 1, Tier 3). If it often
disagrees with you on filter flips, discount its verdicts.

## 8. Building a new dataset

Build a fresh dataset when `oct15` no longer looks like production: new
profiles or feeds, a shift in what arXiv is publishing, or simply to
re-test on unseen papers.

```bash
uv run python -m evals build-dataset --name <name> --config $CONFIG \
    --knowledge-dir $PROD/lithos/knowledge \
    --lookback-days 4 --batches-per-profile 2 --tier1-per-profile 4 --tier3-limit 40
```

- `--seed` makes sampling deterministic, but the feeds are live: the same
  command next week snapshots different papers. A dataset is a fixed
  snapshot; keep it rather than rebuilding it.
- `--profiles a,b` limits it to some profiles. `--no-rss` skips feeds.
- Tier-3 cases come from prod notes that have a `## Full Text` section
  (4k–120k chars).
- A new dataset needs a new baseline run, and its own labels: labels belong
  to a dataset's candidates and do not carry over.

## 9. On-prem and other OpenAI-compatible servers

Add a provider block to a copy of the config; it never needs to be the live
one:

```toml
[providers.local]
base_url = "http://gpu-box.lan:8000/v1"   # vLLM / llama.cpp / Ollama
api_key_env = ""                          # keyless
```

```bash
uv run python -m evals run --dataset oct15 --config evals/local.toml \
    --label qwen3-32b --model qwen3-32b --provider local --no-strict
```

`--no-strict` drops the OpenAI-only `json_schema` response format. Output is
still validated against the production Pydantic schemas. Cost is `n/a` for a
keyless provider. Remove `reasoning_effort` from the copy's `[models.*]`
slots if the server rejects it (see §4).

## 10. Troubleshooting

| symptom | cause / fix |
|---|---|
| `run 'x' not found under …` | `--baseline`/`--candidate`/`--runs` must match a `--label` used with `run` on the same `--dataset` |
| `no labels in …/labels.jsonl` | run `label` first; `grade` needs at least one label |
| `labels.jsonl:N: bad label` | line N was hand-edited into an invalid shape (tier must be an integer 0–3); fix or delete that line |
| warning `labels.jsonl:N: skipping torn line` | a crash cut a label short mid-write; it is ignored and the next label goes on a fresh line. Relabel that item if it matters |
| `run 'x' has no filter results to grade` | that run was made with `--slots` excluding `filter`, or its `filter.jsonl` is missing |
| every call fails with HTTP 402 | OpenRouter credits exhausted — top up and re-run; finished calls replay from the cache |
| Tier-3 errors are `network` timeouts | raise `--timeout`; Luna's p95 was 19s with a 44s worst case |
| many *salvaged* results | the model wraps JSON in prose; try `--strict` |
| judge errors on long Tier-3 sources | a source over `--judge-source-cap` is skipped, never clipped; raise the cap if the judge model's context allows |
| you need fresh calls for an unchanged request | delete the matching files in `evals/.cache/` (or all of it) |
