"""Characterization coverage for the playground no-follow filesystem primitives.

This is the symlink-safety layer every other playground domain calls before it
deletes anything on the host. Every guard in it exists because the alternative
is deleting the wrong inode: a planted symlink, a directory swapped between the
check and the act, a staging path that is not a directory, a cleanup that walks
off the storage volume.

Those guards were the least covered code in `orchestrator.py` and they are about
to move to a sibling module. A refusal that is never exercised is a refusal
nobody has seen work, so the branch that survives a move untested is exactly the
branch that matters. These cases pin each one before the move, against
`orchestrator` where the code still lives; they keep passing through it
afterwards because the names stay reachable there.

The races are provoked by replacing the inode inside a patched `os.stat` or
`os.open` at the point production would lose it. That is real inode replacement
rather than a mocked mismatch: the function is given a genuinely different file
between one probe and the next, which is what an attacker with write access to
the session directory would do.
"""

from __future__ import annotations

import errno
import os
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _open_descriptor_count() -> int:
    """How many descriptors this process holds, for leak assertions."""
    return len(os.listdir("/proc/self/fd"))


# ---------------------------------------------------------------------------
# _open_directory_no_follow: the absolute walk from /
# ---------------------------------------------------------------------------


def test_absolute_walk_refuses_a_symlinked_component_without_leaking(tmp_path):
    """A symlink anywhere in the walk is refused, and the partial walk is closed.

    The walk holds one descriptor at a time and replaces it as it descends. The
    failure path has to close whatever it is holding, or every refused cleanup
    leaks a descriptor into a long-lived Celery worker.
    """
    from astinuslab.playground import filesystem_safety as fs

    real_directory = tmp_path / "real"
    real_directory.mkdir()
    (real_directory / "child").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_directory, target_is_directory=True)

    before = _open_descriptor_count()
    with pytest.raises(OSError) as excinfo:
        fs._open_directory_no_follow(link / "child")
    assert excinfo.value.errno in (errno.ELOOP, errno.ENOTDIR)
    assert _open_descriptor_count() == before


def test_absolute_walk_returns_a_descriptor_for_a_real_directory(tmp_path):
    """The success path is the baseline the refusals are measured against."""
    from astinuslab.playground import filesystem_safety as fs

    directory = tmp_path / "playground"
    directory.mkdir()

    before = _open_descriptor_count()
    descriptor = fs._open_directory_no_follow(directory)
    try:
        assert os.fstat(descriptor).st_ino == directory.stat().st_ino
    finally:
        os.close(descriptor)
    assert _open_descriptor_count() == before


# ---------------------------------------------------------------------------
# _open_relative_directory_no_follow: containment on one volume
# ---------------------------------------------------------------------------


def test_relative_walk_refuses_to_cross_a_mount_point(monkeypatch, tmp_path):
    """Cleanup must stop at a volume boundary rather than descend through it.

    A session directory that is really a mount point belongs to whatever is
    mounted there. Deleting through it would remove files the playground never
    created, so the walk compares st_dev against the root it started from.
    Provoking a real mount needs privileges this suite does not have, so the
    second stat reports a different device instead.
    """
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "playground"
    (root / "pg-1").mkdir(parents=True)
    real_fstat = os.fstat
    seen: list[int] = []

    def fstat_reporting_another_volume(fd):
        result = real_fstat(fd)
        seen.append(fd)
        if len(seen) == 1:
            return result
        return SimpleNamespace(st_dev=result.st_dev + 1, st_ino=result.st_ino, st_mode=result.st_mode)

    root_fd = fs._open_directory_no_follow(root)
    try:
        before = _open_descriptor_count()
        monkeypatch.setattr(os, "fstat", fstat_reporting_another_volume)
        with pytest.raises(RuntimeError, match="refusing playground cleanup across a mount point"):
            fs._open_relative_directory_no_follow(root_fd, ("pg-1",))
        monkeypatch.undo()
        assert _open_descriptor_count() == before
    finally:
        os.close(root_fd)

    assert (root / "pg-1").is_dir()


