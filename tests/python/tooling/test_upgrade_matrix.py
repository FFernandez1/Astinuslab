"""Coverage contract for the install/upgrade/rollback acceptance matrix.

The matrix itself only runs on a disposable host, so nothing in CI executes it.
What CI can hold is its shape: that it still parses, and that the scenarios the
remediation plan requires have not quietly fallen out of it.
"""

import gzip
import hashlib
import json
import os
import pwd
import re
import shlex
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[3]
MATRIX = ROOT / "tests/shell/host/upgrade_matrix.sh"
pytestmark = [pytest.mark.unit]

REQUIRED_ROWS = (
    "genuine prior-version upgrade",
    "clean install",
    "rerun",
    "update from changed candidate",
    "failed migration",
    "failed service",
    "failed readiness",
    "custom storage root",
    "split identity migration",
    "existing split upgrade",
    "existing split rollback",
    "uninstall, preserve",
    "uninstall, delete",
    "unsafe install parent rejection",
)


@pytest.fixture
def matrix_sources(tmp_path: Path) -> dict[str, str]:
    """Real archives and extracted trees; no executable installer is invoked."""
    repo = tmp_path / "matrix-repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repo, env=env, text=True).strip()

    git("init", "--quiet")
    deploy = repo / "ops/deploy"
    deploy.mkdir(parents=True)
    for name in ("install.sh", "uninstall.sh"):
        script = deploy / name
        script.write_text("#!/bin/sh\nexit 97\n")
        script.chmod(0o755)
    result = {}
    for label in ("PRIOR", "CANDIDATE"):
        (repo / "revision").write_text(label)
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", label)
        archive = tmp_path / f"{label}.tar"
        git("archive", "--format=tar", f"--output={archive}", "HEAD")
        source = tmp_path / label
        source.mkdir()
        subprocess.run(["tar", "-xf", str(archive), "-C", str(source)], check=True)
        result[f"{label}_SHA"] = git("rev-parse", "HEAD")
        result[f"{label}_ARCHIVE"] = str(archive)
        result["SRC" if label == "CANDIDATE" else "PRIOR_SRC"] = str(source)
    return result


