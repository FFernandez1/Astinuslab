"""Audit error-field naming against the convention in docs/architecture/adr-error-field-naming.md.

Three rules:
  R1. SQLAlchemy mapped columns and Pydantic model attributes named for an "error"
      slot must be called `error_summary`. Flag `error` or `detail` on a class that
      subclasses DeclarativeBase / BaseModel.
  R2. JSONResponse / dict literals returned from `app.exception_handler`-decorated
      functions must use the key `"detail"`. Flag `"error"` / `"error_summary"`.
  R3. Internal status dict literals (not in models.py / schemas.py / exception
      handlers) that carry an error string slot must use the key `"error"`. Flag
      `"error_summary"` / `"detail"` in those dicts.

Documented exceptions in `HTTP_SERIALIZERS`: `ToolStatus.public_dict` builds the
Volatility readiness payload and `ReadinessCheck.public_dict` builds the deep-readiness
payload, so R2 (`"detail"`) applies to them rather than R3.
The exception is pinned to that file, class, and method — it does not exempt every
method named `public_dict`, and the dicts it covers are still audited against the
HTTP key set, so `"error"` / `"error_summary"` there would still be flagged.

Exit non-zero on any violation; print file:line: message.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "src" / "astinuslab"

MODEL_FILES = {APP / "db" / "models.py", APP / "api" / "schemas.py"}
# (file, class, method) serializers whose dicts are HTTP responses, so R2 applies.
HTTP_SERIALIZERS = {
    (APP / "forensics" / "vol3_tools.py", "ToolStatus", "public_dict"),
    (APP / "readiness.py", "ReadinessCheck", "public_dict"),
}
ALLOWED_PERSISTED = {"error_summary"}
ALLOWED_HTTP = {"detail"}
ALLOWED_INPROC = {"error"}
ERROR_LIKE_KEYS = {"error", "error_summary", "detail"}


def is_exception_handler(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            if dec.func.attr == "exception_handler":
                return True
    return False


def check_persisted(path: Path, tree: ast.AST, violations: list[str]) -> None:
    if path not in MODEL_FILES:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                target_name = None
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target_name = stmt.target.id
                elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    target_name = stmt.targets[0].id
                if target_name in ERROR_LIKE_KEYS and target_name not in ALLOWED_PERSISTED:
                    violations.append(f"{path}:{stmt.lineno}: persisted field `{target_name}` must be `error_summary`")


def _dicts_in_handlers(tree: ast.AST) -> set[int]:
    """Return ids of Dict nodes carrying HTTP error response bodies: dicts nested
    inside any @app.exception_handler function, or passed to a JSONResponse call."""
    handler_dicts: set[int] = set()
    for node in ast.walk(tree):
        if is_exception_handler(node):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Dict):
                    handler_dicts.add(id(inner))
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "JSONResponse":
                for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                    if isinstance(arg, ast.Dict):
                        handler_dicts.add(id(arg))
    return handler_dicts


def _dicts_in_http_serializers(path: Path, tree: ast.AST) -> set[int]:
    """Return dicts emitted by the HTTP_SERIALIZERS allowlisted methods in this file."""
    wanted = {(cls, method) for serializer_path, cls, method in HTTP_SERIALIZERS if serializer_path == path}
    if not wanted:
        return set()
    serializer_dicts: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for member in node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (node.name, member.name) not in wanted:
                continue
            for inner in ast.walk(member):
                if isinstance(inner, ast.Dict):
                    serializer_dicts.add(id(inner))
    return serializer_dicts


def check_dict_keys(path: Path, tree: ast.AST, violations: list[str]) -> None:
    if path in MODEL_FILES:
        return
    handler_dicts = _dicts_in_handlers(tree)
    http_serializer_dicts = _dicts_in_http_serializers(path, tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        in_handler = id(node) in handler_dicts or id(node) in http_serializer_dicts
        for key in node.keys:
            if not isinstance(key, ast.Constant) or key.value not in ERROR_LIKE_KEYS:
                continue
            allowed = ALLOWED_HTTP if in_handler else ALLOWED_INPROC
            if key.value not in allowed:
                ctx = "HTTP error response" if in_handler else "in-process dict"
                expected = "detail" if in_handler else "error"
                violations.append(f"{path}:{key.lineno}: {ctx} key `{key.value!r}` must be `{expected!r}`")


def audit() -> int:
    violations: list[str] = []
    inspected = 0
    skipped = 0
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            skipped += 1
            continue
        check_persisted(path, tree, violations)
        check_dict_keys(path, tree, violations)
        inspected += 1
    for v in violations:
        print(v)
    print(f"Inspected {inspected} Python source files; found {len(violations)} violation(s).")
    if skipped:
        print(f"Skipped {skipped} unparseable file(s).")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(audit())
