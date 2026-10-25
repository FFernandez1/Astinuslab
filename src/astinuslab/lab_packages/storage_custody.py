"""Private package bytes, optionally shared by the two installed package writers."""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

IDENTITIES_PATH = Path("/etc/astinuslab/runtime-identities")
ROOT_POLICY_PATH = Path("/etc/astinuslab/acl-policy.json")
IDENTITIES = (
    b"mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab\n"
)
STORE_NAMES = ("lab-packages", "lab-content")


class PackageCustodyError(OSError):
    """A package-writer boundary could not be established safely."""


def failure_message(message: str, error: BaseException) -> str:
    if isinstance(error, (PackageCustodyError, PermissionError)):
        return (
            message + "; ask the operator to run the quiesced split package-custody migration "
            "(installer update with --split-users; see docs/usage/running-locally.md)"
        )
    return message


def configured_storage_root() -> Path:
    # Installer invocation remains stdlib-only under isolated system Python.
    from ..core.config import settings

    return Path(settings.storage_dir)


@dataclass(frozen=True)
class PackageCustody:
    storage_root: Path
    api_uid: int
    runner_uid: int

    @property
    def writers(self) -> frozenset[int]:
        return frozenset((self.api_uid, self.runner_uid))


def _protected_bytes(path: Path) -> bytes:
    # Every ancestor is root-controlled; neither service may replace a policy
    # parent and replay a different root-owned file through it.
    for ancestor in reversed(path.parents):
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PackageCustodyError("package custody policy has an unprotected ancestor")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o022:
            raise PackageCustodyError("package custody policy is not a protected regular file")
        data = os.read(descriptor, 16385)
        if len(data) > 16384:
            raise PackageCustodyError("package custody policy is too large")
        return data
    finally:
        os.close(descriptor)


def policy_for(path: Path) -> PackageCustody | None:
    root = configured_storage_root().absolute()
    path = path.absolute()
    if ".." in path.parts or ".." in root.parts:
        raise PackageCustodyError("package custody paths must be canonical")
    if not any(path.is_relative_to(root / name) for name in STORE_NAMES):
        return None
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise PackageCustodyError("package custody path contains a symbolic link")
    try:
        IDENTITIES_PATH.lstat()
    except FileNotFoundError:
        return None
    if _protected_bytes(IDENTITIES_PATH) != IDENTITIES:
        raise PackageCustodyError("invalid split package custody identity marker")
    try:
        policy = json.loads(_protected_bytes(ROOT_POLICY_PATH))
        if not isinstance(policy, dict):
            raise ValueError("package custody root policy must be an object")
        callers = policy.get("allowed_callers")
        if (
            policy.get("schema_version") != "astinuslab-acl-policy/v1"
            or not root.is_absolute()
            or policy.get("allowed_roots") != [str(root)]
            or not isinstance(callers, list)
            or not all(isinstance(caller, str) for caller in callers)
            or "astinuslab-runner" not in callers
        ):
            raise ValueError("package storage root is not authorized")
        api_uid = pwd.getpwnam("astinuslab-api").pw_uid
        runner_uid = pwd.getpwnam("astinuslab-runner").pw_uid
        analysis_uid = pwd.getpwnam("astinuslab-analysis").pw_uid
        if 0 in (api_uid, runner_uid, analysis_uid) or len({api_uid, runner_uid, analysis_uid}) != 3:
            raise ValueError("package roles must have distinct unprivileged UIDs")
    except (KeyError, TypeError, ValueError) as exc:
        raise PackageCustodyError("invalid installed package custody policy") from exc
    root_info = root.lstat()
    if root_info.st_uid != api_uid or not root_info.st_mode & stat.S_ISVTX:
        raise PackageCustodyError("package storage root needs the quiesced split custody migration")
    return PackageCustody(root, api_uid, runner_uid)


def _entries(policy: PackageCustody, *, directory: bool) -> set[str]:
    permissions = "rwx" if directory else "rw-"
    entries = {f"user::{permissions}", "group::---", f"mask::{permissions}", "other::---"}
    entries.update(f"user:{uid}:{permissions}" for uid in policy.writers)
    if directory:
        entries |= {"default:" + entry for entry in tuple(entries)}
    return entries


