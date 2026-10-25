"""Real UID/ACL fixtures for the split-service log boundary, no host identities."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_role_log_custody_and_conservative_rollback():
    script = r"""
import importlib.util, json, os, pathlib, subprocess, tempfile, traceback
from astinuslab.core import log_layout
spec = importlib.util.spec_from_file_location("service_logs", HELPER)
logs = importlib.util.module_from_spec(spec); spec.loader.exec_module(logs)
roles = {"api": 1, "runner": 2, "analysis": 3}
with tempfile.TemporaryDirectory(prefix="astinus-log-uids-") as temporary:
    outer = pathlib.Path(temporary); outer.chmod(0o755)
    # Host ancestors have overflow UIDs inside the private namespace. Restrict
    # the metadata parent check to this owned fixture; kernel ACLs remain real.
    def fixture_parent(path):
        assert path.parent == outer
        assert outer.stat().st_uid == 0 and outer.stat().st_mode & 0o022 == 0
    logs._protected_parent = fixture_parent
    root = outer / "storage"; root.mkdir(); root.chmod(0o2770); os.chown(root, 1, 0)
    anchor = root / "logs"; anchor.mkdir()
    legacy = anchor / "api.log"; legacy.write_bytes(b"retained history"); legacy.chmod(0o600)
    record = outer / "record.json"; recovery = outer / "recovery"
    def as_uid(uid, operation, success=True):
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([0]); os.setgid(0); os.setuid(uid); os.umask(0o077)
                operation()
            except BaseException:
                if success: traceback.print_exc()
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert (os.waitstatus_to_exitcode(status) == 0) == success, (uid, status)
    before = legacy.stat().st_ino
    logs.provision(root, roles, 0, record)
    assert anchor.stat().st_uid == 0 and anchor.stat().st_mode & 0o1000
    assert legacy.stat().st_ino == before and legacy.read_bytes() == b"retained history"
    for role, uid in roles.items():
        target = anchor / role / logs.FILES[role]
        as_uid(uid, lambda: log_layout.log_path(root, logs.FILES[role]))
        as_uid(uid, lambda: target.write_bytes(b"written by role"))
        as_uid(1, lambda: target.read_bytes())
        # The admin truncates existing worker files without O_CREAT.
        as_uid(1, lambda: os.close(os.open(target, os.O_WRONLY | os.O_TRUNC)))
        as_uid(uid, lambda: target.rename(target.with_name(target.name + ".1")))
        as_uid(uid, lambda: target.write_bytes(b"after rotation"))
        as_uid(1, lambda: target.read_bytes())
    runner = anchor / "runner/runner.log"
    as_uid(2, lambda: runner.with_name("runner.log.12").write_bytes(b"configured rotation"))
    second_record = outer / "upgrade-record.json"
    logs.provision(root, roles, 0, second_record)
    logs.rollback(root, roles, 0, second_record, recovery)
    assert runner.with_name("runner.log.12").read_bytes() == b"configured rotation"
    as_uid(3, lambda: runner.read_bytes(), False)
    as_uid(3, lambda: runner.unlink(), False)
    as_uid(2, lambda: (anchor / "analysis").rename(anchor / "stolen"), False)
    as_uid(2, lambda: (anchor / "job-test-qemu-img.log").write_bytes(b"diagnostic"))
    unexpected = anchor / "job-test-qemu-img.log"
    try: logs.rollback(root, roles, 0, record, recovery)
    except OSError: pass
    else: raise AssertionError("untracked new runner bytes accepted")
    assert unexpected.read_bytes() == b"diagnostic" and (anchor / logs.MARKER).exists()
    unexpected.unlink()
    logs.rollback(root, roles, 0, record, recovery)
    assert not (anchor / logs.MARKER).exists()
    assert not (anchor / "runner").exists()
    saved = list(recovery.rglob("runner.log"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"after rotation" and saved[0].stat().st_uid == 0
    assert (saved[0].parent / "runner.log.12").read_bytes() == b"configured rotation"
    assert legacy.stat().st_ino == before and legacy.read_bytes() == b"retained history"
    assert recovery.stat().st_mode & 0o777 == 0o700
print("role custody and retained rollback: passed")
""".replace("HELPER", repr(str(ROOT / "ops/deploy/service_logs.py")))
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_log_preflight_preserves_unknown_existing_entries_and_created_anchor_rollback():
    script = r"""
import importlib.util, os, pathlib, subprocess, tempfile
spec = importlib.util.spec_from_file_location("service_logs", HELPER)
logs = importlib.util.module_from_spec(spec); spec.loader.exec_module(logs)
roles = {"api": 1, "runner": 2, "analysis": 3}
with tempfile.TemporaryDirectory(prefix="astinus-log-preflight-") as temporary:
    outer = pathlib.Path(temporary)
    logs._protected_parent = lambda path: None  # Fixture host ancestors use overflow UIDs.
    root = outer / "storage"; root.mkdir()
    logs.provision(root, roles, 0, outer / "new.json")
    logs.rollback(root, roles, 0, outer / "new.json", outer / "new-recovery")
    assert not (root / "logs").exists()
    anchor = root / "logs"; anchor.mkdir()
    old = anchor / "api.log"; old.write_bytes(b"flat history"); old.chmod(0o600)
    unknown = anchor / "operator-private"; unknown.write_bytes(b"operator bytes"); unknown.chmod(0o400)
    before = (unknown.stat(), subprocess.check_output(["getfacl", "-cpn", str(unknown)]))
    invalid = anchor / "serial"; invalid.write_bytes(b"unexpected file at directory path")
    old_acl = subprocess.check_output(["getfacl", "-cpn", str(old)])
    try: logs.provision(root, roles, 0, outer / "bad.json")
    except OSError: pass
    else: raise AssertionError("wrong legacy entry type accepted")
    assert old_acl == subprocess.check_output(["getfacl", "-cpn", str(old)])
    assert not (outer / "bad.json").exists()
    invalid.unlink()
    for attack in ("symlink", "hardlink", "foreign"):
        if attack == "symlink": invalid.symlink_to(old)
        elif attack == "hardlink": invalid.hardlink_to(old)
        else: invalid.write_bytes(b"foreign"); os.chown(invalid, 4, 0)
        try: logs.provision(root, roles, 0, outer / "bad.json")
        except OSError: pass
        else: raise AssertionError(attack + " accepted")
        invalid.unlink()
        assert old_acl == subprocess.check_output(["getfacl", "-cpn", str(old)])
    logs.provision(root, roles, 0, outer / "valid.json")
    assert unknown.read_bytes() == b"operator bytes"
    assert (unknown.stat().st_ino, unknown.stat().st_uid, unknown.stat().st_mode) == (before[0].st_ino, before[0].st_uid, before[0].st_mode)
    assert before[1] == subprocess.check_output(["getfacl", "-cpn", str(unknown)])
    (anchor / logs.MARKER).chmod(0o600)
    try: logs.provision(root, roles, 0, outer / "invalid-marker.json")
    except OSError: pass
    else: raise AssertionError("runtime-inaccessible marker accepted on upgrade")
    assert not (outer / "invalid-marker.json").exists()
""".replace("HELPER", repr(str(ROOT / "ops/deploy/service_logs.py")))
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_existing_qemu_serial_grants_survive_without_cross_role_access():
    script = r"""
import importlib.util, os, pathlib, subprocess, tempfile, traceback
spec = importlib.util.spec_from_file_location("service_logs", HELPER)
logs = importlib.util.module_from_spec(spec); spec.loader.exec_module(logs)
with tempfile.TemporaryDirectory(prefix="astinus-log-qemu-") as temporary:
    outer = pathlib.Path(temporary); outer.chmod(0o755)
    logs._protected_parent = lambda path: None  # Only namespace fixture ancestors.
    logs._qemu_uids = lambda: {4}
    root = outer / "storage"; root.mkdir(mode=0o755)
    anchor = root / "logs"; anchor.mkdir(mode=0o700)
    serial = anchor / "serial"; serial.mkdir(mode=0o700)
    target = serial / "astinuslab-job-proof-serial.log"; target.write_bytes(b"prior serial")
    subprocess.run(["setfacl", "-m", "u:4:--x", str(anchor)], check=True)
    subprocess.run(["setfacl", "-m", "u:4:rwx,d:u:4:rwx", str(serial)], check=True)
    subprocess.run(["setfacl", "-m", "u:4:rw-", str(target)], check=True)
    logs.provision(root, {"api":1,"runner":2,"analysis":3}, 0, outer / "record")
    pid = os.fork()
    if pid == 0:
        try:
            os.setgroups([0]); os.setgid(0); os.setuid(4)
            with target.open("ab") as stream: stream.write(b" continued")
            (serial / "astinuslab-job-new-serial.log").write_bytes(b"new serial")
            try: (anchor / "api/api.log").write_bytes(b"cross role")
            except PermissionError: pass
            else: raise AssertionError("qemu acquired role-directory write")
        except BaseException: traceback.print_exc(); os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert target.read_bytes() == b"prior serial continued"
    assert "default:user:4:rwx" in subprocess.check_output(["getfacl", "-cpn", str(serial)], text=True)
""".replace("HELPER", repr(str(ROOT / "ops/deploy/service_logs.py")))
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cross_filesystem_retention_verifies_before_retiring_sources():
    script = r"""
import errno, importlib.util, os, pathlib, tempfile
spec = importlib.util.spec_from_file_location("service_logs", HELPER)
logs = importlib.util.module_from_spec(spec); spec.loader.exec_module(logs)
roles = {"api":1,"runner":2,"analysis":3}
with tempfile.TemporaryDirectory(prefix="astinus-log-exdev-") as temporary:
    outer = pathlib.Path(temporary)
    logs._protected_parent = lambda path: None  # Only namespace fixture ancestors.
    original_rename = os.rename
    def exdev(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if src_dir_fd is not None and dst_dir_fd is not None:
            raise OSError(errno.EXDEV, "synthetic filesystem boundary")
        return original_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
    original_copy = logs._copy_retained_file
    for fail in (True, False):
        label = "fail" if fail else "success"
        root = outer / label; root.mkdir()
        record = outer / (label + ".json"); recovery = outer / (label + "-recovery")
        logs.provision(root, roles, 0, record)
        current = root / "logs/api/api.log"
        current.write_bytes(b"current bytes"); os.chown(current, 1, 0)
        rotation = current.with_name("api.log.12")
        rotation.write_bytes(b"rotated bytes"); os.chown(rotation, 1, 0)
        def copy(source, destination, name):
            result = original_copy(source, destination, name)
            if fail: raise OSError("injected failure after durable copy")
            return result
        logs._copy_retained_file = copy
        os.rename = exdev
        try:
            try: logs.rollback(root, roles, 0, record, recovery)
            except OSError:
                if not fail: raise
            else:
                assert not fail
        finally: os.rename = original_rename
        if fail:
            assert current.read_bytes() == b"current bytes" and rotation.read_bytes() == b"rotated bytes"
            assert (root / "logs" / logs.MARKER).exists()
            assert any(p.is_file() for p in recovery.rglob("*"))
        else:
            assert not (root / "logs").exists()
            assert (recovery / "api/api.log").read_bytes() == b"current bytes"
            assert (recovery / "api/api.log.12").read_bytes() == b"rotated bytes"
            assert (recovery / "api/api.log").stat().st_uid == 0
        assert recovery.stat().st_mode & 0o777 == 0o700
""".replace("HELPER", repr(str(ROOT / "ops/deploy/service_logs.py")))
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
