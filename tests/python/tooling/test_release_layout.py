"""Contract for the versioned release layout.

An installed release is immutable and addressed by content: source and the
virtualenv live under ``releases/<version>-<digest>/`` and are reached through
the ``current`` symlink, while storage, config, images, the Volatility symbol
cache and ``astinuslab.toml`` stay at the stable install root so they survive a
release switch. The install root is therefore also what the privileged helper
bakes as its identity, which is what stops the storage helper from denying
after every upgrade.
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "ops/deploy/install.sh"
UNINSTALL = ROOT / "ops/deploy/uninstall.sh"
pytestmark = [pytest.mark.unit, pytest.mark.host]


def run_bash(script: str, *, env=None, cwd=ROOT):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(["bash", "-c", script], cwd=cwd, env=merged, capture_output=True, text=True)


def _source_tree(root: Path) -> Path:
    """A minimal stand-in for the checkout the installer copies from."""
    repo = root / "checkout"
    (repo / "src/astinuslab").mkdir(parents=True)
    (repo / "ops/host").mkdir(parents=True)
    (repo / "ops/systemd").mkdir(parents=True)
    (repo / "src/astinuslab/main.py").write_text("# app\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname = "astinuslab"\nversion = "1.0.0"\n', encoding="utf-8")
    (repo / "requirements.lock").write_text("", encoding="utf-8")
    return repo


def _harness(install: Path, repo: Path, body: str) -> str:
    return f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    REPO_DIR={shlex.quote(str(repo))}
    SERVICE_USER=$(id -un)
    API_USER=$(id -un)
    RUNNER_USER=$(id -un)
    ANALYSIS_USER=$(id -un)
    {body}
    """


def test_release_id_pairs_the_version_with_a_content_digest(tmp_path):
    repo = _source_tree(tmp_path)
    install = tmp_path / "install"
    install.mkdir()

    first = run_bash(_harness(install, repo, 'release_id_for "$REPO_DIR"'))
    second = run_bash(_harness(install, repo, 'release_id_for "$REPO_DIR"'))
    (repo / "src/astinuslab/main.py").write_text("# app changed\n", encoding="utf-8")
    third = run_bash(_harness(install, repo, 'release_id_for "$REPO_DIR"'))

    assert first.returncode == 0, first.stderr
    identifier = first.stdout.strip()
    assert identifier.startswith("1.0.0-"), identifier
    assert len(identifier.split("-", 1)[1]) == 12, identifier
    assert second.stdout.strip() == identifier, "the same source must resolve to the same release"
    assert third.stdout.strip() != identifier, "changed source must resolve to a new release"


def test_source_and_venv_stage_inside_the_release_directory(tmp_path):
    repo = _source_tree(tmp_path)
    install = tmp_path / "install"
    install.mkdir()

    result = run_bash(_harness(install, repo, 'create_venv() { mkdir -p "$RELEASE_DIR/.venv/bin"; }\nstage_release'))

    assert result.returncode == 0, result.stderr
    releases = sorted((install / "releases").iterdir())
    assert len(releases) == 1, releases
    release = releases[0]
    assert release.name.startswith("1.0.0-")
    assert (release / "src/astinuslab/main.py").is_file()
    assert not (install / "backend").exists(), "source must not be staged at the stable root"
    assert not list((install / "releases").glob(".staging*")), "staging directories must not survive"


def test_current_symlink_switches_atomically_between_releases(tmp_path):
    repo = _source_tree(tmp_path)
    install = tmp_path / "install"
    (install / "releases/1.0.0-aaaaaaaaaaaa").mkdir(parents=True)
    (install / "releases/1.0.0-bbbbbbbbbbbb").mkdir(parents=True)

    first = run_bash(_harness(install, repo, 'switch_current_release "$INSTALL_DIR/releases/1.0.0-aaaaaaaaaaaa"'))
    after_first = os.readlink(install / "current")
    second = run_bash(_harness(install, repo, 'switch_current_release "$INSTALL_DIR/releases/1.0.0-bbbbbbbbbbbb"'))
    after_second = os.readlink(install / "current")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert after_first.endswith("1.0.0-aaaaaaaaaaaa")
    assert after_second.endswith("1.0.0-bbbbbbbbbbbb"), "a second switch must replace the link, not fail"
    assert not (install / "current.tmp").exists(), "the staging link must not survive"


def test_switch_replaces_the_link_with_a_rename(tmp_path):
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("switch_current_release()") : text.index("stage_release()")]

    # A remove-then-create leaves a window with no `current` at all, which is
    # exactly the window a running service would resolve through.
    assert "mv -T" in body or "mv --no-target-directory" in body, body
    assert "ln -s" in body, "the replacement link is built before the rename"


def test_mutable_paths_are_never_placed_inside_a_release(tmp_path):
    text = INSTALL.read_text(encoding="utf-8")
    for mutable in ("storage", "config", "images/definitions", "vol3/symbols"):
        assert f"$RELEASE_DIR/{mutable}" not in text, mutable
    assert '"$INSTALL_DIR/storage"' in text
    # Environment wiring belongs to the templates, not an incidental token in
    # install.sh. Check the actual rendered units without installing services.
    install = tmp_path / "install"
    for name in ("api", "runner", "analysis"):
        template = ROOT / f"ops/systemd/astinuslab-{name}.service"
        unit = tmp_path / f"{name}.service"
        result = run_bash(
            f"source {shlex.quote(str(INSTALL))}\n"
            f"render_template {shlex.quote(str(template))} {shlex.quote(str(unit))} {shlex.quote(str(install))}"
        )
        assert result.returncode == 0, result.stderr
        assert f"Environment=ASTINUSLAB_DATA_DIR={install}\n" in unit.read_text()


