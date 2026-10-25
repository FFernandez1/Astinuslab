"""Playground no-follow filesystem primitives: the symlink-safety layer.

Split out of `orchestrator.py` as the last of its six domains. Every other
playground domain calls through here before it creates or deletes anything under
the storage roots, which is why it moved last: a refusal that is only reachable
from a caller in another module is a refusal nobody can test in isolation, and
this group was the least covered code in `orchestrator.py` when the split began.

What these functions defend against is a path that is not what the database said
it was. `Path.mkdir(parents=True)` follows symlinks and `os.unlink` acts on a
name, so a link or a rename planted in a session directory is enough to make a
teardown delete an operator's file or a capture land somewhere other than the
path recorded for the evidence. Every descent here opens one component at a time
relative to a descriptor for its parent, with O_NOFOLLOW, and compares the opened
inode against the one that was probed. Where the name is used again after the
descriptor exists -- the unlink, the rmdir, each child of a seed directory -- it
is probed again, because holding the right descriptor does not prove the name
still refers to it.

The refusals are deliberate dead ends. None of them tries to recover: an
unexpected inode means the assumption the caller was operating under is already
false, so the operation stops and the caller reports it rather than guessing
which of the two paths was the real one.

`settings` and `_spawn_artifact_paths` are reached through the orchestrator
module object (`po.<name>`) rather than imported by name. That is not style.
`orchestrator.settings` is what the suite patches to point a removal at a
temporary root, and a `from .orchestrator import settings` here would bind the
real configuration at import time, so a patched value would silently not apply
and the removal would run against the real storage root. Attribute access
resolves at call time and keeps that working. `_spawn_artifact_paths` stays in
`orchestrator.py` for the reason three earlier slices left a helper behind: it
encodes what a spawn writes, which is VM-domain knowledge, not filesystem
safety. This module only knows how to remove such a set without being tricked.

`_playground_artifacts_present`, `_remove_playground_artifacts` and
`_create_directory_no_follow` are also reachable as `orchestrator.<name>`, as
delegating wrappers. `reconciliation_phases` imports the first two from
`orchestrator` and `acquisition` and `vm_lifecycle` call the third as
`po.<name>`, and the suite patches all three on `orchestrator` to intercept the
calls those modules make.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from pathlib import Path

from . import orchestrator as po

_CAPTURE_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_no_follow(path: Path) -> int:
    absolute_path = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = os.open("/", flags)
    try:
        for component in absolute_path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _open_relative_directory_no_follow(root_fd: int, parts: tuple[str, ...]) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = os.dup(root_fd)
    try:
        root_device = os.fstat(root_fd).st_dev
        for component in parts:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            if os.fstat(next_fd).st_dev != root_device:
                os.close(next_fd)
                raise RuntimeError(f"refusing playground cleanup across a mount point: {component}")
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _open_or_create_component(parent_descriptor: int, name: str) -> int:
    """Open `name` below `parent_descriptor`, creating it only when it is missing.

    Creating unconditionally raises EROFS on a read-only mount even when every
    directory already exists, failing a capture that needed no creation at all.
    A symlinked component still raises here rather than being followed, because
    the open carries O_NOFOLLOW and its error is not FileNotFoundError.
    """
    try:
        return os.open(name, _CAPTURE_DIR_FLAGS, dir_fd=parent_descriptor)
    except FileNotFoundError:
        pass
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, dir_fd=parent_descriptor)
    return os.open(name, _CAPTURE_DIR_FLAGS, dir_fd=parent_descriptor)


def _create_directory_no_follow(root: Path, *components: str, final_mode: int | None = None) -> None:
    """Create `components` beneath `root` without ever traversing a symlink.

    `Path.mkdir(parents=True)` follows symlinks, so a link planted below the
    capture root is written through: the capture then lands somewhere other than
    the path recorded for the evidence. Each component here is created relative
    to a descriptor for its parent opened with O_NOFOLLOW, so a symlinked
    component is refused instead of followed. Existing directories are
    tolerated, matching the previous exist_ok=True behaviour.

    `root` is opened no-follow too. Callers resolve the configured capture
    directory first, so relocating it onto a larger disk via symlink still works:
    the link is dereferenced before it gets here, and what arrives is the real
    directory. A `root` that is still a symlink at this point was not the
    configured path and is refused.
    """
    if any(component in ("", ".", "..") or "/" in component for component in components):
        raise RuntimeError(f"unsafe capture path component in {components!r}")

    descriptor = os.open(root, _CAPTURE_DIR_FLAGS)
    try:
        for component in components:
            child_descriptor = _open_or_create_component(descriptor, component)
            os.close(descriptor)
            descriptor = child_descriptor
        if final_mode is not None:
            os.fchmod(descriptor, final_mode)
    finally:
        os.close(descriptor)


def _unlink_regular_no_follow(parent_fd: int, name: str) -> None:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(entry_stat.st_mode):
        raise RuntimeError(f"refusing to delete symlinked playground artifact: {name}")
    if not stat.S_ISREG(entry_stat.st_mode):
        raise RuntimeError(f"refusing to delete non-file playground artifact: {name}")

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    artifact_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened_stat = os.fstat(artifact_fd)
        expected = (entry_stat.st_dev, entry_stat.st_ino)
        opened = (opened_stat.st_dev, opened_stat.st_ino)
        if not stat.S_ISREG(opened_stat.st_mode) or opened != expected:
            raise RuntimeError(f"playground artifact was replaced during cleanup: {name}")
        current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current_stat.st_dev, current_stat.st_ino) != opened:
            raise RuntimeError(f"playground artifact was replaced during cleanup: {name}")
        os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(artifact_fd)


def _remove_cloud_init_directory_no_follow(parent_fd: int, name: str) -> None:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(entry_stat.st_mode):
        raise RuntimeError(f"refusing to delete symlinked playground artifact: {name}")
    if not stat.S_ISDIR(entry_stat.st_mode):
        raise RuntimeError(f"refusing to delete non-directory playground artifact: {name}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    cloud_init_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened_stat = os.fstat(cloud_init_fd)
        if (opened_stat.st_dev, opened_stat.st_ino) != (entry_stat.st_dev, entry_stat.st_ino):
            raise RuntimeError(f"playground artifact was replaced during cleanup: {name}")
        for entry in os.scandir(cloud_init_fd):
            child_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISREG(child_stat.st_mode):
                raise RuntimeError(f"refusing unexpected cloud-init artifact during cleanup: {name}/{entry.name}")
            current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (current_stat.st_dev, current_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
                raise RuntimeError(f"playground artifact was replaced during cleanup: {name}")
            _unlink_regular_no_follow(cloud_init_fd, entry.name)
    finally:
        os.close(cloud_init_fd)
    current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (current_stat.st_dev, current_stat.st_ino) != (entry_stat.st_dev, entry_stat.st_ino):
        raise RuntimeError(f"playground artifact was replaced during cleanup: {name}")
    os.rmdir(name, dir_fd=parent_fd)


def _remove_empty_relative_directory_no_follow(root_fd: int, parts: tuple[str, ...]) -> None:
    """Remove a contained staging directory only when the opened inode is still empty."""
    if not parts:
        return
    ancestor_fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=ancestor_fd,
            )
            os.close(ancestor_fd)
            ancestor_fd = next_fd
        name = parts[-1]
        before = os.stat(name, dir_fd=ancestor_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise RuntimeError(f"refusing to remove non-directory playground staging path: {name}")
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=ancestor_fd,
        )
        try:
            opened = os.fstat(directory_fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError(f"playground staging directory was replaced during cleanup: {name}")
            with os.scandir(directory_fd) as entries:
                if next(entries, None) is not None:
                    return
        finally:
            os.close(directory_fd)
        current = os.stat(name, dir_fd=ancestor_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError(f"playground staging directory was replaced during cleanup: {name}")
        try:
            os.rmdir(name, dir_fd=ancestor_fd)
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise
    except FileNotFoundError:
        # Another cleanup already removed this contained staging directory.
        return
    finally:
        os.close(ancestor_fd)


def _playground_artifacts_present(disk_path: Path) -> bool:
    """Descriptor-safe residue probe used by legacy reconciliation."""
    playground_root = Path(po.settings.playground_disk_dir).resolve(strict=True)
    absolute_disk_path = Path(os.path.abspath(disk_path))
    try:
        relative_disk_path = absolute_disk_path.relative_to(playground_root)
    except ValueError as exc:
        raise RuntimeError(f"refusing to inspect artifact outside playground dir: {disk_path}") from exc
    if relative_disk_path.parent == Path("."):
        raise RuntimeError(f"refusing to inspect playground artifact at storage root: {disk_path}")

    root_fd = _open_directory_no_follow(playground_root)
    try:
        try:
            parent_fd = _open_relative_directory_no_follow(root_fd, relative_disk_path.parent.parts)
        except FileNotFoundError:
            return False
        try:
            names = [path.name for path in po._spawn_artifact_paths(absolute_disk_path)]
            names.append(f"{absolute_disk_path.stem}-cloud-init")
            for name in names:
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                return True
            return False
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)


def _remove_playground_artifacts(disk_path: Path) -> None:
    # Spawn records paths below the configured root's physical destination so
    # a supported root symlink (for example to a larger disk) must be resolved
    # the same way here. Descendants are still opened with O_NOFOLLOW below.
    playground_root = Path(po.settings.playground_disk_dir).resolve(strict=True)
    absolute_disk_path = Path(os.path.abspath(disk_path))
    try:
        relative_disk_path = absolute_disk_path.relative_to(playground_root)
    except ValueError as exc:
        raise RuntimeError(f"refusing to delete artifact outside playground dir: {disk_path}") from exc
    if relative_disk_path.parent == Path("."):
        raise RuntimeError(f"refusing to delete playground artifact at storage root: {disk_path}")

    root_fd = _open_directory_no_follow(playground_root)
    try:
        try:
            parent_fd = _open_relative_directory_no_follow(root_fd, relative_disk_path.parent.parts)
        except FileNotFoundError:
            return
        try:
            artifact_names = dict.fromkeys(path.name for path in po._spawn_artifact_paths(absolute_disk_path))
            for artifact_name in artifact_names:
                _unlink_regular_no_follow(parent_fd, artifact_name)
            _remove_cloud_init_directory_no_follow(
                parent_fd,
                f"{absolute_disk_path.stem}-cloud-init",
            )
        finally:
            os.close(parent_fd)
        _remove_empty_relative_directory_no_follow(root_fd, relative_disk_path.parent.parts)
    finally:
        os.close(root_fd)
