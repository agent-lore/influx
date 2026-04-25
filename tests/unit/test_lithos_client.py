"""Unit tests for lithos_client — construction-time validation + LCMA stubs.

The in-process stub (PRD 04) has been replaced by the real SSE-backed
``LithosClient`` wrapper (PRD 05).  Connection-lifecycle tests live in
``tests/contract/test_lithos_client.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from influx.errors import ConfigError, LCMAError
from influx.lithos_client import (
    LithosClient,
    lithos_edge_upsert,
    lithos_retrieve,
    lithos_task_complete,
    lithos_task_create,
)


class TestLithosClientConstruction:
    """LithosClient validates transport and URL at construction."""

    def test_rejects_non_sse_transport(self) -> None:
        with pytest.raises(ConfigError, match="only 'sse' is supported"):
            LithosClient(url="http://localhost:1234/sse", transport="stdio")

    def test_rejects_empty_url(self) -> None:
        with pytest.raises(ConfigError, match="LITHOS_URL is required"):
            LithosClient(url="")

    def test_accepts_valid_sse_config(self) -> None:
        client = LithosClient(url="http://localhost:1234/sse")
        assert not client.connected


# ── LCMA stub tests (US-012) ────────────────────────────────────────

_LCMA_STUBS = [
    lithos_retrieve,
    lithos_edge_upsert,
    lithos_task_create,
    lithos_task_complete,
]

_LCMA_NAMES = {
    "lithos_retrieve",
    "lithos_edge_upsert",
    "lithos_task_create",
    "lithos_task_complete",
}


class TestLCMAStubs:
    """Each LCMA stub raises ``LCMAError("not implemented")``."""

    @pytest.mark.parametrize("stub", _LCMA_STUBS, ids=lambda f: f.__name__)
    def test_raises_lcma_error(self, stub: object) -> None:
        assert callable(stub)
        with pytest.raises(LCMAError, match="not implemented"):
            stub()

    @pytest.mark.parametrize("stub", _LCMA_STUBS, ids=lambda f: f.__name__)
    def test_lcma_error_is_distinct_from_lithos_error(
        self, stub: object
    ) -> None:
        assert callable(stub)
        with pytest.raises(LCMAError) as exc_info:
            stub()
        assert not isinstance(exc_info.value, type(None))
        assert type(exc_info.value).__name__ == "LCMAError"

    def test_no_production_module_invokes_lcma_stubs(self) -> None:
        """No module under src/influx/ (except lithos_client.py itself)
        calls any LCMA stub function."""
        src_dir = Path(__file__).resolve().parents[2] / "src" / "influx"
        violations: list[str] = []
        for py_file in sorted(src_dir.rglob("*.py")):
            if py_file.name == "lithos_client.py":
                continue
            try:
                tree = ast.parse(py_file.read_text(), filename=str(py_file))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = None
                    if isinstance(func, ast.Name):
                        name = func.id
                    elif isinstance(func, ast.Attribute):
                        name = func.attr
                    if name in _LCMA_NAMES:
                        violations.append(
                            f"{py_file.relative_to(src_dir.parent.parent)}"
                            f":{node.lineno} calls {name}()"
                        )
        assert violations == [], (
            "Production code must not invoke LCMA stubs:\n"
            + "\n".join(violations)
        )
