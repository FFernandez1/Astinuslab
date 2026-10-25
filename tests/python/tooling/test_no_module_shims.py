"""No astinuslab module may be a sys.modules re-export shim.

The src-layout migration left 35 of these at the package root. They cost
nothing at runtime but they let callers keep importing pre-migration paths,
which is how the old layout survives in a new tree.

Detection is AST-based and recursive, for two reasons a substring match got
wrong. It must reach NESTED modules: the original ratchet globbed
``src/astinuslab/*.py`` only, so a shim one directory down was invisible to it.
And it must not fire on the two legitimate ``sys.modules[__name__]`` uses in
this package -- ``api/__init__.py`` assigns the module's ``__class__`` for a
``__getattr__`` facade, and ``db/__init__.py`` binds ``astinuslab.db`` to
itself so ``python -m astinuslab.db`` shares one module. Neither is an
assignment TO ``sys.modules[__name__]``, which is what a shim is.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "src/astinuslab"


def _is_sys_modules_self_target(node: ast.expr) -> bool:
    """True for the expression ``sys.modules[__name__]`` used as an assignment target."""
    if not isinstance(node, ast.Subscript):
        return False
    value = node.value
    if not (isinstance(value, ast.Attribute) and value.attr == "modules"):
        return False
    if not (isinstance(value.value, ast.Name) and value.value.id == "sys"):
        return False
    return isinstance(node.slice, ast.Name) and node.slice.id == "__name__"


def _shim_modules() -> list[str]:
    shims = []
    for path in sorted(PACKAGE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a parse failure is a different problem
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(_is_sys_modules_self_target(t) for t in node.targets):
                shims.append(str(path.relative_to(PACKAGE)))
                break
    return shims


def test_no_module_is_a_reexport_shim() -> None:
    shims = _shim_modules()

    assert shims == [], (
        f"{len(shims)} compatibility shim(s) remain: {shims}. Import the owning package directly instead."
    )


def test_the_detector_recognises_a_nested_shim(tmp_path: Path) -> None:
    """The original ratchet globbed the package root only and missed nested shims."""
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    (nested / "alias.py").write_text(
        "import sys\nfrom importlib import import_module\nsys.modules[__name__] = import_module('os')\n",
        encoding="utf-8",
    )
    tree = ast.parse((nested / "alias.py").read_text(encoding="utf-8"))
    targets = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]

    assert any(_is_sys_modules_self_target(t) for node in targets for t in node.targets)


def test_the_detector_ignores_the_legitimate_uses() -> None:
    """api/__init__ sets __class__; db/__init__ binds itself. Neither is a shim."""
    for relative in ("api/__init__.py", "db/__init__.py"):
        source = (PACKAGE / relative).read_text(encoding="utf-8")
        assert "sys.modules[__name__]" in source, f"{relative} no longer exercises this case"

    assert _shim_modules() == []


def test_the_real_flat_modules_are_still_present() -> None:
    """Guard against the ratchet being satisfied by deleting live code."""
    expected = {
        "__init__.py",
        "app_settings.py",
        "catalog.py",
        "csrf.py",
        "dbcli.py",
        "i18n.py",
        "main.py",
        "playground_constants.py",
        "readiness.py",
        "serve.py",
    }

    actual = {path.name for path in PACKAGE.glob("*.py")}

    assert expected <= actual, f"live flat modules went missing: {expected - actual}"