# ---------------------------------------------------------------------------
# _unlink_regular_no_follow: only a regular file, only the one probed
# ---------------------------------------------------------------------------


def test_symlinked_artifact_is_never_unlinked(tmp_path):
    """A symlink in the artifact's place is refused, not followed and deleted."""
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()
    target = tmp_path / "operator-data.qcow2"
    target.write_bytes(b"not ours")
    (parent / "web.qcow2").symlink_to(target)

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="refusing to delete symlinked playground artifact"):
            fs._unlink_regular_no_follow(parent_fd, "web.qcow2")
    finally:
        os.close(parent_fd)

    assert target.read_bytes() == b"not ours"
    assert (parent / "web.qcow2").is_symlink()


def test_non_file_artifact_is_never_unlinked(tmp_path):
    """A directory where a disk image belongs is refused rather than removed."""
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()
    (parent / "web.qcow2").mkdir()

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="refusing to delete non-file playground artifact"):
            fs._unlink_regular_no_follow(parent_fd, "web.qcow2")
    finally:
        os.close(parent_fd)

    assert (parent / "web.qcow2").is_dir()


def test_artifact_replaced_after_the_descriptor_check_is_not_unlinked(monkeypatch, tmp_path):
    """The re-probe after opening must catch a swap the fstat comparison cannot.

    Comparing the opened descriptor against the first stat proves the descriptor
    is the file that was probed. It does not prove the *name* still refers to
    it, and `os.unlink` acts on the name. A rename into place between those two
    points would make the unlink delete the replacement, so the name is probed
    once more and compared against the descriptor.
    """
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()
    artifact = parent / "web.qcow2"
    artifact.write_bytes(b"original")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    real_stat = os.stat
    probes = 0

    def replace_between_probes(path, *args, dir_fd=None, follow_symlinks=True, **kwargs):
        nonlocal probes
        if path == artifact.name and dir_fd == parent_fd:
            probes += 1
            if probes == 2:
                artifact.unlink()
                artifact.write_bytes(b"replacement")
        return real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks, **kwargs)

    monkeypatch.setattr(os, "stat", replace_between_probes)
    try:
        with pytest.raises(RuntimeError, match="was replaced during cleanup"):
            fs._unlink_regular_no_follow(parent_fd, artifact.name)
    finally:
        monkeypatch.undo()
        os.close(parent_fd)

    assert artifact.read_bytes() == b"replacement"


# ---------------------------------------------------------------------------
# _remove_cloud_init_directory_no_follow: a directory of regular files, or nothing
# ---------------------------------------------------------------------------


def test_non_directory_cloud_init_path_is_refused(tmp_path):
    """A regular file in the cloud-init directory's place is not removed."""
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()
    (parent / "web-cloud-init").write_bytes(b"operator note")

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="refusing to delete non-directory playground artifact"):
            fs._remove_cloud_init_directory_no_follow(parent_fd, "web-cloud-init")
    finally:
        os.close(parent_fd)

    assert (parent / "web-cloud-init").read_bytes() == b"operator note"


def test_cloud_init_directory_replaced_before_it_is_opened_is_refused(monkeypatch, tmp_path):
    """A directory swapped between the stat and the open is not emptied."""
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()
    original = parent / "web-cloud-init"
    original.mkdir()
    (original / "user-data").write_text("ours", encoding="utf-8")
    displaced = parent / "displaced"
    real_open = os.open
    swapped = False

    def swap_before_descriptor_open(path, flags, *args, dir_fd=None, **kwargs):
        nonlocal swapped
        if path == original.name and dir_fd == parent_fd and not swapped:
            swapped = True
            original.rename(displaced)
            original.mkdir()
            (original / "planted").write_text("theirs", encoding="utf-8")
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(os, "open", swap_before_descriptor_open)
    try:
        with pytest.raises(RuntimeError, match="was replaced during cleanup"):
            fs._remove_cloud_init_directory_no_follow(parent_fd, original.name)
    finally:
        monkeypatch.undo()
        os.close(parent_fd)

    assert (original / "planted").read_text(encoding="utf-8") == "theirs"
    assert (displaced / "user-data").read_text(encoding="utf-8") == "ours"


