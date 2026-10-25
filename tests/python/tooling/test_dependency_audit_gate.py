"""Contract for the CI dependency-audit gate.

Before this gate existed there was no dependency or vulnerability scanning at
all, and the two lockfiles had silently drifted apart: ``requirements.lock``
(``uv export --frozen``, what ``deploy/install.sh`` installs) carried
starlette 1.3.1 and python-multipart 0.0.32, while ``requirements-dev.lock``
(``uv pip compile``, what every CI job installs) sat on starlette 1.0.0 and
python-multipart 0.0.27 — seven advisories between them. Nothing reported it,
because nothing looked.

These tests pin the properties that keep it looking:

* **Both lockfiles are audited.** Auditing only the production one would have
  missed the entire finding above; auditing only the dev one would let a
  shipped advisory through.
* **The docs resolution is audited too.** Its extra is not in either exported
  lockfile, so auditing those alone misses vulnerable documentation assets.
* **The audit is blocking.** A reporting-only step is a step people stop
  reading.
* **Every lockfile stays auditable.** ``--strict`` turns "pip-audit could not
  resolve this pin" into a failure rather than a quiet pass.
* **Dependabot covers all three ecosystems**, including github-actions, whose
  major-tag pins float silently.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
DEPENDABOT = ROOT / ".github/dependabot.yml"
LOCKFILES = ("requirements.lock", "requirements-dev.lock")
pytestmark = [pytest.mark.unit]


def _audit_job() -> dict:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"].get("dependency-audit")
    assert job is not None, "ci.yml no longer defines the dependency-audit job"
    return job


def _run_bodies(job: dict) -> str:
    return "\n".join(step["run"] for step in job["steps"] if "run" in step)


@pytest.mark.parametrize("lockfile", LOCKFILES)
def test_ci_audits_every_committed_lockfile(lockfile: str) -> None:
    assert (ROOT / lockfile).is_file(), f"{lockfile} is missing"

    body = _run_bodies(_audit_job())

    assert re.search(rf"pip-audit\s+-r\s+{re.escape(lockfile)}\b", body), (
        f"the dependency-audit job does not run pip-audit against {lockfile}"
    )


@pytest.mark.parametrize("lockfile", LOCKFILES)
def test_lockfile_audits_are_strict(lockfile: str) -> None:
    body = _run_bodies(_audit_job())

    invocation = re.search(rf"pip-audit\s+-r\s+{re.escape(lockfile)}\b[^\n]*", body)
    assert invocation, f"no pip-audit invocation found for {lockfile}"
    assert "--strict" in invocation.group(0), (
        f"pip-audit on {lockfile} must pass --strict so an unresolvable pin fails instead of passing quietly"
    )


def test_ci_exports_the_locked_docs_resolution_for_audit() -> None:
    body = _run_bodies(_audit_job())
    expected = (
        'uv export --frozen --extra docs --no-dev --no-emit-project --no-hashes -o "$RUNNER_TEMP/docs-constraints.txt"'
    )
    assert expected in [line.strip() for line in body.splitlines()], (
        "the dependency-audit job must export the same frozen docs constraints used by the docs and frontend jobs"
    )


def test_ci_audits_docs_constraints_strictly_after_export() -> None:
    body = _run_bodies(_audit_job())
    invocation = re.search(r'^\s*pip-audit\s+-r\s+"\$RUNNER_TEMP/docs-constraints\.txt"[^\n]*', body, re.MULTILINE)
    assert invocation, "the dependency-audit job does not audit the exported docs resolution"
    assert "--strict" in invocation.group(0).split(), "the docs audit must fail when a locked dependency cannot resolve"
    export = re.search(r'^\s*uv export[^\n]*-o\s+"\$RUNNER_TEMP/docs-constraints\.txt"', body, re.MULTILINE)
    assert export and export.start() < invocation.start(), "CI must export the docs resolution before auditing it"


def test_dependency_audit_is_blocking() -> None:
    """`continue-on-error` or a trailing `|| true` would make this decorative."""
    job = _audit_job()

    assert not job.get("continue-on-error"), "the dependency-audit job must not be continue-on-error"
    for step in job["steps"]:
        assert not step.get("continue-on-error"), f"audit step is continue-on-error: {step.get('name')}"

    body = _run_bodies(job)
    for line in body.splitlines():
        if "audit" in line:
            assert "|| true" not in line, f"audit invocation swallows its exit status: {line.strip()}"


def test_npm_audit_declares_a_severity_floor() -> None:
    body = _run_bodies(_audit_job())

    invocation = re.search(r"npm audit[^\n]*", body)
    assert invocation, "the dependency-audit job does not run npm audit"
    assert "--audit-level=" in invocation.group(0), (
        "npm audit must state its severity floor explicitly rather than inherit the default"
    )


def test_dependabot_covers_pip_npm_and_actions() -> None:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))

    ecosystems = {entry["package-ecosystem"] for entry in config["updates"]}
    assert {"pip", "npm", "github-actions"} <= ecosystems, f"dependabot.yml is missing an ecosystem: {ecosystems}"


def test_ci_verifies_uv_lock_matches_pyproject() -> None:
    """A stale uv.lock is how the drift started: exports were faithful to a lock
    that was itself behind pyproject.toml."""
    body = _run_bodies(_audit_job())

    assert re.search(r"\buv lock --check\b", body), (
        "the dependency-audit job does not verify uv.lock against pyproject.toml"
    )


@pytest.mark.parametrize(
    ("lockfile", "flags"),
    [
        ("requirements.lock", "--frozen --no-dev --no-emit-project"),
        ("requirements-dev.lock", "--frozen --extra dev --no-emit-project"),
    ],
)
def test_ci_regenerates_each_lockfile_with_the_canonical_command(lockfile: str, flags: str) -> None:
    """uv writes its own argument list into the file header, so the CI command
    must match the committed header exactly -- flag order included -- or the
    regeneration diffs against itself forever."""
    body = _run_bodies(_audit_job())
    expected = f"uv export {flags} -o {lockfile}"

    assert expected in body, f"CI must regenerate {lockfile} with exactly: {expected}"

    header = (ROOT / lockfile).read_text(encoding="utf-8").splitlines()[1]
    assert header.strip() == f"#    {expected}", (
        f"{lockfile} header records a different command than CI runs: {header.strip()!r}"
    )


def test_ci_fails_on_any_regenerated_lockfile_diff() -> None:
    body = _run_bodies(_audit_job())
    invocation = next((line.strip() for line in body.splitlines() if "git diff --exit-code" in line), "")

    assert invocation, "CI regenerates the lockfiles but does not fail on a diff"
    for lockfile in LOCKFILES:
        assert re.search(rf"(?<!\S){re.escape(lockfile)}(?!\S)", invocation), (
            f"the authoritative git diff invocation omits {lockfile}: {invocation}"
        )


def test_ci_pins_the_uv_version() -> None:
    """uv export formatting is not guaranteed stable across releases, so an
    unpinned uv turns the diff gate into a flake."""
    job = _audit_job()
    setup = [step for step in job["steps"] if "astral-sh/setup-uv" in str(step.get("uses", ""))]
    assert setup, "the dependency-audit job does not install uv via astral-sh/setup-uv"
    ci_version = setup[0].get("with", {}).get("version")
    assert ci_version == "0.11.16", "astral-sh/setup-uv must pin uv 0.11.16 exactly"

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required_version = pyproject["tool"]["uv"]["required-version"]
    assert required_version == f"=={ci_version}", (
        "pyproject.toml and CI must require the same exact uv version: "
        f"pyproject={required_version!r} CI={ci_version!r}"
    )


@pytest.mark.parametrize("job_name", ["docs", "frontend"])
def test_docs_install_uses_the_locked_resolution(job_name: str) -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"][job_name]
    setup = [step for step in job["steps"] if "astral-sh/setup-uv" in str(step.get("uses", ""))]
    assert setup and setup[0]["with"]["version"] == "0.11.16"
    install = next(step["run"] for step in job["steps"] if step.get("name") == "Install docs dependencies")
    assert "uv export --frozen --extra docs --no-dev --no-emit-project --no-hashes" in install
    assert '-o "$RUNNER_TEMP/docs-constraints.txt"' in install
    assert 'pip install -c "$RUNNER_TEMP/docs-constraints.txt" mkdocs-material mkdocs-static-i18n' in install
