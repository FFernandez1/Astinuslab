"""Real SQLite and private filesystem transactions; no installed state/services."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import ExitStack, closing
from pathlib import Path

import pytest

from ops.deploy import shared_databases as layout

pytestmark = [pytest.mark.unit, pytest.mark.host]


def make_database(path: Path, value: str = "original") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("create table marker(value text)")
        connection.execute("insert into marker values (?)", (value,))


def value(path: Path) -> str:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return connection.execute("select value from marker").fetchone()[0]


@pytest.fixture
def installation(tmp_path):
    install = tmp_path / "install"
    storage = install / "storage"
    storage.mkdir(parents=True)
    (install / "config").mkdir()
    (install / "config/astinuslab.env").write_text("KEEP=operator-value\n")
    external = tmp_path / "evidence"
    external.mkdir()
    endpoints = {}
    for role, name in (
        ("application", "astinuslab.db"),
        ("broker", "celery_broker.db"),
        ("result", "celery_results.db"),
    ):
        path = storage / name
        make_database(path, role)
        endpoints[role] = {"kind": "sqlite", "path": str(path)}
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(mode=0o700)
    return install, external, endpoints, snapshot


def build(installation):
    install, external, endpoints, _ = installation
    return layout.build_plan(
        install, external, endpoints, api_uid=os.getuid(), shared_gid=os.getgid(), allowed_uids={os.getuid()}
    )


def test_standard_flat_endpoints_plan_all_three_destinations(installation):
    install, _, _, _ = installation
    plan = build(installation)
    assert {entry["destination"] for entry in plan["entries"]} == {
        str(install / "storage/databases" / name) for name in ("astinuslab.db", "celery_broker.db", "celery_results.db")
    }
    assert not (install / "storage/databases").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo", "destination"])
def test_alias_or_conflicting_destination_refuses_before_mutation(installation, unsafe):
    install, _, endpoints, _ = installation
    application = Path(endpoints["application"]["path"])
    if unsafe == "symlink":
        application.rename(application.with_suffix(".original"))
        application.symlink_to(application.with_suffix(".original"))
    elif unsafe == "hardlink":
        os.link(application, application.with_suffix(".alias"))
    elif unsafe == "fifo":
        application.unlink()
        os.mkfifo(application)
    else:
        destination = install / "storage/databases"
        destination.mkdir()
        make_database(destination / "astinuslab.db", "unrelated")
    before = (install / "config/astinuslab.env").read_bytes()
    with pytest.raises(layout.LayoutError):
        build(installation)
    assert (install / "config/astinuslab.env").read_bytes() == before
    assert not (install / ".sqlite-layout.pending.json").exists()


def test_safe_custom_and_nonlocal_endpoints_are_preserved(installation):
    install, _, endpoints, _ = installation
    custom = install / "storage/custom-db"
    custom.mkdir(mode=0o770)
    custom.chmod(0o2770)
    make_database(custom / "operator.db", "custom")
    (custom / "operator.db").chmod(0o660)
    endpoints["application"] = {"kind": "sqlite", "path": str(custom / "operator.db")}
    endpoints["broker"] = {"kind": "external"}
    plan = build(installation)
    assert set(plan["replacements"]) == {"ASTINUSLAB_CELERY_RESULT_BACKEND"}
    assert value(custom / "operator.db") == "custom"


def test_unsafe_custom_endpoint_refuses_before_mutation(installation, tmp_path):
    _, _, endpoints, _ = installation
    outside = tmp_path / "operator.db"
    make_database(outside)
    endpoints["application"] = {"kind": "sqlite", "path": str(outside)}
    with pytest.raises(layout.LayoutError, match="custom"):
        build(installation)
    assert value(outside) == "original"


def test_staging_preserves_original_bytes_and_inodes_and_does_not_create_external_dbs(installation):
    install, external, endpoints, snapshot = installation
    before = {
        role: (Path(row["path"]).stat().st_ino, Path(row["path"]).read_bytes()) for role, row in endpoints.items()
    }
    plan = build(installation)
    layout.stage(plan, snapshot)
    for role, row in endpoints.items():
        old = Path(row["path"])
        assert (old.stat().st_ino, old.read_bytes()) == before[role]
        target = install / "storage/databases" / old.name
        assert value(target) == role
        assert stat.S_IMODE(target.stat().st_mode) == 0o660
    assert stat.S_IMODE((install / "storage/databases").stat().st_mode) == 0o2770
    assert not list(external.glob("*.db"))


@pytest.mark.parametrize("role", ["application", "broker", "result"])
def test_staging_preserves_committed_wal_only_rows(installation, role):
    _, _, endpoints, snapshot = installation
    path = Path(endpoints[role]["path"])
    connection = sqlite3.connect(path)
    connection.execute("pragma journal_mode=wal")
    connection.execute("pragma wal_autocheckpoint=0")
    connection.execute("update marker set value='wal-only'")
    connection.commit()
    # Copy a quiescent family's exact bytes while this fixture holds its WAL
    # alive; no concurrent writer runs during staging.
    try:
        plan = build(installation)
        layout.stage(plan, snapshot)
        target = path.parent / "databases" / path.name
        assert value(target) == "wal-only"
    finally:
        connection.close()


def test_pending_receipt_blocks_normal_reentry(installation):
    install, _, _, snapshot = installation
    layout.stage(build(installation), snapshot)
    with pytest.raises(layout.LayoutError, match="pending"):
        layout.check_pending(install)


def test_rollback_preserves_candidate_before_restoring_original_layout(installation):
    install, _, endpoints, snapshot = installation
    plan = build(installation)
    layout.stage(plan, snapshot)
    candidate = install / "storage/databases/astinuslab.db"
    with sqlite3.connect(candidate) as connection:
        connection.execute("update marker set value='candidate-write'")
    layout.retain_candidate(snapshot)
    # Model release_rollback's schema-backup restoration after retention.
    with sqlite3.connect(candidate) as connection:
        connection.execute("update marker set value='schema-restored'")
    layout.rollback(snapshot)
    assert value(Path(endpoints["application"]["path"])) == "application"
    journal = json.loads((snapshot / "sqlite-layout.json").read_text())
    retained = Path(journal["transaction_dir"]) / "candidate-on-rollback/application/astinuslab.db"
    assert value(retained) == "candidate-write"
    record = layout._load(snapshot)
    assert record is not None
    assert (
        record["retained_families"]["application"][""]["sha256"] == layout._record(retained, {os.geteuid()})["sha256"]
    )
    assert not (install / ".sqlite-layout.pending.json").exists()
    assert not candidate.exists()


def test_unexpected_candidate_entry_retains_identity_custody_and_snapshot(installation):
    install, _, _, snapshot = installation
    layout.stage(build(installation), snapshot)
    unexpected = install / "storage/databases/operator-evidence"
    unexpected.write_bytes(b"retain me")
    with pytest.raises(layout.LayoutError, match="unexpected"):
        layout.rollback(snapshot)
    assert unexpected.read_bytes() == b"retain me"
    assert (install / ".sqlite-layout.pending.json").exists()


def configured(installation, plan):
    from ops.deploy.managed_config import set_database_endpoints

    install, _, endpoints, _ = installation
    set_database_endpoints(install / "config/astinuslab.env", plan["replacements"], plan["environment"]["sha256"])
    effective = {role: dict(endpoint) for role, endpoint in endpoints.items()}
    for entry in plan["entries"]:
        effective[entry["role"]]["path"] = entry["destination"]
    layout.verify(plan, effective)
    return effective


def test_committed_receipt_allows_runtime_writes_and_sidecar_recreation(installation):
    install, external, _, snapshot = installation
    plan = build(installation)
    layout.stage(plan, snapshot)
    effective = configured(installation, plan)
    layout.retire(plan, snapshot)
    layout.commit(plan, snapshot)
    live = install / "storage/databases/astinuslab.db"
    with sqlite3.connect(live) as connection:
        connection.execute("update marker set value='live-after-commit'")
    again = layout.build_plan(
        install, external, effective, api_uid=os.getuid(), shared_gid=os.getgid(), allowed_uids={os.getuid()}
    )
    assert not again["replacements"]
    assert all(not row["migrate"] for row in again["entries"])
    assert value(live) == "live-after-commit"
    assert not (install / "storage/astinuslab.db").exists()


@pytest.mark.parametrize(
    "journal_mode,failure_move",
    [("delete", step) for step in range(1, 4)]
    + [("persist", step) for step in range(1, 7)]
    + [("wal", step) for step in range(1, 10)],
)
def test_retirement_failure_rolls_back_original_inodes(installation, monkeypatch, journal_mode, failure_move):
    from ops.deploy import acl_snapshot, managed_config

    install, _, endpoints, snapshot = installation
    with ExitStack() as connections:
        for role, row in endpoints.items():
            connection = connections.enter_context(closing(sqlite3.connect(row["path"])))
            assert connection.execute(f"pragma journal_mode={journal_mode}").fetchone()[0] == journal_mode
            connection.execute("pragma wal_autocheckpoint=0")
            connection.execute("update marker set value='retirement-fixture-write'")
            connection.execute("update marker set value=?", (role,))
            connection.commit()
        families = sorted((install / "storage").iterdir())
        assert len(families) == {"delete": 3, "persist": 6, "wal": 9}[journal_mode]

        def original_state(path):
            info = path.stat()
            return (
                path.read_bytes(),
                info.st_dev,
                info.st_ino,
                info.st_uid,
                info.st_gid,
                stat.S_IMODE(info.st_mode),
                {name: os.getxattr(path, name) for name in os.listxattr(path)},
            )

        originals = {path: original_state(path) for path in families}
        environment = install / "config/astinuslab.env"
        old_environment = original_state(environment)
        configuration_snapshot = snapshot / "managed.json"
        custody_snapshot = snapshot / "storage-acl.json"
        managed_config.capture(install, configuration_snapshot)
        acl_snapshot.capture(str(install / "storage"), str(custody_snapshot))
        plan = build(installation)
        layout.stage(plan, snapshot)
        configured(installation, plan)
        original_rename = os.rename
        count = 0

        def fail_selected(source, destination, *args, **kwargs):
            nonlocal count
            count += 1
            if count == failure_move:
                raise OSError("injected retirement failure")
            return original_rename(source, destination, *args, **kwargs)

        monkeypatch.setattr(layout.os, "rename", fail_selected)
        with pytest.raises(OSError, match="injected"):
            layout.retire(plan, snapshot)
        assert count == failure_move
        monkeypatch.setattr(layout.os, "rename", original_rename)
        layout.rollback(snapshot)
        managed_config.restore(install, configuration_snapshot)
        acl_snapshot.restore(str(custody_snapshot), [])
        assert {path: original_state(path) for path in families} == originals
        assert original_state(environment) == old_environment
        for role, row in endpoints.items():
            assert value(Path(row["path"])) == role
        assert not (install / "storage/databases").exists()
        assert not (install / ".sqlite-layout.pending.json").exists()


def test_rollback_rejects_concurrent_environment_change(installation):
    install, _, _, snapshot = installation
    plan = build(installation)
    layout.stage(plan, snapshot)
    configured(installation, plan)
    path = install / "config/astinuslab.env"
    path.write_text("KEEP=new-operator-change\n")
    with pytest.raises(layout.LayoutError, match="environment"):
        layout.rollback(snapshot)
    assert path.read_text() == "KEEP=new-operator-change\n"


def test_matching_pending_receipt_is_post_commit_cleanup(installation, monkeypatch):
    install, _, _, snapshot = installation
    plan = build(installation)
    layout.stage(plan, snapshot)
    configured(installation, plan)
    layout.retire(plan, snapshot)
    unlink = Path.unlink

    def fail_pending(path, *args, **kwargs):
        if path.name == ".sqlite-layout.pending.json":
            raise OSError("injected pending cleanup failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_pending)
    assert layout.commit(plan, snapshot) is False
    assert (install / ".sqlite-layout.pending.json").exists()
    layout.check_pending(install)
    with pytest.raises(layout.LayoutError, match="committed"):
        layout.rollback(snapshot)


def test_stale_shm_is_rebuilt_only_in_staged_copy(installation):
    _, _, endpoints, snapshot = installation
    source = Path(endpoints["application"]["path"])
    connection = sqlite3.connect(source)
    connection.execute("pragma journal_mode=wal")
    connection.execute("update marker set value='wal-committed'")
    connection.commit()
    # A closed/crashed writer can leave stale shm; recovery must use the WAL.
    shm = Path(str(source) + "-shm")
    shm.write_bytes(b"\0" * 32768)
    original_shm = shm.read_bytes()
    try:
        plan = build(installation)
        layout.stage(plan, snapshot)
        assert value(Path(plan["entries"][0]["destination"])) == "wal-committed"
        assert shm.read_bytes() == original_shm
    finally:
        connection.close()


def test_hot_rollback_journal_recovers_copy_without_touching_original(installation):
    import subprocess
    import sys

    _, _, endpoints, snapshot = installation
    source = Path(endpoints["application"]["path"])
    with sqlite3.connect(source) as connection:
        connection.execute("create table spill(value blob)")
        connection.executemany("insert into spill values (?)", [(b"a" * 4096,)] * 200)
    code = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('pragma cache_size=5')
connection.execute('begin immediate')
connection.execute("update marker set value='uncommitted'")
connection.execute("update spill set value=zeroblob(4096)")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(source)], check=True)
    journal = Path(str(source) + "-journal")
    assert journal.read_bytes()[:8] != b"\0" * 8
    originals = {path: path.read_bytes() for path in (source, journal)}
    plan = build(installation)
    layout.stage(plan, snapshot)
    assert value(Path(plan["entries"][0]["destination"])) == "application"
    assert all(path.read_bytes() == data for path, data in originals.items())


@pytest.mark.parametrize("damage", ["mode", "file-acl", "directory-acl"])
def test_committed_layout_rejects_custody_drift(installation, damage):
    import subprocess

    install, _, endpoints, snapshot = installation
    plan = build(installation)
    layout.stage(plan, snapshot)
    configured(installation, plan)
    layout.retire(plan, snapshot)
    layout.commit(plan, snapshot)
    for entry in plan["entries"]:
        endpoints[entry["role"]]["path"] = entry["destination"]
    target = install / "storage/databases/astinuslab.db"
    if damage == "mode":
        target.chmod(0o640)
    elif damage == "file-acl":
        subprocess.run(["setfacl", "-m", f"u:{os.getuid()}:r--", str(target)], check=True)
    else:
        subprocess.run(["setfacl", "-m", f"d:u:{os.getuid()}:r--", str(target.parent)], check=True)
    with pytest.raises(layout.LayoutError, match="custody|ACL"):
        build(installation)


def test_installed_environment_resolver_and_endpoint_publication(installation, tmp_path, monkeypatch):
    import pwd
    import shlex
    import sys

    install, _, endpoints, snapshot = installation
    release = tmp_path / "release"
    binary = release / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    root = Path(__file__).resolve().parents[3]
    binary.write_text(
        f'#!/bin/bash\nexport PYTHONPATH={shlex.quote(str(root / "src"))}\nexec {shlex.quote(sys.executable)} "$@"\n'
    )
    binary.chmod(0o755)
    (install / "astinuslab.toml").write_text('[storage]\ndb_path = "storage/ignored.db"\n')
    environment = install / "config/astinuslab.env"
    environment.write_text(f'ASTINUSLAB_DB_PATH="{endpoints["application"]["path"]}"\nASTINUSLAB_FAKE_LIBVIRT=1\n')
    monkeypatch.setenv("ASTINUSLAB_DB_PATH", str(tmp_path / "must-not-read.db"))
    identity = pwd.getpwuid(os.getuid()).pw_name
    assert layout.resolve_endpoints(install, release, identity) == endpoints
    plan = build(installation)
    layout.stage(plan, snapshot)
    layout.configure(plan)
    layout.verify(plan, layout.resolve_endpoints(install, release, identity))
    assert value(install / "storage/databases/astinuslab.db") == "application"
    assert not (install / "storage/ignored.db").exists()
    assert not (tmp_path / "must-not-read.db").exists()


@pytest.mark.parametrize("creator", [1, 2, 3])
@pytest.mark.parametrize(
    "database,journal_mode",
    [(name, "wal") for name in ("astinuslab.db", "celery_broker.db", "celery_results.db")]
    + [(name, "delete") for name in ("celery_broker.db", "celery_results.db")],
)
def test_shared_sqlite_remains_writable_across_three_real_identities(creator, database, journal_mode):
    import subprocess
    import sys

    script = (
        r"""
