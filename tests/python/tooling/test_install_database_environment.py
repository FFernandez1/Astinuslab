"""Real Bash/SQLite with disposable releases; identity/service boundaries are fixtures."""

import os
import pwd
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALL = ROOT / "ops/deploy/install.sh"


def marker(path):
    with sqlite3.connect(path) as db:
        return db.execute("select value from remediation_marker").fetchone()[0]


@pytest.fixture
def deployment(tmp_path):
    install = tmp_path / "install"
    release = install / "releases/candidate"
    (release / ".venv/bin").mkdir(parents=True)
    (install / "config").mkdir()
    (install / "storage").mkdir()
    python = release / ".venv/bin/python"
    python.write_text(
        f'#!/bin/bash\nexport PYTHONPATH={shlex.quote(str(ROOT / "src") + ":" + str(ROOT))}\nexec {shlex.quote(sys.executable)} "$@"\n'
    )
    python.chmod(0o755)
    paths = {name: install / "storage" / f"{name}.db" for name in ("actual", "toml", "ambient", "astinuslab")}
    for name, path in paths.items():
        with sqlite3.connect(path) as db:
            db.execute("create table remediation_marker(value text)")
            db.execute("insert into remediation_marker values(?)", (name,))
    (install / "astinuslab.toml").write_text('[storage]\ndb_path = "storage/toml.db"\n')
    (install / "config/astinuslab.env").write_text(
        f'ASTINUSLAB_DB_PATH="{paths["actual"]}"\nASTINUSLAB_FAKE_LIBVIRT=1\nASTINUSLAB_FAKE_PLAYGROUND=1\n'
    )
    harness = f"""
source {shlex.quote(str(INSTALL))}
INSTALL_DIR={shlex.quote(str(install))}
RELEASE_DIR={shlex.quote(str(release))}
SERVICE_USER={shlex.quote(pwd.getpwuid(os.getuid()).pw_name)}
API_USER={shlex.quote(pwd.getpwuid(os.getuid()).pw_name)}
install() {{ mkdir -p "${{@: -1}}"; }}
sudo() {{ shift 2; "$@"; }}
runuser() {{ shift 3; "$@"; }}
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASTINUSLAB_")}
    env.update(ASTINUSLAB_DATA_DIR=str(tmp_path / "ambient-root"), ASTINUSLAB_DB_PATH=str(paths["ambient"]))

    def run(body):
        return subprocess.run(["bash", "-c", harness + body], env=env, text=True, capture_output=True)

    return install, paths, run


@pytest.mark.parametrize("split", ["false", "true"])
def test_backup_migrate_restore_and_readiness_use_service_database(deployment, split):
    install, paths, run = deployment
    before = {name: path.read_bytes() for name, path in paths.items() if name != "actual"}
    body = f"""
SPLIT_USERS={split}
backup_database_before_migration || exit 11
apply_db_migrations || exit 12
run_deep_readiness_as fixture "$RELEASE_DIR" 'from astinuslab.core.config import settings; from astinuslab.db import current_revision; print("readiness_database=" + str(settings.db_path)); assert current_revision()' || exit 13
"$RELEASE_DIR/.venv/bin/python" -c {shlex.quote(f'import sqlite3; db=sqlite3.connect({str(paths["actual"])!r}); db.execute("UPDATE remediation_marker SET value=99"); db.commit()')}
restore_database_from_backup || exit 14
"""
    result = run(body)
    assert result.returncode == 0, result.stdout + result.stderr
    backups = list((install / "backups").glob("*.db"))
    assert len(backups) == 1
    assert marker(backups[0]) == "actual"
    assert marker(paths["actual"]) == "actual"
    assert f"readiness_database={paths['actual']}" in result.stdout
    assert str(paths["actual"]) in result.stdout + result.stderr
    assert all(path.read_bytes() == before[name] for name, path in paths.items() if name != "actual")


def test_migration_child_failure_propagates_in_negated_condition(deployment):
    _, _, run = deployment
    result = run("""
sudo() { echo 'migration-child-failed-27' >&2; return 27; }
if ! apply_db_migrations; then echo rejected; else echo MASKED; fi
""")
    assert result.returncode == 0
    assert "rejected" in result.stdout
    assert "Database migrations applied" not in result.stdout


def test_migration_bad_release_fails_before_child(deployment):
    _, _, run = deployment
    result = run("""
