"""Regressions for B1: capture-dir must not be nested under disk-dir, and the
startup sweep must only delete `pg-<hex>` session directories."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from astinuslab.core.config import Settings
from tests.python._support.helpers import autospec_setattr


def test_default_playground_paths_are_repo_local_and_not_nested():
    s = Settings()
    disk = s.playground_disk_dir.resolve()
    cap = s.playground_capture_dir.resolve()
    assert disk == s.data_dir / "storage" / "playground" / "disks"
    assert cap == s.data_dir / "storage" / "playground" / "captures"
    assert disk != cap
    assert not disk.is_relative_to(cap)
    assert not cap.is_relative_to(disk)


@pytest.mark.parametrize(
    "disk, cap",
    [
        ("/var/lib/x", "/var/lib/x/captures"),  # captures inside disks
        ("/var/lib/x/disks", "/var/lib/x/disks"),  # identical
        ("/var/lib/x/disks", "/var/lib/x/disks/c"),  # captures inside disks
        ("/var/lib/x/disks/d", "/var/lib/x/disks"),  # disks inside captures
    ],
)
def test_validator_rejects_nested_paths(disk: str, cap: str):
    with pytest.raises(ValidationError):
        Settings(playground_disk_dir=Path(disk), playground_capture_dir=Path(cap))


def test_validator_accepts_sibling_paths():
    s = Settings(
        playground_disk_dir=Path("/var/lib/x/disks"),
        playground_capture_dir=Path("/var/lib/x/captures"),
    )
    assert s.playground_disk_dir != s.playground_capture_dir


def _install_libvirt_stub(monkeypatch):
    """Monkeypatch libvirt.open so cleanup_stale_playground_resources progresses
    past the libvirt section into the filesystem sweep we care about."""
    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = []
    fake_conn.listAllNetworks.return_value = []
    libvirt_mod = sys.modules["libvirt"]
    autospec_setattr(monkeypatch, libvirt_mod, "open", lambda uri: fake_conn)


def test_cleanup_preserves_non_session_dirs(monkeypatch, tmp_path, in_memory_session_scope):
    from astinuslab.jobs import startup_recovery

    disk_root = tmp_path / "disks"
    cap_root = tmp_path / "captures"
    disk_root.mkdir()
    cap_root.mkdir()

    # Non-active disk-overlay session dir must be wiped.
    (disk_root / "pg-aaaaaaaaaaaa").mkdir()
    (disk_root / "pg-aaaaaaaaaaaa" / "x.qcow2").write_bytes(b"x")
    # C1: capture-dir session subdirs are forensic evidence and must survive
    # the startup sweep, regardless of session liveness.
    (cap_root / "pg-bbbbbbbbbbbb").mkdir()
    (cap_root / "pg-bbbbbbbbbbbb" / "memory.elf").write_bytes(b"y")

    # Non-session siblings that MUST survive.
    sibling_captures = disk_root / "captures"  # legacy accidental nest
    sibling_captures.mkdir()
    (sibling_captures / "legacy.elf").write_bytes(b"legacy")

    keep = disk_root / "keep-me-please"
    keep.mkdir()
    (keep / "note.txt").write_text("hands off")

    cap_random = cap_root / "random"
    cap_random.mkdir()
    (cap_random / "n.txt").write_text("hands off")

    tmp_base = tmp_path / "storage" / "tmp"
    tmp_base.mkdir(parents=True)
    (tmp_base / "pg-ccccccccccc1").mkdir()
    (tmp_base / "unrelated").mkdir()

    fake_settings = types.SimpleNamespace(
        libvirt_uri="qemu:///system",
        playground_disk_dir=disk_root,
        playground_capture_dir=cap_root,
        base_dir=tmp_path,
        data_dir=tmp_path,
        storage_dir=tmp_path / "storage",
    )
    monkeypatch.setattr(startup_recovery, "settings", fake_settings)
    _install_libvirt_stub(monkeypatch)
    monkeypatch.setattr(startup_recovery, "_pg_session_scope", in_memory_session_scope)

    startup_recovery.cleanup_stale_playground_resources()

    # Disk-overlay + tmp session dirs gone; capture-dir session preserved (C1).
    assert not (disk_root / "pg-aaaaaaaaaaaa").exists()
    assert (cap_root / "pg-bbbbbbbbbbbb" / "memory.elf").read_bytes() == b"y"
    assert not (tmp_base / "pg-ccccccccccc1").exists()

    # Non-session content preserved.
    assert (sibling_captures / "legacy.elf").read_bytes() == b"legacy"
    assert (keep / "note.txt").read_text() == "hands off"
    assert (cap_random / "n.txt").read_text() == "hands off"
    assert (tmp_base / "unrelated").exists()


def _walk_modes(root: Path, leaf: Path) -> list[tuple[Path, int]]:
    """Every component the privileged ACL helper walks, storage root excluded.

    The helper starts at the policy root and descends to its target, so the root
    itself is never one of the components it inspects.
    """
    return [
        (parent, parent.stat().st_mode & 0o7777)
        for parent in [*reversed(leaf.parents), leaf]
        if parent != root and parent.is_relative_to(root)
    ]


def test_playground_roots_are_created_without_group_write(tmp_path, monkeypatch, in_memory_session_scope):
    """Regression: a bare mkdir left the playground roots group-writable.

    `fix_libvirt_path_acl.py` refuses to walk a group- or world-writable path
    component, and `Path.mkdir(parents=True)` inherits the umask: 0002 yields
    0775 on `playground` and `playground/disks`. The pre-boot ACL grant then
    failed with "ACL path component is group writable", spawn_vm raised before
    libvirt was asked to define the domain, and the guest never reached DHCP.

    A root that already exists too loose has to be tightened as well, because a
    checkout that booted once under the old code carries 0775 forward.
    """
    from astinuslab import db
    from astinuslab.jobs.celery_worker import celery_app
    from astinuslab.playground import acquisition as pa
    from astinuslab.playground import network_lifecycle as nl
    from astinuslab.playground import orchestrator as po

    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(db, "session_scope", in_memory_session_scope)

    storage = tmp_path / "storage"
    storage.mkdir()
    # The installed layout hands the service group write access on the root and
    # names that group in the ACL policy. Nothing below may copy that bit.
    os.chmod(storage, 0o2770)
    # A playground root left behind by the old code, before the fix landed.
    loose = storage / "playground"
    loose.mkdir()
    os.chmod(loose, 0o775)

    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(
        po,
        "settings",
        types.SimpleNamespace(
            playground_disk_dir=storage / "playground" / "disks",
            playground_capture_dir=storage / "playground" / "captures",
            playground_default_vcpus=2,
            playground_default_memory_mb=2048,
            libvirt_uri="qemu:///system",
            base_dir=tmp_path,
            data_dir=tmp_path,
            storage_dir=storage,
        ),
    )
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())
    vm_mock = MagicMock()
    vm_mock.get_ip.return_value = "10.42.1.10"
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: vm_mock)
    base_image = tmp_path / "base.qcow2"
    base_image.write_bytes(b"placeholder image")
    autospec_setattr(
        monkeypatch,
        po,
        "lookup_playground_image",
        lambda _, *, role="target": {
            "id": "img",
            "path": str(base_image),
            "format": "qcow2",
            "architecture": "x86_64",
            "credentials": {"username": "u", "password": "p"},
        },
    )
    autospec_setattr(monkeypatch, pa, "_dump_memory", lambda domain, out: out.write_bytes(b"memory"))

    previous_umask = os.umask(0o002)
    try:
        po.create_session.delay(job_id="pg-mode1").get()
        po.create_network.delay(
            job_id="pg-mode1",
            network_id="n1",
            name="ab",
            subnet_cidr="10.42.1.0/24",
            isolated=True,
        ).get()
        po.spawn_vm.delay(
            job_id="pg-mode1",
            vm_id="v1",
            label="t1",
            image_id="img",
            nic_specs=[{"network_id": "n1", "nic_id": "nic1"}],
        ).get()
        po.capture_memory.delay(vm_id="v1").get()
    finally:
        os.umask(previous_umask)

    walked = [
        *_walk_modes(storage, storage / "playground" / "disks" / "pg-mode1"),
        *_walk_modes(storage, storage / "playground" / "captures" / "pg-mode1" / "v1"),
    ]
    assert walked, "the capture and overlay trees were never created"
    offenders = [(str(path), oct(mode)) for path, mode in walked if mode & 0o022]
    assert offenders == [], f"ACL helper would refuse these components: {offenders}"
    # The root keeps the group-write bit the installer grants it on purpose.
    assert storage.stat().st_mode & 0o2770 == 0o2770