def test_unexpected_cloud_init_child_is_refused(tmp_path):
    """Cloud-init seed directories hold regular files; anything else stops cleanup.

    A symlinked child would be unlinked safely by the file helper, but its
    presence means this is not the directory the spawn wrote. Removing the rest
    of it would destroy state the playground does not own, so the whole
    directory is left alone and the operator is told.
    """
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()
    cloud_init = parent / "web-cloud-init"
    cloud_init.mkdir()
    (cloud_init / "user-data").write_text("seed", encoding="utf-8")
    outside = tmp_path / "operator-secret"
    outside.write_text("keep", encoding="utf-8")
    (cloud_init / "meta-data").symlink_to(outside)

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="refusing unexpected cloud-init artifact during cleanup"):
            fs._remove_cloud_init_directory_no_follow(parent_fd, cloud_init.name)
    finally:
        os.close(parent_fd)

    assert outside.read_text(encoding="utf-8") == "keep"
    assert cloud_init.is_dir()


def test_cloud_init_directory_replaced_mid_scan_is_refused(monkeypatch, tmp_path):
    """The name is re-probed for every child, not once before the loop.

    Emptying a directory takes as many syscalls as it has entries, and the name
    can be swapped part-way through. The descriptor keeps pointing at the right
    inode, so the unlinks stay contained, but continuing would end with an rmdir
    against whatever now holds the name.

    The observable difference between this check and the one after the loop is
    whether the children survive. This check fires before the unlink, so the
    displaced directory keeps its contents; without it the loop empties a
    directory that no longer answers to the name it was opened under, and only
    the post-loop probe notices. Both raise the same message, so the surviving
    child is what separates them.
    """
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()
    cloud_init = parent / "web-cloud-init"
    cloud_init.mkdir()
    (cloud_init / "user-data").write_text("seed", encoding="utf-8")
    displaced = parent / "displaced"
    real_stat = os.stat
    probes = 0

    def swap_during_the_scan(path, *args, dir_fd=None, follow_symlinks=True, **kwargs):
        nonlocal probes
        if path == cloud_init.name and dir_fd == parent_fd:
            probes += 1
            if probes == 2:
                cloud_init.rename(displaced)
                cloud_init.mkdir()
        return real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks, **kwargs)

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(os, "stat", swap_during_the_scan)
    try:
        with pytest.raises(RuntimeError, match="was replaced during cleanup"):
            fs._remove_cloud_init_directory_no_follow(parent_fd, cloud_init.name)
    finally:
        monkeypatch.undo()
        os.close(parent_fd)

    assert cloud_init.is_dir()
    assert (displaced / "user-data").read_text(encoding="utf-8") == "seed", (
        "the refusal must precede the unlink, or the opened directory is emptied anyway"
    )


def test_cloud_init_directory_replaced_before_rmdir_is_refused(monkeypatch, tmp_path):
    """An empty directory is still re-probed before the rmdir that removes it."""
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()
    cloud_init = parent / "web-cloud-init"
    cloud_init.mkdir()
    displaced = parent / "displaced"
    real_stat = os.stat
    probes = 0

    def swap_before_rmdir(path, *args, dir_fd=None, follow_symlinks=True, **kwargs):
        nonlocal probes
        if path == cloud_init.name and dir_fd == parent_fd:
            probes += 1
            if probes == 2:
                cloud_init.rename(displaced)
                cloud_init.mkdir()
                (cloud_init / "planted").write_text("theirs", encoding="utf-8")
        return real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks, **kwargs)

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(os, "stat", swap_before_rmdir)
    try:
        with pytest.raises(RuntimeError, match="was replaced during cleanup"):
            fs._remove_cloud_init_directory_no_follow(parent_fd, cloud_init.name)
    finally:
        monkeypatch.undo()
        os.close(parent_fd)

    assert (cloud_init / "planted").read_text(encoding="utf-8") == "theirs"


def test_cloud_init_directory_missing_is_not_an_error(tmp_path):
    """A cleanup that has already happened must not fail the second teardown."""
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fs._remove_cloud_init_directory_no_follow(parent_fd, "web-cloud-init")
    finally:
        os.close(parent_fd)


