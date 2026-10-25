#!/usr/bin/env python3
"""Quiesced installer-only provisioning and conservative rollback of role logs."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
from pathlib import Path

FILES = {"api": "api.log", "runner": "runner.log", "analysis": "analysis-worker.log"}
MARKER = ".astinuslab-service-logs.json"
SCHEMA = "astinuslab-service-logs/v1"
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("log custody paths must be absolute and canonical")
    fd = os.open("/", FLAGS | os.O_DIRECTORY)
    try:
        for name in path.parts[1:]:
            child = os.open(name, FLAGS | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _protected_parent(path: Path) -> None:
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
        ):
            raise OSError("log recovery metadata requires protected root-owned ancestors")


def _write_record(path: Path, value: dict, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", closefd=False) as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    parent = _directory(path.parent)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _read_record(path: Path, *, mode: int | None = None) -> dict:
    fd = os.open(path, FLAGS)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
        ):
            raise OSError("unsafe log custody metadata")
        with os.fdopen(fd, "r", closefd=False) as stream:
            result = json.load(stream)
        if not isinstance(result, dict):
            raise OSError("invalid log custody metadata")
        return result
    finally:
        os.close(fd)


def _tree(fd: int, owners: set[int], prefix: str = "", records: dict | None = None) -> dict:
    records = {} if records is None else records
    info = os.fstat(fd)
    if info.st_uid not in owners or len(records) >= 50000 or prefix.count("/") > 32:
        raise OSError("log tree has unexpected custody or exceeds safety bounds")
    directory = stat.S_ISDIR(info.st_mode)
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise OSError("log tree contains a link or special file")
    records[prefix] = [info.st_dev, info.st_ino]
    if directory:
        for name in sorted(os.listdir(fd)):
            child = os.open(name, FLAGS, dir_fd=fd)
            try:
                if os.fstat(child).st_dev != info.st_dev:
                    raise OSError("log tree crosses a filesystem")
                _tree(child, owners, f"{prefix}/{name}".lstrip("/"), records)
            finally:
                os.close(child)
    return records


def _at(fd: int, name: str) -> int:
    current = os.dup(fd)
    try:
        for component in name.split("/") if name else []:
            child = os.open(component, FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _acl(fd: int, owner: int, entries: set[str], mode: int) -> None:
    os.fchown(fd, owner, 0)
    os.fchmod(fd, mode)
    target = f"/proc/self/fd/{fd}"
    subprocess.run(["setfacl", "-b", "-k", "--", target], pass_fds=(fd,), check=True, capture_output=True)
    subprocess.run(
        ["setfacl", "--set", ",".join(sorted(entries)), "--", target], pass_fds=(fd,), check=True, capture_output=True
    )
    actual = subprocess.check_output(["getfacl", "-cnp", "--", target], pass_fds=(fd,), text=True)
    if {line for line in actual.splitlines() if line and not line.startswith("#")} != entries:
        raise OSError("log ACL differs from the role policy")


def _qemu_uids() -> set[int]:
    result = set()
    for name in ("libvirt-qemu", "qemu"):
        try:
            uid = pwd.getpwnam(name).pw_uid
        except KeyError:
            continue
        if uid > 0:
            result.add(uid)
    return result


def _qemu_grants(fd: int, identities: set[int], *, anchor: bool = False) -> set[str]:
    """Retain effective existing serial grants, never invent a qemu grant."""
    output = subprocess.check_output(["getfacl", "-cnp", "--", f"/proc/self/fd/{fd}"], pass_fds=(fd,), text=True)
    entries = {}
    for line in output.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            key, value = line.rsplit(":", 1)
            entries[key] = value
    result = set()
    owner = os.fstat(fd).st_uid
    for uid in identities:
        for prefix in ("", "default:") if not anchor else ("",):
            permissions = entries.get(f"{prefix}user:{uid}")
            if not prefix and owner == uid:
                permissions = entries.get("user:")
            elif permissions is not None:
                mask = entries.get(f"{prefix}mask:", "rwx")
                permissions = "".join(p if p == m else "-" for p, m in zip(permissions, mask, strict=True))
            if permissions is not None:
                if anchor:
                    permissions = "--x" if "x" in permissions else "---"
                if permissions != "---":
                    result.add(f"{prefix}user:{uid}:{permissions}")
    return result


def _role_acl(api: int, *, directory: bool) -> set[str]:
    base = {
        "user::rwx" if directory else "user::rw-",
        "group::---",
        "other::---",
        "mask::rwx" if directory else "mask::rw-",
        f"user:{api}:r-x" if directory else f"user:{api}:rw-",
    }
    if directory:
        # Default rwx allows file creation mode to remove execute, retaining API rw.
        base |= {
            "default:user::rwx",
            f"default:user:{api}:rwx",
            "default:group::---",
            "default:mask::rwx",
            "default:other::---",
        }
    return base


def _validate(roles: dict[str, int]) -> None:
    if (
        os.geteuid() != 0
        or set(roles) != set(FILES)
        or len(set(roles.values())) != 3
        or any(uid <= 0 for uid in roles.values())
    ):
        raise OSError("log provisioning requires root and three distinct unprivileged roles")


def _log_family(name: str, filename: str) -> bool:
    # Configurable rotation counts need not equal the default five. Bound both
    # suffix parsing and total tree entries; accept only canonical positive IDs.
    return name == filename or re.fullmatch(re.escape(filename) + r"\.[1-9][0-9]{0,17}", name) is not None


def _legacy_kind(name: str) -> str | None:
    if name == "docs.log" or any(_log_family(name, filename) for filename in FILES.values()):
        return "history"
    if name == "serial" or re.fullmatch(r"serial/astinuslab-[A-Za-z0-9_-]+-serial\.log", name):
        return "diagnostic"
    if re.fullmatch(r"[A-Za-z0-9_-]+-(?:qemu-img\.log|disk-dump(?:\.raw)?\.sha256)", name):
        return "diagnostic"
    return None


def _audit_roles(fd: int, roles: dict[str, int], original: dict) -> None:
    for name in original:
        role = name.split("/")[0]
        kind = _legacy_kind(name)
        if role not in roles and kind is None:
            continue
        child = _at(fd, name)
        try:
            info = os.fstat(child)
            if kind is not None:
                if stat.S_ISDIR(info.st_mode) != (name == "serial"):
                    raise OSError("legacy log has unexpected type")
                continue
            permitted = name == role or _log_family(name.removeprefix(role + "/"), FILES[role])
            expected_type = stat.S_ISDIR(info.st_mode) if name == role else stat.S_ISREG(info.st_mode)
            if not permitted or info.st_uid != roles[role] or not expected_type:
                raise OSError("existing role logs have unexpected entries or custody")
        finally:
            os.close(child)


def provision(root: Path, roles: dict[str, int], legacy: int, record: Path) -> None:
    _validate(roles)
    _protected_parent(record)
    root_fd = _directory(root)
    try:
        try:
            os.mkdir("logs", 0o700, dir_fd=root_fd)
            created_anchor = True
        except FileExistsError:
            created_anchor = False
        os.fsync(root_fd)
        fd = os.open("logs", FLAGS | os.O_DIRECTORY, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        qemu = _qemu_uids() - set(roles.values())
        original = _tree(fd, {0, legacy, *roles.values(), *qemu})
        for name in original:
            current = _at(fd, name)
            try:
                if os.fstat(current).st_uid in qemu and not (
                    (name == "serial" or name.startswith("serial/")) and _legacy_kind(name) == "diagnostic"
                ):
                    raise OSError("qemu custody is only permitted for recognized serial logs")
            finally:
                os.close(current)
        marker = root / "logs" / MARKER
        installed = MARKER in original
        if installed:
            layout = _read_record(marker, mode=0o644)
            if layout != {"schema": SCHEMA, "roles": roles} or any(
                type(uid) is not int for uid in layout.get("roles", {}).values()
            ):
                raise OSError("existing log layout belongs to different roles")
        if not installed and any(role in original for role in FILES):
            raise OSError("unmarked log role directories require operator reconciliation")
        _audit_roles(fd, roles, original)
        _write_record(
            record,
            {
                "schema": SCHEMA,
                "root": str(root),
                "roles": roles,
                "installed": installed,
                "created_anchor": created_anchor,
                "original": original,
            },
        )
        # Audit the whole tree before normalizing any existing inode.
        for name, identity in original.items():
            current = _at(fd, name)
            try:
                info = os.fstat(current)
                if [info.st_dev, info.st_ino] != identity:
                    raise OSError("log tree changed during provisioning")
                if name == MARKER or name.split("/")[0] in FILES:
                    continue
                kind = _legacy_kind(name)
                if kind is not None:
                    directory = stat.S_ISDIR(info.st_mode)
                    if kind == "history":
                        _acl(
                            current,
                            info.st_uid,
                            {"user::rw-", "group::---", "other::---", "mask::r--", f"user:{roles['api']}:r--"},
                            0o640,
                        )
                    else:
                        preserved = (
                            _qemu_grants(current, qemu) if name == "serial" or name.startswith("serial/") else set()
                        )
                        _acl(
                            current,
                            roles["runner"],
                            _role_acl(roles["api"], directory=directory) | preserved,
                            0o750 if directory else 0o640,
                        )
            finally:
                os.close(current)
        anchor_acl = {
            "user::rwx",
            "group::---",
            "other::---",
            "mask::rwx",
            f"user:{roles['api']}:r-x",
            f"user:{roles['runner']}:rwx",
            f"user:{roles['analysis']}:--x",
        }
        _acl(fd, 0, anchor_acl | _qemu_grants(fd, qemu, anchor=True), 0o1700)
        for role, uid in roles.items():
            try:
                os.mkdir(role, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            role_fd = os.open(role, FLAGS | os.O_DIRECTORY, dir_fd=fd)
            try:
                _acl(role_fd, uid, _role_acl(roles["api"], directory=True), 0o750)
                for name in os.listdir(role_fd):
                    if not _log_family(name, FILES[role]):
                        raise OSError("unexpected role log entry")
                    child = os.open(name, FLAGS, dir_fd=role_fd)
                    try:
                        info = os.fstat(child)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid:
                            raise OSError("role log has unexpected custody")
                        _acl(child, uid, _role_acl(roles["api"], directory=False), 0o640)
                    finally:
                        os.close(child)
            finally:
                os.close(role_fd)
        if not installed:
            _write_record(marker, {"schema": SCHEMA, "roles": roles}, 0o644)
        os.fsync(fd)
    finally:
        os.close(fd)


def _version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _copy_retained_file(source: int, destination: int, name: str) -> tuple[int, int, int, int, int]:
    source_fd = os.open(name, FLAGS, dir_fd=source)
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OSError("log retention source is not a single-link regular file")
        target_fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=destination)
        try:
            os.fchmod(target_fd, 0o600)
            digest = hashlib.sha256()
            with (
                os.fdopen(source_fd, "rb", closefd=False) as reader,
                os.fdopen(target_fd, "w+b", closefd=False) as writer,
            ):
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(target_fd)
                writer.seek(0)
                retained_digest = hashlib.sha256()
                for chunk in iter(lambda: writer.read(1024 * 1024), b""):
                    retained_digest.update(chunk)
            if digest.digest() != retained_digest.digest() or _version(before) != _version(os.fstat(source_fd)):
                raise OSError("log retention copy did not verify; original bytes preserved")
            return _version(before)
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)


def _retain_role(source: int, destination: int, role: str) -> None:
    try:
        os.rename(role, role, src_dir_fd=source, dst_dir_fd=destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    source_fd = os.open(role, FLAGS | os.O_DIRECTORY, dir_fd=source)
    try:
        # The stopped writer cannot replace entries after this custody change.
        # On failure keep the complete source plus private partial copy; the
        # caller must retain identities/snapshot and require operator recovery.
        _acl(source_fd, 0, {"user::rwx", "group::---", "other::---"}, 0o700)
        os.mkdir(role, 0o700, dir_fd=destination)
        target_fd = os.open(role, FLAGS | os.O_DIRECTORY, dir_fd=destination)
        try:
            copied = {}
            for name in os.listdir(source_fd):
                if not _log_family(name, FILES[role]):
                    raise OSError("unexpected role log appeared during retention")
                copied[name] = _copy_retained_file(source_fd, target_fd, name)
            os.fsync(target_fd)
            os.fsync(destination)
            for name, identity in copied.items():
                if _version(os.stat(name, dir_fd=source_fd, follow_symlinks=False)) != identity:
                    raise OSError("role log changed before source retirement")
            if set(os.listdir(source_fd)) != set(copied):
                raise OSError("role log family changed before source retirement")
            for name in copied:
                os.unlink(name, dir_fd=source_fd)
            os.fsync(source_fd)
        finally:
            os.close(target_fd)
        os.rmdir(role, dir_fd=source)
    finally:
        os.close(source_fd)


def rollback(root: Path, roles: dict[str, int], legacy: int, record: Path, recovery: Path) -> None:
    _validate(roles)
    _protected_parent(record)
    if not record.exists():
        return
    saved = _read_record(record)
    if saved.get("schema") != SCHEMA or saved.get("root") != str(root) or saved.get("roles") != roles:
        raise OSError("log rollback record does not match this migration")
    fd = _directory(root / "logs")
    try:
        current = _tree(fd, {0, legacy, *roles.values(), *_qemu_uids()})
        original = saved["original"]
        for name, identity in original.items():
            if current.get(name) != identity:
                raise OSError("original log material changed identity; preserve service users for recovery")
        if saved["installed"]:
            return  # Existing split users and their current logs remain installed.
        for name in set(current) - set(original):
            if name in {MARKER, *FILES}:
                continue
            role, _, leaf = name.partition("/")
            if role not in FILES or not _log_family(leaf, FILES[role]):
                raise OSError("new unexpected log bytes require operator recovery before identity removal")
        for name in set(current) - set(original):
            child = _at(fd, name)
            try:
                info = os.fstat(child)
                role = name.split("/")[0]
                expected_uid = roles[role] if role in roles else 0
                if (
                    info.st_uid != expected_uid
                    or (name in FILES and not stat.S_ISDIR(info.st_mode))
                    or ("/" in name and not stat.S_ISREG(info.st_mode))
                ):
                    raise OSError("new log material has unexpected custody")
            finally:
                os.close(child)
        _protected_parent(recovery)
        recovery.mkdir(mode=0o700)
        parent_fd = _directory(recovery.parent)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        recovery_fd = _directory(recovery)
        try:
            # Move complete role directories outside the disposable snapshot;
            # retain every byte and inode before any service identity is removed.
            for role in FILES:
                if role in current:
                    _retain_role(fd, recovery_fd, role)
            if MARKER in current:
                try:
                    os.rename(MARKER, MARKER, src_dir_fd=fd, dst_dir_fd=recovery_fd)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    identity = _copy_retained_file(fd, recovery_fd, MARKER)
                    os.fsync(recovery_fd)
                    if _version(os.stat(MARKER, dir_fd=fd, follow_symlinks=False)) != identity:
                        raise OSError("layout marker changed during retention") from exc
                    os.unlink(MARKER, dir_fd=fd)
            retained = _tree(recovery_fd, {0, *roles.values()})
            for name in retained:
                child = _at(recovery_fd, name)
                try:
                    directory = stat.S_ISDIR(os.fstat(child).st_mode)
                    _acl(
                        child,
                        0,
                        {"user::rwx" if directory else "user::rw-", "group::---", "other::---"},
                        0o700 if directory else 0o600,
                    )
                finally:
                    os.close(child)
            os.fsync(recovery_fd)
            os.fsync(fd)
        finally:
            os.close(recovery_fd)
        # Original legacy entries are restored by the installer's inode-bound
        # ACL snapshot. Refuse identity removal if that later restore fails.
        if saved["created_anchor"]:
            parent = _directory(root)
            try:
                info = os.stat("logs", dir_fd=parent, follow_symlinks=False)
                if [info.st_dev, info.st_ino] != original[""]:
                    raise OSError("created log anchor changed before removal")
                os.rmdir("logs", dir_fd=parent)
            finally:
                os.close(parent)
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("provision", "rollback"))
    parser.add_argument("root", type=Path)
    parser.add_argument("api")
    parser.add_argument("runner")
    parser.add_argument("analysis")
    parser.add_argument("legacy")
    parser.add_argument("record", type=Path)
    parser.add_argument("recovery", nargs="?", type=Path)
    args = parser.parse_args()
    roles = {role: pwd.getpwnam(getattr(args, role)).pw_uid for role in FILES}
    legacy = pwd.getpwnam(args.legacy).pw_uid
    if args.operation == "provision":
        provision(args.root, roles, legacy, args.record)
    elif args.recovery is None:
        parser.error("rollback requires a private recovery directory")
    else:
        rollback(args.root, roles, legacy, args.record, args.recovery)


if __name__ == "__main__":
    main()