import errno, os, pathlib, sqlite3, tempfile, traceback
from ops.deploy import shared_databases as layout

def spawn(uid, operation):
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([0]); os.setgid(0); os.setuid(uid); os.umask(0o077)
            operation()
        except BaseException:
            traceback.print_exc(); os._exit(1)
        os._exit(0)
    return child

def wait(child):
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0

with tempfile.TemporaryDirectory(prefix="astinus-sqlite-uids-") as temporary:
    outer = pathlib.Path(temporary); outer.chmod(0o755)
    install = outer / "install"; install.mkdir(mode=0o755)
    storage = install / "storage"; storage.mkdir(); os.chown(storage, 1, 0); storage.chmod(0o3770)
    config = install / "config"; config.mkdir(); (config / "astinuslab.env").write_text("KEEP=original\n")
    snapshot = outer / "snapshot"; snapshot.mkdir(mode=0o700)
    endpoints = {}
    for role, (name, _, _) in layout.FIELDS.items():
        path = storage / name
        with sqlite3.connect(path) as db:
            db.execute("create table marker(value text)"); db.execute("insert into marker values('original')")
        endpoints[role] = {"kind": "sqlite", "path": str(path)}
    plan = layout.build_plan(install, storage, endpoints, api_uid=1, shared_gid=0, allowed_uids={0, 1, 2, 3})
    layout.stage(plan, snapshot)
    dbpath = storage / "databases" / DATABASE
    roles = [CREATOR] + sorted({1, 2, 3} - {CREATOR})
    expected = 'original'
    if JOURNAL_MODE == 'wal':
        # Every connection closes between cycles; a different UID must recreate
        # both sidecars, and both peers must reopen and commit against them.
        for cycle, owner in enumerate(roles[:2]):
            ready_read, ready_write = os.pipe(); finish_read, finish_write = os.pipe()
            written = f'holder-{cycle}-{owner}'
            def hold():
                db = sqlite3.connect(dbpath)
                try:
                    assert db.execute('pragma journal_mode=wal').fetchone()[0] == 'wal'
                    assert db.execute('select value from marker').fetchone()[0] == expected
                    db.execute('update marker set value=?', (written,)); db.commit()
                    os.write(ready_write, b'1'); os.read(finish_read, 1)
                finally:
                    db.close()
            child = spawn(owner, hold)
            os.close(ready_write); os.close(finish_read)
            assert os.read(ready_read, 1) == b'1'
            expected = written
            for suffix in ('-wal', '-shm'):
                assert pathlib.Path(str(dbpath) + suffix).stat().st_uid == owner
            try:
                for uid in sorted({1, 2, 3} - {owner}):
                    written = f'peer-{cycle}-{uid}'
                    def peer_write():
                        db = sqlite3.connect(dbpath)
                        try:
                            assert db.execute('select value from marker').fetchone()[0] == expected
                            db.execute('update marker set value=?', (written,)); db.commit()
                        finally:
                            db.close()
                    wait(spawn(uid, peer_write)); expected = written
            finally:
                os.write(finish_write, b'1'); wait(child)
                os.close(ready_read); os.close(finish_write)
            for suffix in ('-wal', '-shm'):
                assert not pathlib.Path(str(dbpath) + suffix).exists(), 'last close must remove sidecars'
    else:
        # DELETE mode removes each journal on rollback/commit. Repeated roles
        # reopen, observe the prior committed row, and create their own journal.
        for cycle in range(2):
            for uid in roles:
                written = f'delete-{cycle}-{uid}'
                def delete_write():
                    db = sqlite3.connect(dbpath)
                    journal = pathlib.Path(str(dbpath) + '-journal')
                    try:
                        assert db.execute('pragma journal_mode=delete').fetchone()[0] == 'delete'
                        assert db.execute('select value from marker').fetchone()[0] == expected
                        db.execute('begin immediate')
                        db.execute("update marker set value='discarded'")
                        assert journal.stat().st_uid == uid
                        db.rollback()
                        assert not journal.exists()
                        assert db.execute('select value from marker').fetchone()[0] == expected
                        db.execute('update marker set value=?', (written,))
                        assert journal.stat().st_uid == uid
                        db.commit(); assert not journal.exists()
                    finally:
                        db.close()
                wait(spawn(uid, delete_write)); expected = written
    def final_reopen():
        db = sqlite3.connect(dbpath)
        try:
            assert db.execute('select value from marker').fetchone()[0] == expected
        finally:
            db.close()
    for uid in roles:
        wait(spawn(uid, final_reopen))

    package_root = storage / 'lab-packages'; package_root.mkdir()
    os.chown(package_root, 2, 0); package_root.chmod(0o2770)
    package_inode = package_root.stat().st_ino
    replacement = storage / 'analysis-replacement'; replacement.mkdir()
    os.chown(replacement, 3, 0)
    def analysis_cannot_replace():
        # Positive parent-write control: denial must come from sticky custody.
        writable = storage / 'analysis-write-probe'; writable.mkdir(); writable.rmdir()
        for operation in (
            lambda: package_root.rename(storage / 'displaced-packages'),
            lambda: os.replace(replacement, package_root),
            package_root.rmdir,
        ):
            try:
                operation()
            except PermissionError as exc:
                assert exc.errno in (errno.EPERM, errno.EACCES)
            else:
                raise AssertionError('analysis replaced protected package-root child')
    wait(spawn(3, analysis_cannot_replace))
    assert package_root.stat().st_ino == package_inode
    assert package_root.stat().st_uid == 2
    assert replacement.exists() and not (storage / 'displaced-packages').exists()
    assert storage.stat().st_mode & 0o1000
    assert not dbpath.parent.stat().st_mode & 0o1000
    layout._shared_custody(dbpath, 1, 0, {0, 1, 2, 3})
