# Influx Agent Instructions

## Architecture guardrails & generated docs

`docs/generated/` holds generated views of the code — component diagram, domain
model, architecture metrics, and per-component drill-down pages (indexed by
`docs/generated/README.md`) — produced by `tests/guardrail/` (renamed from
`tests/meta/`) and drift-checked in CI:

- `make diagrams` regenerates everything (it just runs `pytest tests/guardrail/ -q`).
  Note `make test` runs the same tests, so a test run rewrites `docs/generated/`
  as a side effect — commit the result if it changed.
- The CI job `diagrams` fails when the committed files disagree with what the
  code generates. Fix: `make diagrams`, commit.
- `docs/architecture.toml` is the source of truth for components, tiers,
  domain-model scanning, and the accepted metric budgets. Adding a new module,
  component, or model? The guardrail orphan/completeness checks fail until you map
  it there.
- Directional import rules (Entrypoints → Core → Foundation) are enforced by
  import-linter (`pyproject.toml [tool.importlinter]`, checked by
  `test_layering_contract.py`); `tests/guardrail/` also carries the repo-specific
  `test_no_stubs.py` and `test_v1_gate.py` guards.
- This is the portable "diagrams as tests" kit; `tests/guardrail/AGENTS.md` has the
  generator contracts. The kit's optional tool-catalog and container adapters are
  not enabled here (influx is an MCP client to Lithos; its stores are file-based
  with no central store-config to anchor a container view to).

## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues for `agent-lore/influx`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default Matt Pocock skills triage label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo. Read root `CONTEXT.md` and `docs/adr/` if present, plus `docs/SPECIFICATION.md` as the current source of truth. See `docs/agents/domain.md`.

## Guardrail policy

Guardrails support maintainability; passing a metric alone is not evidence of
good design. Dependency restrictions, cycle limits, and private-access contracts
are architectural invariants. Size, complexity, and dependency counts are review
thresholds: do not split cohesive code or add forwarding layers just to meet a
number. Refactor when a clean seam improves clarity, cohesion, or testability.

`[policies]` classifies the accepted budgets; `[review]` holds per-entity thresholds
and optional new/worsened-code gates. If keeping code together is clearer, record
a proportionate, bounded `[[exceptions]]` entry with a reason and review reference,
or explain a threshold adjustment in the same PR. Never automatically raise
budgets or bypass an invariant. Lower thresholds when that protects useful gains.
`enforce_new` is on: the `diagrams` CI job fails a PR that adds a function or
module over a `[review]` threshold, or pushes an existing one across it, unless
it carries an exception. `enforce_worsened` is off, so growth in code that is
already over a threshold is reported in the job summary but does not fail the PR.
Run `make metrics-diff` to see the comparison locally.
See `docs/quality-guide.md` for the migration and evidence contracts.
