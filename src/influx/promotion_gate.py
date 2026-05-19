"""Promotion gate evaluating recent run health (issue #165).

Builds on the :func:`influx.run_ledger.classify_degradation_severity`
split from #164 to produce a single pass/fail signal a promotion
script can consume:

* **Hard fail** on any ``unexpected_failure`` run in the recent
  window — write-time data integrity, scorer execution failures,
  stall ratchets, etc.  These are always actionable regressions.
* **Soft fail** when the ratio of ``expected_lossy`` runs in the
  window exceeds a configured threshold — tolerated upstream noise
  is fine in small doses but becomes a quality signal if it
  dominates.
* **Pass** otherwise, with a human-readable summary listing the
  severity breakdown and the top driver reasons / domains so the
  operator sees *why* the gate landed where it did even on success.

Pure module — no IO, no Lithos client.  The CLI subcommand in
``scripts/influx-diagnose.py`` reads ``runs.jsonl`` and passes the
entries in; tests pass synthetic entries directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from influx.run_ledger import classify_degradation_severity

__all__ = [
    "PromotionGateConfig",
    "PromotionGateResult",
    "evaluate_promotion_gate",
    "format_gate_summary",
]


@dataclass(frozen=True, slots=True)
class PromotionGateConfig:
    """Tunables for :func:`evaluate_promotion_gate`.

    Attributes
    ----------
    window_runs:
        Maximum number of recent scheduled completed runs to
        evaluate.  Runs older than this are ignored.  Defaults to
        20 — a roughly-day-long window at the typical hourly cadence.
    max_expected_lossy_ratio:
        Hard ceiling on the ratio of ``expected_lossy`` runs in the
        window.  Above this the gate fails with reason
        ``"expected_lossy_above_threshold"`` even when every run is
        below ``unexpected_failure``.  Defaults to 0.5 (half the
        window).  Set to 1.0 to disable the ratio check entirely.
    min_runs_required:
        Refuse to evaluate when fewer than this many scheduled
        completed runs are present in the window.  Prevents
        spurious pass / fail on a freshly-started deployment.
        Defaults to 5.
    """

    window_runs: int = 20
    max_expected_lossy_ratio: float = 0.5
    min_runs_required: int = 5


@dataclass(frozen=True, slots=True)
class PromotionGateResult:
    """Outcome of :func:`evaluate_promotion_gate`.

    Attributes
    ----------
    passed:
        Overall verdict — ``True`` only when no unexpected failures
        landed in the window AND the expected-lossy ratio is at or
        below the configured threshold.
    reason:
        Stable machine-readable code: ``"ok"``,
        ``"insufficient_runs"``, ``"unexpected_failure_present"``,
        ``"expected_lossy_above_threshold"``.  Callers can branch
        on this without parsing the human-readable summary.
    runs_evaluated:
        How many scheduled completed runs the gate actually
        inspected (clamped to ``config.window_runs``).
    severity_counts:
        ``{"success": N, "expected_lossy": N, "unexpected_failure": N}``
        — exhaustive bucket counts for the inspected runs.
    expected_lossy_ratio:
        ``expected_lossy_count / runs_evaluated`` — ``0.0`` when
        ``runs_evaluated == 0``.
    unexpected_failure_runs:
        The actual ledger entries that triggered the hard fail (or
        empty when ``"unexpected_failure" not in reason``).  Surfaced
        so the operator can drill into the offending run_ids without
        a second ledger fetch.
    top_drivers:
        ``{"reasons": {reason: count}, "profiles": {profile: count}}``
        breakdown across all evaluated runs.  Helps the operator
        spot whether a single profile / a single reason dominates.
    """

    passed: bool
    reason: str
    runs_evaluated: int
    severity_counts: dict[str, int]
    expected_lossy_ratio: float
    unexpected_failure_runs: list[dict[str, Any]] = field(default_factory=list)
    top_drivers: dict[str, dict[str, int]] = field(default_factory=dict)


def evaluate_promotion_gate(
    entries: list[dict[str, Any]],
    config: PromotionGateConfig | None = None,
) -> PromotionGateResult:
    """Evaluate the promotion gate against *entries* (newest first).

    *entries* is the raw shape returned by
    :meth:`influx.run_ledger.RunLedger.recent` — the gate filters
    to ``status=="completed"`` AND ``kind=="scheduled"`` runs only
    (skipped / failed / manual / backfill runs are excluded because
    their semantics don't fit the steady-state quality signal we are
    gating on).

    Pure function — order-sensitive on the inspected window (the
    "recent" semantics come from the caller passing entries
    newest-first), otherwise stateless.
    """
    cfg = config if config is not None else PromotionGateConfig()
    severity_counts = {
        "success": 0,
        "expected_lossy": 0,
        "unexpected_failure": 0,
    }
    inspected: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    profile_counts: dict[str, int] = {}

    for entry in entries:
        if len(inspected) >= cfg.window_runs:
            break
        if entry.get("status") != "completed":
            continue
        if entry.get("kind") != "scheduled":
            continue
        inspected.append(entry)
        # Prefer the persisted severity field (issue #164); fall
        # back to recomputing from the reason list for entries
        # written before #164 landed.
        severity = entry.get("degradation_severity")
        if severity not in severity_counts:
            severity = classify_degradation_severity(
                [
                    str(r)
                    for r in (entry.get("degraded_reasons") or [])
                    if isinstance(r, str)
                ]
            )
        severity_counts[severity] += 1
        if severity == "unexpected_failure":
            unexpected.append(entry)
        profile = entry.get("profile")
        if isinstance(profile, str) and profile:
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
        for reason in entry.get("degraded_reasons") or []:
            if isinstance(reason, str):
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

    runs_evaluated = len(inspected)
    expected_lossy_ratio = (
        severity_counts["expected_lossy"] / runs_evaluated
        if runs_evaluated > 0
        else 0.0
    )
    top_drivers = {
        "reasons": reason_counts,
        "profiles": profile_counts,
    }

    if runs_evaluated < cfg.min_runs_required:
        return PromotionGateResult(
            passed=False,
            reason="insufficient_runs",
            runs_evaluated=runs_evaluated,
            severity_counts=severity_counts,
            expected_lossy_ratio=expected_lossy_ratio,
            top_drivers=top_drivers,
        )

    if unexpected:
        return PromotionGateResult(
            passed=False,
            reason="unexpected_failure_present",
            runs_evaluated=runs_evaluated,
            severity_counts=severity_counts,
            expected_lossy_ratio=expected_lossy_ratio,
            unexpected_failure_runs=unexpected,
            top_drivers=top_drivers,
        )

    if expected_lossy_ratio > cfg.max_expected_lossy_ratio:
        return PromotionGateResult(
            passed=False,
            reason="expected_lossy_above_threshold",
            runs_evaluated=runs_evaluated,
            severity_counts=severity_counts,
            expected_lossy_ratio=expected_lossy_ratio,
            top_drivers=top_drivers,
        )

    return PromotionGateResult(
        passed=True,
        reason="ok",
        runs_evaluated=runs_evaluated,
        severity_counts=severity_counts,
        expected_lossy_ratio=expected_lossy_ratio,
        top_drivers=top_drivers,
    )


def format_gate_summary(
    result: PromotionGateResult,
    config: PromotionGateConfig | None = None,
) -> str:
    """Render a human-readable promotion-gate summary.

    Stable, line-oriented so operators can ``grep`` / ``less``.
    Includes the verdict, the severity counts, the expected-lossy
    ratio (relative to the configured threshold), the top driver
    reasons / profiles, and — on a hard fail — the specific failing
    run_ids so the operator has a direct drill-in target.
    """
    cfg = config if config is not None else PromotionGateConfig()
    verdict = "PASS" if result.passed else "FAIL"
    lines: list[str] = []
    lines.append(f"Promotion gate: {verdict}")
    lines.append(f"  reason          : {result.reason}")
    lines.append(
        f"  runs_evaluated  : {result.runs_evaluated} "
        f"(window={cfg.window_runs}, min_required={cfg.min_runs_required})"
    )
    counts = result.severity_counts
    lines.append(
        "  severity_counts : "
        f"success={counts.get('success', 0)} "
        f"expected_lossy={counts.get('expected_lossy', 0)} "
        f"unexpected_failure={counts.get('unexpected_failure', 0)}"
    )
    lines.append(
        "  expected_lossy  : "
        f"ratio={result.expected_lossy_ratio:.2f} "
        f"(max={cfg.max_expected_lossy_ratio:.2f})"
    )

    reasons = result.top_drivers.get("reasons") or {}
    if reasons:
        ranked = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        formatted = " ".join(f"{r}={c}" for r, c in ranked[:5])
        lines.append(f"  top reasons     : {formatted}")

    profiles = result.top_drivers.get("profiles") or {}
    if profiles:
        ranked = sorted(profiles.items(), key=lambda kv: (-kv[1], kv[0]))
        formatted = " ".join(f"{p}={c}" for p, c in ranked[:5])
        lines.append(f"  profiles        : {formatted}")

    if result.unexpected_failure_runs:
        lines.append("")
        lines.append("Unexpected-failure runs (most recent first):")
        for entry in result.unexpected_failure_runs:
            run_id = entry.get("run_id", "?")
            profile = entry.get("profile", "?")
            reasons_list = entry.get("degraded_reasons") or []
            reasons_str = ",".join(str(r) for r in reasons_list) or "<none>"
            lines.append(f"  run_id={run_id} profile={profile} reasons={reasons_str}")

    return "\n".join(lines) + "\n"