# ---------------------------------------------------------------------------
# _remove_empty_relative_directory_no_follow: the contained staging directory
# ---------------------------------------------------------------------------


def test_empty_relative_parts_remove_nothing(tmp_path):
    """No relative parts means the disk lived at the root, which is never removed."""
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "playground"
    root.mkdir()

    root_fd = fs._open_directory_no_follow(root)
    try:
        fs._remove_empty_relative_directory_no_follow(root_fd, ())
    finally:
        os.close(root_fd)

    assert root.is_dir()


def test_nested_staging_directory_is_removed_through_its_ancestors(tmp_path):
    """The descent holds one ancestor descriptor at a time down to the last part."""
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "playground"
    nested = root / "tenant-a" / "pg-1"
    nested.mkdir(parents=True)

    root_fd = fs._open_directory_no_follow(root)
    try:
        before = _open_descriptor_count()
        fs._remove_empty_relative_directory_no_follow(root_fd, ("tenant-a", "pg-1"))
        assert _open_descriptor_count() == before
    finally:
        os.close(root_fd)

    assert not nested.exists()
    assert nested.parent.is_dir(), "only the last part is removed"


def test_non_directory_staging_path_is_refused(tmp_path):
    """A regular file where the session directory belongs is not unlinked here."""
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "playground"
    root.mkdir()
    (root / "pg-1").write_bytes(b"operator note")

    root_fd = fs._open_directory_no_follow(root)
    try:
        with pytest.raises(RuntimeError, match="refusing to remove non-directory playground staging path"):
            fs._remove_empty_relative_directory_no_follow(root_fd, ("pg-1",))
    finally:
        os.close(root_fd)

    assert (root / "pg-1").read_bytes() == b"operator note"


def test_staging_directory_replaced_before_it_is_opened_is_refused(monkeypatch, tmp_path):
    """The emptiness check must be made against the inode that gets removed."""
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "playground"
    session_dir = root / "pg-1"
    session_dir.mkdir(parents=True)
    displaced = root / "displaced"
    real_open = os.open
    swapped = False
    root_fd = fs._open_directory_no_follow(root)

    def swap_before_descriptor_open(path, flags, *args, dir_fd=None, **kwargs):
        nonlocal swapped
        if path == session_dir.name and dir_fd is not None and dir_fd != root_fd and not swapped:
            swapped = True
            session_dir.rename(displaced)
            session_dir.mkdir()
            (session_dir / "planted").write_text("theirs", encoding="utf-8")
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_descriptor_open)
    try:
        with pytest.raises(RuntimeError, match="staging directory was replaced during cleanup"):
            fs._remove_empty_relative_directory_no_follow(root_fd, ("pg-1",))
    finally:
        monkeypatch.undo()
        os.close(root_fd)

    assert (session_dir / "planted").read_text(encoding="utf-8") == "theirs"


def test_staging_directory_replaced_before_rmdir_is_refused(monkeypatch, tmp_path):
    """An empty directory is re-probed after the scan and before the rmdir."""
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "playground"
    session_dir = root / "pg-1"
    session_dir.mkdir(parents=True)
    displaced = root / "displaced"
    real_stat = os.stat
    probes = 0

    def swap_before_rmdir(path, *args, dir_fd=None, follow_symlinks=True, **kwargs):
        nonlocal probes
        if path == session_dir.name and dir_fd is not None:
            probes += 1
            if probes == 2:
                session_dir.rename(displaced)
                session_dir.mkdir()
                (session_dir / "planted").write_text("theirs", encoding="utf-8")
        return real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks, **kwargs)

    root_fd = fs._open_directory_no_follow(root)
    monkeypatch.setattr(os, "stat", swap_before_rmdir)
    try:
        with pytest.raises(RuntimeError, match="staging directory was replaced during cleanup"):
            fs._remove_empty_relative_directory_no_follow(root_fd, ("pg-1",))
    finally:
        monkeypatch.undo()
        os.close(root_fd)

    assert (session_dir / "planted").read_text(encoding="utf-8") == "theirs"


