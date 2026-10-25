"""Build the legacy-audit candidate ledger under build/legacy-audit/.

Reference counting rule (design doc section 4.3): a count that matches only the
absolute form `astinuslab.<name>` produces false "dead" verdicts, because
relative imports are the dominant intra-package form in this codebase and JS
modules import each other by relative path. Every form below is matched.

Some relationships cannot be reached by name-based text matching and require
manual adjudication downstream:
- `__getattr__` / f-string module facades, e.g. `src/astinuslab/api/__init__.py`
  builds `f"{__name__}.routes"`.
- `src/astinuslab/migrations/versions/` — Alembic modules are linked by
  `down_revision` string ids, never by module name, so every migration scores
  zero.

Usage:
    python tools/quality/legacy_audit.py refs <name> [<name> ...]
    python tools/quality/legacy_audit.py ledger
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "build/legacy-audit"

# Extensions worth scanning. Lockfiles and binaries are excluded by tracked-file
# listing plus this filter.
SCANNED_SUFFIXES = {
    ".py",
    ".pyi",
    ".js",
    ".mjs",
    ".cjs",
    ".html",
    ".css",
    ".sh",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".cfg",
    ".ini",
    ".json",
    ".service",
}

# This audit's own working documents discuss every module in the codebase by
# name. A mention there is commentary about the code, not a use of it, so it
# must not count as a reference. The documents are kept outside the public
# repository; list a tracked one here if it returns. User-facing docs are NOT
# excluded: a module documented for operators is not dead.
AUDIT_ARTIFACTS: tuple[str, ...] = ()


def _patterns(name: str) -> dict[str, re.Pattern[str]]:
    n = re.escape(name)
    return {
        # import astinuslab.foo / astinuslab.foo.bar
        "absolute": re.compile(rf"\bastinuslab(?:\.\w+)*\.{n}\b"),
        # from astinuslab import foo
        "from_import": re.compile(rf"from\s+astinuslab[\w.]*\s+import\s+[^\n]*\b{n}\b"),
        # from .foo / from ..foo / from ...foo import
        "relative": re.compile(
            rf"from\s+\.{{1,4}}(?:\w+\.)*{n}\b"
            rf"|from\s+\.{{1,4}}[\w.]*\s+import\s+[^\n]*\b{n}\b"
        ),
        # importlib.import_module("...foo")
        "importlib": re.compile(rf"import_module\(\s*['\"][.\w]*(?<![\w-]){n}['\"]"),
        # ./foo.js  ../foo.js  "/static/foo.css"
        "js_specifier": re.compile(rf"[./][\w/]*(?<![\w-]){n}\.(?:js|mjs|cjs|css)\b"),
        # {% include "foo.html" %} / url_for(... "foo.html")
        "template": re.compile(rf"['\"][\w/]*(?<![\w-]){n}\.html['\"]"),
        # bare file literals in shell, YAML, TOML, docs
        "literal": re.compile(rf"\b{n}\.(?:sh|py|yaml|yml|json)\b"),
    }


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [root / p for p in out.split("\0") if p and Path(p).suffix in SCANNED_SUFFIXES and p not in AUDIT_ARTIFACTS]


def count_references(name: str, root: Path, exclude: Iterable[Path] = ()) -> dict[str, int]:
    """Count references to `name` across every import/reference form.

    Returns a dict keyed by form name. A form scoring zero is meaningful only
    alongside the others: see the module docstring.
    """
    excluded = {Path(p).resolve() for p in exclude}
    pats = _patterns(name)
    counts = dict.fromkeys(pats, 0)

    for path in tracked_files(root):
        if path.resolve() in excluded:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for form, pat in pats.items():
            counts[form] += len(pat.findall(text))

    return counts


def _basename_resolution(name: str, root: Path) -> tuple[list[Path], bool]:
    """Resolve module files and whether the basename names multiple locations."""
    package = root / "src/astinuslab"
    candidates = sorted([*package.rglob(f"{name}.py"), *package.rglob(f"{name}.pyi")])
    stems = {candidate.with_suffix("") for candidate in candidates}
    package_inits = list(package.rglob(f"{name}/__init__.py"))
    has_python_module = any(candidate.suffix == ".py" for candidate in candidates)
    ambiguous = len(stems) > 1 or (has_python_module and bool(package_inits))
    return candidates, ambiguous


def _self_path(name: str, root: Path) -> list[Path]:
    """The module's own file, which must not count as a reference to itself.

    When several files share a basename -- systematic in this tree, where every
    flat shim collides with its nested target -- excluding all of them would drop
    real cross-module references and push the count toward a false "dead"
    verdict. Exclude nothing in that case and let the caller flag the ambiguity:
    an inflated count is safe, a deflated one is not.
    """
    candidates, ambiguous = _basename_resolution(name, root)
    return [] if ambiguous else candidates


def _has_ambiguous_basename(name: str, root: Path) -> bool:
    _, ambiguous = _basename_resolution(name, root)
    return ambiguous


def cmd_refs(names: list[str]) -> int:
    for name in names:
        ambiguous = _has_ambiguous_basename(name, ROOT)
        counts = count_references(name, ROOT, exclude=_self_path(name, ROOT))
        total = sum(counts.values())
        forms = " ".join(f"{k}={v}" for k, v in counts.items() if v)
        marker = "\t[ambiguous-basename]" if ambiguous else ""
        print(f"{name}\ttotal={total}\t{forms or '(none)'}{marker}")
    return 0


def cmd_ledger(output_dir: Path | None = None) -> int:
    """Assemble ledger.jsonl from the shim inventory plus reference counts.

    Sweep output (vulture, deadcode, knip) is read from build/legacy-audit/raw/
    when present; task 0B produces it.
    """
    output_dir = OUT if output_dir is None else output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "raw").mkdir(exist_ok=True)
    (output_dir / "graph").mkdir(exist_ok=True)
    (output_dir / "evidence").mkdir(exist_ok=True)

    rows = []
    flat = sorted((ROOT / "src/astinuslab").glob("*.py"))
    for path in flat:
        text = path.read_text(encoding="utf-8")
        if "sys.modules[__name__]" not in text:
            continue
        name = path.stem
        target = re.search(r"astinuslab\.([\w.]+)`", text)
        counts = count_references(name, ROOT, exclude=_self_path(name, ROOT))
        rows.append(
            {
                "id": f"shim:{name}",
                "path": str(path.relative_to(ROOT)),
                "kind": "alias-shim",
                "target": target.group(1) if target else None,
                "ambiguous_basename": _has_ambiguous_basename(name, ROOT),
                "refs": counts,
                "refs_total": sum(counts.values()),
                "part": None,
                "verdict": "",
            }
        )

    ledger = output_dir / "ledger.jsonl"
    with ledger.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    display_path = ledger.relative_to(ROOT) if ledger.is_relative_to(ROOT) else ledger
    print(f"wrote {len(rows)} rows to {display_path}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[1] == "refs":
        return cmd_refs(argv[2:])
    if argv[1] == "ledger":
        return cmd_ledger()
    print(f"unknown command: {argv[1]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
