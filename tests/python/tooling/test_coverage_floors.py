"""The per-module coverage floors must fail when a listed module slips.

A gate that cannot fail is worse than no gate: it is a green check that means
nothing. These cases drive the checker with synthetic reports so each way it is
supposed to refuse is exercised, and confirm that CI actually runs it against a
report the coverage step actually produces.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools/quality/check_coverage_floors.py"
WORKFLOW = ROOT / ".github/workflows/ci.yml"
pytestmark = pytest.mark.unit


def _gate():
    spec = importlib.util.spec_from_file_location("check_coverage_floors", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(tmp_path: Path, percentages: dict[str, float]) -> Path:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "files": {
                    path: {"summary": {"percent_covered": percent, "num_statements": 100}}
                    for path, percent in percentages.items()
                },
                "totals": {"percent_covered": 90.0},
            }
        ),
        encoding="utf-8",
    )
    return report


def _at_floor(gate, **overrides: float) -> dict[str, float]:
    """Every module exactly at its floor, before applying any overrides."""
    return {path: float(floor) for path, floor in gate.FLOORS.items()} | overrides


def test_a_module_exactly_at_its_floor_passes(tmp_path):
    gate = _gate()

    failures, notes = gate.check_floors(_report(tmp_path, _at_floor(gate)))

    assert failures == []
    assert notes == []


def test_a_module_below_its_floor_fails(tmp_path):
    gate = _gate()
    path = "src/astinuslab/playground/reconciliation.py"
    floor = gate.FLOORS[path]

    failures, _ = gate.check_floors(_report(tmp_path, _at_floor(gate, **{path: floor - 0.1})))

    assert len(failures) == 1
    assert path in failures[0]
    assert f"floor is {floor}%" in failures[0]


def test_a_module_missing_from_the_report_fails(tmp_path):
    gate = _gate()
    percentages = _at_floor(gate)
    removed = percentages.pop("src/astinuslab/storage/publication.py")
    assert removed  # the fixture really did contain it

    failures, _ = gate.check_floors(_report(tmp_path, percentages))

    # Renamed, deleted, or dropped from --cov scope. None of those is a pass.
    assert len(failures) == 1
    assert "absent from the coverage report" in failures[0]


def test_a_module_well_clear_of_its_floor_is_reported_so_the_floor_rises(tmp_path):
    gate = _gate()
    path = "src/astinuslab/jobs/capacity.py"
    floor = gate.FLOORS[path]

    failures, notes = gate.check_floors(_report(tmp_path, _at_floor(gate, **{path: floor + gate.SLACK})))

    assert failures == []
    assert len(notes) == 1
    assert "raise it" in notes[0]


def test_an_unreadable_report_fails_rather_than_passing_vacuously(tmp_path):
    gate = _gate()
    report = tmp_path / "coverage.json"
    report.write_text("not json", encoding="utf-8")

    failures, _ = gate.check_floors(report)

    assert len(failures) == 1
    assert "Cannot read coverage report" in failures[0]


def test_an_empty_report_fails(tmp_path):
    gate = _gate()
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"files": {}, "totals": {}}), encoding="utf-8")

    failures, _ = gate.check_floors(report)

    # A run that measured nothing must not clear every floor at once.
    assert len(failures) == 1
    assert "no per-file coverage" in failures[0]


def test_the_script_exits_non_zero_when_a_floor_is_breached(tmp_path):
    gate = _gate()
    path = "src/astinuslab/forensics/capture_support.py"
    report = _report(tmp_path, _at_floor(gate, **{path: 1.0}))

    result = subprocess.run([sys.executable, str(SCRIPT), str(report)], capture_output=True, text=True)

    assert result.returncode == 1
    assert path in result.stderr


def test_stale_floors_only_fail_when_asked(tmp_path):
    gate = _gate()
    path = "src/astinuslab/jobs/capacity.py"
    report = _report(tmp_path, _at_floor(gate, **{path: gate.FLOORS[path] + gate.SLACK}))

    quiet = subprocess.run([sys.executable, str(SCRIPT), str(report)], capture_output=True, text=True)
    strict = subprocess.run(
        [sys.executable, str(SCRIPT), str(report), "--raise-stale-floors"], capture_output=True, text=True
    )

    # Routine CI should not break because a module improved; the opt-in flag is
    # for the maintainer sweeping the floors upward.
    assert quiet.returncode == 0
    assert "raise it" in quiet.stdout
    assert strict.returncode == 1


def test_every_floored_module_exists():
    gate = _gate()

    missing = [path for path in gate.FLOORS if not (ROOT / path).is_file()]

    assert not missing, "FLOORS names modules that no longer exist: " + ", ".join(missing)


def test_ci_produces_the_report_and_runs_the_check():
    """The gate is only real if CI emits the JSON and then reads it."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    commands = [step.get("run", "") for step in steps]

    produces = [c for c in commands if "--cov-report=json" in c]
    consumes = [c for c in commands if "check_coverage_floors.py" in c]

    assert produces, "no CI step emits a JSON coverage report"
    assert consumes, "no CI step runs the coverage-floor check"
    report_paths = {c.split("--cov-report=json:")[1].split()[0] for c in produces}
    assert any(path in consumed for path in report_paths for consumed in consumes), (
        f"the floor check does not read the report CI writes: writes {report_paths}, reads {consumes}"
    )
