from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

APP_ROOT = Path(__file__).resolve().parents[3] / "src" / "astinuslab"


def _data_storage_derivations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if not isinstance(node.right, ast.Constant) or node.right.value != "storage":
            continue
        if isinstance(node.left, ast.Attribute) and node.left.attr == "data_dir":
            lines.append(node.lineno)
    return lines


def test_runtime_modules_do_not_bypass_configured_storage_dir():
    violations: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.name == "config.py":
            continue
        for line in _data_storage_derivations(path):
            violations.append(f"{path.relative_to(APP_ROOT)}:{line}")

    assert violations == []
