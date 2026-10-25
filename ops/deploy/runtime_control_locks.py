"""Provision only forensic intent locks while the installer has stopped writers.

API and lifecycle runner share these coordination files. Analysis has no access.
The enclosing tmp directory and all evidence retain their existing custody.
Installer ACL snapshots restore this subtree if a later migration step fails.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path


def provision(storage: Path, *, api_uid: int, runner_uid: int, legacy_uid: int) -> None:
    if os.geteuid() != 0:
        raise OSError("forensic intent-lock migration requires the installer identity")
    if not storage.is_absolute() or storage.resolve() != storage or ".." in storage.parts:
        raise OSError("forensic intent-lock storage root must be canonical")
    if api_uid == runner_uid or 0 in (api_uid, runner_uid):
        raise OSError("API and runner must have distinct unprivileged identities")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    allowed = {0, legacy_uid, api_uid, runner_uid}
    root_fd = os.open(storage, flags | os.O_DIRECTORY)
    try:
        tmp_fd = os.open("tmp", flags | os.O_DIRECTORY, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        directory_fd = os.open("forensic-capture-locks", flags | os.O_DIRECTORY, dir_fd=tmp_fd)
    finally:
        os.close(tmp_fd)
    try:
        directory = os.fstat(directory_fd)
        if directory.st_uid not in allowed:
            raise OSError("forensic intent-lock directory has foreign custody")
        records: list[tuple[str, int, int]] = []
        for name in os.listdir(directory_fd):
            if len(records) >= 200_000 or not name.endswith(".lock"):
                raise OSError("forensic intent-lock directory has unexpected entries")
            descriptor = os.open(name, flags, dir_fd=directory_fd)
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid not in allowed
                    or info.st_dev != directory.st_dev
                ):
                    raise OSError("forensic intent-lock entry has unsafe or foreign custody")
                records.append((name, info.st_dev, info.st_ino))
            finally:
                os.close(descriptor)

        def apply(descriptor: int, *, is_directory: bool) -> None:
            permissions = "rwx" if is_directory else "rw-"
            acl = [
                f"user::{permissions}",
                f"user:{api_uid}:{permissions}",
                f"user:{runner_uid}:{permissions}",
                "group::---",
                f"mask::{permissions}",
                "other::---",
            ]
            if is_directory:
                acl += ["default:" + entry for entry in tuple(acl)]
            os.fchown(descriptor, runner_uid, -1)
            target = f"/proc/self/fd/{descriptor}"
            subprocess.run(
                ["setfacl", "--set", ",".join(acl), "--", target],
                check=True,
                pass_fds=(descriptor,),
                capture_output=True,
            )
            actual = subprocess.check_output(
                ["getfacl", "-cpn", "--", target],
                text=True,
                pass_fds=(descriptor,),
            )
            if set(actual.splitlines()) - {""} != set(acl):
                raise OSError("forensic intent-lock ACL verification failed; restore installer snapshot")

        # Every candidate was audited before changing any ACL or ownership.
        for name, device, inode in records:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
            try:
                current = os.fstat(descriptor)
                if (current.st_dev, current.st_ino) != (device, inode) or current.st_nlink != 1:
                    raise OSError("forensic intent locks changed during quiesced migration")
                apply(descriptor, is_directory=False)
            finally:
                os.close(descriptor)
        apply(directory_fd, is_directory=True)
    finally:
        os.close(directory_fd)


def rollback_new_locks(storage: Path, snapshots: list[Path], *, allowed_owners: set[int]) -> None:
    """Remove only empty post-snapshot locks, after all services are quiesced.

    Preserve every snapshotted inode for the installer's existing ACL restore.
    Reject nonempty/foreign/symlinked newcomers before removing any file. Locks
    contain no evidence and can be recreated by the restored service identity.
    """
    if os.geteuid() != 0 or not storage.is_absolute() or storage.resolve() != storage:
        raise OSError("unsafe forensic lock rollback boundary")
    lock_path = storage / "tmp" / "forensic-capture-locks"
    recorded: dict[str, tuple[int, int]] = {}
    for snapshot in snapshots:
        if not snapshot.exists():
            continue
        data = json.loads(snapshot.read_text())
        if data.get("version") != 1:
            raise OSError("unsupported forensic lock rollback snapshot")
        snapshot_root = Path(data["root"])
        for row in data["records"]:
            path = snapshot_root / row["path"]
            if path.parent == lock_path:
                recorded[path.name] = (row["dev"], row["ino"])
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    root_fd = os.open(storage, flags | os.O_DIRECTORY)
    try:
        try:
            tmp_fd = os.open("tmp", flags | os.O_DIRECTORY, dir_fd=root_fd)
        except FileNotFoundError:
            return
    finally:
        os.close(root_fd)
    try:
        try:
            directory_fd = os.open("forensic-capture-locks", flags | os.O_DIRECTORY, dir_fd=tmp_fd)
        except FileNotFoundError:
            return
    finally:
        os.close(tmp_fd)
    try:
        directory = os.fstat(directory_fd)
        if directory.st_uid not in allowed_owners:
            raise OSError("forensic lock rollback directory has foreign custody")
        remove = []
        for name in os.listdir(directory_fd):
            descriptor = os.open(name, flags, dir_fd=directory_fd)
            try:
                info = os.fstat(descriptor)
                identity = (info.st_dev, info.st_ino)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != directory.st_dev:
                    raise OSError("unsafe forensic lock rollback entry")
                if name in recorded:
                    if recorded[name] != identity:
                        raise OSError("snapshotted forensic lock identity changed")
                    continue
                if not name.endswith(".lock") or info.st_size != 0 or info.st_uid not in allowed_owners:
                    raise OSError("preserving unrecognized post-snapshot forensic lock data")
                remove.append((name, identity))
            finally:
                os.close(descriptor)
        for name, identity in remove:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != identity or info.st_nlink != 1 or info.st_size != 0:
                raise OSError("forensic locks changed during quiesced rollback")
            os.unlink(name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "--rollback":
            _, storage, api, runner, legacy, *snapshots = sys.argv[1:]
            rollback_new_locks(
                Path(storage),
                [Path(path) for path in snapshots],
                allowed_owners={0, *(pwd.getpwnam(name).pw_uid for name in (api, runner, legacy))},
            )
        else:
            storage, api, runner, legacy = sys.argv[1:]
            provision(
                Path(storage),
                api_uid=pwd.getpwnam(api).pw_uid,
                runner_uid=pwd.getpwnam(runner).pw_uid,
                legacy_uid=pwd.getpwnam(legacy).pw_uid,
            )
    except (OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        raise SystemExit(f"Cannot reconcile forensic intent locks between API and runner: {exc}") from exc