print('three distinct SQLite writers, recreation and package protection passed')
""".replace("CREATOR", str(creator))
        .replace("DATABASE", repr(database))
        .replace("JOURNAL_MODE", repr(journal_mode))
    )
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rollback_resumes_after_candidate_directory_rename(installation, monkeypatch):
    install, _, _, snapshot = installation
    plan = build(installation)
    layout.stage(plan, snapshot)
    configured(installation, plan)
    save = layout._save

    def interrupted(current):
        if current["phase"] == "rolled_back":
            raise OSError("injected journal flush interruption")
        save(current)

    monkeypatch.setattr(layout, "_save", interrupted)
    with pytest.raises(OSError, match="interruption"):
        layout.rollback(snapshot)
    monkeypatch.setattr(layout, "_save", save)
    layout.rollback(snapshot)
    assert not (install / "storage/databases").exists()
    assert value(install / "storage/astinuslab.db") == "application"


def test_custom_database_refuses_untraversable_intermediate_parent(installation):
    install, _, endpoints, _ = installation
    private = install / "storage/private"
    private.mkdir(mode=0o700)
    shared = private / "shared"
    shared.mkdir(mode=0o2770)
    shared.chmod(0o2770)
    target = shared / "operator.db"
    make_database(target)
    target.chmod(0o660)
    endpoints["application"] = {"kind": "sqlite", "path": str(target)}
    before = target.read_bytes()
    with pytest.raises(layout.LayoutError, match="parent|ancestor"):
        build(installation)
    assert target.read_bytes() == before
    assert stat.S_IMODE(private.stat().st_mode) == 0o700


def test_existing_committed_upgrade_has_its_own_durable_receipt(installation, monkeypatch):
    install, _, endpoints, snapshot = installation
    first = build(installation)
    layout.stage(first, snapshot)
    configured(installation, first)
    layout.retire(first, snapshot)
    assert layout.commit(first, snapshot)
    old_receipt = json.loads((install / ".sqlite-layout.json").read_text())
    for entry in first["entries"]:
        endpoints[entry["role"]]["path"] = entry["destination"]
    next_snapshot = snapshot.with_name("next-snapshot")
    next_snapshot.mkdir(mode=0o700)
    repeated = (*installation[:3], next_snapshot)
    second = build(repeated)
    layout.stage(second, next_snapshot)
    configured(repeated, second)
    layout.retire(second, next_snapshot)
    save = layout._save

    def fail_cleanup(current):
        if current["phase"] == "committed":
            raise OSError("injected post-publication interruption")
        save(current)

    monkeypatch.setattr(layout, "_save", fail_cleanup)
    assert layout.commit(second, next_snapshot) is False
    receipt = json.loads((install / ".sqlite-layout.json").read_text())
    assert receipt["transaction_id"] == second["transaction_id"] != old_receipt["transaction_id"]
    assert receipt["mapping"] == old_receipt["mapping"]
    with pytest.raises(layout.LayoutError, match="committed"):
        layout.rollback(next_snapshot)


def test_candidate_retention_refuses_changed_configuration_before_schema_restore(installation):
    install, _, _, snapshot = installation
    config = install / "astinuslab.toml"
    config.write_text("[storage]\n")
    plan = build(installation)
    layout.stage(plan, snapshot)
    configured(installation, plan)
    config.write_text('[storage]\ndb_path="elsewhere.db"\n')
    with pytest.raises(layout.LayoutError, match="contents"):
        layout.retain_candidate(snapshot)
    assert not (layout._transaction(plan) / "candidate-on-rollback").exists()


def test_private_backups_refuses_symlink_without_changing_target(installation, tmp_path):
    install, _, _, _ = installation
    outside = tmp_path / "operator-private"
    outside.mkdir(mode=0o755)
    (install / "backups").symlink_to(outside)
    before = outside.stat()
    with pytest.raises(OSError):
        layout.private_backups(install)
    after = outside.stat()
    assert (after.st_mode, after.st_uid, after.st_gid) == (before.st_mode, before.st_uid, before.st_gid)
    assert not list(outside.iterdir())


def test_recovery_reference_cannot_select_another_installation(installation, tmp_path):
    _, _, _, snapshot = installation
    plan = build(installation)
    layout.stage(plan, snapshot)
    with pytest.raises(layout.LayoutError, match="different installation"):
        layout._load(snapshot, tmp_path / "other-install")


@pytest.mark.parametrize("failure", ["copy", "environment"])
def test_staging_failure_preserves_originals_and_allows_bounded_rollback(installation, monkeypatch, failure):
    from ops.deploy import managed_config

    install, _, endpoints, snapshot = installation
    configuration_snapshot = snapshot / "managed.json"
    managed_config.capture(install, configuration_snapshot)
    original_env = (install / "config/astinuslab.env").read_bytes()
    original = {Path(item["path"]): Path(item["path"]).read_bytes() for item in endpoints.values()}
    plan = build(installation)
    if failure == "copy":
        copy = layout._copy

        def fail_copy(source, destination, record):
            if destination.name == "celery_broker.db":
                destination.write_bytes(b"partial candidate")
                raise OSError("injected copy failure")
            copy(source, destination, record)

        monkeypatch.setattr(layout, "_copy", fail_copy)
        with pytest.raises(OSError, match="copy failure"):
            layout.stage(plan, snapshot)
        monkeypatch.setattr(layout, "_copy", copy)
    else:
        layout.stage(plan, snapshot)
        partial = managed_config.database_endpoint_payload(original_env, plan["replacements"])[:-10]
        (install / "config/astinuslab.env").write_bytes(partial)
    layout.rollback(snapshot)
    managed_config.restore(install, configuration_snapshot)
    assert (install / "config/astinuslab.env").read_bytes() == original_env
    assert all(path.read_bytes() == data for path, data in original.items())
    assert not (install / "storage/databases").exists()
    assert not (install / ".sqlite-layout.pending.json").exists()


@pytest.mark.parametrize("after_publication", [False, True])
def test_commit_directory_fsync_failure_obeys_publication_boundary(installation, monkeypatch, after_publication):
    install, _, _, snapshot = installation
    plan = build(installation)
    layout.stage(plan, snapshot)
    configured(installation, plan)
    layout.retire(plan, snapshot)
    original_fsync = os.fsync
    count = 0

    def fail_once(descriptor):
        nonlocal count
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode) and info.st_ino == install.stat().st_ino:
            count += 1
            if count == (2 if after_publication else 1):
                raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_once)
    if after_publication:
        assert layout.commit(plan, snapshot) is False
        with pytest.raises(layout.LayoutError, match="committed"):
            layout.rollback(snapshot)
    else:
        with pytest.raises(OSError, match="fsync"):
            layout.commit(plan, snapshot)
        assert not (install / ".sqlite-layout.json").exists()
        layout.rollback(snapshot)
        assert value(install / "storage/astinuslab.db") == "application"


def test_installer_cli_dispatch_uses_private_state_and_installed_settings():
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[3]
    script = r"""