def test_staging_directory_that_filled_up_during_cleanup_is_left_alone(monkeypatch, tmp_path):
    """A concurrent write between the scan and the rmdir is not an error.

    ENOTEMPTY here means another worker put something in the directory after it
    was found empty. That is the directory being in use, not a safety failure,
    so the removal is abandoned quietly rather than reported as a teardown
    error the operator has to triage.
    """
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "playground"
    session_dir = root / "pg-1"
    session_dir.mkdir(parents=True)

    def rmdir_reporting_a_filled_directory(*args, **kwargs):
        raise OSError(errno.ENOTEMPTY, "Directory not empty")

    root_fd = fs._open_directory_no_follow(root)
    monkeypatch.setattr(os, "rmdir", rmdir_reporting_a_filled_directory)
    try:
        fs._remove_empty_relative_directory_no_follow(root_fd, ("pg-1",))
    finally:
        monkeypatch.undo()
        os.close(root_fd)

    assert session_dir.is_dir()


def test_staging_directory_rmdir_failure_is_reported(monkeypatch, tmp_path):
    """Any errno other than a filled directory is a real failure and travels up."""
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "playground"
    (root / "pg-1").mkdir(parents=True)

    def rmdir_refused(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    root_fd = fs._open_directory_no_follow(root)
    monkeypatch.setattr(os, "rmdir", rmdir_refused)
    try:
        with pytest.raises(OSError) as excinfo:
            fs._remove_empty_relative_directory_no_follow(root_fd, ("pg-1",))
        assert excinfo.value.errno == errno.EACCES
    finally:
        monkeypatch.undo()
        os.close(root_fd)


def test_staging_directory_already_removed_is_not_an_error(tmp_path):
    """Two teardowns racing to the same session directory must both succeed."""
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "playground"
    root.mkdir()

    root_fd = fs._open_directory_no_follow(root)
    try:
        fs._remove_empty_relative_directory_no_follow(root_fd, ("already-gone", "pg-1"))
    finally:
        os.close(root_fd)


# ---------------------------------------------------------------------------
# _playground_artifacts_present: the residue probe reconciliation depends on
# ---------------------------------------------------------------------------


def test_residue_probe_refuses_an_artifact_at_the_storage_root(monkeypatch, tmp_path):
    """A disk recorded directly at the root has no session directory to probe.

    Accepting it would make the probe -- and the removal that follows it --
    operate on the storage root itself, where the names it builds could match
    anything an operator put there.
    """
    from astinuslab.playground import filesystem_safety as fs
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "playground"
    root.mkdir()
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root))

    with pytest.raises(RuntimeError, match="refusing to inspect playground artifact at storage root"):
        fs._playground_artifacts_present(root / "web.qcow2")


def test_residue_probe_refuses_an_artifact_outside_the_storage_root(monkeypatch, tmp_path):
    """A path that is not below the configured root is refused, not probed."""
    from astinuslab.playground import filesystem_safety as fs
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "playground"
    root.mkdir()
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root))

    with pytest.raises(RuntimeError, match="refusing to inspect artifact outside playground dir"):
        fs._playground_artifacts_present(tmp_path / "elsewhere" / "web.qcow2")


def test_residue_probe_reports_nothing_when_the_session_directory_is_gone(monkeypatch, tmp_path):
    """A missing session directory is answered False, not raised.

    Reconciliation asks this about rows whose host state it does not know. A
    directory that is already gone is the answer the caller wants, not an error
    it has to interpret.
    """
    from astinuslab.playground import filesystem_safety as fs
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "playground"
    root.mkdir()
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root))

    assert fs._playground_artifacts_present(root / "pg-1" / "web.qcow2") is False


def test_residue_probe_reports_nothing_for_an_empty_session_directory(monkeypatch, tmp_path):
    """Every artifact name is probed before the answer is False."""
    from astinuslab.playground import filesystem_safety as fs
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "playground"
    (root / "pg-1").mkdir(parents=True)
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root))

    assert fs._playground_artifacts_present(root / "pg-1" / "web.qcow2") is False