RELEASE_DIR="$INSTALL_DIR/missing"
sudo() { echo CHILD-MUST-NOT-RUN; }
if ! apply_db_migrations; then echo rejected; fi
""")
    assert "rejected" in result.stdout
    assert "CHILD-MUST-NOT-RUN" not in result.stdout


def test_no_start_upgrade_rolls_back_before_switch_on_real_migration_failure(deployment):
    _, _, run = deployment
    result = run("""
START_SERVICES=false
migration_snapshot() { :; }
install_source() { :; }
quiesce_services() { :; }
migration_snapshot_mutable_state() { :; }
prepare_single_upgrade_mutable_state() { :; }
prepare_storage() { :; }
sudo() { return 27; }
rollback_existing_single_upgrade() { echo ROLLBACK; restore_database_from_backup; }
handle_ssh_key() { echo FORBIDDEN-AFTER-MIGRATION; return 1; }
switch_release_and_restart() { echo FORBIDDEN-SWITCH; }
if ! upgrade_single_identity_install; then echo rejected; fi
""")
    assert result.returncode == 0, result.stderr
    assert "ROLLBACK" in result.stdout and "rejected" in result.stdout
    assert "FORBIDDEN" not in result.stdout
    assert "Database migrations applied" not in result.stdout


@pytest.mark.parametrize(
    ("assignment", "value"),
    [
        ("plain  interior  spaces  ", "plain  interior  spaces"),
        ('"path with spaces#hash"', "path with spaces#hash"),
        ("'literal $VAR $(command) `command`'", "literal $VAR $(command) `command`"),
        ('unquoted"quotes"', 'unquoted"quotes"'),
        ("foo\\ bar", "foo bar"),
        ("one\\\ntwo", "onetwo"),
        ('"one\\\ntwo"', "onetwo"),
        ("'one\ntwo'", "one\ntwo"),
        ('"one\\qtwo"', "one\\qtwo"),
        ('"one\\$two"', "one$two"),
        ("path\\ ", "path "),
        ('"path\rname"', "path\rname"),
        ("'path\rname'", "path\rname"),
        ("", ""),
    ],
)
def test_environment_file_preserves_systemd_values(tmp_path, assignment, value):
    from ops.deploy.runtime_environment import read_environment

    path = tmp_path / "runtime.env"
    path.write_text("# ignored\n; ignored too\nKEY=overridden\nKEY=" + assignment + "\n")
    assert read_environment(path) == {"KEY": value}


@pytest.mark.parametrize(
    "contents", ["not-an-assignment", "1INVALID=foo", 'KEY="unterminated', "KEY=bad\x00value", "KEY=value\\"]
)
def test_environment_parser_rejects_ambiguous_input(tmp_path, contents):
    from ops.deploy.runtime_environment import read_environment

    path = tmp_path / "runtime.env"
    path.write_text(contents)
    with pytest.raises(ValueError):
        read_environment(path)


@pytest.mark.parametrize("mode", ["env-relative", "toml", "default", "data-root"])
def test_effective_database_precedence_without_ambient_leakage(deployment, mode):
    install, paths, run = deployment
    env_file = install / "config/astinuslab.env"
    if mode == "env-relative":
        env_file.write_text('ASTINUSLAB_DB_PATH="storage/actual.db"\n')
        expected = paths["actual"]
    elif mode == "toml":
        env_file.write_text("")
        expected = paths["toml"]
    elif mode == "default":
        env_file.unlink()
        (install / "astinuslab.toml").unlink()
        expected = paths["astinuslab"]
    else:
        other = install / "other-data"
        other.mkdir()
        (other / "astinuslab.toml").write_text(f'[storage]\ndb_path="{paths["actual"]}"\n')
        env_file.write_text(f'ASTINUSLAB_DATA_DIR="{other}"\n')
        expected = paths["actual"]
    result = run("backup_database_before_migration")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"effective_database={expected}" in result.stderr
    assert marker(next((install / "backups").glob("*.db"))) == marker(expected)


def test_root_database_environment_cannot_execute_shell_or_inject_python(deployment):
    install, _, run = deployment
    injected = install / "python-injection"
    injected.mkdir()
    canary = install / "forbidden-execution"
    (injected / "sitecustomize.py").write_text(f"from pathlib import Path; Path({str(canary)!r}).touch()")
    with (install / "config/astinuslab.env").open("a") as file:
        file.write(f"PYTHONPATH={injected}\nUNUSED=$(touch {canary})\n")
    command = [
        sys.executable,
        "-I",
        str(ROOT / "ops/deploy/runtime_environment.py"),
        "--data-dir",
        str(install),
        "--settings-only",
        "--",
        sys.executable,
        "-c",
        'import os; assert "PYTHONPATH" not in os.environ; assert "UNUSED" not in os.environ',
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not canary.exists()


def test_post_switch_failure_restores_service_db_and_previous_release(deployment):
    install, paths, run = deployment
    previous = install / "releases/previous"
    previous.mkdir()
    result = run(f"""
