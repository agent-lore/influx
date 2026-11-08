# Model evals and the GPT-4.1-mini → GPT-6 Luna migration

Status: **harness shipped; all three slots on Luna since 2026-10-15; filter
graded against human labels and relevance lowered to 6 on 2026-11-08 (§7.6)**
Date: 2026-10-15
Scope: Influx only (its three `[models.*]` slots). The sibling Lithos project
has its own model usage and is planned separately.

## 1. Why

Every Influx LLM call — the filter scorer, Tier-1 enrichment and Tier-3
extraction — runs on `openai/gpt-4.1-mini` via OpenRouter. OpenAI has
scheduled GPT-4.1 mini for shutdown on **2027-01-14** with `gpt-6-luna` as
the named replacement, and Luna's list price is a quarter of 4.1-mini's on
input and under a third on output:

| model (OpenRouter id) | $/M input | $/M output | notes |
|---|---:|---:|---|
| `openai/gpt-4.1-mini` (current) | 0.40 | 1.60 | retires 2027-01-14 |
| `openai/gpt-6-luna` | 0.10 | 0.50 | 1.05M context, structured outputs, `reasoning_effort` incl. `none` |
| `openai/gpt-6-sol` | 2.00 | 10.00 | mid tier, supports `none` |
| `openai/gpt-6.1-sol` | 2.00 | 10.00 | no `none` effort |
| `openai/gpt-6-astra` | 10.00 | 50.00 | flagship, no `none` effort |

OpenAI's migration guide (help.openai.com article 20001600, "Migrating from
GPT-4.x to GPT-6 Luna") is short: pick Luna, start with reasoning effort
`none`, and *"evaluate the model on representative workloads before moving
production traffic"* — establish a baseline, evaluate, tune and retest, roll
out gradually. The GPT-6 developer guide adds the API facts that matter here:

- `reasoning_effort` defaults to `medium`; Luna and GPT-6 Sol accept `none`,
  Astra and 6.1 Sol do not.
- When effort is anything but `none`, `temperature` / `top_p` must be
  removed from the request. With `none` they are accepted.
- Hidden reasoning tokens are billed as output and count against
  `max_tokens` (a 2k cap can be eaten entirely by reasoning).
- Structured outputs (`response_format: json_schema, strict: true`) are
  supported on Chat Completions, which is the only surface Influx speaks.

A live probe through OpenRouter on 2026-10-15 confirmed all of this: with
`reasoning_effort: "none"` Luna honours `temperature`, strict JSON schema
and returns `usage.cost`; without it the same one-line prompt spent 10
reasoning tokens.

The same harness is the yardstick for the next step: on-prem models behind
any OpenAI-compatible server (vLLM, llama.cpp, Ollama) are just another
`[providers.*]` block, so "is the local model good enough for the filter?"
becomes a measured answer.

## 2. What was wrong before

Influx had no way to measure a model change. Three call sites each built
their own request body, there was no record of per-call latency or token
usage, and the only quality signal was production notes after the fact. A
slot could not express `reasoning_effort`, so pointing it at GPT-6 Luna
would have run every call at `medium` reasoning (slower, pricier, and a
2048-token `max_tokens` partly consumed by hidden tokens).

## 3. What shipped

### 3.1 Production code

- `influx/llm_request.py`: one `build_chat_request(slot, prompt, …)` and
  `provider_endpoint(provider)` used by the filter scorer, both enrichment
  callers and the `validate-config` JSON-mode probe. Mapped to the `Config`
  component; Foundation tier.
- `ModelSlotConfig.reasoning_effort` (`none | minimal | low | medium | high
  | xhigh | max`, default unset). Sent as `reasoning_effort`; when it is set
  to anything other than `none`, `temperature` is omitted automatically.
- `filter.render_filter_user_message`, `enrich.render_tier1_prompt` and
  `enrich.render_tier3_prompt` expose the exact prompt text production
  sends so the harness cannot drift from it.

### 3.2 The harness (`evals/`, run with `uv run python -m evals`)

