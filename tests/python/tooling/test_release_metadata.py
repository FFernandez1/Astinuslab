import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [pytest.mark.unit]

PYPROJECT = ROOT / "pyproject.toml"
MAIN = ROOT / "src/astinuslab/main.py"

# Release-facing documentation: what a user reads to decide what 1.0.0 supports.
# Deliberately an allowlist, not a tree walk. Historical plans, prompts, evidence
# checklists, and review artifacts record what was believed at the time and must
# keep saying so, so they are never scanned here.
_RELEASE_FACING_ENGLISH = (
    "README.md",
    "OVERVIEW.md",
    "docs/usage/getting-started.md",
    "docs/usage/running-locally.md",
    "docs/development/BACKEND.md",
    "docs/development/ROADMAP.md",
    "docs/user/VOLATILITY3.md",
    "docs/user/PLAYGROUND.md",
    "docs/reference/FORENSIC_INTEGRITY.md",
    "docs/reference/DISTRO_SUPPORT.md",
)
_TRANSLATIONS = ("es", "fr", "ca")

# Approved 1.0.0 matrix: Ubuntu 24.04 LTS x86_64, Python 3.12 and 3.13, Linux
# memory analysis via Linux ISFs. `requires-python = ">=3.12"` is the packaging
# floor and says nothing about 3.14+ being tested.
_UNTESTED_PYTHON_CLAIMS = (
    "3.12+",
    "3.12 or newer",
    "3.12 or later",
    "3.12 o superior",
    "3.12 o posterior",
    "3.12 ou supérieur",
    "3.12 ou plus récent",
    "3.12 o més recent",
    "3.12 o superior o",
)
# Windows PDB/catalog work is experimental groundwork. Naming Windows alongside
# Linux as one analysis path promises a workflow 1.0.0 does not accept.
_JOINT_WINDOWS_SUPPORT_CLAIMS = (
    "Linux and Windows",
    "Linux y Windows",
    "Linux et Windows",
    "Linux i Windows",
    "Windows and Linux",
)
_EXPERIMENTAL_MARKERS = ("experimental", "expérimental")
# Docs that describe Volatility/Windows analysis capability must label the
# Windows surface rather than leaving it to read as supported.
_WINDOWS_CAPABILITY_DOCS = (
    "OVERVIEW.md",
    "docs/development/ROADMAP.md",
    "docs/user/VOLATILITY3.md",
    "docs/reference/FORENSIC_INTEGRITY.md",
)


def _release_facing_docs() -> list[Path]:
    """Every release-facing English doc plus each localized sibling that exists."""
    paths: list[Path] = []
    for relative in _RELEASE_FACING_ENGLISH:
        english = ROOT / relative
        assert english.is_file(), f"release-facing doc allowlist names a missing file: {relative}"
        paths.append(english)
        for language in _TRANSLATIONS:
            sibling = english.with_suffix(f".{language}.md")
            if sibling.is_file():
                paths.append(sibling)
    return paths


def _windows_capability_docs() -> list[Path]:
    paths: list[Path] = []
    for relative in _WINDOWS_CAPABILITY_DOCS:
        english = ROOT / relative
        paths.append(english)
        paths.extend(
            sibling for language in _TRANSLATIONS if (sibling := english.with_suffix(f".{language}.md")).is_file()
        )
    return paths


def _offending_lines(path: Path, needles: tuple[str, ...]) -> list[str]:
    return [
        f"{path.relative_to(ROOT)}:{number}: {needle}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for needle in needles
        if needle in line
    ]


def test_release_docs_do_not_promise_untested_python_versions():
    """1.0.0 is tested on 3.12 and 3.13; "3.12+" also promises 3.14 and later."""
    offenders = [
        offender for path in _release_facing_docs() for offender in _offending_lines(path, _UNTESTED_PYTHON_CLAIMS)
    ]

    assert not offenders, f"release-facing docs claim untested Python versions: {offenders}"


def test_release_docs_do_not_present_windows_as_a_supported_analysis_path():
    """The supported 1.0.0 Volatility path is Linux memory with a Linux ISF."""
    offenders = [
        offender
        for path in _release_facing_docs()
        for offender in _offending_lines(path, _JOINT_WINDOWS_SUPPORT_CLAIMS)
    ]

    assert not offenders, f"release-facing docs present Windows as a supported analysis path: {offenders}"


def test_docs_stating_a_python_requirement_name_both_tested_runtimes():
    unnamed = []
    for path in _release_facing_docs():
        text = path.read_text(encoding="utf-8")
        if "Python 3.1" not in text:
            continue
        if "3.12" in text and "3.13" in text:
            continue
        unnamed.append(str(path.relative_to(ROOT)))

    assert not unnamed, f"docs state a Python requirement without naming 3.12 and 3.13: {unnamed}"


def test_local_runbook_separates_the_packaging_floor_from_the_tested_matrix():
    """`requires-python = ">=3.12"` must not be readable as "3.14 is supported"."""
    runbook = (ROOT / "docs/usage/running-locally.md").read_text(encoding="utf-8")

    assert "requires-python" in runbook
    assert "packaging minimum" in runbook


def test_windows_analysis_surfaces_are_labelled_experimental():
    unlabelled = []
    for path in _windows_capability_docs():
        text = path.read_text(encoding="utf-8")
        if "Windows" not in text:
            continue
        if any(marker in text.lower() for marker in _EXPERIMENTAL_MARKERS):
            continue
        unlabelled.append(str(path.relative_to(ROOT)))

    assert not unlabelled, f"docs describe Windows analysis without an experimental label: {unlabelled}"


def _package_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def _api_version() -> str:
    # The FastAPI(...) call is multi-line once it carries dependencies, so match
    # the version keyword inside the call rather than a fixed single-line shape.
    match = re.search(
        r'FastAPI\((?:[^()]|\([^()]*\))*?version="([^"]+)"',
        MAIN.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match, "could not locate the FastAPI(...) version literal in src/astinuslab/main.py"
    return match.group(1)


def test_api_metadata_version_matches_package_version():
    """A silent split here ships an API whose OpenAPI doc misreports the release."""
    assert _api_version() == _package_version()


def test_release_version_is_the_target_release():
    assert _package_version() == "1.0.0"