PREVIOUS_RELEASE_DIR={shlex.quote(str(previous))}
backup_database_before_migration || exit 11
apply_db_migrations || exit 12
switch_current_release "$RELEASE_DIR" || exit 13
quiesce_services() {{ :; }}
release_rollback "injected post-switch failure" false || exit 14
""")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "current").resolve() == previous
    assert marker(paths["actual"]) == "actual"
    with sqlite3.connect(paths["actual"]) as db:
        assert db.execute("select name from sqlite_master where type='table'").fetchall() == [("remediation_marker",)]


def test_service_checks_do_not_require_traversal_of_installer_checkout(deployment):
    _, _, run = deployment
    # Model the service identity's EACCES at a private operator checkout. The
    # installer can open its own evaluator, while API/runner identities cannot.
    result = run("""
sudo() {
  shift 2
  for argument; do
    if [[ "$argument" == "$SCRIPT_DIR/runtime_environment.py" ]]; then
      echo 'service cannot traverse private installer checkout' >&2
      return 77
    fi
  done
  "$@"
}
runuser() { shift 3; sudo -u fixture "$@"; }
apply_db_migrations || exit 12
run_deep_readiness_as fixture "$RELEASE_DIR" 'print("service probe reached")' || exit 13
""")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "service probe reached" in result.stdout


def test_continuation_preserves_systemd_significant_whitespace(tmp_path):
    import ctypes

    from ops.deploy.runtime_environment import read_environment

    library = Path("/usr/lib/x86_64-linux-gnu/systemd/libsystemd-shared-255.so")
    if not library.exists():
        pytest.skip("systemd 255 parser library required for component parity")
    env_file = tmp_path / "runtime.env"
    env_file.write_bytes(b"ASTINUSLAB_DB_PATH=storage/actual.db  \\\n\n")
    lib = ctypes.CDLL(str(library))
    vector = ctypes.POINTER(ctypes.c_char_p)
    lib.load_env_file.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(vector)]
    lib.load_env_file.restype = ctypes.c_int
    lib.strv_free.argtypes = [vector]
    lib.strv_free.restype = ctypes.c_void_p
    result = vector()
    status = lib.load_env_file(None, str(env_file).encode(), ctypes.byref(result))
    try:
        assert status == 0
        systemd_value = result[0].decode().split("=", 1)[1]
    finally:
        lib.strv_free(result)
    assert systemd_value == "storage/actual.db  "
    assert read_environment(env_file)["ASTINUSLAB_DB_PATH"] == systemd_value


def test_continuation_database_backup_migrate_restore_protects_spaced_sentinel(deployment):
    install, paths, run = deployment
    service_db = paths["actual"].with_name("actual.db  ")
    with sqlite3.connect(service_db) as db:
        db.execute("create table remediation_marker(value text)")
        db.execute("insert into remediation_marker values(?)", ("SERVICE_TRAILING_SPACES",))
    before = {name: path.read_bytes() for name, path in paths.items()}
    (install / "config/astinuslab.env").write_bytes(b"ASTINUSLAB_DB_PATH=storage/actual.db  \\\n\n")
    readiness_code = f"from astinuslab.core.config import settings; from astinuslab.db import current_revision; assert str(settings.db_path) == {str(service_db)!r}; assert current_revision()"
    mutate = f'import sqlite3; db=sqlite3.connect({str(service_db)!r}); db.execute("UPDATE remediation_marker SET value=99"); db.commit()'
    result = run(f"""
backup_database_before_migration || exit 11
apply_db_migrations || exit 12
run_deep_readiness_as fixture "$RELEASE_DIR" {shlex.quote(readiness_code)} || exit 13
"$RELEASE_DIR/.venv/bin/python" -c {shlex.quote(mutate)} || exit 14
restore_database_from_backup || exit 15
""")
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker(next((install / "backups").glob("*.db"))) == "SERVICE_TRAILING_SPACES"
    assert marker(service_db) == "SERVICE_TRAILING_SPACES"
    assert all(path.read_bytes() == before[name] for name, path in paths.items())
    with sqlite3.connect(service_db) as db:
        assert db.execute("select name from sqlite_master where type='table'").fetchall() == [("remediation_marker",)]