def _secure_descriptor(descriptor: int, policy: PackageCustody, *, directory: bool) -> None:
    inspected = os.fstat(descriptor)
    valid_type = stat.S_ISDIR(inspected.st_mode) if directory else stat.S_ISREG(inspected.st_mode)
    if not valid_type or inspected.st_uid not in policy.writers or (not directory and inspected.st_nlink != 1):
        raise PackageCustodyError("package storage has foreign or unsafe custody")
    if os.geteuid() not in policy.writers | {0}:
        raise PackageCustodyError("caller is not an installed package writer")
    expected = _entries(policy, directory=directory)
    target = f"/proc/self/fd/{descriptor}"
    try:
        if os.geteuid() in {0, inspected.st_uid}:
            # Replacing the complete ACL prevents inherited analysis/group entries
            # becoming effective when the named-user mask is restored after 0600.
            subprocess.run(
                ["setfacl", "--set", ",".join(sorted(expected)), "--", target],
                check=True,
                pass_fds=(descriptor,),
                capture_output=True,
            )
        result = subprocess.run(
            ["getfacl", "-cnp", "--", target], check=True, capture_output=True, text=True, pass_fds=(descriptor,)
        )
    except subprocess.SubprocessError as exc:
        raise PackageCustodyError("could not establish or verify package custody ACL") from exc
    actual = {line for line in result.stdout.splitlines() if line and not line.startswith("#")}
    if actual != expected:
        raise PackageCustodyError("package storage ACL differs from the exact installed writer policy")


def secure_file(descriptor: int, path: Path) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PackageCustodyError("package file must be a single-link regular file")
    policy = policy_for(path)
    if policy is None:
        os.fchmod(descriptor, 0o600)
    else:
        _secure_descriptor(descriptor, policy, directory=False)


def secure_directory(path: Path) -> None:
    policy = policy_for(path)
    if policy is None:
        os.chmod(path, 0o700)
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _secure_descriptor(descriptor, policy, directory=True)
    finally:
        os.close(descriptor)


def migrate_store(root: Path, *, policy: PackageCustody, allowed_owners: set[int], audit_only: bool = False) -> None:
    """Installer-only descriptor-bound migration; audit every entry before edits."""
    if not policy.storage_root.is_absolute() or policy.storage_root.resolve() != policy.storage_root:
        raise PackageCustodyError("package storage root must be canonical")
    if os.geteuid() != 0 or root not in {policy.storage_root / name for name in STORE_NAMES}:
        raise PackageCustodyError("invalid package custody migration")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    root_fd = os.open(root, flags | os.O_DIRECTORY)
    records: list[tuple[tuple[str, ...], int, int]] = []
    device = os.fstat(root_fd).st_dev

    def audit(fd: int, parts: tuple[str, ...]) -> None:
        info = os.fstat(fd)
        if len(parts) > 64 or len(records) >= 200000:
            raise PackageCustodyError("package custody tree exceeds its safety bounds")
        if info.st_uid not in allowed_owners or info.st_dev != device:
            raise PackageCustodyError("package storage has foreign custody or crosses a filesystem")
        if not stat.S_ISDIR(info.st_mode) and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
            raise PackageCustodyError("package storage contains unsafe entries")
        records.append((parts, info.st_dev, info.st_ino))
        if stat.S_ISDIR(info.st_mode):
            for name in os.listdir(fd):
                child = os.open(name, flags, dir_fd=fd)
                try:
                    audit(child, (*parts, name))
                finally:
                    os.close(child)

    try:
        audit(root_fd, ())
        if audit_only:
            return
        for parts, expected_device, expected_inode in records:
            descriptor = os.dup(root_fd)
            try:
                for name in parts:
                    child = os.open(name, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) != (expected_device, expected_inode) or (
                    stat.S_ISREG(info.st_mode) and info.st_nlink != 1
                ):
                    raise PackageCustodyError("package storage changed during migration")
                os.fchown(descriptor, policy.api_uid, -1)
                _secure_descriptor(descriptor, policy, directory=stat.S_ISDIR(info.st_mode))
            finally:
                os.close(descriptor)
    finally:
        os.close(root_fd)