def test_privileged_policy_binds_the_stable_root_not_the_release():
    """The helper bakes the resolved install root's device and inode. Binding a
    release directory would change that identity on every upgrade and the
    storage helper would deny after each switch."""
    text = INSTALL.read_text(encoding="utf-8")
    rule = text[text.index("install_sudoers_rule()") : text.index("configure_mac()")]

    assert '--install-dir "$INSTALL_DIR"' in rule
    assert '--install-dir "$RELEASE_DIR"' not in rule
    assert '--helper-source-dir "$RELEASE_DIR/ops/host"' in rule


def test_legacy_flat_source_cleanup_removes_only_what_the_release_ships(tmp_path):
    """Upgrading a pre-release-layout install leaves the whole old tree beside
    the new release. Everything the release itself ships is superseded and goes;
    mutable paths and anything the operator put there stay."""
    repo = _source_tree(tmp_path)
    install = tmp_path / "install"
    release = install / "releases/1.0.0-test00000000"
    (release / "src/astinuslab").mkdir(parents=True)
    (release / "docs").mkdir()
    (release / "LICENSE").write_text("license\n", encoding="utf-8")

    # The superseded flat tree, plus mutable state and an operator's own file.
    (install / "src/astinuslab").mkdir(parents=True)
    (install / "docs").mkdir()
    (install / "LICENSE").write_text("stale license\n", encoding="utf-8")
    (install / ".venv/bin").mkdir(parents=True)
    (install / "storage/dumps").mkdir(parents=True)
    (install / "config").mkdir()
    (install / "images").mkdir()
    (install / "vol3").mkdir()
    (install / "astinuslab.toml").write_text("operator config\n", encoding="utf-8")
    (install / "operator-notes.txt").write_text("mine\n", encoding="utf-8")

    result = run_bash(_harness(install, repo, f"RELEASE_DIR={shlex.quote(str(release))}\nremove_legacy_flat_source"))

    assert result.returncode == 0, result.stderr
    assert not (install / "backend").exists(), "superseded source must go"
    assert not (install / "docs").exists()
    assert not (install / "LICENSE").exists()
    assert not (install / ".venv").exists()
    assert (install / "storage/dumps").is_dir(), "mutable state must survive"
    assert (install / "config").is_dir()
    assert (install / "images").is_dir()
    assert (install / "vol3").is_dir()
    assert (install / "astinuslab.toml").read_text(encoding="utf-8") == "operator config\n"
    assert (install / "operator-notes.txt").is_file(), "an operator's own file is not ours to remove"
    assert (release / "src/astinuslab").is_dir(), "the release itself is untouched"


def test_uninstall_names_the_release_tree_when_it_preserves_the_install_root():
    """Deleting the install root removes the release tree with it. Preserving it
    leaves a full application tree behind with no units to run it, so the
    operator has to be told where that source now lives."""
    text = UNINSTALL.read_text(encoding="utf-8")
    skipped = text[text.index("Skipped \u2014 $INSTALL_DIR left in place") - 400 :]

    assert "releases/" in skipped
    assert "current" in skipped


def test_staging_no_longer_decides_whether_to_restart(tmp_path):
    """Task 28 made a changed release set RESTART_SERVICES so stale processes
    could not survive an upgrade. Task 30 made restarting unconditional, so
    staging has no say in it at all — a stronger guarantee, and one fewer
    branch that can be wrong."""
    repo = _source_tree(tmp_path)
    install = tmp_path / "install"
    install.mkdir()

    result = run_bash(
        _harness(
            install,
            repo,
            'create_venv() { mkdir -p "$RELEASE_DIR/.venv/bin"; }\n'
            "    RESTART_SERVICES=false\n"
            "    stage_release\n"
            '    echo "restart=$RESTART_SERVICES"',
        )
    )

    assert result.returncode == 0, result.stderr
    assert "restart=false" in result.stdout, "staging must not set the restart flag any more"


def test_the_release_id_ignores_build_artefacts_the_install_never_ships(tmp_path):
    """The id has to address what lands, not what happens to sit in the
    operator's checkout. rsync already excludes __pycache__ and .pyc, so a
    checkout that has been imported from once would otherwise produce a
    different release than the identical checkout that has not."""
    repo = _source_tree(tmp_path)
    install = tmp_path / "install"
    install.mkdir()
    harness = 'create_venv() { mkdir -p "$RELEASE_DIR/.venv/bin"; }\n    stage_release\n    echo "id=$RELEASE_ID"'

    first = run_bash(_harness(install, repo, harness))
    (repo / "src/astinuslab/__pycache__").mkdir(parents=True)
    (repo / "src/astinuslab/__pycache__/main.cpython-312.pyc").write_bytes(b"\x00compiled\x00")
    second = run_bash(_harness(install, repo, harness))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    identifier = [line for line in first.stdout.splitlines() if line.startswith("id=")][0]
    assert identifier in second.stdout, "a build artefact must not change the release id"


def test_no_shipped_text_reaches_the_source_through_the_install_root():
    """Everything a release ships moved under `current/`; only mutable state kept
    its place. Three separate defects came from text that still named the old
    location - the storage-root helper died on a missing template, image
    downloads landed in a tree no service reads, and the documented sudoers
    re-point named a directory that no longer exists - so the invariant is
    checked rather than remembered.
    """
    source_dirs = ("deploy", "src", "ops", "tools", "guest", "tests")
    offenders = []
    for path in sorted(ROOT.glob("docs/**/*.md")) + [ROOT / "README.md", ROOT / "ops/deploy/install.sh"]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for name in (*source_dirs, "astinuslab.sh"):
                if f"/opt/astinuslab/{name}" in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")

    assert not offenders, "installed source is reached through /opt/astinuslab/current:\n" + "\n".join(offenders)
