"""Only real flock contention is busy; unsafe custody never degrades to busy."""

import errno
import fcntl
import os

import pytest

from astinuslab.lab_packages import prepare_authority
from astinuslab.lab_packages.prepare_authority import PrepareClaimLost, PreparePackageBusy, try_package_lock


def test_package_lock_is_stable_nonblocking_and_closes_descriptor(tmp_path):
    path = tmp_path / "prepare.lock"
    with try_package_lock(path, "package") as lock:
        descriptor = lock.descriptor
        inode = path.stat().st_ino
        assert fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        with pytest.raises(PrepareClaimLost):
            lock.require("different-package")
        with pytest.raises(PreparePackageBusy):
            with try_package_lock(path, "package"):
                pytest.fail("same-process contender acquired custody")
    assert not lock.active
    with pytest.raises(OSError) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF
    with try_package_lock(path, "package"):
        assert path.stat().st_ino == inode


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo"])
def test_package_lock_refuses_unsafe_inode_without_changing_target(tmp_path, unsafe):
    path, target = tmp_path / "prepare.lock", tmp_path / "target"
    target.write_bytes(b"unchanged")
    if unsafe == "symlink":
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path)
    with pytest.raises(OSError):
        with try_package_lock(path, "package"):
            pytest.fail("unsafe lock accepted")
    assert target.read_bytes() == b"unchanged"


@pytest.mark.parametrize("stage", ["acl", "flock"])
def test_lock_permission_failure_propagates_and_closes_descriptor(tmp_path, monkeypatch, stage):
    path = tmp_path / "prepare.lock"
    original_open = os.open
    opened = []

    def observed_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def deny(*args, **kwargs):
        raise PermissionError(errno.EACCES, "synthetic custody failure")

    monkeypatch.setattr(prepare_authority.os, "open", observed_open)
    if stage == "acl":
        monkeypatch.setattr(prepare_authority, "secure_file", deny)
    else:
        monkeypatch.setattr(prepare_authority.fcntl, "flock", deny)
    with pytest.raises(PermissionError):
        with try_package_lock(path, "package"):
            pytest.fail("permission failure accepted")
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF
