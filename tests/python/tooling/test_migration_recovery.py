"""Contract for the installer's migration recovery protocol.

Rolling the ``current`` symlink back is instant, but the schema only moves
forward, so an upgrade that migrates has to take a verified backup first and put
both halves back when anything after it fails.
"""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "ops/deploy/install.sh"
pytestmark = [pytest.mark.unit, pytest.mark.host]


def run_bash(script: str, *, env=None, cwd=ROOT):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(["bash", "-c", script], cwd=cwd, env=merged, capture_output=True, text=True)


def _harness(install: Path, body: str, *, release: Path | None = None) -> str:
    release = release or (install / "releases/1.0.0-current00000")
    return f"""
    source {INSTALL}
    INSTALL_DIR={shlex.quote(str(install))}
    RELEASE_DIR={shlex.quote(str(release))}
    SERVICE_USER=$(id -un)
    API_USER=$(id -un)
    {body}
    """


def test_backup_precedes_the_migration_and_the_migration_precedes_readiness():
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("upgrade_single_identity_install()") : text.index("upgrade_existing_split_install()")]

    quiesce = body.index("quiesce_services")
    backup = body.index("backup_database_before_migration")
    migrate = body.index("apply_db_migrations")

    assert quiesce < backup < migrate, "the schema is only touched after a verified backup exists"


def test_the_backup_lives_outside_the_storage_tree():
    """Storage is what evidence bundles are cut from, and it holds the SSH key
    directory. The backup of the custody record belongs beside it, not in it."""
    text = INSTALL.read_text(encoding="utf-8")
    body = text[text.index("backup_database_before_migration()") : text.index("restore_database_from_backup()")]

    assert '"$INSTALL_DIR/backups' in body
    assert "storage" not in body.split("DB_BACKUP=")[0] or "$INSTALL_DIR/storage" not in body


def test_backup_records_the_hash_and_mode_it_was_given(tmp_path):
    install = tmp_path / "install"
    release = install / "releases/1.0.0-current00000"
    (release / ".venv/bin").mkdir(parents=True)
    fake = release / ".venv/bin/python"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "backup: path=/tmp/x.db sha256=abc123 mode=0600 revision=a021_evidence_tombstones"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)

    body = (
        # `install -d -o root` needs root; the directory itself is not what this
        # test is about.
        'install() { mkdir -p "${@: -1}"; }\n'
        "    backup_database_before_migration\n"
        '    echo "recorded=$DB_BACKUP_SHA256"'
    )
    result = run_bash(_harness(install, body))

    assert result.returncode == 0, result.stderr
    assert "recorded=abc123" in result.stdout
    assert "a021_evidence_tombstones" in result.stdout, "the revision a rollback targets is recorded"


def test_a_failed_migration_restores_the_database_and_the_previous_release(tmp_path):
    install = tmp_path / "install"
    previous = install / "releases/1.0.0-previous0000"
    current = install / "releases/1.0.0-current00000"
    previous.mkdir(parents=True)
    current.mkdir(parents=True)
    (install / "current").symlink_to(current)
    calls = tmp_path / "calls.log"

    body = f"""
    PREVIOUS_RELEASE_DIR={shlex.quote(str(previous))}
    DB_BACKUP={shlex.quote(str(tmp_path / "pre.db"))}
    restore_database_from_backup() {{ echo restore >> {shlex.quote(str(calls))}; }}
    control_services() {{ echo restart >> {shlex.quote(str(calls))}; }}
    verify_release_readiness() {{ :; }}
    quiesce_services() {{ :; }}
    release_rollback "migration failed"
    echo "exit=$?"
    """
    result = run_bash(_harness(install, body, release=current))

    assert "exit=0" in result.stdout, result.stdout + result.stderr
    assert calls.read_text(encoding="utf-8").split() == ["restore", "restart"], "the database goes back before services"
    assert os.readlink(install / "current").endswith("1.0.0-previous0000")


def test_rollback_without_a_previous_release_still_restores_the_database(tmp_path):
    """A first install has nothing to roll the symlink back to. The schema must
    still be returned, and the failure must stay visible."""
    install = tmp_path / "install"
    current = install / "releases/1.0.0-current00000"
    current.mkdir(parents=True)
    (install / "current").symlink_to(current)
    calls = tmp_path / "calls.log"

    body = f"""
    PREVIOUS_RELEASE_DIR=""
    DB_BACKUP={shlex.quote(str(tmp_path / "pre.db"))}
    restore_database_from_backup() {{ echo restore >> {shlex.quote(str(calls))}; }}
    control_services() {{ :; }}
    verify_release_readiness() {{ :; }}
    quiesce_services() {{ :; }}
    release_rollback "migration failed"
    echo "exit=$?"
    """
    run_bash(_harness(install, body, release=current))

    assert "restore" in calls.read_text(encoding="utf-8")
    assert os.readlink(install / "current").endswith("1.0.0-current00000"), "nothing to switch back to"


def test_a_failed_restore_is_surfaced_and_names_the_backup(tmp_path):
    install = tmp_path / "install"
    current = install / "releases/1.0.0-current00000"
    previous = install / "releases/1.0.0-previous0000"
    for path in (current, previous):
        path.mkdir(parents=True)
    (install / "current").symlink_to(current)
    calls = tmp_path / "calls.log"

    body = f"""
    PREVIOUS_RELEASE_DIR={shlex.quote(str(previous))}
    DB_BACKUP={shlex.quote(str(tmp_path / "pre.db"))}
    restore_database_from_backup() {{ return 1; }}
    control_services() {{ echo restart >> {shlex.quote(str(calls))}; }}
    verify_release_readiness() {{ echo readiness >> {shlex.quote(str(calls))}; }}
    quiesce_services() {{ :; }}
    if release_rollback "migration failed"; then echo "exit=0"; else echo "exit=1"; fi
    """
    result = run_bash(_harness(install, body, release=current))

    combined = result.stdout + result.stderr
    assert "exit=1" in result.stdout, combined
    assert str(tmp_path / "pre.db") in combined, "a failed restore has to name the backup an operator must use"
    assert not calls.exists(), "old code must stay stopped against a database that was not restored"
    assert os.readlink(install / "current").endswith("1.0.0-previous0000")


