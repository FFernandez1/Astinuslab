"""The counter must see every import form, not just the absolute one.

Regression test for the design-phase near-miss: `csrf` and
`playground_constants` scored zero under an absolute-only grep and were nearly
deleted. Both are live via relative imports.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("legacy_audit", ROOT / "tools/quality/legacy_audit.py")
assert _spec is not None and _spec.loader is not None
legacy_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy_audit)


def test_relative_imports_are_counted() -> None:
    """`csrf` is imported only relatively from Python. Absolute-only would miss it."""
    counts = legacy_audit.count_references("csrf", ROOT)

    assert counts["absolute"] == 0
    assert counts["relative"] >= 4
    assert sum(counts.values()) > 0


def test_js_specifiers_are_counted() -> None:
    """The CSRF browser module is referenced by multiple JavaScript modules."""
    counts = legacy_audit.count_references("csrf", ROOT)

    assert counts["js_specifier"] >= 3


def test_deeply_relative_imports_are_counted() -> None:
    """The playground constants module is reached through a deep relative import."""
    counts = legacy_audit.count_references("playground_constants", ROOT)

    assert counts["relative"] == 1


def test_a_genuinely_absent_name_scores_zero() -> None:
    counts = legacy_audit.count_references("no_such_module_anywhere", ROOT)

    assert sum(counts.values()) == 0


def test_the_module_itself_can_be_excluded() -> None:
    """A module's implementation must not count as a reference to itself."""
    module_path = (ROOT / "src/astinuslab" / "csrf").with_suffix(".py")
    counts = legacy_audit.count_references("csrf", ROOT, exclude=[module_path])

    assert counts["absolute"] == 0


def test_the_audits_own_documents_are_not_counted_as_references(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan names every module; counting it would mask real dead code."""
    artifact = "docs/development/REPOSITORY_LAYOUT.md"
    monkeypatch.setattr(legacy_audit, "AUDIT_ARTIFACTS", (artifact,))
    tracked = legacy_audit.tracked_files(ROOT)
    raw = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.splitlines()

    assert artifact in raw
    assert ROOT / artifact not in tracked
    # a normal doc still counts
    assert any(p.suffix == ".md" and "docs/user" in str(p) for p in tracked)


def test_bare_relative_package_imports_are_counted() -> None:
    """A module reached only by a bare relative package import must score.

    The importing sites live in storage and ui; naming them here in import
    syntax would make this test satisfy itself.
    """
    counts = legacy_audit.count_references("deletion", ROOT)

    assert counts["relative"] >= 2
    assert counts["from_import"] >= 1


def test_dotted_relative_imports_are_counted() -> None:
    """A route module reached only through a dotted relative import must score."""
    counts = legacy_audit.count_references("nics", ROOT)

    assert counts["relative"] >= 1


def test_absolute_subpackage_imports_are_counted() -> None:
    """An absolute subpackage import must score."""
    counts = legacy_audit.count_references("deletion", ROOT)

    assert counts["from_import"] > 0 or counts["absolute"] > 0


def test_ambiguous_basenames_are_not_excluded() -> None:
    assert legacy_audit._self_path("metrics", ROOT) == []


@pytest.mark.parametrize(
    ("name", "minimum_relative_references"),
    [("workloads", 9), ("routes", 11)],
)
def test_package_module_collisions_keep_live_references(name: str, minimum_relative_references: int) -> None:
    assert legacy_audit._has_ambiguous_basename(name, ROOT)
    assert legacy_audit._self_path(name, ROOT) == []

    counts = legacy_audit.count_references(name, ROOT, exclude=legacy_audit._self_path(name, ROOT))

    assert counts["relative"] >= minimum_relative_references


def test_refs_field_layout_is_stable(capsys: pytest.CaptureFixture[str]) -> None:
    assert legacy_audit.cmd_refs(["csrf", "workloads"]) == 0

    clear, ambiguous = (line.split("\t") for line in capsys.readouterr().out.splitlines())
    assert len(clear) == 3
    assert clear[0] == "csrf"
    assert clear[1].startswith("total=")
    assert "[ambiguous-basename]" not in clear
    assert len(ambiguous) == 4
    assert ambiguous[0] == "workloads"
    assert ambiguous[1].startswith("total=")
    assert ambiguous[-1] == "[ambiguous-basename]"


def test_ledger_records_basename_ambiguity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_root = tmp_path / "fake-repo"
    fake_package = fake_root / "src/astinuslab"
    (fake_package / "db").mkdir(parents=True)
    (fake_package / "playground").mkdir()
    (fake_package / "db/models.py").write_text("", encoding="utf-8")
    (fake_package / "playground/workloads.py").write_text("", encoding="utf-8")
    for name, target in (("models", "db.models"), ("workloads", "playground.workloads")):
        (fake_package / f"{name}.py").write_text(
            f'"""Compatibility alias for `astinuslab.{target}`."""\n'
            "import sys\n"
            f'sys.modules[__name__] = sys.modules["astinuslab.{target}"]\n',
            encoding="utf-8",
        )
    subprocess.run(["git", "init", "-q"], cwd=fake_root, check=True)
    subprocess.run(["git", "add", "."], cwd=fake_root, check=True)

    monkeypatch.setattr(legacy_audit, "ROOT", fake_root)
    fake_out = fake_root / "build/legacy-audit"
    assert legacy_audit.cmd_ledger(fake_out) == 0
    fake_rows = [json.loads(line) for line in (fake_out / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]

    assert {row["id"] for row in fake_rows} == {"shim:models", "shim:workloads"}
    assert {row["target"] for row in fake_rows} == {"db.models", "playground.workloads"}
    assert all(row["ambiguous_basename"] is True for row in fake_rows)

    real_root = ROOT
    monkeypatch.setattr(legacy_audit, "ROOT", real_root)
    real_out = tmp_path / "real-legacy-audit"
    assert legacy_audit.cmd_ledger(real_out) == 0
    real_rows = [json.loads(line) for line in (real_out / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    expected_real_rows = sum(
        "sys.modules[__name__]" in path.read_text(encoding="utf-8")
        for path in (real_root / "src/astinuslab").glob("*.py")
    )

    assert len(real_rows) == expected_real_rows
    assert all(row["target"] and isinstance(row["ambiguous_basename"], bool) for row in real_rows)
