# PRD 10 — OTEL Telemetry + Rejection-Rate Logging + Final Polish

**Part of:** Influx v1 (see `tasks/prd-influx-v1-index.md`)
**Covers master PRD stories:** S-14 + S-15
**Milestone:** M4
**Prerequisites:** PRD 01, 02, 03, 04, 05, 06, 07, 08, 09
**Downstream PRDs:** None. This is the last PRD; v1 tags after it.

---

## 1. Context

With PRD 09 the v1 product is functionally complete. This PRD adds
observability polish and cleans up any remaining seams. Two independent
strands:

1. **OTEL telemetry (S-14).** Opt-in, additive, optional-dependency.
   The service MUST run identically with or without OTEL installed and
   enabled. Key spans and attributes per FR-OBS-4 are emitted when
   enabled.
2. **Rejection-rate logging + stub removal (S-15).** Per-tag rejection
   rates logged every `feedback.recalibrate_after_runs` runs. Remove
   any remaining internal seams or temporary stubs introduced during
   earlier PRDs (§2.1 of master PRD). Final sweep of AC-X-1 / AC-X-2 /
   AC-X-6 across the whole codebase.

## 2. In scope

### 2.1 OTEL

- `src/influx/telemetry.py` implements span creation + attribute
  setting behind a thin wrapper. When OTEL optional packages are not
  installed, the wrapper is a no-op.
- Optional dependency group in `pyproject.toml`:
  `[project.optional-dependencies] otel = [...]`.
- Env-driven enablement: `INFLUX_OTEL_ENABLED=true` (default false).
- `INFLUX_OTEL_CONSOLE_FALLBACK=true` prints spans to stdout when no
  collector is configured.
- Key spans: `influx.run`, `influx.fetch.arxiv`, `influx.fetch.rss`,
  `influx.filter`, `influx.enrich.tier1`, `influx.enrich.tier2`,
  `influx.enrich.tier3`, `influx.lithos.write`,
  `influx.lithos.retrieve`, `influx.archive.download`.
- Attributes: `influx.profile`, `influx.run_id`, `influx.run_type`,
  `influx.source`, `influx.item_count`.

### 2.2 Rejection-rate logging

- Per-tag rejection rates logged every
  `feedback.recalibrate_after_runs` runs per profile. The metric is
  "proportion of filter results carrying each tag that were
  subsequently rejected by the user via `influx:rejected:<profile>`".
- A small in-memory state, keyed by profile, counts runs since the
  last recalibration log. Not persisted — a restart resets the
  counter; this is acceptable (FR-OBS-5 is informative only).

### 2.3 Final polish

- Remove any remaining `stub`/`no-op`/temporary-seam comments or
  modules introduced in earlier PRDs.
- Final assertion of AC-X-1 (no hardcoded constants outside config
  parsing), AC-X-2 (provider swap works without code change), AC-X-6
  (coverage thresholds met across pure modules).
- Re-read every PRD's "Internal seams permitted" section and verify
  each seam is either explicitly owned by this PRD's scope or has
  already been replaced by an earlier PRD.

## 3. Out of scope

- No new functional behaviour. No new tool calls. No new Lithos tags.

## 4. Internal seams permitted

- None. This PRD is the "stubs must be gone" gate. If any seam
  remains, either this PRD must remove it or the PRD's author must
  document in the master PRD why it legitimately stays.

## 5. Functional Requirements (master PRD §6.14 + §6.15-adjacent)

### 5.1 Logging

- **FR-OBS-1.** Logging is JSON to stdout only. No log files.
  `INFLUX_LOG_LEVEL` controls verbosity. (Already established by
  earlier PRDs; assert still holding.)

### 5.2 OTEL

- **FR-OBS-2.** OTEL is opt-in (`INFLUX_OTEL_ENABLED=true`), additive,
  and installed via optional packages. When disabled the service has
  identical behaviour but no span export.
- **FR-OBS-3.** `INFLUX_OTEL_CONSOLE_FALLBACK=true` prints spans to
  stdout when no collector is configured.
- **FR-OBS-4.** Key spans: `influx.run`, `influx.fetch.arxiv`,
  `influx.fetch.rss`, `influx.filter`, `influx.enrich.tier1`,
  `influx.enrich.tier2`, `influx.enrich.tier3`, `influx.lithos.write`,
  `influx.lithos.retrieve`, `influx.archive.download`. Attributes:
  `influx.profile`, `influx.run_id`, `influx.run_type`,
  `influx.source`, `influx.item_count`.

### 5.3 Rejection-rate logging

- **FR-OBS-5.** Tag rejection rates are logged every
  `feedback.recalibrate_after_runs` runs.

## 6. Files to create / modify

### Create
- `src/influx/telemetry.py` — no-op by default; real when enabled.
- `tests/unit/test_telemetry.py` — assert no-op behaviour with OTEL
  packages absent (simulated) and span-emission behaviour when
  enabled.
- `tests/integration/test_otel_spans.py` — emits spans to an
  in-memory OTEL exporter and asserts the attribute set.