def test_every_migration_call_site_is_preceded_by_a_backup():
    """One protected path is not enough. `--split-users` bootstraps a candidate
    of its own, and that path migrates too."""
    lines = INSTALL.read_text(encoding="utf-8").splitlines()
    unprotected = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("apply_db_migrations") or stripped.startswith("apply_db_migrations()"):
            continue
        window = "\n".join(lines[max(0, index - 4) : index])
        if "backup_database_before_migration" not in window:
            unprotected.append(f"{index + 1}: {stripped}")
    assert unprotected == [], unprotected


def _recording_python(release: Path) -> Path:
    """A stand-in for the release interpreter that reports how it was invoked."""
    (release / ".venv/bin").mkdir(parents=True, exist_ok=True)
    python = release / ".venv/bin/python"
    python.write_text(
        "#!/usr/bin/env bash\n"
        'echo "cwd=$PWD data_dir=${ASTINUSLAB_DATA_DIR:-unset}" >&2\n'
        'echo "backup: path=/tmp/x.db sha256=abc mode=0600 revision=a021"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    return python


def test_backup_runs_against_the_installed_database_not_the_checkout(tmp_path):
    """`python -m astinuslab.db` puts the working directory on sys.path. Run
    from the source checkout without the data root, it resolves its own paths
    and backs up the checkout's database instead of the installed one."""
    install = tmp_path / "install"
    release = install / "releases/1.0.0-current00000"
    _recording_python(release)

    body = 'install() { mkdir -p "${@: -1}"; }\n    backup_database_before_migration'
    result = run_bash(_harness(install, body, release=release))

    assert result.returncode == 0, result.stderr
    assert f"cwd={release}" in result.stderr, result.stderr
    assert f"data_dir={install}" in result.stderr, result.stderr


def test_restore_runs_against_the_installed_database_not_the_checkout(tmp_path):
    install = tmp_path / "install"
    release = install / "releases/1.0.0-current00000"
    _recording_python(release)

    body = f"DB_BACKUP={shlex.quote(str(tmp_path / 'pre.db'))}\n    restore_database_from_backup"
    result = run_bash(_harness(install, body, release=release))

    assert result.returncode == 0, result.stderr
    assert f"cwd={release}" in result.stderr, result.stderr
    assert f"data_dir={install}" in result.stderr, result.stderr


def test_a_rolled_back_migration_never_continues_installing():
    """A successful rollback is still a failed install. Treating it as recovered
    let main() carry on and switch forward to the very release it had just
    rolled away from."""
    text = INSTALL.read_text(encoding="utf-8")
    # Every place the installer migrates must guard the call, not chain it: a
    # `cmd || rollback || die` chain is satisfied when the rollback succeeds and
    # execution simply carries on.
    call_sites = [
        line.strip()
        for line in text.splitlines()
        if "apply_db_migrations" in line and "apply_db_migrations()" not in line
    ]
    assert call_sites, "no migration call sites found"
    for site in call_sites:
        assert site.startswith("if ! apply_db_migrations"), site


def test_rollback_verifies_the_release_it_restored(tmp_path):
    """`control_services` returns once systemd has accepted the restart, which is
    before the restored release is serving. Reporting "restored" at that moment
    hands the operator a host that is still down and calls it recovered."""
    install = tmp_path / "install"
    current = install / "releases/1.0.0-current00000"
    previous = install / "releases/1.0.0-previous0000"
    for path in (current, previous):
        path.mkdir(parents=True)
    (install / "current").symlink_to(current)
    calls = tmp_path / "calls.log"
    log = shlex.quote(str(calls))

    body = f"""
    PREVIOUS_RELEASE_DIR={shlex.quote(str(previous))}
    DB_BACKUP=""
    control_services() {{ echo restart >> {log}; }}
    verify_release_readiness() {{ echo "readiness=$1" >> {log}; return 0; }}
    quiesce_services() {{ :; }}
    release_rollback "migration failed"
    """
    run_bash(_harness(install, body, release=current))

    logged = calls.read_text(encoding="utf-8")
    assert f"readiness={previous}" in logged, "the restored release is the one that must be verified"
    assert logged.index("restart") < logged.index("readiness"), "verify after the restart, not before it"


def test_a_restored_release_that_does_not_serve_is_not_reported_as_recovered(tmp_path):
    install = tmp_path / "install"
    current = install / "releases/1.0.0-current00000"
    previous = install / "releases/1.0.0-previous0000"
    for path in (current, previous):
        path.mkdir(parents=True)
    (install / "current").symlink_to(current)

    body = f"""
    PREVIOUS_RELEASE_DIR={shlex.quote(str(previous))}
    DB_BACKUP=""
    control_services() {{ :; }}
    verify_release_readiness() {{ return 1; }}
    quiesce_services() {{ :; }}
    if release_rollback "migration failed"; then echo "exit=0"; else echo "exit=1"; fi
    """
    result = run_bash(_harness(install, body, release=current))

    combined = result.stdout + result.stderr
    assert "exit=1" in combined, "a rollback that leaves the host down has not completed"
    assert "not serving" in combined