def test_residue_probe_finds_a_cloud_init_directory_with_no_disk(monkeypatch, tmp_path):
    """Residue is not only the disk: a leftover seed directory still counts.

    The probe appends the cloud-init directory to the spawn artifact names for
    exactly this case -- a spawn that wrote its seed and then failed before the
    overlay existed leaves residue no disk-only probe would see.
    """
    from astinuslab.playground import filesystem_safety as fs
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "playground"
    session_dir = root / "pg-1"
    session_dir.mkdir(parents=True)
    (session_dir / "web-cloud-init").mkdir()
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root))

    assert fs._playground_artifacts_present(session_dir / "web.qcow2") is True


# ---------------------------------------------------------------------------
# _remove_playground_artifacts: the containment checks before any unlink
# ---------------------------------------------------------------------------


def test_artifact_removal_refuses_an_artifact_at_the_storage_root(monkeypatch, tmp_path):
    """The storage root is never treated as a session directory to empty."""
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "playground"
    root.mkdir()
    keep = root / "web.qcow2"
    keep.write_bytes(b"operator data")
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root))

    with pytest.raises(RuntimeError, match="refusing to delete playground artifact at storage root"):
        po._remove_playground_artifacts(keep)

    assert keep.read_bytes() == b"operator data"


def test_artifact_removal_refuses_an_artifact_outside_the_storage_root(monkeypatch, tmp_path):
    """A recorded disk path that is not below the configured root is never deleted.

    The row is the only thing that says where a VM's overlay lives, and a row
    can be wrong -- edited, migrated from another host, or written when the
    configured root was somewhere else. Containment is re-established from the
    configured root here rather than trusted from the path.
    """
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "playground"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "pg-1"
    outside.mkdir(parents=True)
    keep = outside / "web.qcow2"
    keep.write_bytes(b"not ours")
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root))

    with pytest.raises(RuntimeError, match="refusing to delete artifact outside playground dir"):
        po._remove_playground_artifacts(keep)

    assert keep.read_bytes() == b"not ours"


def test_artifact_removal_reads_the_configured_root_through_the_orchestrator_module(monkeypatch, tmp_path):
    """The root is resolved from `orchestrator.settings` at call time.

    This is the seam the whole suite patches. Binding `settings` by name in the
    module that performs the removal would make a patched configuration silently
    not apply, and the removal would run against the real storage root.

    The sibling is imported here *before* the patch on purpose. `orchestrator`
    imports it lazily inside the wrapper, so in a process where nothing has been
    torn down yet a by-name binding would be made after the patch and would pick
    the patched value up anyway -- which would make this case pass under either
    design and prove nothing. Importing first reproduces the normal state of a
    long-running worker, where the binding is already made.
    """
    from astinuslab.playground import filesystem_safety as fs
    from astinuslab.playground import orchestrator as po

    assert fs  # imported before the patch, see above

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root in (first_root, second_root):
        session_dir = root / "pg-1"
        session_dir.mkdir(parents=True)
        (session_dir / "web.qcow2").write_bytes(b"overlay")

    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=first_root))
    po._remove_playground_artifacts(first_root / "pg-1" / "web.qcow2")
    assert not (first_root / "pg-1").exists()
    assert (second_root / "pg-1" / "web.qcow2").is_file(), "the other root was untouched"

    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=second_root))
    po._remove_playground_artifacts(second_root / "pg-1" / "web.qcow2")
    assert not (second_root / "pg-1").exists()


def test_artifact_removal_clears_the_seed_iso_and_cloud_init_together(monkeypatch, tmp_path):
    """Every name `_spawn_artifact_paths` builds is removed, plus the seed directory."""
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "playground"
    session_dir = root / "pg-1"
    session_dir.mkdir(parents=True)
    disk = session_dir / "web.qcow2"
    for path in po._spawn_artifact_paths(disk):
        path.write_bytes(b"artifact")
    cloud_init = session_dir / "web-cloud-init"
    cloud_init.mkdir()
    (cloud_init / "user-data").write_text("seed", encoding="utf-8")
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root))

    po._remove_playground_artifacts(disk)

    assert not session_dir.exists()


