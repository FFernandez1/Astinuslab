"""Kernel-enforced API/runner/analysis package custody in a private user namespace."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_package_writers_migration_and_rollback_with_real_uids():
    script = r"""
import hashlib, json, os, pathlib, shlex, sqlite3, subprocess, tempfile, traceback
from astinuslab.lab_packages import storage_custody as custody
from astinuslab.lab_packages.content_store import ContentStore
from astinuslab.lab_packages.package_service import _copy_archive, _exclusive_package_lock
with tempfile.TemporaryDirectory(prefix="astinus-package-uids-") as temporary:
    outer = pathlib.Path(temporary); outer.chmod(0o755)
    root = outer / "storage"; root.mkdir(mode=0o2770); root.chmod(0o2770); os.chown(root, 1, 0)
    packages = root / "lab-packages"; packages.mkdir(mode=0o700)
    content = root / "lab-content"; content.mkdir(mode=0o700)
    old = packages / "legacy.astinuslab"; old.write_bytes(b"retained input"); old.chmod(0o600)
    partial_dir = content / "sha256"; partial_dir.mkdir(mode=0o700)
    evidence = root / "evidence"; evidence.write_bytes(b"protected evidence"); evidence.chmod(0o440)
    evidence_before = (evidence.stat(), evidence.read_bytes())
    snapshot = outer / "before.acl"
    def shell(command):
        result = subprocess.run(["bash", "-c", "source " + shlex.quote(INSTALL) + "; " + command], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    shell(f"snapshot_acl_state {shlex.quote(str(root))} {shlex.quote(str(snapshot))}")
    original_acl = subprocess.check_output(["getfacl", "-Rcpn", str(root)])
    def as_uid(uid, function, success=True):
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([0]); os.setgid(0); os.setuid(uid); os.umask(0o077)
                function()
            except BaseException:
                if success: traceback.print_exc()
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert (os.waitstatus_to_exitcode(status) == 0) == success, (uid, status)
    as_uid(2, lambda: old.read_bytes(), False)
    # A foreign object in the second store must prevent changes to the first.
    unsafe = content / "foreign"; unsafe.write_bytes(b"foreign"); os.chown(unsafe, 3, 0)
    before = subprocess.check_output(["getfacl", "-cpn", str(old)])
    try: custody.provision(root, api_uid=1, runner_uid=2, legacy_uid=0)
    except OSError: pass
    else: raise AssertionError("foreign inode accepted")
    assert before == subprocess.check_output(["getfacl", "-cpn", str(old)])
    unsafe.unlink()
    custody.provision(root, api_uid=1, runner_uid=2, legacy_uid=0)
    assert root.stat().st_mode & 0o1000
    for uid in (1, 2): as_uid(uid, lambda: old.read_bytes())
    as_uid(3, lambda: old.read_bytes(), False)
    as_uid(3, lambda: packages.rename(root / "stolen"), False)
    as_uid(3, lambda: (packages / "injected").write_text("bad"), False)
    # Trusted marker parsing has separate tests; these use real kernel UIDs,
    # ACL commands and actual package/content publication functions.
    policy = custody.PackageCustody(root, 1, 2)
    custody.policy_for = lambda path: policy
    payload = b"verified cross-service bytes"; digest = hashlib.sha256(payload).hexdigest()
    def api():
        store = ContentStore(content)
        assert store.put_resumable(payload[:9], sha256=digest, size_bytes=len(payload)) is None
        with _exclusive_package_lock(packages / "cross.lock"): pass
        _copy_archive(old, packages / "copied.astinuslab", hashlib.sha256(old.read_bytes()).hexdigest(), old.stat().st_size, max_bytes=1024)
    def runner():
        store = ContentStore(content)
        assert store.put_resumable(payload[9:], sha256=digest, size_bytes=len(payload), offset=9)
        assert store.get(digest, size_bytes=len(payload)) == payload
        with _exclusive_package_lock(packages / "cross.lock"): pass
        _copy_archive(old, packages / "runner.astinuslab", hashlib.sha256(old.read_bytes()).hexdigest(), old.stat().st_size, max_bytes=1024)
        other = b"runner temp publication"; store.put(other, sha256=hashlib.sha256(other).hexdigest(), size_bytes=len(other))
    as_uid(1, api); as_uid(2, runner)
    as_uid(1, lambda: ContentStore(content).get(digest, size_bytes=len(payload)))
    as_uid(3, lambda: ContentStore(content).get(digest, size_bytes=len(payload)), False)
    assert old.read_bytes() == b"retained input"
    # Sticky root permits shared SQLite WAL transactions by all three roles.
    db = root / "shared.db"
    with sqlite3.connect(db) as connection:
        connection.execute("pragma journal_mode=wal"); connection.execute("create table items(value integer)")
    connection.close()
    db.chmod(0o660); os.chown(db, 1, 0)
    def db_write():
        os.umask(0o007)
        with sqlite3.connect(db) as connection: connection.execute("insert into items values (1)")
        connection.close()
    for uid in (1, 2, 3): as_uid(uid, db_write)
    # Late rollback preserves all newly published bytes and identities.
    try: custody.preflight_rollback(root, [snapshot], allowed_owners={0,1,2})
    except OSError as exc: assert "new package data" in str(exc)
    else: raise AssertionError("late rollback ignored new package bytes")
    assert (packages / "runner.astinuslab").read_bytes() == b"retained input"
    # Audit unsafe inodes before reapplying an existing split installation.
    for kind in ("symlink", "hardlink"):
        if kind == "symlink": unsafe.symlink_to(evidence)
        else: os.link(evidence, unsafe)
        try: custody.provision(root, api_uid=1, runner_uid=2, legacy_uid=0)
        except OSError: pass
        else: raise AssertionError(kind + " accepted")
        unsafe.unlink()
    custody.provision(root, api_uid=1, runner_uid=2, legacy_uid=0)
    as_uid(2, lambda: old.read_bytes())
    # Explicit fixture cleanup represents operator reconciliation, never the
    # production rollback path. Then test exact restoration of original ACLs.
    for path in sorted(content.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path == partial_dir: continue
        if path.is_dir(): path.rmdir()
        else: path.unlink()
    for path in packages.iterdir():
        if path != old: path.unlink()
    for path in (db, root / "shared.db-wal", root / "shared.db-shm"):
        path.unlink(missing_ok=True)
    custody.preflight_rollback(root, [snapshot], allowed_owners={0,1,2})
    shell(f"restore_acl_snapshot {shlex.quote(str(snapshot))} {shlex.quote(str(root))}")
    restored_acl = subprocess.check_output(["getfacl", "-Rcpn", str(root)])
    assert original_acl == restored_acl, (original_acl, restored_acl)
    assert evidence_before[0].st_mode == evidence.stat().st_mode
    assert evidence_before[0].st_uid == evidence.stat().st_uid
    assert evidence_before[1] == evidence.read_bytes()
    assert old.read_bytes() == b"retained input" and old.stat().st_uid == 0
    print(json.dumps({"uids": [1,2,3], "publication": "passed", "rollback": "passed"}))
""".replace("INSTALL", repr(str(ROOT / "ops/deploy/install.sh")))
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["uids"] == [1, 2, 3]


def test_rollback_preflight_rejects_unknown_directories_and_missing_material():
    script = r"""
import json, os, pathlib, tempfile
from astinuslab.lab_packages import storage_custody as custody
with tempfile.TemporaryDirectory(prefix="astinus-package-rollback-") as temporary:
    root = pathlib.Path(temporary)
    store = root / "lab-packages"; store.mkdir()
    material = store / "old"; material.write_bytes(b"retained")
    snapshot = root / "snapshot"
    snapshot.write_text(json.dumps({"version": 1, "root": str(root), "records": [
        {"path": str(p.relative_to(root)), "dev": p.stat().st_dev, "ino": p.stat().st_ino}
        for p in (store, material)
    ]}))
    def check(): custody.preflight_rollback(root, [snapshot], allowed_owners={0,1,2})
    check()
    new = store / ".locks"; new.mkdir(); os.chown(new, 1, 0)
    try: check()
    except OSError as exc: assert "new package data or directory" in str(exc)
    else: raise AssertionError("unknown empty directory accepted")
    assert new.stat().st_uid == 1 and material.read_bytes() == b"retained"
    new.rmdir(); material.rename(root / "preserved")
    try: check()
    except OSError as exc: assert "material disappeared" in str(exc)
    else: raise AssertionError("missing material accepted")
    (root / "preserved").rename(material)
    # Explicit installer-created empty roots can be removed by its tracked
    # directory rollback; an unknown runtime child must still block it.
    fresh = root / "lab-content"; fresh.mkdir()
    custody.preflight_rollback(root, [snapshot], allowed_owners={0,1,2}, created_dirs={fresh})
    (fresh / "sha256").mkdir()
    try: custody.preflight_rollback(root, [snapshot], allowed_owners={0,1,2}, created_dirs={fresh})
    except OSError: pass
    else: raise AssertionError("runtime child hidden by tracked root")
"""
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
