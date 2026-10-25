"""The upgrade-acceptance workflow must stay manual, guarded and off the
provisioned nightly runner.

`tests/shell/host/upgrade_matrix.sh` installs AstinusLab system-wide as root,
creates service identities, rewrites systemd units, ACLs and MAC policy, and
uninstalls twice. Its own header says it belongs on a disposable host and
nowhere else.

The workflow that runs it is therefore load-bearing in a way most workflows are
not: a scheduled trigger added later, or a runner label changed to the one
host-nightly.yml uses, would quietly start destroying a machine the operator
provisioned by hand and that the rest of the nightly depends on. Each of those
is a one-line edit that looks harmless in review, so each has a case here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/upgrade-acceptance.yml"
NIGHTLY = ROOT / ".github/workflows/host-nightly.yml"
MATRIX = ROOT / "tests/shell/host/upgrade_matrix.sh"
pytestmark = pytest.mark.unit


def _workflow() -> dict:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare `on:` key to the boolean True.
    loaded["on"] = loaded.pop(True, loaded.get("on"))
    return loaded


def _job() -> dict:
    return _workflow()["jobs"]["upgrade-matrix"]


def _steps() -> list[dict]:
    return _job()["steps"]


def test_the_workflow_exists_and_runs_the_matrix():
    assert MATRIX.is_file(), "the matrix this workflow exists to run is missing"

    assert any("upgrade_matrix.sh" in step.get("run", "") for step in _steps())


def test_it_is_manual_only():
    triggers = _workflow()["on"]

    # A schedule here would install and uninstall on the target every night.
    assert set(triggers) == {"workflow_dispatch"}, f"only manual dispatch is allowed, found: {sorted(triggers)}"


def test_it_does_not_run_on_the_provisioned_nightly_runner():
    nightly = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    nightly_labels = {label for job in nightly["jobs"].values() for label in job.get("runs-on", [])}
    labels = set(_job()["runs-on"])

    assert "disposable" in labels, "the matrix needs a host explicitly labelled disposable"
    # The nightly runner carries operator-installed Volatility at a fixed path.
    # Reinstalling AstinusLab over it would break the tier that depends on it.
    assert labels != nightly_labels, "this must not target the same runner as host-nightly.yml"


def test_it_requires_an_explicit_destructive_acknowledgement():
    inputs = _workflow()["on"]["workflow_dispatch"]["inputs"]

    assert "acknowledge_destructive" in inputs
    assert inputs["acknowledge_destructive"]["required"] is True
    guards = [step for step in _steps() if "ACKNOWLEDGEMENT" in str(step.get("env", {}))]
    assert guards, "no step compares the acknowledgement input"
    guard = guards[0]
    assert "DESTROY-THIS-HOST" in guard["run"]
    assert "exit 1" in guard["run"]


def test_it_requires_a_host_local_disposable_marker():
    """The label is not enough: labels get attached to the wrong machine."""
    marker = _job()["env"]["DISPOSABLE_MARKER"]
    guards = [step for step in _steps() if "DISPOSABLE_MARKER" in step.get("run", "")]

    assert guards, "no step checks the disposable marker -- the run would proceed on any labelled host"
    guard = guards[0]
    assert marker.startswith("/")
    assert '[ ! -f "$DISPOSABLE_MARKER" ]' in guard["run"]
    assert "exit 1" in guard["run"]


def test_dispatch_inputs_never_reach_a_shell_by_interpolation():
    """`${{ inputs.* }}` inside `run:` splices the raw string in as code."""
    offenders = [
        step.get("name", "<unnamed>")
        for step in _steps()
        if "${{ inputs." in step.get("run", "") or "${{ github.event" in step.get("run", "")
    ]

    assert not offenders, "dispatch input interpolated into a run block: " + ", ".join(offenders)


def test_the_prior_ref_is_format_checked_before_it_reaches_git():
    resolve = next(step for step in _steps() if step.get("id") == "commits")

    assert "PRIOR_REF" in resolve["env"]
    assert "grep -qE" in resolve["run"], "prior_ref must be validated against a character set"


def test_the_report_is_uploaded_even_when_the_matrix_fails():
    upload = next(step for step in _steps() if "upload-artifact" in str(step.get("uses", "")))

    # The matrix runs every row even after one fails, so a failing run is
    # exactly when its report matters most.
    assert upload.get("if") == "always()"
    assert upload["with"]["if-no-files-found"] == "error"


def test_concurrency_prevents_two_runs_sharing_one_host():
    concurrency = _workflow()["concurrency"]

    assert concurrency["group"]
    # Cancelling mid-run would leave the target part-installed.
    assert concurrency["cancel-in-progress"] is False
