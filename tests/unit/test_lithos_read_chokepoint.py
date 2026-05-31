"""Meta-test: ``read_note`` is the sole consumer of the ``lithos_read`` tool.

Issue #187 was caused by reading note fields at the top level of a
``lithos_read`` response when Lithos nests them under ``metadata``. PR #188
fixed it by normalising the envelope exactly once, inside
``LithosClient.read_note`` (``_normalise_read_envelope``). That fix only holds
while *every* read flows through ``read_note``: any other code that calls
``call_tool("lithos_read", …)`` directly would bypass normalisation and
silently reintroduce the whole #187 bug class.

This scans the ``influx`` package source for ``call_tool("lithos_read", …)``
call expressions and asserts the only one lives in ``read_note``. It parses
with :mod:`ast` rather than grepping text so it matches genuine calls only —
the chokepoint comment in ``read_note`` (which mentions the tool name) and any
docstrings are ignored, and quote-style/formatting changes can't fool it.
Mirrors the source-scan meta-test in ``test_run_ledger.py``
(``TestKnownDegradedReasonsCoverage``).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from influx import lithos_client

# Directory of the installed ``influx`` package (= ``src/influx``); scanning
# from the imported module keeps the test correct regardless of CWD/layout.
_SRC_DIR = Path(inspect.getfile(lithos_client)).parent

# The tool that must only ever be invoked from ``read_note``.
_GUARDED_TOOL = "lithos_read"
# The one function permitted to invoke it, and the module it lives in.
_ALLOWED_FUNCTION = "read_note"
_ALLOWED_MODULE = "lithos_client.py"


def _is_call_tool_with_literal(node: ast.Call, literal: str) -> bool:
    """True if *node* is ``<obj>.call_tool("<literal>", …)``."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "call_tool":
        return False
    if not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Constant) and first.value == literal


class _CallToolVisitor(ast.NodeVisitor):
    """Record ``call_tool("lithos_read", …)`` calls with their enclosing def."""

    def __init__(self, module: str) -> None:
        self._module = module
        self._func_stack: list[str] = []
        # (module filename, enclosing function name, line number)
        self.hits: list[tuple[str, str, int]] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        if _is_call_tool_with_literal(node, _GUARDED_TOOL):
            enclosing = self._func_stack[-1] if self._func_stack else "<module>"
            self.hits.append((self._module, enclosing, node.lineno))
        self.generic_visit(node)


def _scan_lithos_read_callers() -> list[tuple[str, str, int]]:
    """Scan ``src/influx`` for ``call_tool("lithos_read", …)`` call sites."""
    hits: list[tuple[str, str, int]] = []
    for path in sorted(_SRC_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _CallToolVisitor(path.name)
        visitor.visit(tree)
        hits.extend(visitor.hits)
    return hits


class TestLithosReadChokepoint:
    """``call_tool("lithos_read", …)`` must only appear in ``read_note``."""

    def test_read_note_is_sole_lithos_read_caller(self) -> None:
        hits = _scan_lithos_read_callers()

        # Sanity: a broken matcher must not let the guard pass vacuously.
        assert hits, (
            'expected to find the call_tool("lithos_read", …) site in '
            "read_note; matcher found none, so this test would be a no-op"
        )

        offenders = [
            (module, func, line)
            for (module, func, line) in hits
            if not (module == _ALLOWED_MODULE and func == _ALLOWED_FUNCTION)
        ]
        assert not offenders, (
            "lithos_read must only be read through LithosClient.read_note "
            "(the single envelope-normalisation chokepoint, #187/#190). "
            "Route these reads through read_note() instead of calling "
            'call_tool("lithos_read", …) directly:\n'
            + "\n".join(
                f"  {module}:{line} in {func}()" for module, func, line in offenders
            )
        )

        # Exactly one sanctioned call site — not zero (read_note must still
        # make the call) and not duplicated.
        assert len(hits) == 1, (
            'expected exactly one call_tool("lithos_read", …) site '
            f"(read_note); found {len(hits)}: {hits}"
        )
