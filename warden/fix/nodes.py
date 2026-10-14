"""Test node ids in a Python test file, as pytest names them (classes included)."""

from __future__ import annotations

import ast
import re

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)


def test_nodes(source: str) -> list[str]:
    """pytest node-id tails for the test functions in `source`: `test_x`, `TestA::test_x`,
    `TestA::TestB::test_x`. A file that does not parse (yet) falls back to bare names."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return list(dict.fromkeys(_TEST_DEF.findall(source)))
    out: list[str] = []

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name.startswith("test_") and f"{prefix}{node.name}" not in out:
                    out.append(f"{prefix}{node.name}")
            elif isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}{node.name}::")

    walk(tree.body, "")
    return out
