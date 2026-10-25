"""Capture and restore the ownership, mode and POSIX ACL of a managed tree.

The installer takes one of these snapshots before a migration mutates a tree it
does not own outright, and replays it when the migration has to be rolled back.
Both halves address every entry by descriptor -- never by path -- so a directory
or file swapped for a symlink between capture and restore fails closed instead
of relabelling whatever the link points at.

This lived in two `python3 - <<'PY'` heredocs inside ops/deploy/install.sh,
where Ruff, Mypy and coverage could not read it: a heredoc body is a shell
string. The snapshot format is the contract between the two halves, so they
belong in one module rather than two strings that have to agree by inspection.

Invoked as a subprocess by `snapshot_acl_state` and `restore_acl_snapshot`,
which pass the arguments they always passed after the subcommand:

    acl_snapshot.py capture ROOT DESTINATION
    acl_snapshot.py restore SNAPSHOT [TOLERATED_ROOT ...]
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from typing import Any

# The snapshot's own schema version. `restore` refuses anything else rather
# than guessing at a payload written by a different installer.
SNAPSHOT_VERSION = 1

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
PATH_FLAGS = os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

# Bounds on a single capture. A tree past either limit is not the managed tree
# this snapshot exists for, and walking it would turn a rollback into a hang.
MAX_DEPTH = 64
MAX_ENTRIES = 200_000

# SQLite creates and removes these six siblings while a database is open, so a
# stopped service leaves them absent. The same basename anywhere else is
# material and must still exist at restore time.
SIDECAR_NAMES = tuple(
    f"{database}.db{suffix}"
    for database in ("astinuslab", "celery_broker", "celery_results")
    for suffix in ("-wal", "-shm")
)


def _acl_text(fd: int) -> str:
    return subprocess.run(
        ["getfacl", "-cp", "--", f"/proc/self/fd/{fd}"],
        check=True,
        capture_output=True,
        text=True,
        pass_fds=(fd,),
    ).stdout


def capture(root: str, destination: str) -> None:
    """Write a snapshot of *root* to *destination*, which must not exist."""
    root, destination = os.path.abspath(root), os.path.abspath(destination)
    records: list[dict[str, Any]] = []
    seen = 0

    def visit(fd: int, relative: str, *, root_device: int, depth: int) -> None:
        nonlocal seen
        if depth > MAX_DEPTH:
            raise SystemExit("ACL snapshot tree exceeds depth limit")
        info = os.fstat(fd)
        kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else ""
        if not kind or info.st_dev != root_device or (kind == "file" and info.st_nlink != 1):
            raise SystemExit(f"ACL snapshot refuses unsafe entry: {relative}")
        records.append(
            {
                "path": relative,
                "kind": kind,
                "dev": info.st_dev,
                "ino": info.st_ino,
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mode": stat.S_IMODE(info.st_mode),
                "acl": _acl_text(fd),
            }
        )
        if kind != "directory":
            return
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            seen += 1
            if seen > MAX_ENTRIES:
                raise SystemExit("ACL snapshot tree exceeds entry limit")
            path_fd = os.open(name, PATH_FLAGS, dir_fd=fd)
            try:
                path_info = os.fstat(path_fd)
                if stat.S_ISLNK(path_info.st_mode):
                    # Symbolic links have no independently restorable access ACL,
                    # and following one would escape the descriptor-held tree.
                    continue
                child_flags = DIRECTORY_FLAGS if stat.S_ISDIR(path_info.st_mode) else FILE_FLAGS
                child_fd = os.open(name, child_flags, dir_fd=fd)
                child_info = os.fstat(child_fd)
                if (child_info.st_dev, child_info.st_ino) != (path_info.st_dev, path_info.st_ino):
                    os.close(child_fd)
                    raise SystemExit(f"ACL snapshot entry changed during capture: {relative}/{name}")
            except OSError as exc:
                raise SystemExit(f"ACL snapshot refuses unbound entry: {relative}/{name}") from exc
            finally:
                os.close(path_fd)
            try:
                child_relative = name if relative == "." else f"{relative}/{name}"
                visit(child_fd, child_relative, root_device=root_device, depth=depth + 1)
            finally:
                os.close(child_fd)

    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        root_info = os.fstat(root_fd)
        visit(root_fd, ".", root_device=root_info.st_dev, depth=0)
    finally:
        os.close(root_fd)

    snapshot_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        payload = json.dumps(
            {"version": SNAPSHOT_VERSION, "root": root, "records": records},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        view = memoryview(payload)
        while view:
            view = view[os.write(snapshot_fd, view) :]
        os.fsync(snapshot_fd)
    finally:
        os.close(snapshot_fd)


def _load_snapshot(snapshot: str) -> tuple[str, dict[str, dict[str, Any]], list[str]]:
    """Read and validate a snapshot file, returning its root and records."""
    snapshot_fd = os.open(snapshot, FILE_FLAGS)
    try:
        snapshot_info = os.fstat(snapshot_fd)
        if (
            not stat.S_ISREG(snapshot_info.st_mode)
            or snapshot_info.st_nlink != 1
            or snapshot_info.st_uid != os.geteuid()
            or stat.S_IMODE(snapshot_info.st_mode) & 0o077
        ):
            raise SystemExit("ACL snapshot has unsafe custody")
        with os.fdopen(os.dup(snapshot_fd), encoding="utf-8") as handle:
            payload = json.load(handle)
    finally:
        os.close(snapshot_fd)

    if not isinstance(payload, dict) or payload.get("version") != SNAPSHOT_VERSION:
        raise SystemExit("ACL snapshot has an unsupported format")
    root = payload.get("root")
    raw_records = payload.get("records")
    if not isinstance(root, str) or root != os.path.abspath(root) or not isinstance(raw_records, list):
        raise SystemExit("ACL snapshot is malformed")

    records: dict[str, dict[str, Any]] = {}
    ordered_paths: list[str] = []
    for record in raw_records:
        if not isinstance(record, dict):
            raise SystemExit("ACL snapshot contains a malformed record")
        relative = record.get("path")
        kind = record.get("kind")
        if (
            not isinstance(relative, str)
            or relative != os.path.normpath(relative)
            or (relative != "." and (os.path.isabs(relative) or ".." in relative.split(os.sep)))
            or kind not in {"directory", "file"}
            or relative in records
        ):
            raise SystemExit("ACL snapshot contains an unsafe path")
        for field in ("dev", "ino", "uid", "gid", "mode"):
            if not isinstance(record.get(field), int) or record[field] < 0:
                raise SystemExit("ACL snapshot contains invalid metadata")
        if not isinstance(record.get("acl"), str):
            raise SystemExit("ACL snapshot contains invalid ACL data")
        records[relative] = record
        ordered_paths.append(relative)
    if not ordered_paths or ordered_paths[0] != "." or records["."]["kind"] != "directory":
        raise SystemExit("ACL snapshot has no directory root")
    return root, records, ordered_paths


def restore(snapshot: str, allowed: list[str]) -> None:
    """Replay *snapshot*, tolerating service-managed absences under *allowed*."""
    snapshot = os.path.abspath(snapshot)
    allowed_paths = [os.path.abspath(value) for value in allowed if value]
    root, records, ordered_paths = _load_snapshot(snapshot)

    def verify_info(info: os.stat_result, record: dict[str, Any], relative: str) -> None:
        expected_type = stat.S_ISDIR if record["kind"] == "directory" else stat.S_ISREG
        if (
            not expected_type(info.st_mode)
            or (record["kind"] == "file" and info.st_nlink != 1)
            or (info.st_dev, info.st_ino) != (record["dev"], record["ino"])
        ):
            raise SystemExit(f"ACL snapshot path changed identity: {os.path.join(root, relative)}")

    def open_record(relative: str) -> int:
        if relative == ".":
            descriptor = os.dup(root_fd)
            verify_info(os.fstat(descriptor), records[relative], relative)
            return descriptor
        descriptor = os.dup(root_fd)
        traversed: list[str] = []
        try:
            components = relative.split(os.sep)
            for index, component in enumerate(components):
                traversed.append(component)
                current = os.path.join(*traversed)
                expected = records.get(current)
                if expected is None:
                    raise SystemExit(f"ACL snapshot lacks parent custody: {os.path.join(root, current)}")
                flags = (
                    DIRECTORY_FLAGS if index < len(components) - 1 or expected["kind"] == "directory" else FILE_FLAGS
                )
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                verify_info(os.fstat(descriptor), expected, current)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        verify_info(os.fstat(root_fd), records["."], ".")

        # Two kinds of recorded path may legitimately be gone by the time a
        # rollback runs, both service-managed and reconstructible, both bounded
        # to an exact location. Everything else is material and must still exist.
        #
        # 1. The SQLite sidecars in SIDECAR_NAMES, which a stopped service
        #    leaves absent. The same basename anywhere else is material.
        # 2. Playground disk scratch: the split services start before the
        #    migration commits, and their startup recovery purges staging for
        #    sessions that no longer exist. Only descendants are tolerated, and
        #    only while the scratch tree itself survives; captures live
        #    elsewhere and stay material.
        allowed_relatives: list[str] = []
        for allowed_path in allowed_paths:
            try:
                if os.path.commonpath((root, allowed_path)) != root:
                    continue
            except ValueError:
                continue
            relative = os.path.relpath(allowed_path, root)
            if relative not in records or records[relative]["kind"] != "directory":
                continue
            try:
                descriptor = open_record(relative)
            except (FileNotFoundError, NotADirectoryError):
                continue
            else:
                os.close(descriptor)
                allowed_relatives.append(relative)

        transient_trees: list[str] = []
        for allowed_relative in allowed_relatives:
            tree = os.path.normpath(os.path.join(allowed_relative, "playground", "disks"))
            if tree not in records or records[tree]["kind"] != "directory":
                continue
            try:
                descriptor = open_record(tree)
            except (FileNotFoundError, NotADirectoryError):
                continue
            else:
                os.close(descriptor)
                transient_trees.append(tree)

        def tolerated_missing(relative: str) -> bool:
            parent, name = os.path.split(relative)
            parent = parent or "."
            if parent in allowed_relatives and name in SIDECAR_NAMES:
                return True
            return any(relative.startswith(tree + os.sep) for tree in transient_trees)

        # Preflight the complete snapshot before changing any metadata. A
        # swapped parent, symlink, or material disappearance therefore fails
        # closed without a partial restore.
        missing = set()
        for relative in ordered_paths:
            try:
                descriptor = open_record(relative)
            except (FileNotFoundError, NotADirectoryError):
                if tolerated_missing(relative):
                    missing.add(relative)
                    continue
                raise SystemExit(f"ACL snapshot material path disappeared: {os.path.join(root, relative)}") from None
            except OSError as exc:
                raise SystemExit(f"ACL snapshot refuses unsafe path: {os.path.join(root, relative)}") from exc
            else:
                os.close(descriptor)

        for relative in ordered_paths:
            if relative in missing:
                continue
            try:
                descriptor = open_record(relative)
            except OSError as exc:
                raise SystemExit(f"ACL snapshot path changed during restore: {os.path.join(root, relative)}") from exc
            try:
                record = records[relative]
                os.fchown(descriptor, record["uid"], record["gid"])
                os.fchmod(descriptor, record["mode"])
                # --set-file replaces access entries but leaves a newly introduced
                # default ACL when the snapshot contains no defaults. Remove that
                # inheritance first, then restore the complete captured ACL.
                if record["kind"] == "directory":
                    subprocess.run(
                        ["setfacl", "--remove-default", "--", f"/proc/self/fd/{descriptor}"],
                        check=True,
                        pass_fds=(descriptor,),
                    )
                subprocess.run(
                    ["setfacl", "--set-file=-", "--", f"/proc/self/fd/{descriptor}"],
                    input=record["acl"],
                    text=True,
                    check=True,
                    pass_fds=(descriptor,),
                )
                verify_info(os.fstat(descriptor), record, relative)
                if stat.S_IMODE(os.fstat(descriptor).st_mode) != record["mode"]:
                    raise SystemExit(f"ACL restore changed mode unexpectedly: {os.path.join(root, relative)}")
            finally:
                os.close(descriptor)
    finally:
        os.close(root_fd)


def main(argv: list[str]) -> None:
    if len(argv) == 3 and argv[0] == "capture":
        capture(argv[1], argv[2])
    elif len(argv) >= 2 and argv[0] == "restore":
        restore(argv[1], argv[2:])
    else:
        raise SystemExit("usage: acl_snapshot.py capture ROOT DESTINATION | restore SNAPSHOT [ALLOWED ...]")


if __name__ == "__main__":
    main(sys.argv[1:])