| module | job |
|---|---|
| `cases.py` | frozen case/result records, JSONL IO |
| `dataset.py` | build a dataset: arXiv + RSS candidate snapshot per profile (filter + tier1 cases), Tier-3 cases mined from prod notes' `## Full Text` |
| `runner.py` | send cases through production rendering + `build_chat_request` with the slot overridden by a `ModelSpec`; records validated output, latency, tokens, OpenRouter `usage.cost`; disk cache keyed by request body |
| `metrics.py` | pure: validity/latency/cost summary; filter score agreement vs a baseline run (exact, ±1, MAE, pass/fail confusion + Cohen's kappa at 7/8/9); output-shape stats |
| `judge.py` | pairwise LLM judge, both position orders, disagreement → tie |
| `labels.py` | human relevance labels for filter candidates (0 skip … 3 deep extract) and the blind, resumable terminal tool that collects them |
| `report.py` | markdown tables |
| `__main__.py` | `build-dataset`, `run`, `compare`, `label`, `grade` |

Datasets, runs and the cache live under `evals/{datasets,runs,.cache}` and
are git-ignored (they contain snapshotted paper text and raw model output).

### 3.3 Why these metrics

- **Filter** is a gate, so what matters is the *decision*, not the number:
  agreement at each profile threshold (relevance 7, full text 8, deep
  extract 9) with Cohen's kappa so chance agreement on the sub-threshold
  majority does not flatter the result. Every flipped decision is listed
  so the operator can read it, and the judge can arbitrate it against the
  profile description.
- **Tier 1 / Tier 3** have no key. Structural stats catch regressions that
  break notes (empty lists, lost arXiv ids in `builds_on`, bloated
  summaries); the pairwise judge, run in both orders against the source
  text, answers "is it at least as good?".
- **Validity** (schema-valid JSON per call) and **cost / latency** come for
  free from every run and are the first things to look at: a model that
  fails validation 5% of the time costs repair sweeps and terminal notes.

## 4. How to run it

The operator guide, [`docs/operations/model-evals.md`](../operations/model-evals.md),
is the maintained how-to: comparing a new model against production,
labelling the filter set and grading runs against it. The commands below
are the ones used for the migration.

```bash
# API keys (OPENROUTER_API_KEY) are read from the env or --env-file.
cd ~/projects/lithos/code/influx
PROD=~/projects/lithos/data/prod

# 1. Snapshot a dataset from the live feeds + prod notes
uv run python -m evals build-dataset --name oct15 \
    --config $PROD/config/influx.toml \
    --knowledge-dir $PROD/lithos/knowledge \
    --lookback-days 4 --batches-per-profile 2 --tier1-per-profile 4 --tier3-limit 40

# 2. Baseline and candidate runs (same dataset, same prompts)
uv run python -m evals run --dataset oct15 --config $PROD/config/influx.toml \
    --env-file docker/.env.prod --label gpt41mini \
    --model openai/gpt-4.1-mini --provider openrouter
uv run python -m evals run --dataset oct15 --config $PROD/config/influx.toml \
    --env-file docker/.env.prod --label luna-none \
    --model openai/gpt-6-luna --provider openrouter --reasoning-effort none

# 3. Compare, with a judge for the un-keyed slots and the filter flips
uv run python -m evals compare --dataset oct15 --config $PROD/config/influx.toml \
    --env-file docker/.env.prod --baseline gpt41mini --candidate luna-none \
    --judge-model anthropic/claude-sonnet-4.6 --judge-provider openrouter
```

For an on-prem model add a provider block to the config the harness loads
(it never needs to be the live one) and point `--provider` at it:

```toml
[providers.local]
base_url = "http://gpu-box.lan:8000/v1"   # vLLM / llama.cpp / Ollama
api_key_env = ""                          # keyless
```

```bash
uv run python -m evals run --dataset oct15 --config evals/local.toml \
    --label qwen3-32b --model qwen3-32b --provider local --no-strict
```

`--no-strict` drops the OpenAI-only `json_schema` response format for
servers that reject it; the harness then validates the plain JSON against
the same Pydantic schemas production uses. Costs are unknown for a keyless
provider and reported as `n/a`.

## 5. Decision rule for a slot swap

A candidate replaces the baseline for a slot when, on the same dataset:

1. validity ≥ baseline (and ≥ 98% absolute);
2. **filter**: relevance-threshold agreement ≥ 90% and kappa ≥ 0.6, and the
   judge prefers the candidate on at least as many flips as the baseline;
3. **tier1 / tier3**: judge win rate ≥ loss rate, no structural regression
   (no new empty-list rate above 10%, arXiv-id rate in `builds_on` not
   lower by more than 10 points);
4. p95 latency within the slot's `request_timeout` with headroom (≤ 50%);
5. cost per case lower (the point of the exercise).

Anything short of that is a prompt-tuning or `reasoning_effort` iteration
(the guide's "tune and retest"), not a rollout.

## 6. Rollout

1. Merge the harness + `reasoning_effort` support (this branch).
2. Staging: set all three slots to `openai/gpt-6-luna`, `reasoning_effort =
   "none"`, keep `json_schema_strict = true`; rebuild (`./docker/run.sh
   staging up`) and run a manual run per profile; check `/status`, the run
   ledger and a sample of fresh notes.
3. Prod: same config edit; rebuild **outside** the 03:00–07:45 / 15:00–19:45
   UTC tick windows (a restart mid-stagger silently skips the remaining
   profiles). The code change needs the rebuild, not just `docker restart`.
4. Watch for a week: `filter_errors_total`, tier validate failures in the
   repair sweep, notes per profile vs the prior week, OpenRouter spend.
   Rollback is the one-line model change back to `openai/gpt-4.1-mini`
   (and dropping `reasoning_effort`).

## 7. Results (dataset `oct15`, 2026-10-15)

Dataset: 23 filter batches (230 candidates: live arXiv + RSS for the 8 prod
profiles), 32 Tier-1 cases sampled from those candidates, 40 Tier-3 cases
mined from prod notes with full text (4k–114k chars). Prompts, profiles and
thresholds are the live prod `influx.toml`. Judge: `anthropic/claude-sonnet-4.6`
via OpenRouter, both position orders, disagreement counted as a tie, the
*whole* source text supplied (a source over the judge's cap is excluded,
never clipped). Total spend for every run and the judge: about $12.

### 7.1 Validity, latency, cost

| slot | model | valid | salvaged | p50 s | p95 s | completion tok | cost / case |
|---|---|---:|---:|---:|---:|---:|---:|
| filter | gpt-4.1-mini | 100% | 0 | 3.9 | 5.7 | 8953 | $0.0022 |
| filter | gpt-6-luna (none, strict) | 100% | 0 | 2.0 | 3.4 | 3863 | $0.0006 |
| tier1 | gpt-4.1-mini | 100% | 0 | 2.1 | 2.7 | 6041 | $0.0008 |
| tier1 | gpt-6-luna (none, strict) | 100% | 1 | 2.8 | 4.0 | 4770 | $0.0001 |
| tier3 | gpt-4.1-mini | 100% | 0 | 6.1 | 11.8 | 33050 | $0.0050 |
| tier3 | gpt-6-luna (none, strict) | 100% | 5 | 13.2 | 19.0 | 51504 | $0.0007 |

Per call Luna costs 3.7x less on the filter, 6x less on Tier 1 and 7x less
on Tier 3. Tier 3 is slower (p50 13s vs 6s, worst case 44s) because Luna
writes ~55% more output; the extract slot's timeout goes to 120s.

### 7.2 Two Luna quirks, both handled in code

1. **Thinking aloud in `json_object` mode.** With `reasoning_effort: "none"`
   and plain JSON mode (the filter slot), Luna prefixed 5 of 23 batches with
   prose ("We need JSON only. Assess scores…") and once never got to the
   JSON. Strict structured outputs (`json_schema_strict = true`) removed it
   completely (23/23). The filter scorer now sends the `FilterResponse`
   schema when the slot opts in — a review of PR #309 caught that the
   first cut only did so in the harness, and a test now pins the
   production and eval request bodies to each other.
2. **Object, stray tokens, object again.** On long Tier-3 outputs Luna
   sometimes emits a complete object, then junk such as `ciao` or a leaked
   `assistant to=final` marker, then the object again. Strict mode does not
   prevent it (5/40). `parse_json_content` now decodes the first complete
   object and logs a WARNING with the discarded prefix/suffix sizes; with
   it every Luna call in the dataset validates.

### 7.3 Filter: Luna is stricter, and the judge says it is right

| | luna (none, strict) |
|---|---:|
| candidates compared | 230 |
| scored by both (≥ 6) | 43 |
| within ±1 of baseline | 70% |
| mean score baseline / Luna | 8.00 / 7.02 |
| decision agreement at relevance 7 | 77% (kappa 0.38) |
| baseline passes, Luna fails (at 7) | 49 |
| Luna passes, baseline fails (at 7) | 5 |

Agreement is well below the §5 bar, so the 53 flips were judged against
the profile descriptions: **Luna 33, 4.1-mini 8, ties 12**. The judge's
reasons are consistent: 4.1-mini scores 7–8 for items with no real tie to
the profile (an AlphaGo retrospective for ai-agents, a wearable-localization
paper for robotics, a blog post with an empty abstract) while Luna omits
them. Expect ingestion volume to drop noticeably after the switch; that is
the filter doing its job, not a regression. `low` reasoning on the filter
did not change the picture (kappa 0.41, +1.9k reasoning tokens, 2x latency)
so `none` stays.

> **Correction (2026-10-15, after the fact).** The judge took its flips at
> score 7 for every profile, and the report's "flips at threshold 6: 49"
> line used yet another cut; neither honoured `retro-computing`'s own
> `relevance = 6`. At each profile's own gate there are 55 flips, not 54:
> one judged pair was not an ingest flip and two real ones were never
> judged, all three `retro-computing` RSS items. The split above stands to
> within those three. `compare` now takes flips at each profile's gate for
> both the report and the judge.

> **Against human labels (2026-11-08).** On the 55 flips the operator's
> labels side with Luna 33 times and with 4.1-mini 22 times. Luna is right
> more often, but less decisively than the judge's 33–8 suggests, and its
> strictness costs recall: 18 of the items only 4.1-mini passed were ones
> the operator wanted. See §7.6.

### 7.4 Tier 1 / Tier 3: judge prefers Luna

| slot | Luna wins | baseline wins | ties | mean judge score baseline → Luna |
|---|---:|---:|---:|---|
| tier1 | 23 | 6 | 3 | 2.4 → 3.5 |
| tier3 | 40 | 0 | 0 | 2.7 → 4.7 |

Luna is more faithful and more specific (concrete numbers from the source,
no invented facts); where 4.1-mini won on Tier 1 it was for listing more
contributions (4.5 vs 2.4 on average) — Luna is terser. One structural
regression to watch: only 48% of Luna's `builds_on` entries carry an
`arXiv:<id>` (baseline 70%), because Luna lists more related work and
cites ids only where the text has them. Tier-3 `claims` drop from ~19 to
~14.5 items; the judge rated them tighter.

### 7.5 Decision

All three slots move to `openai/gpt-6-luna`, `reasoning_effort = "none"`,
`json_schema_strict = true` (filter included), extract `max_tokens = 8192`
and `request_timeout = 120`. The §5 filter-agreement bar was not met, but
it assumed the baseline was right; the judged flips say otherwise, so the
bar for the *next* migration is agreement with Luna.

### 7.6 Filter against human labels (2026-11-08)

The operator labelled all 230 `oct15` candidates with `evals label`, blind
(no model scores shown), on the pipeline's tiers: 165 skip, 58 ingest, 7
full text, 0 deep extract. Relevance is sparse and uneven: 4 of 30 for
ai-agents and 2 of 30 for knowledge-systems, against 15–16 of 30 for
ai-foundations and robotics. Feeds that publish their whole archive fill
some profiles' RSS pools with old, off-topic posts (#320).

**Ingest gate**, each profile at its own `relevance` (7; 6 for
retro-computing):

| run | precision | recall | F1 | kappa |
|---|---:|---:|---:|---:|
| gpt41mini | 49% | 58% | 0.54 | 0.33 |
| luna-strict (prod) | 65% | 34% | 0.44 | 0.31 |
| luna-none | 60% | 32% | 0.42 | 0.28 |
| luna-low-filter | 57% | 32% | 0.41 | 0.26 |

Luna lets in less noise but finds only a third of what the operator
wanted. The three Luna variants differ by two or three items, which is
noise at 65 positives.

**The flips.** Of the 49 items 4.1-mini passed and Luna rejected, 31 were
noise and 18 were wanted. Of the 6 Luna passed alone, 2 were wanted. On
the 53 judged flips the judge agreed with the labels 32 times, disagreed 9
times and tied 12 times, so it matched the labels on 78% of its non-tie
calls. Eight of its nine disagreements backed Luna: six where Luna had
rejected an item the operator wanted, and two where it had let in noise.
The judge is a usable proxy, but it leans towards Luna.

**Where Luna misses.** Of luna-strict's 43 missed items, 34 were omitted
(scored below `min_score_in_results = 6`) and 9 scored 6. About half are in
two profiles: ai-foundations (1 of 15 found) and morrow (0 of 7).
- Luna gives the descriptions' MEDIUM-interest topics a 6, which falls
  below relevance 7. In ai-foundations it scored all three
  computational-neuroscience papers the operator wanted a 6; 4.1-mini
  scored two of them 7–8.
- About half of the ai-foundations positives fall outside its description,
  such as essays and statistics/methods papers. The description excludes
  "engineering-focused ML papers without theoretical novelty", and Luna
  applies that.
- morrow is meant to be restrictive. After reviewing Luna's misses, the
  operator relabelled 6 of its 7 positives as skip, so morrow's labels are
  no longer blind. With that revision the mix is 171 / 52 / 7 / 0.

**Higher gates.** Seven items were labelled full text. luna-strict scored
none of them 8 or more, and promoted 13 items labelled lower; 4.1-mini
found 2 and promoted 39. Nothing was labelled deep extract, while Luna
promoted 4 items to 9 or more and 4.1-mini 15. There are too few positives
to judge either gate.

**Decision: prod `relevance` lowered from 7 to 6 on every profile.**
retro-computing was already at 6. The filter prompt already returns every
item scoring 6 or more, so the effect could be measured from the stored
replies without a re-run. With the revised labels:

| run | relevance | precision | recall | F1 |
|---|---:|---:|---:|---:|
| gpt41mini | 7 | 47% | 61% | 0.53 |
| luna-strict | 7 | 65% | 37% | 0.47 |
| luna-strict | 6 | 57% | 51% | 0.54 |

At 6, Luna ingests about 55% more items than at 7: 53 of the 230
candidates instead of 34. The threshold was chosen on the same set it is
graded on, so these figures flatter it. A fresh, blind-labelled dataset is
the real test (§8).

## 8. Later

- Record `usage` (tokens, cost) per production call in telemetry so the
  weekly spend per slot is visible without the harness.
- Tune `reasoning_effort` per slot if Tier 3 benefits from `low`.
- ~~Grow the dataset: add a human-labelled filter set (operator-scored
  candidates) so the filter has a key, not just a baseline.~~ Shipped:
  `evals label` collects blind labels on the pipeline's own tiers (0 skip,
  1 ingest, 2 full text, 3 deep extract) into `datasets/<name>/labels.jsonl`;
  `evals grade` (and `compare`, whenever labels exist) reports per-gate
  precision/recall/kappa against them and checks the judge's filter-flip
  verdicts against the labels. `oct15` was labelled on 2026-11-08 (§7.6).
- Re-test relevance 6 on a fresh, blind-labelled dataset, no earlier than
  2026-11-29. Build it with `--batches-per-profile 3` so the full-text and
  deep-extract gates have some positives.
- Decide whether to widen the ai-foundations description to cover what the
  operator actually labelled relevant (§7.6), or to keep the narrower scope.
- For the next filter migration, grade against labels first
  (`docs/operations/model-evals.md` §5). This replaces §7.5's "agreement
  with Luna" bar. The judge agreed with the labels on 78% of its decisive
  filter calls, and leaned towards Luna.
- Filter spend: every scheduled run re-scores each RSS feed's whole archive
  (#320).