- `tests/unit/test_rejection_rate_logging.py`

### Modify
- `pyproject.toml` — add `[project.optional-dependencies] otel = [...]`.
- `src/influx/service.py`, `sources/arxiv.py`, `sources/rss.py`,
  `filter.py`, `enrich.py`, `storage.py`, `lithos_client.py`,
  `lcma.py` — wrap key operations in telemetry spans.
- Various modules — remove any remaining `# TODO(PRD-NN)` / stub
  comments that survived.

## 7. Dependencies to add (optional group)

| Purpose | Package |
|---|---|
| OTEL core | `opentelemetry-api`, `opentelemetry-sdk` |
| OTEL OTLP exporter | `opentelemetry-exporter-otlp-proto-http` |

All under the `otel` optional-dependencies group. Base install does not
require them.

## 8. Acceptance Criteria

### From master PRD §7.4

- **AC-M4-1.** With `INFLUX_OTEL_ENABLED=true` and a local collector,
  all spans from FR-OBS-4 are emitted with the documented attributes.
- **AC-M4-2.** With OTEL disabled, the service operates identically
  to M3.
- **AC-M4-3.** With OTEL optional packages uninstalled and OTEL
  disabled, the service starts, serves requests, and completes a
  run. (Test this by running in a virtualenv without the `otel`
  extras; if that is not feasible in CI, simulate the
  `ImportError` via an import-stub in the test.)
- **AC-M4-4.** Per-run logs include filtered/ingested counts per
  profile.
- **AC-M4-5.** Every `feedback.recalibrate_after_runs` runs the
  service logs per-tag rejection rates for the profile.

### From master PRD §7.5

- **AC-X-1** (final). All tunable values from the requirements can be
  set in `influx.toml` and take effect on restart. No hardcoded
  constants are used in filtering, enrichment, download, resilience,
  or extraction code paths. Pure defaults live in config-parsing
  code only. A search of the codebase (grep / linter) must confirm
  no stray constants exist.
- **AC-X-2** (final). Replacing `[models.extract]` `provider` from
  `anthropic` to `openrouter` (with a corresponding
  `[providers.openrouter]` block) redirects deep-extraction traffic
  without code changes. Test by running the integration suite with
  the alternate provider block.
- **AC-X-6** (final). Coverage of pure modules (config, URL, path,
  schemas, prompts, slug) is ≥ 80%. Every Lithos tool called by
  Influx has a happy-path and an error-envelope contract test.

### New for this PRD

- **AC-10-A.** With OTEL disabled, `influx.run` and friends are
  no-ops: no spans are created, no attribute-setting calls are
  made. The wrapper's overhead is a dictionary lookup / attribute
  access, not object instantiation.
- **AC-10-B.** With OTEL enabled + console fallback, running
  `POST /runs` produces at least one `influx.run` span + one of
  each fetch / filter / enrich / write span, each with the
  documented attributes.
- **AC-10-C.** Every PRD 01-09 "seams permitted" item is resolved:
  a checklist in this PRD's completion notes enumerates them and
  confirms each is gone.
- **AC-10-D.** Rejection-rate log emission: a seeded state with
  `feedback.recalibrate_after_runs = 3` emits the rejection-rate
  log line on the third, sixth, ninth … runs, and NOT on other
  runs.

## 9. Tests required

- OTEL wrapper unit tests for both enabled and disabled paths.
- Integration test emitting spans to an in-memory exporter; assert
  the full set of spans from FR-OBS-4 and the attribute set on
  each.
- Rejection-rate logging cadence unit test.
- Seam removal: a dedicated search-style test (or manual review
  checklist embedded as `tests/meta/test_no_stubs.py`) that asserts
  the codebase contains no `TODO(PRD-` or `# STUB:` markers left
  behind.
- Coverage target ≥ 80% across the full pure-module list from
  master PRD §18.2. This is the final gate.

## 10. Definition of Done

- [ ] All AC-M4-1…5, AC-X-1, AC-X-2, AC-X-6, AC-10-A…D satisfied.
- [ ] OTEL optional-extras group works (`uv pip install .[otel]`
      installs cleanly).
- [ ] No `TODO(PRD-...)`, `STUB:`, or other seam markers remain.
- [ ] `docker/Dockerfile` and `docker/docker-compose.yml` updated
      per master PRD §9.4.4 (if not already done in earlier PRDs):
      `/archive` mount, port 8080 exposed, `CMD ["influx", "serve"]`.
- [ ] Full contract + integration suite green against a local fake
      Lithos + fake webhook + fake LLM provider.
- [ ] `python -m influx validate-config` against `influx.example.toml`
      succeeds with the full v0.7 validation pipeline (config
      schema + prompt vars + JSON-mode + SSE dry-connect).
- [ ] **v1 tag candidate.** With this PRD complete, the
      implementation satisfies every FR-* in master PRD §6 and
      every AC-* in master PRD §7.
- [ ] Ruff, pyright, pytest all green. Coverage ≥ 80% on pure
      modules.