def _run_matrix_preflight(values: dict[str, str], *, through_row_zero: bool = False):
    source = MATRIX.read_text()
    boundary = "# ── ROW 1:" if through_row_zero else "# ── ROW 0:"
    prefix = source.split(boundary, 1)[0]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRIOR_", "CANDIDATE_"))}
    env.pop("RELEASE_MODE", None)
    env.update(values)
    # Any privileged command is a test failure, even if row selection regresses.
    guard = 'sudo() { echo "unexpected privileged operation" >&2; exit 97; }; export -f sudo\n'
    return subprocess.run(
        ["bash", "-c", guard + prefix + '\nprintf "preflight_complete\\n"\n'],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_first_release_reaches_clean_install_without_prior_or_privileged_operations(matrix_sources):
    values = {k: v for k, v in matrix_sources.items() if not k.startswith("PRIOR_")}
    result = _run_matrix_preflight({**values, "RELEASE_MODE": "first"}, through_row_zero=True)
    assert result.returncode == 0, result.stderr
    assert "release_mode=first" in result.stdout
    assert "row0=not_applicable:first_release" in result.stdout
    assert "preflight_complete" in result.stdout
    assert "prior_sha=" not in result.stdout
    assert "  PASS " not in result.stdout


@pytest.mark.parametrize("mode", [None, "upgrade"])
def test_upgrade_mode_still_requires_all_prior_inputs(matrix_sources, mode):
    values = {k: v for k, v in matrix_sources.items() if not k.startswith("PRIOR_")}
    if mode is not None:
        values["RELEASE_MODE"] = mode
    result = _run_matrix_preflight(values)
    assert result.returncode != 0
    assert "PRIOR_SRC" in result.stderr
    assert "preflight_complete" not in result.stdout


@pytest.mark.parametrize("name", ["PRIOR_SRC", "PRIOR_ARCHIVE", "PRIOR_SHA"])
def test_first_release_rejects_any_conflicting_prior_input(matrix_sources, name):
    values = {k: v for k, v in matrix_sources.items() if not k.startswith("PRIOR_") or k == name}
    result = _run_matrix_preflight({**values, "RELEASE_MODE": "first"})
    assert result.returncode == 2
    assert "first-release mode refuses prior-release inputs" in result.stderr
    assert "preflight_complete" not in result.stdout


@pytest.mark.parametrize("mode", ["", "FIRST", "anything"])
def test_matrix_rejects_invalid_release_modes_before_execution(matrix_sources, mode):
    result = _run_matrix_preflight({**matrix_sources, "RELEASE_MODE": mode})
    assert result.returncode == 2
    assert "RELEASE_MODE must be first or upgrade" in result.stderr


def test_first_release_still_rejects_candidate_archive_identity_mismatch(matrix_sources):
    values = {k: v for k, v in matrix_sources.items() if not k.startswith("PRIOR_")}
    result = _run_matrix_preflight({**values, "RELEASE_MODE": "first", "CANDIDATE_SHA": "0" * 40})
    assert result.returncode == 2
    assert "candidate archive commit does not match" in result.stderr


def test_first_release_still_rejects_modified_extracted_candidate(matrix_sources):
    Path(matrix_sources["SRC"], "revision").write_text("modified after archive extraction")
    values = {k: v for k, v in matrix_sources.items() if not k.startswith("PRIOR_")}
    result = _run_matrix_preflight({**values, "RELEASE_MODE": "first"})
    assert result.returncode == 2
    assert "SRC does not match the candidate archive" in result.stderr


def test_upgrade_preflight_still_binds_both_real_archives(matrix_sources):
    result = _run_matrix_preflight(matrix_sources)
    assert result.returncode == 0, result.stderr
    assert f"prior_sha={matrix_sources['PRIOR_SHA']}" in result.stdout
    assert "release_mode=upgrade" in result.stdout


@pytest.fixture
def genuine_git_archive(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "archive-source"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repo, env=env, text=True).strip()

    git("init", "--quiet")
    (repo / "payload.bin").write_bytes(bytes(range(256)) * 4096)
    git("add", "payload.bin")
    git(
        "-c",
        "user.name=Archive fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    archive = tmp_path / "genuine.tar"
    git("archive", "--format=tar", f"--output={archive}", "HEAD")
    assert archive.stat().st_size > 1024 * 1024
    return archive, git("rev-parse", "HEAD")


def _run_archive_commit(archive: Path) -> subprocess.CompletedProcess[str]:
    text = MATRIX.read_text(encoding="utf-8")
    start = text.index("archive_commit() {")
    end = text.index("\n}\n", start) + 3
    return subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n" + text[start:end] + '\narchive_commit "$1"\n',
            "matrix-fixture",
            str(archive),
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("suffix", [".tar", ".tar.gz", ".tgz"])
def test_archive_commit_accepts_large_genuine_archives(genuine_git_archive: tuple[Path, str], suffix: str):
    archive, expected = genuine_git_archive
    if suffix != ".tar":
        compressed = archive.with_name("genuine" + suffix)
        compressed.write_bytes(gzip.compress(archive.read_bytes()))
        archive = compressed

    result = _run_archive_commit(archive)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("damage", ["crc", "truncated"])
def test_archive_commit_rejects_damaged_gzip_after_valid_git_header(genuine_git_archive: tuple[Path, str], damage: str):
    archive, _ = genuine_git_archive
    compressed = bytearray(gzip.compress(archive.read_bytes()))
    if damage == "crc":
        compressed[-8] ^= 1
    else:
        del compressed[-4:]
    damaged = archive.with_suffix(".tar.gz")
    damaged.write_bytes(compressed)

    result = _run_archive_commit(damaged)

    assert result.returncode != 0
    assert not result.stdout.strip(), "A failed archive must not publish its plausible commit header"


def test_archive_commit_rejects_gzip_without_a_git_header(tmp_path: Path):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(bytes(range(256)) * 4096)
    archive = tmp_path / "ordinary.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(payload, arcname="payload.bin")

    result = _run_archive_commit(archive)

    assert result.returncode != 0
    assert not result.stdout.strip()


def _run_matrix_head_revision(tmp_path: Path, python_path: Path) -> subprocess.CompletedProcess[str]:
    text = MATRIX.read_text(encoding="utf-8")
    start = text.index("head_revision() {")
    end = text.index("\n}\n", start) + 3
    interpreter = tmp_path / "install/current/.venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    interpreter.chmod(0o755)
    script = """set -euo pipefail
db_identity() { echo matrix-fixture; }
sudo() {
  [[ "$1" == -u && "$2" == matrix-fixture ]] || return 2
  shift 2
  "$@"
}
""" + text[start:end]
    return subprocess.run(
        ["bash", "-c", script + "\nhead_revision\n"],
        cwd=tmp_path,
        env={
            **os.environ,
            "INSTALL_DIR": str(tmp_path / "install"),
            "PYTHONPATH": str(python_path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ASTINUSLAB_DATA_DIR": str(tmp_path / "data"),
            "ASTINUSLAB_STORAGE_DIR": str(tmp_path / "storage"),
            "ASTINUSLAB_DB_PATH": str(tmp_path / "matrix.db"),
        },
        capture_output=True,
        text=True,
    )


def test_head_revision_executes_against_the_packaged_migrations(tmp_path: Path):
    expected = ScriptDirectory(str(ROOT / "src/astinuslab/migrations")).get_current_head()

    result = _run_matrix_head_revision(tmp_path, ROOT / "src")

    assert result.returncode == 0, result.stderr
    assert expected and result.stdout.strip() == expected


def test_head_revision_rejects_an_empty_head_with_a_visible_error(tmp_path: Path):
    package = tmp_path / "packages/astinuslab"
    migrations = package / "migrations"
    (migrations / "versions").mkdir(parents=True)
    (package / "__init__.py").touch()
    (migrations / "__init__.py").touch()
    (migrations / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s\n", encoding="utf-8")

    result = _run_matrix_head_revision(tmp_path, package.parent)

    assert result.returncode != 0
    assert not result.stdout.strip()
    assert "Candidate migration head is empty" in result.stderr


@pytest.mark.parametrize("operation", ["db_path", "schema", "database_content"])
@pytest.mark.parametrize("invalid_environment", [False, True])
def test_database_probes_use_installed_environment_not_flat_default(
    tmp_path: Path, operation: str, invalid_environment: bool
):
    install = tmp_path / "installed"
    config = install / "config"
    config.mkdir(parents=True)
    database = install / "storage/databases/selected database.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('installed-revision')")
        connection.execute("CREATE TABLE acceptance_marker(v TEXT)")
        connection.execute("INSERT INTO acceptance_marker VALUES ('installed-marker')")
    assignment = 'ASTINUSLAB_DB_PATH="unterminated' if invalid_environment else f'ASTINUSLAB_DB_PATH="{database}"\n'
    (config / "astinuslab.env").write_text(assignment, encoding="utf-8")
    interpreter = install / "current/.venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    interpreter.chmod(0o755)
    text = MATRIX.read_text(encoding="utf-8")
    # Execute the real cooperating probes. Only the service UID switch is stubbed.
    start = text.index("db_identity() {")
    end = text.index("process_cwd() {", start)
    script = (
        "set -euo pipefail\n"
        + text[start:end]
        + """
db_identity() { echo matrix-fixture; }
sudo() {
  [[ "$1" == -u && "$2" == matrix-fixture ]] || return 2
  shift 2
  "$@"
}
"""
    )
    result = subprocess.run(
        ["bash", "-c", script + operation],
        cwd=tmp_path,
        env={
            **os.environ,
            "INSTALL_DIR": str(install),
            "SRC": str(ROOT),
            "ASTINUSLAB_DB_PATH": str(tmp_path / "operator-decoy.db"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    if invalid_environment:
        assert result.returncode != 0
        assert "unterminated environment quote" in result.stderr
        assert not result.stdout.strip(), "A refused configuration must not publish plausible fallback DB evidence"
    else:
        assert result.returncode == 0, result.stderr
        expected = {"db_path": str(database), "schema": "installed-revision", "database_content": "installed-marker"}
        assert result.stdout.strip() == expected[operation]
    assert not (install / "storage/astinuslab.db").exists()
    assert not (tmp_path / "operator-decoy.db").exists()


def test_the_matrix_still_parses():
    result = subprocess.run(["bash", "-n", str(MATRIX)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_the_matrix_covers_every_required_scenario():
    text = MATRIX.read_text(encoding="utf-8")

    missing = [name for name in REQUIRED_ROWS if 'row "' not in text or name not in text]
    assert not missing, f"the acceptance matrix no longer covers: {missing}"


def test_the_matrix_asserts_the_defect_it_was_written_for():
    """Uninstall left a standing `u:libvirt-qemu:x` on the operator's home. The
    row that proves it is gone is the reason this file exists."""
    text = MATRIX.read_text(encoding="utf-8")

    assert "operator home grants untouched" in text
    assert "traversal manifest consumed" in text
    assert "libexec fully removed" in text


def test_failed_service_row_forces_a_real_systemd_start_failure():
    text = MATRIX.read_text(encoding="utf-8")
    service_row = text[text.index('row "5 failed service') : text.index('row "6 failed readiness')]

    assert "/run/systemd/system" in service_row
    assert "ExecStart=/bin/false" in service_row
    assert "verify_release_readiness() {" not in service_row


def test_failed_readiness_row_keeps_real_services_and_fails_only_the_first_probe():
    text = MATRIX.read_text(encoding="utf-8")
    readiness_row = text[text.index('row "6 failed readiness') : text.index('row "7 custom storage root')]

    assert "real_verify_release_readiness" in readiness_row
    assert "forced_readiness_failure" in readiness_row
    assert "ExecStart=/bin/false" not in readiness_row
    assert 'check "restored release is serving" 200' in readiness_row


def test_custom_root_row_exercises_runtime_and_privileged_helper():
    text = MATRIX.read_text(encoding="utf-8")

    assert "--restart" in text
    assert "runtime uses the custom root" in text
    assert "custom-root ACL helper works" in text
    assert "custom-root runtime write works" in text
    assert "custom-root MAC ownership recorded" in text
    assert "custom-root MAC policy effective" in text
    assert "wait_health" in text, "a synchronous restart still needs an API readiness retry"
    assert '-m 0750 "$(dirname "$CUSTOM_ROOT")"' in text
    assert '-g astinuslab -m 0750 "$(dirname "$CUSTOM_ROOT")"' in text
    assert '-g astinuslab -m 2770 "$CUSTOM_ROOT"' in text


@pytest.mark.parametrize("acl_state", ["present", "missing", "unreadable"])
def test_custom_root_acl_observation_does_not_depend_on_old_operator_groups(acl_state):
    """A clean install adds a group that the running operator shell cannot inherit."""
    text = MATRIX.read_text()
    assertion = text.split('check "custom-root qemu ACL effective"', 1)[1].split('check "libvirt opens a disk', 1)[0]
    script = r"""
set -uo pipefail
CUSTOM_PROBE=/protected/custom-root/probe
getfacl() {
  [[ "${ACL_OBSERVER_ROOT:-0}" == 1 ]] || return 13
  [[ "$ACL_STATE" != unreadable ]] || return 13
  if [[ "$ACL_STATE" == present ]]; then
    printf 'user:libvirt-qemu:r-x\t#effective:r-x\n'
  else
    printf 'user::rwx\ngroup::r-x\nother::---\n'
  fi
}
sudo() {
  [[ "$1" == getfacl ]] || return 97
  ACL_OBSERVER_ROOT=1 "$@"
}
check() { [[ "$2" == "$3" ]]; }
"""
    result = subprocess.run(
        ["bash", "-c", script + 'check "custom-root qemu ACL effective"' + assertion],
        env={**os.environ, "ACL_STATE": acl_state},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) == (acl_state == "present"), result.stderr


@pytest.mark.parametrize("acl_exit", [0, 37])
def test_custom_root_disk_probe_respects_directory_acl_and_cleans_up(tmp_path: Path, acl_exit: int):
    """Run the real probe with its privileged/VM boundary replaced, never sudo or KVM."""
    custom_root = tmp_path / "storage"
    custom_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    boundary = bin_dir / "sudo"
    boundary.write_text(
        f"#!{sys.executable}\n"
        + """import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from ops.host.acl_policy import validate_target
from ops.host.fix_libvirt_path_acl import _libvirt_write_target

args = sys.argv[1:]
while args[0] in ("-u", "-n", "sudo"):
    del args[:2 if args[0] == "-u" else 1]
command, *args = args
root = Path(os.environ["CUSTOM_ROOT"])
events = Path(os.environ["PROBE_EVENTS"])
state = Path(os.environ["PROBE_STATE"])

def record(action, **fields):
    with events.open("a") as handle:
        handle.write(json.dumps({"action": action, **fields}) + "\\n")

if command in ("mktemp", "stat", "test", "rm", "rmdir"):
    raise SystemExit(subprocess.call([command, *args]))
if command == "qemu-img":
    assert args[:4] == ["create", "-q", "-f", "qcow2"] and args[-1] == "64M"
    Path(args[-2]).touch(mode=0o600)
elif command.endswith("/fix_libvirt_path_acl.sh"):
    target = validate_target(args[0])
    assert target.parent == root and (target / "probe.qcow2").is_file()
    record("acl", target=str(target), writable=_libvirt_write_target(target.relative_to(root).parts))
    raise SystemExit(int(os.environ["ACL_EXIT"]))
elif command == "virsh":
    assert args[:2] == ["--connect", "qemu:///system"]
    action = args[2]
    record(action)
    if action == "define":
        disk = ET.parse(args[3]).find("./devices/disk")
        assert disk is not None
        source = Path(disk.find("source").attrib["file"])
        prior = json.loads(events.read_text().splitlines()[0])
        assert prior["action"] == "acl" and source.parent == Path(prior["target"])
        assert source.is_file()
        assert prior["writable"] or disk.find("readonly") is not None, "probe ACL permits only reads"
        state.write_text("shut off")
    elif action == "start":
        assert state.is_file()
        state.write_text("running")
    elif action == "dominfo":
        raise SystemExit(0 if state.exists() else 1)
    elif action == "domstate":
        print(state.read_text())
    elif action == "destroy":
        state.write_text("shut off")
    elif action == "undefine":
        state.unlink()
    else:
        raise AssertionError(action)
else:
    raise AssertionError(command)
""",
        encoding="utf-8",
    )
    boundary.chmod(0o755)
    text = MATRIX.read_text(encoding="utf-8")
    start = text.index("custom_root_libvirt_disk_open() {")
    end = text.index("\n}\n", start) + 3
    # Only relocate the probe's operator-owned log/XML paths into this fixture.
    function = text[start:end].replace("/tmp/", f"{tmp_path}/")
    result = subprocess.run(
        ["bash", "-c", function + "\ncustom_root_libvirt_disk_open\n"],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CUSTOM_ROOT": str(custom_root),
            "SERVICE_USER": "matrix-fixture",
            "ACL_EXIT": str(acl_exit),
            "PROBE_EVENTS": str(tmp_path / "events.jsonl"),
            "PROBE_STATE": str(tmp_path / "domain-state"),
        },
        capture_output=True,
        text=True,
    )

    acl_log = tmp_path / "astinuslab-matrix-libvirt-acl.log"
    define_log = tmp_path / "astinuslab-matrix-libvirt-define.log"
    assert not list(custom_root.iterdir()), "owned disk and probe directory must be removed even after ACL refusal"
    assert not list(tmp_path.glob("astinuslab-matrix-storage-*.xml"))
    assert not (tmp_path / "domain-state").exists()
    assert result.returncode == acl_exit, (
        result.stderr + acl_log.read_text() + (define_log.read_text() if define_log.exists() else "")
    )
    events = [json.loads(line)["action"] for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    expected = (
        ["acl", "dominfo"]
        if acl_exit
        else ["acl", "define", "start", "dominfo", "domstate", "destroy", "domstate", "undefine", "dominfo"]
    )
    assert events == expected


def test_every_candidate_installer_invocation_uses_the_resolved_path_and_install_dir():
    text = MATRIX.read_text(encoding="utf-8")
    direct_invocations = [
        line for line in text.splitlines() if 'sudo "$' in line and "INSTALLER" in line and "UNINSTALLER" not in line
    ]

    assert direct_invocations
    assert all(
        '--install-dir "$INSTALL_DIR"' in line
        or '--install-dir "$LEGACY_INSTALL_DIR"' in line
        or '--install-dir "$HOME_ROOT"' in line
        for line in direct_invocations
    )
    assert "sudo ./deploy/install.sh" not in text


def test_home_root_row_rejects_the_untrusted_parent_before_mutation():
    text = MATRIX.read_text(encoding="utf-8")

    row = text[text.index('row "13 unsafe install parent rejection"') :]
    assert "unsafe home-parent install rejected" in row
    assert "Unsafe install-root parent authority" in row
    assert "operator home grants untouched" in row
    assert "rejected install root never created" in row


def test_split_rows_cover_migration_and_an_existing_split_upgrade():
    text = MATRIX.read_text(encoding="utf-8")

    assert text.count("--split-users") >= 2
    assert "runtime identity marker valid" in text
    assert "existing split path selected" in text
    assert "api process identity" in text
    assert "runner helper access" in text
    assert "existing split rollback" in text
    assert "real_verify_split_services" in text
    assert "privileged files restored" in text
    assert "split process runs the new release" in text
    assert "split schema at candidate head" in text
    assert "existing split api process exact uid" in text
    assert "existing split runner process exact uid" in text
    assert "existing split analysis process exact uid" in text
    assert 'root.rglob("*")' in text
    assert "catalog child custody" in text
    assert "private key custody" in text
    assert "database child custody" in text
    assert "image child custody" in text
    assert "symbol child custody" in text
    assert "custom evidence child custody" in text


@pytest.mark.parametrize(
    "existing_dumps, failed_probe",
    [(False, ""), (True, ""), (False, "probe"), (False, "json"), (False, "bin")],
)
def test_custody_probes_enter_migration_and_do_not_trigger_image_discovery(
    tmp_path, monkeypatch, existing_dumps, failed_probe
):
    from astinuslab.images import image_admin_support

    source = MATRIX.read_text()
    row = source.split('row "8 split identity migration"', 1)[1].split("# ── ROW 9:", 1)[0]
    probes = list(re.finditer(r'sudo install -o [^\n]+/dev/null \\\n  "[^\n]+"', row))
    assert len(probes) == 3
    assert all(probe.start() < row.index('(cd "$SRC"') for probe in probes)
    install = tmp_path / "install"
    storage = tmp_path / "custom"
    for directory in (install / "images/definitions", install / "vol3/symbols", storage):
        directory.mkdir(parents=True)
    if existing_dumps:
        (storage / "dumps").mkdir()
    username = pwd.getpwuid(os.getuid()).pw_name
    # Run only the pre-migration setup, owned by this test user. A fresh custom
    # root has no dumps directory until migration or its first capture creates it.
    # No installer, service or privileged operation is reachable.
    commands = row.split('(cd "$SRC"', 1)[0]
    guard = """
        sudo() { "$@"; }
        install() {
            if [[ -n "$FAILED_PROBE" && "${@: -1}" == *".matrix-custody.$FAILED_PROBE" ]]; then
                return 73
            fi
            command install "$@"
        }
    """
    result = subprocess.run(
        ["bash", "-c", guard + commands + "\necho setup-completed"],
        env={
            **os.environ,
            "INSTALL_DIR": str(install),
            "CUSTOM_ROOT": str(storage),
            "SERVICE_USER": username,
            "FAILED_PROBE": failed_probe,
        },
        capture_output=True,
        text=True,
    )
    if failed_probe:
        assert result.returncode != 0
        assert "setup-completed" not in result.stdout
        return
    assert result.returncode == 0, result.stderr
    image_probe = next((install / "images/definitions").iterdir())
    assert image_probe.stat().st_uid == os.getuid()
    monkeypatch.setattr(image_admin_support, "settings", SimpleNamespace(images_dir=image_probe.parent))
    assert image_admin_support.list_images() == []
    assert list(image_probe.parent.iterdir()) == [image_probe]
    # Evidence migration grants shared reads while retaining its original
    # owner/group and mode; the matrix must check that historical custody.
    evidence = next((storage / "dumps").iterdir())
    before = evidence.stat()
    subprocess.run(
        [sys.executable, str(ROOT / "ops/deploy/permissions.py"), "drop-legacy-evidence-acl", str(storage), username],
        check=True,
    )
    after = evidence.stat()
    assert (after.st_uid, after.st_gid, after.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
    acl = subprocess.check_output(["getfacl", "-cpE", str(evidence)], text=True)
    assert "group:astinuslab:r--\nmask::r--" in acl
    assert 'check "$phase custom evidence child custody" "$SERVICE_USER:$SERVICE_USER:440"' in source


def test_rollback_rows_compare_database_privileged_runtime_and_manifest_state():
    text = MATRIX.read_text(encoding="utf-8")

    assert "privileged_state_digest" in text
    assert "service_state_digest" in text
    assert "database content restored" in text
    assert "database revision restored" in text
    assert "process cwd restored" in text
    assert "unit enablement restored" in text
    assert "access manifests and helpers restored" in text


def test_uninstall_rows_audit_every_recorded_acl_and_mac_change():
    text = MATRIX.read_text(encoding="utf-8")

    assert "recorded_acl_grants_remaining" in text
    assert "recorded_mac_changes_remaining" in text
    assert "all recorded traversal grants revoked" in text
    assert "all recorded MAC changes revoked" in text
    assert "MAC ownership record captured" in text
    assert "test-owned custom root removed" in text


def test_the_matrix_is_executable():
    assert MATRIX.stat().st_mode & 0o111, "operators run this directly"


def test_source_provenance_binds_archives_commits_and_extracted_trees():
    text = MATRIX.read_text(encoding="utf-8")

    assert "CANDIDATE_ARCHIVE" in text
    assert "PRIOR_ARCHIVE" in text
    assert "git get-tar-commit-id" in text
    assert '"$CANDIDATE_ARCHIVE_COMMIT" == "$CANDIDATE_SHA"' in text
    assert '"$PRIOR_ARCHIVE_COMMIT" == "$PRIOR_SHA"' in text
    assert "candidate_archive_sha256=" in text
    assert "prior_archive_sha256=" in text
    assert 'source_tree_digest "$SRC"' in text
    assert 'source_tree_digest "$PRIOR_SRC"' in text


def test_matrix_mutates_only_its_copy_and_proves_candidate_immutability():
    text = MATRIX.read_text(encoding="utf-8")

    assert 'CHANGED_SRC="$(mktemp -d /tmp/astinuslab-matrix-source.' in text
    assert 'check "exact candidate source remained immutable"' in text
    assert '"$(source_tree_digest "$SRC")"' in text
    assert "retaining unexpected matrix source fixture" in text


def test_selinux_probe_uses_the_exact_escaped_manifest_expression():
    text = MATRIX.read_text(encoding="utf-8")

    assert "selinux_expression" in text
    assert 'record.get("kind") == "selinux" and record.get("target") == root' in text
    assert 'grep -F "$CUSTOM_ROOT(/.*)?"' not in text


def test_existing_split_upgrade_preserves_all_mutable_root_custody():
    text = MATRIX.read_text(encoding="utf-8")

    assert "split_mutable_custody_digest" in text
    assert "split mutable custody unchanged" in text
    for relative in (
        "astinuslab.toml",
        "config",
        "images/definitions",
        "vol3/symbols",
        "storage",
        "storage/dumps",
        "storage/ssh_keys",
        "storage/playground/disks",
        "storage/playground/captures",
    ):
        assert relative in text


def test_uninstall_proves_all_single_and_split_units_are_quiesced():
    text = MATRIX.read_text(encoding="utf-8")

    assert "managed_units_with_live_pid" in text
    assert "all six units inactive with no PID" in text


def test_row8_checks_actual_split_role_logs_before_the_upgrade():
    source = MATRIX.read_text()
    row8 = source.split('row "8 split identity migration"', 1)[1].split('row "9 existing split upgrade"', 1)[0]
    assert 'check "split role logs written and API-readable"' in row8
    assert "verify_split_role_logs" in row8
    function = source.split("verify_split_role_logs() {", 1)[1].split("\n}\n", 1)[0]
    assert "timeout --kill-after=1s 28s" in function
    assert '"$SRC/tests/shell/host/verify_split_logs.py"' in function
    assert '--install "$INSTALL_DIR" --storage "$CUSTOM_ROOT"' in function


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "runuser-path",
        "missing",
        "empty",
        "wrong-uid",
        "unsafe-marker",
        "invalid-schema",
        "api-denied",
        "extra-acl",
        "current-warning",
        "missing-invocation",
        "malformed-invocation",
        "truncated-journal",
    ],
)
def test_split_role_log_acceptance_uses_real_metadata_and_installed_api_reader(failure):
    helper = ROOT / "tests/shell/host/verify_split_logs.py"
    script = (
        r"""
import importlib.util, json, os, pathlib, shutil, subprocess, sys, tempfile, types
spec = importlib.util.spec_from_file_location('proof', HELPER)
proof = importlib.util.module_from_spec(spec); spec.loader.exec_module(proof)
roles = {'api': 1, 'runner': 2, 'analysis': 3}
original_user = proof.pwd.getpwnam
proof.pwd.getpwnam = lambda name: types.SimpleNamespace(pw_uid=roles[name.removeprefix('astinuslab-')]) if name.startswith('astinuslab-') else original_user(name)
proof.grp.getgrnam = lambda name: types.SimpleNamespace(gr_gid=0)
original_run = proof.subprocess.run
reader_called = []
def run(command, **kwargs):
    if command[0] == 'runuser':
        reader_called.append(command)
        if FAILURE == 'runuser-path':
            assert shutil.which(command[0], path=kwargs['env']['PATH']), 'runuser absent from child PATH'
        if FAILURE == 'api-denied':
            return subprocess.CompletedProcess(command, 1)
        child = "import os,sys; os.setgroups([0]); os.setgid(0); os.setuid(1); os.execv(sys.argv[1], sys.argv[1:])"
        command = [sys.executable, '-c', child, *command[4:]]
    return original_run(command, **kwargs)
proof.subprocess.run = run
invocations = {role: str(uid) * 32 for role, uid in roles.items()}
def capture(command, deadline, limit):
    if command[0] == 'systemctl':
        role = command[2].removeprefix('astinuslab-').removesuffix('.service')
        if FAILURE == 'missing-invocation': return b''
        if FAILURE == 'malformed-invocation': return b'not-an-invocation\n'
        return (invocations[role] + '\n').encode()
    assert command[0] == 'journalctl'
    # A historical warning is outside every exact current invocation query.
    matches = [value for value in command if value.startswith('_SYSTEMD_INVOCATION_ID=')]
    assert len(matches) == 1 and matches[0].split('=', 1)[1] in invocations.values()
    if FAILURE == 'current-warning': return b'file logging disabled; preserved stale log file\n'
    if FAILURE == 'truncated-journal': return b'healthy line\n' * 201
    return b'current invocation started successfully\n'
proof.capture = capture
with tempfile.TemporaryDirectory(prefix='matrix-role-logs-') as temporary:
    outer = pathlib.Path(temporary); outer.chmod(0o755)
    install = outer / 'install'; install.mkdir(mode=0o755)
    release = install / 'releases/candidate'; (release / '.venv/bin').mkdir(parents=True)
    binary = release / '.venv/bin/python'; binary.symlink_to('/usr/bin/python3')
    (release / '.venv/pyvenv.cfg').write_text('home = /usr/bin\ninclude-system-site-packages = false\n')
    package = release / f'.venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/astinuslab/core'
    package.mkdir(parents=True)
    (package.parent / '__init__.py').touch(); (package / '__init__.py').touch()
    shutil.copyfile(READER, package / 'log_layout.py')
    (install / 'current').symlink_to(release)
    storage = outer / 'custom-root'; storage.mkdir(mode=0o755)
    anchor = storage / 'logs'; anchor.mkdir()
    def acl(path, entries):
        subprocess.run(['setfacl', '--set', ','.join(entries), str(path)], check=True)
    anchor.chmod(0o1700)
    acl(anchor, {'u::rwx','g::---','m::rwx','o::---','u:1:r-x','u:2:rwx','u:3:--x'})
    marker = anchor / '.astinuslab-service-logs.json'
    marker.write_text(json.dumps({'schema':'astinuslab-service-logs/v1','roles':roles})); marker.chmod(0o644)
    for role, filename in proof.FILES.items():
        directory = anchor / role; directory.mkdir(); os.chown(directory, roles[role], 0)
        acl(directory, {'u::rwx','u:1:r-x','g::---','m::rwx','o::---','d:u::rwx','d:u:1:rwx','d:g::---','d:m::rwx','d:o::---'})
        path = directory / filename; path.write_bytes(b'synthetic role log\n'); os.chown(path, roles[role], 0)
        acl(path, {'u::rw-','u:1:rw-','g::---','m::rw-','o::---'})
    victim = anchor / 'analysis/analysis-worker.log'
    if FAILURE == 'missing': victim.unlink()
    elif FAILURE == 'empty': victim.write_bytes(b'')
    elif FAILURE == 'wrong-uid': os.chown(victim, 2, 0)
    elif FAILURE == 'unsafe-marker': marker.chmod(0o666)
    elif FAILURE == 'invalid-schema': marker.write_text(json.dumps({'schema':'astinuslab-service-logs/v1','roles':dict(roles, api=1.0)}))
    elif FAILURE == 'extra-acl': subprocess.run(['setfacl','-m','u:4:r--',str(victim)],check=True)
    before = {p: (p.lstat().st_mode,p.lstat().st_uid,p.read_bytes()) for p in anchor.rglob('*') if p.is_file()}
    try: proof.verify(install, storage, wait_seconds=0)
    except (OSError, ValueError, TimeoutError) as error:
        expected = {'missing':'nonempty', 'empty':'nonempty', 'wrong-uid':'log custody', 'unsafe-marker':'marker custody', 'invalid-schema':'role schema', 'api-denied':'API log reader', 'extra-acl':'log ACL', 'current-warning':'file logging disabled', 'missing-invocation':'invocation', 'malformed-invocation':'invocation', 'truncated-journal':'journal completeness'}
        assert FAILURE in expected and expected[FAILURE] in str(error), (FAILURE, str(error))
    else:
        assert FAILURE in {'none', 'runuser-path'}, FAILURE
        assert len(reader_called) == 3
    after = {p: (p.lstat().st_mode,p.lstat().st_uid,p.read_bytes()) for p in anchor.rglob('*') if p.is_file()}
    assert before == after, 'acceptance proof modified installed log state'
""".replace("HELPER", repr(str(helper)))
        .replace("READER", repr(str(ROOT / "src/astinuslab/core/log_layout.py")))
        .replace("FAILURE", repr(failure))
    )
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "relative, ignored",
    [
        ("storage/dbs/application.sqlite-wal", True),
        ("storage/dbs/broker.sqlite-shm", True),
        ("storage/dbs/results.sqlite-journal", True),
        ("storage/logs/api/api.log", True),
        ("storage/logs/runner/runner.log.12", True),
        ("storage/dbs/application.sqlite", False),
        ("storage/dbs/unrelated.sqlite-wal", False),
        ("storage/logs/api.log", False),
        ("storage/logs/api/api.log.0", False),
        ("storage/logs/runner/runner.log.01", False),
        ("storage/logs/analysis/analysis-worker.log.1000000000000000000", False),
        ("storage/logs/runner/operator.log", False),
        ("storage/logs/.astinuslab-service-logs.json", False),
    ],
)
def test_mutable_custody_digest_omits_only_configured_sidecars_and_role_rotations(tmp_path, relative, ignored):
    install = tmp_path / "install"
    custom = tmp_path / "custom"
    custom.mkdir()
    for name in (
        "config",
        "images/definitions",
        "vol3/symbols",
        "storage/dbs",
        "storage/logs/api",
        "storage/logs/runner",
        "storage/logs/analysis",
    ):
        (install / name).mkdir(parents=True)
    (install / "astinuslab.toml").write_text("[app]\n")
    target = install / relative
    target.write_bytes(b"stable synthetic bytes")
    target.chmod(0o644)
    databases = [str(install / "storage/dbs" / (name + ".sqlite")) for name in ("application", "broker", "results")]
    source = MATRIX.read_text()
    function = source.split("split_mutable_custody_digest() {", 1)[1].split("\n}\n", 1)[0]
    harness = f"""
set -euo pipefail
INSTALL_DIR={shlex.quote(str(install))}
CUSTOM_ROOT={shlex.quote(str(custom))}
installed_database_python() {{ printf '%s\\n' {shlex.quote(json.dumps(databases))}; }}
sudo() {{ "$@"; }}
split_mutable_custody_digest() {{{function}
}}
split_mutable_custody_digest
"""
    before = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert before.returncode == 0, before.stderr
    target.chmod(0o600)
    after = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert after.returncode == 0, after.stderr
    assert (before.stdout == after.stdout) is ignored
    target.unlink()
    removed = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert removed.returncode == 0, removed.stderr
    assert (before.stdout == removed.stdout) is ignored


@pytest.mark.parametrize("destination", ["new", "existing", "symlink"])
def test_custody_receipt_records_metadata_without_replacing_existing_files(tmp_path, destination):
    install, custom = tmp_path / "install", tmp_path / "custom"
    for path in (custom, *(install / p for p in ("config", "images/definitions", "vol3/symbols", "storage"))):
        path.mkdir(parents=True)
    (install / "astinuslab.toml").write_text("synthetic bytes must stay private")
    receipt = tmp_path / "receipt.json"
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged")
    if destination == "existing":
        receipt.write_text("unchanged")
    elif destination == "symlink":
        receipt.symlink_to(sentinel)
    body = MATRIX.read_text().split("split_mutable_custody_digest() {", 1)[1].split("\n}\n", 1)[0]
    harness = f"""
    set -eu
    INSTALL_DIR={shlex.quote(str(install))}
    CUSTOM_ROOT={shlex.quote(str(custom))}
    installed_database_python() {{ echo '[]'; }}
    sudo() {{ "$@"; }}
    split_mutable_custody_digest() {{{body}
    }}
    split_mutable_custody_digest {shlex.quote(str(receipt))}
    """
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10)
    assert sentinel.read_text() == "unchanged"
    if destination != "new":
        assert result.returncode != 0
        assert receipt.read_text() == "unchanged"
        return
    assert result.returncode == 0, result.stderr
    payload = receipt.read_text()
    assert "synthetic bytes" not in payload + result.stdout + result.stderr
    assert receipt.stat().st_mode & 0o777 == 0o600
    data = json.loads(payload)
    digest = hashlib.sha256()
    for record in data["records"]:
        digest.update(f"{record['path']}\0{record['uid']}:{record['gid']}:{record['mode']:04o}\0".encode())
        digest.update(record["acl"].encode() + b"\0")
    assert digest.hexdigest() == data["sha256"] == result.stdout.strip()
    assert {record["path"] for record in data["records"]} == {
        str(install / p) for p in ("astinuslab.toml", "config", "images/definitions", "vol3/symbols", "storage")
    } | {str(custom)}


def test_split_custody_assertions_keep_new_anchors_and_logs_at_each_boundary():
    source = MATRIX.read_text()
    assert "$INSTALL_DIR/storage|astinuslab-api:astinuslab:3770" in source
    assert "$CUSTOM_ROOT|astinuslab-api:astinuslab:3770" in source
    assert "$INSTALL_DIR/storage/logs|root:root:1770" in source
    assert "$CUSTOM_ROOT/logs|root:root:1770" in source
    assert source.count('"$(verify_split_role_logs; echo $?)"') == 3
    assert source.index('check_split_custody "pre-upgrade"') < source.index("R9_MUTABLE_CUSTODY_BEFORE=")


@pytest.mark.parametrize("acl", ["traverse", "missing", "read", "write", "extra-user", "masked"])
def test_split_custody_requires_only_evidence_traversal(tmp_path, acl):
    playground = tmp_path / "playground"
    playground.mkdir(mode=0o700)
    entries = "u::rwx,g::---,g:astinuslab:--x,m::--x,o::---"
    if acl == "missing":
        entries = "u::rwx,g::---,o::---"
    elif acl in {"read", "write"}:
        grant = "r-x" if acl == "read" else "-wx"
        entries = f"u::rwx,g::---,g:astinuslab:{grant},m::{grant},o::---"
    elif acl == "extra-user":
        entries += ",u:nobody:--x"
    elif acl == "masked":
        entries = entries.replace("m::--x", "m::---")
    subprocess.run(["setfacl", "--set", entries, str(playground)], check=True)
    body = MATRIX.read_text().split("check_split_custody() {", 1)[1].split("\n}\n", 1)[0]
    harness = f"""
        set -eu
        CUSTOM_ROOT={shlex.quote(str(tmp_path))}
        INSTALL_DIR=/unused-install
        SERVICE_USER=fixture-operator
        check() {{
          case "$1" in
            *"$CUSTOM_ROOT/playground custody"|*"custom Playground evidence traversal ACL")
              [[ "$2" == "$3" ]] || {{ echo "$1"; exit 91; }} ;;
          esac
        }}
        sudo() {{
          case "$1" in
            stat)
              if [[ "${{@: -1}}" == "$CUSTOM_ROOT/playground" ]]; then
                printf 'astinuslab-runner:astinuslab:%s' "$(stat -c %a "$CUSTOM_ROOT/playground")"
              fi ;;
            getfacl) command "$@" ;;
            cat) echo fixture-key ;;
            *) exit 92 ;;
          esac
        }}
        named_acl_permissions() {{ :; }}
        db_path() {{ echo /unused-db; }}
        check_split_custody() {{{body}
        }}
        check_split_custody fixture
    """
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is (acl == "traverse"), result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["none", "bytes", "timeout"])
def test_role_log_journal_capture_is_bounded_and_does_not_print_payload(failure, capsys):
    import importlib.util
    import time

    spec = importlib.util.spec_from_file_location("split_log_proof", ROOT / "tests/shell/host/verify_split_logs.py")
    assert spec is not None and spec.loader is not None
    proof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    code = "import os; os.write(1, b'synthetic current invocation\\n')"
    deadline = time.monotonic() + 2
    limit = 128
    if failure == "bytes":
        code = "import os; os.write(1, b'x' * 1024)"
    elif failure == "timeout":
        code = "import time; time.sleep(1)"
        deadline = time.monotonic() + 0.05
    if failure == "none":
        assert proof.capture([sys.executable, "-c", code], deadline, limit) == b"synthetic current invocation\n"
    else:
        with pytest.raises((ValueError, TimeoutError)):
            proof.capture([sys.executable, "-c", code], deadline, limit)
    assert capsys.readouterr().out == ""