def test_orchestrator_still_exposes_the_three_wrapped_entry_points(monkeypatch, tmp_path):
    """Each name other modules reach on `orchestrator` still delegates to here.

    `reconciliation_phases` imports the two artifact entry points from
    `orchestrator`, `acquisition` and `vm_lifecycle` call
    `po._create_directory_no_follow`, and the suite patches all three on
    `orchestrator` to intercept the calls those modules make. A wrapper that
    stopped delegating, or one that dropped an argument on the way through, would
    leave every one of those cases passing while exercising nothing, so each hop
    is asserted rather than assumed.
    """
    from astinuslab.playground import filesystem_safety as fs
    from astinuslab.playground import orchestrator as po

    reached: list[tuple] = []

    def present(disk_path):
        reached.append(("present", disk_path))
        return True

    def remove(disk_path):
        reached.append(("remove", disk_path))

    def create(root, *components, final_mode=None):
        reached.append(("create", root, components, final_mode))

    monkeypatch.setattr(fs, "_playground_artifacts_present", present)
    monkeypatch.setattr(fs, "_remove_playground_artifacts", remove)
    monkeypatch.setattr(fs, "_create_directory_no_follow", create)

    disk = tmp_path / "pg-1" / "web.qcow2"
    assert po._playground_artifacts_present(disk) is True
    po._remove_playground_artifacts(disk)
    po._create_directory_no_follow(tmp_path, "job-1", "vm-1", final_mode=0o700)

    assert reached == [
        ("present", disk),
        ("remove", disk),
        ("create", tmp_path, ("job-1", "vm-1"), 0o700),
    ]


# ---------------------------------------------------------------------------
# _cleanup_spawn_artifacts: the custody order this layer is called under
# ---------------------------------------------------------------------------


def test_spawn_cleanup_removes_artifacts_when_no_domain_can_exist(monkeypatch, tmp_path):
    """No lifecycle and no domain to destroy still has to clear the disk.

    `domain_may_exist=False` is the caller stating the spawn failed before
    libvirt was reached. There is no domain holding custody of the overlay, so
    withholding the removal would leave residue that reconciliation then has to
    find.
    """
    from astinuslab.playground import orchestrator as po

    root = tmp_path / "playground"
    session_dir = root / "pg-1"
    session_dir.mkdir(parents=True)
    disk = session_dir / "web.qcow2"
    disk.write_bytes(b"overlay")
    monkeypatch.setattr(po, "settings", SimpleNamespace(playground_disk_dir=root, libvirt_uri="qemu:///system"))

    error = po._cleanup_spawn_artifacts(
        lifecycle=None,
        domain_name="astinuslab-pg-pg-1-web",
        disk_path=disk,
        domain_may_exist=False,
    )

    assert error is None
    assert not disk.exists()


def test_spawn_cleanup_reports_a_refused_artifact_removal(monkeypatch, tmp_path):
    """A refusal from the no-follow layer becomes a teardown error, not a crash.

    This is the contract between the two layers: the primitives raise rather
    than delete the wrong inode, and the spawn path turns that into an outcome
    an operator can see on the row.
    """
    from astinuslab.playground import orchestrator as po
    from tests.python._support.helpers import autospec_setattr

    def refuse_replaced_artifact(_disk_path):
        raise RuntimeError("playground artifact was replaced during cleanup: web.qcow2")

    lifecycle = MagicMock()
    lifecycle.destroy.return_value = None
    autospec_setattr(monkeypatch, po, "_remove_playground_artifacts", refuse_replaced_artifact)
    monkeypatch.setattr(po, "settings", SimpleNamespace(libvirt_uri="qemu:///system"))

    error = po._cleanup_spawn_artifacts(
        lifecycle=lifecycle,
        domain_name="astinuslab-pg-pg-1-web",
        disk_path=tmp_path / "playground" / "pg-1" / "web.qcow2",
    )

    assert error is not None
    assert "artifact cleanup failed" in error
    assert "was replaced during cleanup" in error


# ---------------------------------------------------------------------------
# _open_or_create_component and _create_directory_no_follow
# ---------------------------------------------------------------------------