import os, pathlib, shlex, sqlite3, subprocess, sys, tempfile
with tempfile.TemporaryDirectory(prefix="astinus-db-cli-") as temporary:
    outer = pathlib.Path(temporary)
    install = outer / "install"; install.mkdir()
    (install / "storage").mkdir(); (install / "config").mkdir()
    (install / "config/astinuslab.env").write_text("ASTINUSLAB_FAKE_LIBVIRT=1\n")
    release = outer / "release"; (release / ".venv/bin").mkdir(parents=True)
    binary = release / ".venv/bin/python"
    binary.write_text("#!/bin/bash\nexport PYTHONPATH=" + shlex.quote(SOURCE) + "\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    binary.chmod(0o755)
    for name in ("astinuslab.db", "celery_broker.db", "celery_results.db"):
        with sqlite3.connect(install / "storage" / name) as db:
            db.execute("create table marker(value text)"); db.execute("insert into marker values('kept')")
    snapshot = outer / "snapshot"; snapshot.mkdir(mode=0o700)
    base = ["/usr/bin/python3", "-I", HELPER]
    flags = ["--install", str(install), "--storage", str(install / "storage"), "--release", str(release),
             "--snapshot", str(snapshot), "--api", "root", "--runner", "root", "--analysis", "root", "--legacy", "root"]
    for action in ("pending", "preflight", "prepare", "verify", "retire", "commit", "committed", "pending", "preflight"):
        result = subprocess.run(base + [action] + flags, capture_output=True, text=True)
        assert result.returncode == 0, (action, result.stdout, result.stderr)
    assert not (install / "storage/astinuslab.db").exists()
    with sqlite3.connect(install / "storage/databases/astinuslab.db") as db:
        assert db.execute("select value from marker").fetchone() == ('kept',)
    print('isolated installer CLI lifecycle passed')
""".replace("HELPER", repr(str(root / "ops/deploy/shared_databases.py"))).replace("SOURCE", repr(str(root / "src")))
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
