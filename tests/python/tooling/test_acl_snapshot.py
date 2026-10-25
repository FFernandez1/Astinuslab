"""Contract for `ops/deploy/acl_snapshot.py`.

The installer takes one of these snapshots before a migration touches a tree it
does not own outright, and replays it when the migration has to be rolled back.
A snapshot that restores the wrong thing is worse than no snapshot: it runs as
root, and the paths it walks include an operator's own directories.

The code was two `python3 - <<'PY'` heredocs in ops/deploy/install.sh until the
extraction, so the only checks that ever reached it were `compile()` and
pyflakes. These cases exercise the module itself; the shell entry points
`snapshot_acl_state` and `restore_acl_snapshot` keep their own coverage in
test_install_script.py and test_package_custody.py.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ops.deploy import acl_snapshot

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "ops/deploy/acl_snapshot.py"
pytestmark = [pytest.mark.unit, pytest.mark.host]


def _require_acl_tools() -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("ACL tools are not available")


def _acl_text(path: Path) -> str:
    return subprocess.run(["getfacl", "-cp", "--", str(path)], capture_output=True, text=True, check=True).stdout


def _payload(snapshot: Path) -> dict:
    return json.loads(snapshot.read_text(encoding="utf-8"))


def _tree(root: Path) -> None:
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested/file").write_text("content\n", encoding="utf-8")
    (root / "nested/file").chmod(0o640)
    (root / "nested").chmod(0o750)
    root.chmod(0o755)


def test_capture_records_every_entry_with_its_mode_and_acl(tmp_path):
    _require_acl_tools()
    root = tmp_path / "root"
    _tree(root)
    subprocess.run(["setfacl", "-m", "u:nobody:r", str(root / "nested/file")], check=True)
    snapshot = tmp_path / "state.acl"

    acl_snapshot.capture(str(root), str(snapshot))

    payload = _payload(snapshot)
    assert payload["version"] == acl_snapshot.SNAPSHOT_VERSION
    assert payload["root"] == str(root)
    records = {record["path"]: record for record in payload["records"]}
    assert set(records) == {".", "nested", "nested/file"}
    assert records["nested"]["kind"] == "directory"
    assert records["nested/file"]["mode"] == 0o640
    assert "user:nobody:r--" in records["nested/file"]["acl"]
    # The snapshot is rollback evidence and must not be world-readable.
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_capture_refuses_to_overwrite_an_existing_snapshot(tmp_path):
    _require_acl_tools()
    root = tmp_path / "root"
    _tree(root)
    snapshot = tmp_path / "state.acl"
    snapshot.write_text("occupied", encoding="utf-8")

    with pytest.raises(FileExistsError):
        acl_snapshot.capture(str(root), str(snapshot))

    assert snapshot.read_text(encoding="utf-8") == "occupied"


def test_capture_skips_symlinks_rather_than_following_them(tmp_path):
    """A symlink has no independently restorable access ACL.

    Recording one would also mean reaching whatever it points at, which is how
    a snapshot taken as root escapes the tree it was asked to capture.
    """
    _require_acl_tools()
    root = tmp_path / "root"
    _tree(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    snapshot = tmp_path / "state.acl"

    acl_snapshot.capture(str(root), str(snapshot))

    paths = {record["path"] for record in _payload(snapshot)["records"]}
    assert "link" not in paths


def test_restore_puts_back_mode_and_acl(tmp_path):
    _require_acl_tools()
    root = tmp_path / "root"
    _tree(root)
    target = root / "nested/file"
    subprocess.run(["setfacl", "-m", "u:nobody:r", str(target)], check=True)
    snapshot = tmp_path / "state.acl"
    acl_snapshot.capture(str(root), str(snapshot))
    captured_acl = _acl_text(target)

    target.chmod(0o666)
    subprocess.run(["setfacl", "-m", "u:nobody:rwx", str(target)], check=True)
    assert _acl_text(target) != captured_acl

    acl_snapshot.restore(str(snapshot), [])

    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert _acl_text(target) == captured_acl


def test_restore_removes_a_default_acl_the_snapshot_never_had(tmp_path):
    """`--set-file` replaces access entries but leaves a new default ACL behind.

    A default ACL added after the capture would keep applying to everything
    created under the directory afterwards, so the restore has to drop it.
    """
    _require_acl_tools()
    root = tmp_path / "root"
    _tree(root)
    snapshot = tmp_path / "state.acl"
    acl_snapshot.capture(str(root), str(snapshot))

    subprocess.run(["setfacl", "-d", "-m", "u:nobody:rwx", str(root / "nested")], check=True)
    assert "default:user:nobody" in _acl_text(root / "nested")

    acl_snapshot.restore(str(snapshot), [])

    assert "default:user:nobody" not in _acl_text(root / "nested")


def test_restore_refuses_a_snapshot_anyone_else_can_read(tmp_path):
    _require_acl_tools()
    root = tmp_path / "root"
    _tree(root)
    snapshot = tmp_path / "state.acl"
    acl_snapshot.capture(str(root), str(snapshot))
    snapshot.chmod(0o644)

    with pytest.raises(SystemExit, match="unsafe custody"):
        acl_snapshot.restore(str(snapshot), [])


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda payload: payload.update(version=2), "unsupported format"),
        (lambda payload: payload.update(root="relative/root"), "malformed"),
        (lambda payload: payload["records"][1].update(path="../escape"), "unsafe path"),
        (lambda payload: payload["records"][1].update(kind="socket"), "unsafe path"),
        (lambda payload: payload["records"][1].update(mode=-1), "invalid metadata"),
        (lambda payload: payload["records"][1].update(acl=None), "invalid ACL data"),
        (lambda payload: payload["records"].pop(0), "no directory root"),
    ],
)
def test_restore_refuses_a_snapshot_it_cannot_trust(tmp_path, mutate, message):
    """Validation precedes the first `fchown`, so a bad payload changes nothing."""
    _require_acl_tools()
    root = tmp_path / "root"
    _tree(root)
    snapshot = tmp_path / "state.acl"
    acl_snapshot.capture(str(root), str(snapshot))

    payload = _payload(snapshot)
    mutate(payload)
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    snapshot.chmod(0o600)
    before = stat.S_IMODE((root / "nested/file").stat().st_mode)
    (root / "nested/file").chmod(0o600)

    with pytest.raises(SystemExit, match=message):
        acl_snapshot.restore(str(snapshot), [])

    assert stat.S_IMODE((root / "nested/file").stat().st_mode) == 0o600, (
        f"a snapshot rejected as {message!r} must not have restored {oct(before)}"
    )


def test_restore_refuses_a_recorded_path_swapped_for_a_symlink(tmp_path):
    """The inode recorded at capture is what restore reopens and verifies."""
    _require_acl_tools()
    root = tmp_path / "root"
    _tree(root)
    snapshot = tmp_path / "state.acl"
    acl_snapshot.capture(str(root), str(snapshot))

    sentinel = tmp_path / "sentinel"
    sentinel.write_text("outside\n", encoding="utf-8")
    sentinel.chmod(0o666)
    (root / "nested/file").unlink()
    (root / "nested/file").symlink_to(sentinel)

    with pytest.raises(SystemExit):
        acl_snapshot.restore(str(snapshot), [])

    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o666


def test_restore_tolerates_sqlite_sidecars_a_stopped_service_removed(tmp_path):
    """SQLite creates and removes these while a database is open.

    A rollback that insisted on them would fail on every stopped service, so
    their absence is tolerated -- but only directly under a root the caller
    named, and only for these exact basenames.
    """
    _require_acl_tools()
    root = tmp_path / "root"
    root.mkdir()
    database = root / "database"
    database.mkdir()
    (database / "astinuslab.db-wal").write_text("wal", encoding="utf-8")
    (database / "material.db").write_text("material", encoding="utf-8")
    snapshot = tmp_path / "state.acl"
    acl_snapshot.capture(str(root), str(snapshot))

    (database / "astinuslab.db-wal").unlink()
    acl_snapshot.restore(str(snapshot), [str(database)])

    (database / "material.db").unlink()
    with pytest.raises(SystemExit, match="material path disappeared"):
        acl_snapshot.restore(str(snapshot), [str(database)])


def test_restore_refuses_a_sidecar_absence_outside_an_allowed_root(tmp_path):
    """The same basename somewhere the caller did not name is still material."""
    _require_acl_tools()
    root = tmp_path / "root"
    root.mkdir()
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "astinuslab.db-wal").write_text("wal", encoding="utf-8")
    snapshot = tmp_path / "state.acl"
    acl_snapshot.capture(str(root), str(snapshot))

    (elsewhere / "astinuslab.db-wal").unlink()

    with pytest.raises(SystemExit, match="material path disappeared"):
        acl_snapshot.restore(str(snapshot), [])


def test_restore_tolerates_purged_playground_disk_scratch(tmp_path):
    """Split services purge staging for sessions that no longer exist.

    They start before the migration commits, so the scratch tree can lose
    descendants under a rollback. The tree itself stays material.
    """
    _require_acl_tools()
    root = tmp_path / "root"
    scratch = root / "playground/disks/session"
    scratch.mkdir(parents=True)
    (scratch / "overlay.qcow2").write_text("overlay", encoding="utf-8")
    snapshot = tmp_path / "state.acl"
    acl_snapshot.capture(str(root), str(snapshot))

    shutil.rmtree(scratch)
    acl_snapshot.restore(str(snapshot), [str(root)])

    shutil.rmtree(root / "playground/disks")
    with pytest.raises(SystemExit, match="material path disappeared"):
        acl_snapshot.restore(str(snapshot), [str(root)])


def test_capture_refuses_a_tree_deeper_than_the_limit(tmp_path):
    _require_acl_tools()
    root = tmp_path / "root"
    deep = root.joinpath(*[f"d{index}" for index in range(acl_snapshot.MAX_DEPTH + 2)])
    deep.mkdir(parents=True)

    with pytest.raises(SystemExit, match="depth limit"):
        acl_snapshot.capture(str(root), str(tmp_path / "state.acl"))


def _run_module(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MODULE), *arguments],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def test_the_command_line_round_trips_a_tree(tmp_path):
    """The shell functions invoke this as a subprocess, not as an import."""
    _require_acl_tools()
    root = tmp_path / "root"
    _tree(root)
    snapshot = tmp_path / "state.acl"

    assert _run_module("capture", str(root), str(snapshot)).returncode == 0

    (root / "nested/file").chmod(0o666)
    assert _run_module("restore", str(snapshot), str(root)).returncode == 0
    assert stat.S_IMODE((root / "nested/file").stat().st_mode) == 0o640


@pytest.mark.parametrize(
    "arguments",
    [(), ("capture",), ("capture", "one"), ("restore",), ("purge", "one", "two")],
)
def test_the_command_line_refuses_an_argument_count_it_cannot_mean(arguments):
    result = _run_module(*arguments)

    assert result.returncode != 0
    assert "usage: acl_snapshot.py" in result.stderr
