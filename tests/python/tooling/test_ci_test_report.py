"""CI must detect absent or skipped required native-tool coverage."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools/quality/check_ci_test_report.py"
pytestmark = pytest.mark.unit


def _gate():
    spec = importlib.util.spec_from_file_location("ci_test_report", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(tmp_path, tier="backend"):
    gate = _gate()
    suite = ET.Element("testsuite")
    for module in gate.REQUIRED_MODULES[tier]:
        ET.SubElement(suite, "testcase", classname=module, name="test_fixture")
    report = tmp_path / "report.xml"
    ET.ElementTree(suite).write(report)
    return gate, suite, report


def _run(suite, report, tier="backend"):
    ET.ElementTree(suite).write(report)
    return subprocess.run([sys.executable, str(SCRIPT), tier, str(report)], capture_output=True, text=True)


def test_complete_report_passes(tmp_path):
    _, suite, report = _report(tmp_path)
    assert _run(suite, report).returncode == 0


@pytest.mark.parametrize("outcome", ["skipped", "failure", "error"])
def test_required_native_case_cannot_silently_disappear(tmp_path, outcome):
    _, suite, report = _report(tmp_path)
    ET.SubElement(suite[0], outcome, message="required tool unavailable")
    result = _run(suite, report)
    assert result.returncode == 1
    assert "required tool unavailable" in result.stderr


def test_missing_required_module_fails_even_when_other_tests_pass(tmp_path):
    _, suite, report = _report(tmp_path)
    missing = suite[0].attrib["classname"]
    suite.remove(suite[0])
    result = _run(suite, report)
    assert result.returncode == 1
    assert missing in result.stderr


def test_optional_windows_skip_is_visible_without_claiming_acceptance(tmp_path):
    _, suite, report = _report(tmp_path)
    case = ET.SubElement(
        suite, "testcase", classname="tests.python.virtualization.test_windows_runtime", name="test_runtime"
    )
    ET.SubElement(case, "skipped", message="PowerShell required")
    result = _run(suite, report)
    assert result.returncode == 0
    assert "outside required native-tool scope: 1 skipped" in result.stdout


def test_only_exact_root_safety_exception_is_allowed(tmp_path):
    _, suite, report = _report(tmp_path, "tooling")
    case = ET.SubElement(
        suite,
        "testcase",
        classname="tests.python.tooling.test_install_script",
        name="test_privileged_root_command_renders_custom_storage_and_home_protection",
    )
    skipped = ET.SubElement(case, "skipped", message="mutation path is root-only; --check is covered without root")
    assert _run(suite, report, "tooling").returncode == 0
    skipped.set("message", "rsync is required to exercise the sync path")
    assert _run(suite, report, "tooling").returncode == 1


@pytest.mark.parametrize(
    "content",
    [None, "<broken", "<testsuites />", '<!DOCTYPE testsuite [<!ENTITY x "payload">]><testsuite>&x;</testsuite>'],
)
def test_missing_malformed_or_empty_report_fails(tmp_path, content):
    report = tmp_path / "report.xml"
    if content is not None:
        report.write_text(content)
    result = subprocess.run([sys.executable, str(SCRIPT), "backend", str(report)], capture_output=True, text=True)
    assert result.returncode == 1


def test_ci_provisions_and_enforces_native_coverage():
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["backend"]
    steps = job["steps"]
    body = "\n".join(step.get("run", "") for step in steps)
    for package in ("acl", "qemu-utils", "pigz", "rsync", "curl", "shellcheck", "fakeroot"):
        assert package in body
    assert "getent passwd libvirt-qemu" in body
    assert "getent group astinuslab" in body
    for tier in ("backend", "tooling"):
        assert f"--junitxml=test-results/{tier}.xml" in body
        step = next(s for s in steps if f"check_ci_test_report.py {tier}" in s.get("run", ""))
        assert step.get("if") == "always()"
        assert not step.get("continue-on-error")
    assert not job.get("continue-on-error")
