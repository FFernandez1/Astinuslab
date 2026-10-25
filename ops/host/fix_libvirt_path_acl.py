#!/usr/bin/env python3
"""Apply narrowly scoped ACLs to a policy-approved storage directory.

This file is copied into /usr/local/libexec/astinuslab and executed as root.
It accepts the target only; trust roots come from root-owned policy.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path

try:
    from acl_policy import POLICY_PATH, load_policy, select_root  # type: ignore[import-not-found]
except ImportError:
    from ops.host.acl_policy import POLICY_PATH, load_policy, select_root


LIBVIRT_USER = "libvirt-qemu"
MAX_DEPTH = 64
MAX_CHILDREN = 4096
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_NAMED_ACL = re.compile(r"^(default:)?(user|group):([^:]+):([rwx-]+)$")


def _validate_segment(segment: str) -> None:
    if "\x00" in segment:
        raise ValueError("NUL path segment is not allowed")
    if not segment:
        raise ValueError("empty path segment is not allowed")
    if segment == ".":
        raise ValueError("dot path segment is not allowed")
    if segment == "..":
        raise ValueError("parent path segment is not allowed")
    if segment.startswith("/"):
        raise ValueError("absolute path segment is not allowed")
    if "/" in segment:
        raise ValueError("slash in path segment is not allowed")


def _relative_segments(target: Path, root: Path) -> list[str]:
    if not target.is_absolute() or not root.is_absolute():
        raise ValueError("ACL target and root must be absolute")
    if "\x00" in str(target) or "\x00" in str(root):
        raise ValueError("NUL path segment is not allowed")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("ACL target is outside policy root") from exc
    segments = list(relative.parts)
    if len(segments) > MAX_DEPTH:
        raise ValueError("ACL path exceeds depth limit")
    for segment in segments:
        _validate_segment(segment)
    return segments


def _validate_raw_target(value: str) -> None:
    if "\x00" in value:
        raise ValueError("NUL path segment is not allowed")
    if not value.startswith("/"):
        raise ValueError("ACL target must be absolute")
    for segment in value.split("/")[1:]:
        _validate_segment(segment)


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _check_owner_and_mode(
    info: os.stat_result,
    expected_uid: int,
    allowed_gids: frozenset[int],
    allowed_uids: frozenset[int],
    *,
    check_group_write: bool = True,
) -> None:
    if info.st_uid != expected_uid and info.st_uid not in allowed_uids:
        raise OSError("ACL path component owner mismatch")
    if info.st_mode & 0o002:
        raise OSError("ACL path component is world writable")
    # Group write is tolerated only for a group the root-owned policy names, and
    # only then: the installed layout needs storage at 2770, but an arbitrary
    # group-writable component would let a second group member swap directories
    # underneath the walk.
    if check_group_write and info.st_mode & 0o020 and info.st_gid not in allowed_gids:
        raise OSError("ACL path component is group writable")


def _check_directory(
    fd: int,
    expected_uid: int,
    allowed_gids: frozenset[int] = frozenset(),
    allowed_uids: frozenset[int] = frozenset(),
) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("ACL path component is not a directory")
    _check_owner_and_mode(info, expected_uid, allowed_gids, allowed_uids, check_group_write=False)
    if info.st_gid not in allowed_gids:
        # With an extended ACL, stat's group bits describe the mask, not the
        # owning group. Our own qemu rwx grant must survive a second boot.
        # Check the raw group entry so widening the mask cannot activate a
        # previously masked, unapproved group write permission either.
        for line in _acl_text(fd).splitlines():
            entry, _, _annotation = line.partition("\t#effective:")
            if entry.startswith("group::"):
                permissions = entry[len("group::") :]
                if re.fullmatch(r"[r-][w-][x-]", permissions) is None:
                    raise OSError("cannot determine ACL owning group permissions")
                if "w" in permissions:
                    raise OSError("ACL path component is group writable")
                break
        else:
            raise OSError("cannot determine ACL owning group permissions")
    return info


def _acl_text(fd: int) -> str:
    result = subprocess.run(
        ["getfacl", "-cp", "--", f"/proc/self/fd/{fd}"],
        check=True,
        capture_output=True,
        text=True,
        pass_fds=(fd,),
    )
    return result.stdout


def _reject_unexpected_named_entries(
    fd: int,
    allowed: set[tuple[str, str]],
    required_permissions: dict[tuple[str, str], str] | None = None,
) -> None:
    required_permissions = required_permissions or {}
    for line in _acl_text(fd).splitlines():
        entry, _, _annotation = line.partition("\t#effective:")
        match = _NAMED_ACL.match(entry)
        if not match:
            continue
        identity = (match.group(2), match.group(3))
        if match.group(1) is not None or identity not in allowed:
            raise ValueError("unexpected named ACL entry")
        expected = required_permissions.get(identity)
        if expected is not None and _canonical_permissions(match.group(4)) != _canonical_permissions(expected):
            raise ValueError("unexpected named ACL entry permissions")


def _group_permissions(mode: int) -> str:
    bits = (mode & 0o070) >> 3
    return "".join(name for name, bit in (("r", 4), ("w", 2), ("x", 1)) if bits & bit)


def _mask_for(entries: dict[str, str], existing_group: str = "") -> str:
    """Union of the entries this run grants and the group class already in force.

    The ACL mask shares storage with the group permission bits, so a mask built
    from the named entries alone silently rewrites them - it would take the
    supported 2770 storage layout down to 2710 and revoke the service group's
    write access on every component the helper walks. Preserving the existing
    group class grants nothing new: those permissions are already effective.
    """
    permissions: set[str] = set(existing_group)
    for value in entries.values():
        permissions.update(value)
    return "".join(permission for permission in "rwx" if permission in permissions) or "---"


def _canonical_permissions(value: str) -> str:
    return "".join(permission if permission in value else "-" for permission in "rwx")


def _entry_prefix(name: str) -> str:
    return f"{name}:" if name.startswith(("user:", "group:")) else f"user:{name}:"


def _verify_effective(acl_text: str, entries: dict[str, str]) -> None:
    """Require each named entry's EFFECTIVE permissions to equal what was requested.

    getfacl only prints the "#effective:" annotation when the mask clamps an
    entry, so an annotation must be parsed and compared - treating its presence
    as success would accept exactly the silent clamp this check exists to catch.
    """
    for name, permissions in entries.items():
        canonical = _canonical_permissions(permissions)
        prefix = _entry_prefix(name)
        for line in acl_text.splitlines():
            if not line.startswith(prefix):
                continue
            entry, _, annotation = line.partition("\t#effective:")
            effective = annotation.strip() if annotation else entry[len(prefix) :]
            if _canonical_permissions(effective) != canonical:
                raise OSError("ACL entry was not applied effectively")
            break
        else:
            raise OSError("ACL entry was not applied effectively")


def _caller_gids(caller: str) -> frozenset[int]:
    entry = pwd.getpwnam(caller)
    return frozenset(os.getgrouplist(caller, entry.pw_gid))


def _existing_caller_permissions(
    acl_text: str,
    caller: str,
    caller_gids: frozenset[int],
    file_gid: int,
) -> str:
    """Permissions the caller already holds without a named entry of its own.

    A named ``user`` entry outranks every group entry for that UID, so writing
    one that is narrower than the caller's group access is a demotion, not a
    grant. The shared storage root is owned by the API identity and is group
    writable precisely so the runner can create SQLite WAL/SHM siblings; a bare
    traversal entry there turns the broker into a read-only database.
    """
    mask: str | None = None
    candidates: list[str] = []
    for line in acl_text.splitlines():
        entry, _, _annotation = line.partition("\t#effective:")
        entry = entry.strip()
        if entry.startswith("mask::"):
            mask = entry[len("mask::") :]
            continue
        if entry.startswith("group::"):
            if file_gid in caller_gids:
                candidates.append(entry[len("group::") :])
            continue
        match = _NAMED_ACL.match(entry)
        if not match or match.group(1) is not None:
            continue
        kind, name, permissions = match.group(2), match.group(3), match.group(4)
        if kind == "user" and name == caller:
            candidates.append(permissions)
        elif kind == "group":
            try:
                gid = grp.getgrnam(name).gr_gid
            except KeyError:
                continue
            if gid in caller_gids:
                candidates.append(permissions)
    held: set[str] = set()
    for permissions in candidates:
        allowed = set(permissions) if mask is None else set(permissions) & set(mask)
        held.update(allowed)
    return _canonical_permissions("".join(permission for permission in "rwx" if permission in held))


def _apply_fd_acl(
    fd: int,
    *,
    caller: str,
    target: bool,
    directory: bool,
    traverse_only: bool = False,
    shared_groups: tuple[str, ...] = (),
    preserved_groups: tuple[str, ...] = (),
    libvirt_write: bool = False,
) -> None:
    if traverse_only:
        libvirt_permissions = "x"
    elif libvirt_write:
        libvirt_permissions = "rwx" if directory else "rw-"
    else:
        libvirt_permissions = "r-x" if target or directory else "r--"
    entries: dict[str, str] = {f"user:{LIBVIRT_USER}": libvirt_permissions}
    info = os.fstat(fd)
    if caller != pwd.getpwuid(info.st_uid).pw_name:
        requested = "x" if traverse_only else "r-x" if target or directory else "r--"
        held = _existing_caller_permissions(_acl_text(fd), caller, _caller_gids(caller), info.st_gid)
        entries[f"user:{caller}"] = _canonical_permissions(
            "".join(permission for permission in "rwx" if permission in requested or permission in held)
        )
    # Live playground disks are deliberately not exposed to the shared API
    # group. Normal capture/evidence paths do use that group so the separate API
    # identity can serve runner-produced artifacts.
    if not libvirt_write:
        for group in shared_groups:
            entries[f"group:{group}"] = "x" if traverse_only else "r-x" if target or directory else "r--"
    allowed_entries = {(entry_type, name) for entry in entries for entry_type, name in (entry.split(":", 1),)}
    preserved_entries = {("group", name) for name in preserved_groups}
    required_permissions = {identity: "x" for identity in preserved_entries - allowed_entries}
    allowed_entries.update(preserved_entries)
    _reject_unexpected_named_entries(fd, allowed_entries, required_permissions)
    acl_entries = [
        f"{name.replace('user:', 'u:', 1).replace('group:', 'g:', 1)}:{permissions}"
        for name, permissions in entries.items()
    ]
    mask = _mask_for(entries, _group_permissions(os.fstat(fd).st_mode))
    subprocess.run(
        ["setfacl", "-m", ",".join([*acl_entries, f"m::{mask}"]), "--", f"/proc/self/fd/{fd}"],
        check=True,
        pass_fds=(fd,),
    )
    _verify_effective(_acl_text(fd), entries)


def _libvirt_write_target(segments: tuple[str, ...]) -> bool:
    """Only a per-job playground overlay directory may grant qemu write.

    Captures, databases, SSH keys, and the playground disk parent are all kept
    outside this exact shape so a caller cannot use the ACL helper to widen an
    arbitrary storage subtree.
    """
    return len(segments) == 3 and segments[:2] == ("playground", "disks")


def _libvirt_write_child(name: str) -> bool:
    """Only the already-created qcow2 overlay needs qemu write access."""
    return name.endswith(".qcow2")


def _shared_read_target(segments: tuple[str, ...]) -> bool:
    """Limit cross-identity API reads to published evidence namespaces."""
    return (len(segments) >= 2 and segments[0] == "dumps") or (
        len(segments) >= 3 and segments[:2] == ("playground", "captures")
    )


def _policy_gids(names: list[str]) -> frozenset[int]:
    """Resolve policy group names to gids, refusing names this host does not have.

    A policy naming a group that does not exist is a root-side misconfiguration;
    failing loudly beats silently reverting to a stricter rule that then denies
    every capture with an unrelated-looking message.
    """
    gids: set[int] = set()
    for name in names:
        try:
            gids.add(grp.getgrnam(name).gr_gid)
        except KeyError as exc:
            raise ValueError(f"ACL policy names unknown group: {name}") from exc
    return frozenset(gids)


def apply_acl(
    target: Path,
    root: Path,
    caller: str,
    allowed_gids: frozenset[int] = frozenset(),
    evidence_read_groups: tuple[str, ...] = (),
) -> None:
    segments = _relative_segments(target, root)
    libvirt_write = _libvirt_write_target(tuple(segments))
    evidence_target = _shared_read_target(tuple(segments))
    evidence_shared_groups = evidence_read_groups if evidence_target else ()
    caller_uid = pwd.getpwnam(caller).pw_uid
    # Root-owned libvirt artifacts and runner-owned staging directories are
    # both expected below an API-owned split storage root.
    allowed_uids = frozenset({0, caller_uid})
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    directory_fds = [root_fd]
    descriptors: list[tuple[int, os.stat_result]] = []
    primary_error: BaseException | None = None
    try:
        root_info = _check_directory(root_fd, os.fstat(root_fd).st_uid, allowed_gids)
        descriptors.append((root_fd, root_info))
        parent_fd = root_fd
        for segment in segments:
            child_fd = os.open(segment, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            directory_fds.append(child_fd)
            child_info = _check_directory(child_fd, root_info.st_uid, allowed_gids, allowed_uids)
            descriptors.append((child_fd, child_info))
            parent_fd = child_fd
        target_fd = parent_fd
        for index, (fd, _) in enumerate(descriptors):
            prefix = tuple(segments[:index])
            preserve_evidence_groups = evidence_target or index == 0 or prefix == ("playground",)
            _apply_fd_acl(
                fd,
                caller=caller,
                target=index == len(descriptors) - 1,
                directory=True,
                traverse_only=index < len(descriptors) - 1,
                shared_groups=evidence_shared_groups,
                preserved_groups=evidence_read_groups if preserve_evidence_groups else (),
                libvirt_write=libvirt_write and index == len(descriptors) - 1,
            )

        names = os.listdir(target_fd)
        if len(names) > MAX_CHILDREN:
            raise ValueError("ACL target exceeds child count limit")
        children: list[tuple[int, os.stat_result, str]] = []
        child_fds: list[int] = []
        try:
            for name in names:
                _validate_segment(name)
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=target_fd,
                )
                child_fds.append(child_fd)
                child_info = os.fstat(child_fd)
                if not (stat.S_ISREG(child_info.st_mode) or stat.S_ISDIR(child_info.st_mode)):
                    raise ValueError("target contains unsupported child")
                if stat.S_ISREG(child_info.st_mode) and child_info.st_nlink != 1:
                    raise OSError("ACL target contains a multiply-linked regular file")
                if stat.S_ISDIR(child_info.st_mode):
                    _check_directory(child_fd, root_info.st_uid, allowed_gids, allowed_uids)
                else:
                    child_allowed_uids = allowed_uids
                    # libvirt relabels live disks to qemu at boot. Only these
                    # regular VM artifacts in the per-session disk directory
                    # may have that owner; all other ownership checks remain.
                    if libvirt_write and (
                        _libvirt_write_child(name) or name == "seed.iso" or name.endswith("-seed.iso")
                    ):
                        child_allowed_uids = allowed_uids | {pwd.getpwnam(LIBVIRT_USER).pw_uid}
                    _check_owner_and_mode(
                        child_info,
                        root_info.st_uid,
                        allowed_gids,
                        child_allowed_uids,
                        check_group_write=False,
                    )
                children.append((child_fd, child_info, name))
            for child_fd, child_info, name in children:
                _apply_fd_acl(
                    child_fd,
                    caller=caller,
                    target=False,
                    directory=stat.S_ISDIR(child_info.st_mode),
                    shared_groups=evidence_shared_groups,
                    preserved_groups=evidence_read_groups if evidence_target else (),
                    libvirt_write=libvirt_write and _libvirt_write_child(name),
                )
                after = os.fstat(child_fd)
                if (after.st_dev, after.st_ino) != (child_info.st_dev, child_info.st_ino):
                    raise OSError("ACL child identity changed")
        finally:
            for child_fd in child_fds:
                _close_quietly(child_fd)

        for fd, before in descriptors:
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                raise OSError("ACL path identity changed")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        for fd in reversed(directory_fds):
            _close_quietly(fd)
        if primary_error is None:
            descriptors.clear()


def _caller() -> str:
    caller = os.environ.get("SUDO_USER", "")
    if not caller:
        raise ValueError("SUDO_USER is required")
    return caller


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: fix_libvirt_path_acl.py <absolute-target-directory>", file=sys.stderr)
        return 2
    try:
        caller = _caller()
        policy = load_policy(Path(POLICY_PATH))
        if caller not in policy["allowed_callers"]:
            raise ValueError("caller is not listed in ACL policy")
        if args[0] == "--probe":
            return 0
        _validate_raw_target(args[0])
        target = Path(args[0])
        root = select_root(target, policy["allowed_roots"])
        storage_groups = tuple(policy["storage_groups"])
        evidence_read_groups = tuple(policy["evidence_read_groups"])
        apply_acl(target, root, caller, _policy_gids(list(storage_groups)), evidence_read_groups)
    except (OSError, ValueError, KeyError) as exc:
        print(f"DENY: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
