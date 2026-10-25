"""Real ACL enforcement under three disposable, kernel-mapped service UIDs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "ops/deploy/runtime_control_locks.py"


def test_split_service_lock_custody_and_existing_file_migration():
    script = r"""
import importlib.util, json, os, pathlib, subprocess, tempfile
spec = importlib.util.spec_from_file_location("lock_policy", HELPER)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
with tempfile.TemporaryDirectory(prefix="astinus-lock-uids-") as temporary:
    root = pathlib.Path(temporary); root.chmod(0o755)
    tmp = root / "tmp"; tmp.mkdir(mode=0o750); os.chown(tmp, 2, 0)
    locks = tmp / "forensic-capture-locks"; locks.mkdir(mode=0o750); os.chown(locks, 2, 0)
    old = locks / "existing.lock"; old.write_text("retained"); old.chmod(0o640); os.chown(old, 2, 0)
    sentinel = tmp / "unrelated"; sentinel.write_text("untouched"); sentinel.chmod(0o640); os.chown(sentinel, 2, 0)
    before = (sentinel.stat().st_mode, sentinel.stat().st_uid, sentinel.read_bytes(), tmp.stat().st_mode)
    def as_uid(uid, code):
        def identity():
            os.setgroups([0]); os.setgid(0); os.setuid(uid); os.umask(0o007)
        return subprocess.run(["python3", "-c", code], preexec_fn=identity, capture_output=True, text=True)
    assert as_uid(1, f"open({str(old)!r}, 'w').close()").returncode != 0
    module.provision(root, api_uid=1, runner_uid=2, legacy_uid=0)
    assert old.read_text() == "retained"
    for uid in (1, 2):
        assert as_uid(uid, f"open({str(old)!r}, 'a').close()").returncode == 0
    assert as_uid(3, f"open({str(old)!r}).read()").returncode != 0
    new = locks / "api-created.lock"
    assert as_uid(1, f"open({str(new)!r}, 'w').close()").returncode == 0
    assert as_uid(2, f"open({str(new)!r}, 'w').close()").returncode == 0
    assert as_uid(3, f"open({str(new)!r}, 'w').close()").returncode != 0
    # A subsequent generic installer normalization changes modes first.
    for p in (old, new): os.chown(p, 2, 0); p.chmod(0o640)
    locks.chmod(0o750)
    module.provision(root, api_uid=1, runner_uid=2, legacy_uid=0)
    assert as_uid(1, f"open({str(new)!r}, 'w').close()").returncode == 0
    assert before == (sentinel.stat().st_mode, sentinel.stat().st_uid, sentinel.read_bytes(), tmp.stat().st_mode)
    # Audit the complete flat lock directory before any mutation.
    unsafe = locks / "bad.lock"; unsafe.symlink_to(sentinel)
    saved = subprocess.check_output(["getfacl", "-cpn", str(old)])
    try: module.provision(root, api_uid=1, runner_uid=2, legacy_uid=0)
    except OSError: pass
    else: raise AssertionError("symlink accepted")
    assert saved == subprocess.check_output(["getfacl", "-cpn", str(old)])
    unsafe.unlink(); os.link(sentinel, unsafe)
    try: module.provision(root, api_uid=1, runner_uid=2, legacy_uid=0)
    except OSError: pass
    else: raise AssertionError("hardlink accepted")
    unsafe.unlink(); unsafe.write_text("foreign"); os.chown(unsafe, 3, 0)
    try: module.provision(root, api_uid=1, runner_uid=2, legacy_uid=0)
    except OSError: pass
    else: raise AssertionError("foreign owner accepted")
    unsafe.unlink()
    snapshot = root / "before.json"
    snapshot.write_text(json.dumps({"version": 1, "root": str(root), "records": [
        {"path": "tmp/forensic-capture-locks/existing.lock", "dev": old.stat().st_dev, "ino": old.stat().st_ino}
    ]}))
    unknown = locks / "unexpected.lock"; unknown.write_text("do not discard")
    try: module.rollback_new_locks(root, [snapshot], allowed_owners={0,1,2})
    except OSError: pass
    else: raise AssertionError("nonempty post-snapshot data discarded")
    assert new.exists() and unknown.read_text() == "do not discard"
    unknown.unlink()
    module.rollback_new_locks(root, [snapshot], allowed_owners={0,1,2})
    assert not new.exists() and old.read_text() == "retained"
    # Entire newly-created directory is now empty and can be undone safely.
    old.unlink(); new.write_text("")
    module.rollback_new_locks(root, [], allowed_owners={0,1,2})
    assert list(locks.iterdir()) == []
    locks.rmdir()
    print(json.dumps({"uids": [1,2,3], "existing": "passed", "inheritance": "passed", "unsafe": "passed", "unrelated": "preserved"}))
""".replace("HELPER", repr(str(HELPER)))
    result = subprocess.run(
        ["unshare", "--map-root-user", "--map-auto", "python3", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["uids"] == [1, 2, 3]