def test_capture_component_open_refuses_a_symlink_rather_than_creating_through_it(tmp_path):
    """Open-first must not turn a planted symlink into a created directory.

    `_open_or_create_component` treats FileNotFoundError as "create it". A
    symlinked component fails the O_NOFOLLOW open with ELOOP instead, which is
    not FileNotFoundError, so it travels up rather than being mkdir-ed over.
    """
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "captures"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "job-1").symlink_to(elsewhere, target_is_directory=True)

    root_fd = os.open(root, fs._CAPTURE_DIR_FLAGS)
    try:
        with pytest.raises(OSError) as excinfo:
            fs._open_or_create_component(root_fd, "job-1")
        assert excinfo.value.errno in (errno.ELOOP, errno.ENOTDIR)
    finally:
        os.close(root_fd)

    assert list(elsewhere.iterdir()) == []


def test_capture_component_open_tolerates_a_concurrent_creation(monkeypatch, tmp_path):
    """Two captures creating the same job directory must both succeed.

    The mkdir is wrapped in a FileExistsError suppression because the component
    can appear between the failed open and the mkdir. Losing that race is
    normal; failing the capture for it would not be.
    """
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "captures"
    root.mkdir()
    real_mkdir = os.mkdir

    def mkdir_after_another_worker_won(path, *args, dir_fd=None, **kwargs):
        real_mkdir(path, *args, dir_fd=dir_fd, **kwargs)
        raise FileExistsError(errno.EEXIST, "File exists")

    root_fd = os.open(root, fs._CAPTURE_DIR_FLAGS)
    monkeypatch.setattr(os, "mkdir", mkdir_after_another_worker_won)
    try:
        component_fd = fs._open_or_create_component(root_fd, "job-1")
        os.close(component_fd)
    finally:
        monkeypatch.undo()
        os.close(root_fd)

    assert (root / "job-1").is_dir()


def test_capture_directory_creation_leaves_no_descriptor_behind(tmp_path):
    """The descriptor walk closes each parent as it descends, and the last one too."""
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "captures"
    root.mkdir()

    before = _open_descriptor_count()
    fs._create_directory_no_follow(root, "job-1", "vm-1", final_mode=0o700)
    assert _open_descriptor_count() == before
    assert (root / "job-1" / "vm-1").stat().st_mode & 0o777 == 0o700


def test_spawn_artifact_paths_names_the_overlay_iso_and_seed(tmp_path):
    """The artifact name set is what every removal and probe iterates over."""
    from astinuslab.playground import orchestrator as po

    disk = tmp_path / "pg-1" / "web.qcow2"

    assert po._spawn_artifact_paths(disk) == (
        disk,
        disk.with_suffix(".iso"),
        disk.with_name("web-seed.iso"),
    )


def test_no_follow_helpers_refuse_traversal_components(tmp_path):
    """Separators and parent references never reach the descriptor walk."""
    from astinuslab.playground import filesystem_safety as fs

    root = tmp_path / "captures"
    root.mkdir()

    for component in ("..", "", ".", "a/b"):
        with pytest.raises(RuntimeError, match="unsafe capture path component"):
            fs._create_directory_no_follow(root, component)
    assert list(root.iterdir()) == []


def test_symlinked_artifact_probe_uses_lstat_semantics(tmp_path):
    """`stat.S_ISLNK` is reached through the no-follow stat, not a followed one."""
    from astinuslab.playground import filesystem_safety as fs

    parent = tmp_path / "pg-1"
    parent.mkdir()
    target = parent / "real.qcow2"
    target.write_bytes(b"real")
    link = parent / "web.qcow2"
    link.symlink_to(target)

    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        entry_stat = os.stat(link.name, dir_fd=parent_fd, follow_symlinks=False)
        assert stat.S_ISLNK(entry_stat.st_mode)
        with pytest.raises(RuntimeError, match="refusing to delete symlinked playground artifact"):
            fs._unlink_regular_no_follow(parent_fd, link.name)
    finally:
        os.close(parent_fd)

    assert target.read_bytes() == b"real"