def provision(storage: Path, *, api_uid: int, runner_uid: int, legacy_uid: int) -> None:
    """Run only with writers stopped and an installer ACL snapshot retained."""
    if api_uid == runner_uid or 0 in (api_uid, runner_uid):
        raise PackageCustodyError("package writers must have distinct unprivileged UIDs")
    policy = PackageCustody(storage, api_uid, runner_uid)
    owners = {0, legacy_uid, api_uid, runner_uid}
    # Audit BOTH stores before changing either one. Empty missing stores are
    # created and tracked by prepare_managed_directory in the installer.
    for name in STORE_NAMES:
        migrate_store(storage / name, policy=policy, allowed_owners=owners, audit_only=True)
    for name in STORE_NAMES:
        migrate_store(storage / name, policy=policy, allowed_owners=owners)
    descriptor = os.open(storage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != api_uid:
            raise PackageCustodyError("package storage root must belong to API")
        # Shared DB journals still need group directory write. Sticky custody
        # prevents analysis from renaming/replacing either API-owned store.
        os.fchmod(descriptor, stat.S_IMODE(info.st_mode) | stat.S_ISVTX)
    finally:
        os.close(descriptor)


def preflight_rollback(
    storage: Path, snapshots: list[Path], *, allowed_owners: set[int], created_dirs: set[Path] | None = None
) -> None:
    """Never orphan new package bytes by deleting their service identity.

    Refuse before ANY identity/policy restoration. Keep services stopped and
    snapshots intact for operator reconciliation or completing the upgrade.
    """
    if os.geteuid() != 0 or not storage.is_absolute() or storage.resolve() != storage:
        raise PackageCustodyError("unsafe package rollback root")
    recorded = {}
    visited: set[Path] = set()
    tracked_directories = created_dirs or set()
    for snapshot in snapshots:
        if not snapshot.exists():
            continue
        data = json.loads(snapshot.read_text())
        if data.get("version") != 1:
            raise PackageCustodyError("unsupported package rollback snapshot")
        for row in data["records"]:
            path = Path(data["root"]) / row["path"]
            if any(path.is_relative_to(storage / name) for name in STORE_NAMES):
                recorded[path] = (row["dev"], row["ino"])
    count = 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

    def visit(fd: int, path: Path, device: int, depth: int):
        nonlocal count
        count += 1
        info = os.fstat(fd)
        directory = stat.S_ISDIR(info.st_mode)
        if count > 200000 or depth > 64 or info.st_dev != device or info.st_uid not in allowed_owners:
            raise PackageCustodyError("unsafe package rollback tree")
        if not directory and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
            raise PackageCustodyError("unsafe package rollback inode")
        visited.add(path)
        if path in recorded:
            if (info.st_dev, info.st_ino) != recorded[path]:
                raise PackageCustodyError(f"package inode changed since snapshot: {path}")
        elif not directory or path not in tracked_directories:
            raise PackageCustodyError(f"new package data or directory requires reconciliation before rollback: {path}")
        if directory:
            for name in os.listdir(fd):
                child = os.open(name, flags, dir_fd=fd)
                try:
                    visit(child, path / name, device, depth + 1)
                finally:
                    os.close(child)

    root_fd = os.open(storage, flags | os.O_DIRECTORY)
    try:
        for name in STORE_NAMES:
            try:
                descriptor = os.open(name, flags | os.O_DIRECTORY, dir_fd=root_fd)
            except FileNotFoundError:
                continue
            try:
                visit(descriptor, storage / name, os.fstat(root_fd).st_dev, 0)
            finally:
                os.close(descriptor)
    finally:
        os.close(root_fd)
    missing = recorded.keys() - visited
    if missing:
        raise PackageCustodyError(f"recorded package material disappeared before rollback: {min(missing)}")


if __name__ == "__main__":
    import sys

    try:
        if sys.argv[1] == "--rollback":
            _, root, legacy, api, runner, created_file, *snapshots = sys.argv[1:]
            created = Path(created_file)
            tracked = {Path(line) for line in created.read_text().splitlines() if line} if created.is_file() else set()
            preflight_rollback(
                Path(root),
                [Path(p) for p in snapshots],
                allowed_owners={0, *(pwd.getpwnam(n).pw_uid for n in (legacy, api, runner))},
                created_dirs=tracked,
            )
        else:
            root, legacy, api, runner = sys.argv[1:]
            provision(
                Path(root),
                legacy_uid=pwd.getpwnam(legacy).pw_uid,
                api_uid=pwd.getpwnam(api).pw_uid,
                runner_uid=pwd.getpwnam(runner).pw_uid,
            )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise SystemExit(
            f"Package custody failed: {exc}. Keep services stopped; retain the installer snapshot and reconcile package data or complete the split upgrade."
        ) from exc
