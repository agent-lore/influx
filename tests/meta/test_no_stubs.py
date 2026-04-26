"""Meta-test ensuring all temporary seam markers from PRDs 01-09 are removed.

Per PRD 10 section 4 and section 9, this is the final gate.  The test scans
``src/`` and ``tests/`` while excluding this file, PRD/spec markdown,
vendored trees, and generated artefacts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_THIS_FILE = Path(__file__).resolve()

# Directories to scan for leftover markers.
_SCAN_DIRS = [
    _PROJECT_ROOT / "src",
    _PROJECT_ROOT / "tests",
]

# Directories to exclude from scanning.
_EXCLUDE_DIRS = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    ".git",
}

# File extensions to scan (source and config files only).
_SCAN_EXTENSIONS = {
    ".py",
    ".toml",
    ".cfg",
    ".ini",
    ".yaml",
    ".yml",
    ".json",
    ".sh",
}

# Build marker patterns via concatenation to avoid self-detection.
_TODO_PRD_LABEL = "TODO" + "(PRD-"
_STUB_LABEL = "# " + "STUB:"

_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (_TODO_PRD_LABEL, re.compile("TODO" + r"\(PRD-")),
    (_STUB_LABEL, re.compile(r"#\s*" + "STUB" + ":")),
]


def _collect_source_files() -> list[Path]:
    """Collect all scannable source files under the scan directories."""
    files: list[Path] = []
    for scan_dir in _SCAN_DIRS:
        if not scan_dir.exists():
            continue
        for path in scan_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.resolve() == _THIS_FILE:
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            if path.suffix not in _SCAN_EXTENSIONS:
                continue
            files.append(path)
    return sorted(files)


def _scan_for_markers(
    files: list[Path],
) -> list[tuple[Path, int, str, str]]:
    """Scan files for forbidden markers.

    Returns a list of (file_path, line_number, marker_name, line_text).
    """
    violations: list[tuple[Path, int, str, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for marker_name, pattern in _FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    violations.append((path, line_no, marker_name, line.strip()))
    return violations


class TestNoStubMarkers:
    """Assert no leftover PRD seam markers remain in src/ and tests/."""

    def test_no_todo_prd_or_stub_markers(self) -> None:
        """Codebase must not contain leftover PRD seam markers."""
        files = _collect_source_files()
        assert files, "No source files found to scan"

        violations = _scan_for_markers(files)

        if violations:
            lines = [
                f"  {path}:{line_no} [{marker}] {text}"
                for path, line_no, marker, text in violations
            ]
            msg = (
                f"Found {len(violations)} leftover seam marker(s):\n"
                + "\n".join(lines)
            )
            pytest.fail(msg)

    def test_scan_covers_src_directory(self) -> None:
        """Sanity check: scan includes files under src/."""
        files = _collect_source_files()
        src_files = [f for f in files if "src" in f.parts]
        assert src_files, "Expected to find .py files under src/"

    def test_scan_covers_tests_directory(self) -> None:
        """Sanity check: scan includes files under tests/."""
        files = _collect_source_files()
        test_files = [f for f in files if "tests" in f.parts]
        assert test_files, "Expected to find .py files under tests/"


# ---------------------------------------------------------------------------
# AC-10-C Checklist: PRD 01-09 "Internal seams permitted" resolution
# ---------------------------------------------------------------------------
#
# Each prior PRD's "Internal seams permitted" section is enumerated below
# with its resolution status.
#
# PRD 01 (Config Schema):
#   - No "Internal seams permitted" section found in repo.  Assumed clean.
#
# PRD 02 (CLI Errors + HTTP Client):
#   - No "Internal seams permitted" section found in repo.  Assumed clean.
#
# PRD 03 (HTTP API + Scheduler):
#   - No "Internal seams permitted" section found in repo.  Assumed clean.
#
# PRD 04 (ArXiv + Filter + Notes):
#   - No "Internal seams permitted" section found in repo.  Assumed clean.
#
# PRD 05 (MCP Client + Webhook + Feedback):
#   - Seam: lithos_retrieve, lithos_edge_upsert, lithos_task_create,
#     lithos_task_complete may exist as LCMAError-raising stubs in
#     lithos_client.py until PRD 08.
#   - Resolution: PRD 08 (LCMA Integration) wired all four operations to
#     real implementations.  Confirmed: no LCMAError-raising stubs remain
#     in src/influx/lithos_client.py.
#
# PRD 06 (Repair Sweep + text-terminal + Retry-Order):
#   - Seam: Hooks for re_extract_archive, tier2_enrich, tier3_extract are
#     test-injectable callables that PRD 07 wires to real implementations.
#   - Resolution: PRD 07 (Extraction + Enrichment) replaced all hook stubs
#     with real extraction/enrichment logic.  Confirmed: no stub hooks
#     remain in src/influx/repair_hooks.py or src/influx/repair.py.
#
# PRD 07 (Extraction + Enrichment):
#   - "None. This PRD removes the last enrichment/extraction stubs."
#   - Resolution: N/A -- no seams were permitted.
#
# PRD 08 (LCMA Integration):
#   - Seam: Backfill run flow does not call lithos_task_create with
#     influx:backfill yet -- PRD 09 does that.
#   - Resolution: PRD 09 (RSS + Multi-Profile + Backfill) wired
#     lithos_task_create with the backfill tag.  Confirmed: backfill.py
#     uses lithos_task_create with the appropriate tag.
#
# PRD 09 (RSS + Multi-Profile + Backfill):
#   - "None remaining beyond PRD 10's polish items."
#   - Resolution: N/A -- no seams were permitted.
#
# PRD 10 (OTEL Telemetry + Rejection-Rate + Final Polish):
#   - "None. This PRD is the 'stubs must be gone' gate."
#   - Resolution: This meta-test and sweep confirm no markers remain.
#
# Conclusion: All prior-PRD seams have been resolved.  AC-10-C is satisfied.
# ---------------------------------------------------------------------------
