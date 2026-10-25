"""Quiesced installed SQLite layout migration; never a service-side helper.

The sticky package parent cannot also hold cross-UID SQLite sidecars. Only the
three configured local endpoints participate, and original files stay in place
until readiness. Private transaction data outlives the disposable ACL snapshot.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from .managed_config import database_endpoint_payload, set_database_endpoints
else:
    # -I excludes script directories. Add only this explicitly trusted installer
    # helper directory, never the current directory or runtime PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from managed_config import database_endpoint_payload, set_database_endpoints

VERSION = 1
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
SUFFIXES = ("", "-wal", "-shm", "-journal")
FIELDS = {
    "application": ("astinuslab.db", "ASTINUSLAB_DB_PATH", ""),
    "broker": ("celery_broker.db", "ASTINUSLAB_CELERY_BROKER_URL", "sqla+sqlite:///"),
    "result": ("celery_results.db", "ASTINUSLAB_CELERY_RESULT_BACKEND", "db+sqlite:///"),
}
RESERVED_PARENTS = {"logs", "tmp", "dumps", "ssh_keys", "playground", "workload-cache", "lab-packages", "lab-content"}


class LayoutError(RuntimeError):
    """Refuse an ambiguous layout without adopting or deleting its contents."""


def _directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise LayoutError("database path is not canonical")
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _file(path: Path) -> int:
    parent = _directory(path.parent)
    try:
        try:
            descriptor = os.open(path.name, READ_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise LayoutError("database file is unsafe or inaccessible") from exc
    finally:
        os.close(parent)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise LayoutError("database family has a non-regular or aliased file")
    return descriptor


def _digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while block := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def _record(path: Path, owners: set[int]) -> dict[str, Any]:
    descriptor = _file(path)
    try:
        info = os.fstat(descriptor)
        if info.st_uid not in owners:
            raise LayoutError("database family has foreign custody")
        return {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size, "sha256": _digest(descriptor)}
    finally:
        os.close(descriptor)


def _matches(path: Path, expected: dict[str, Any], *, content: bool = True) -> None:
    descriptor = _file(path)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (expected["dev"], expected["ino"]):
            raise LayoutError("database file identity changed")
        if content and _digest(descriptor) != expected["sha256"]:
            raise LayoutError("database file contents changed")
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    descriptor = _file(path)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 131072:
            raise LayoutError("database transaction has unsafe custody or size")
        data = os.read(descriptor, 131073)
        result = json.loads(data)
        if not isinstance(result, dict) or result.get("version") != VERSION:
            raise LayoutError("database transaction has an unsupported format")
        return result
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: dict[str, Any], *, replace: bool = False) -> None:
    parent = _directory(path.parent)
    temporary = f".{path.name}.{secrets.token_hex(12)}"
    descriptor = -1
    try:
        if replace:
            _read_json(path)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
        if replace:
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent)
        os.fsync(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _receipt(install: Path) -> dict[str, Any] | None:
    try:
        result = _read_json(install / ".sqlite-layout.json")
    except FileNotFoundError:
        return None
    if result.get("install") != str(install) or result.get("phase") != "committed":
        raise LayoutError("invalid committed database layout")
    return result


def check_pending(install: Path) -> None:
    try:
        pending = _read_json(install / ".sqlite-layout.pending.json")
    except FileNotFoundError:
        return
    receipt = _receipt(install)
    if (
        receipt
        and pending.get("transaction_id") == receipt.get("transaction_id")
        and pending.get("install") == str(install)
    ):
        # Publication is already committed. Cleanup may be retried, never rollback.
        return
    raise LayoutError("pending database layout requires explicit quiesced recovery")


def _family(path: Path, owners: set[int]) -> dict[str, dict[str, Any]]:
    result = {}
    for suffix in SUFFIXES:
        try:
            result[suffix] = _record(Path(str(path) + suffix), owners)
        except FileNotFoundError:
            if not suffix:
                raise LayoutError("configured database is missing") from None
    return result


def _base_acl(path: Path) -> bool:
    return all(line.startswith(("user::", "group::", "other::")) for line in _acl(path))


def _acl(path: Path) -> set[str]:
    descriptor = _directory(path) if path.is_dir() else _file(path)
    try:
        output = subprocess.run(
            ["getfacl", "-cpn", "--", f"/proc/self/fd/{descriptor}"],
            check=True,
            capture_output=True,
            text=True,
            pass_fds=(descriptor,),
        ).stdout
    finally:
        os.close(descriptor)
    return {line for line in output.splitlines() if line and not line.startswith("#")}


def _shared_custody(path: Path, api_uid: int, group: int, owners: set[int]) -> None:
    directory = _directory(path.parent)
    try:
        info = os.fstat(directory)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (api_uid, group, 0o2770):
            raise LayoutError("committed database parent custody changed")
    finally:
        os.close(directory)
    if _acl(path.parent) != {
        "user::rwx",
        "group::rwx",
        "other::---",
        "default:user::rwx",
        "default:group::rwx",
        "default:other::---",
    }:
        raise LayoutError("committed database parent ACL changed")
    for suffix in _family(path, owners):
        member = Path(str(path) + suffix)
        descriptor = _file(member)
        try:
            info = os.fstat(descriptor)
            if info.st_gid != group or stat.S_IMODE(info.st_mode) != 0o660:
                raise LayoutError("committed database file custody changed")
        finally:
            os.close(descriptor)
        if _acl(member) != {"user::rw-", "group::rw-", "other::---"}:
            raise LayoutError("committed database file ACL changed")


def _custom(path: Path, roots: tuple[Path, Path], group: int, owners: set[int]) -> None:
    root = next((root for root in roots if path.is_relative_to(root) and path.parent != root), None)
    if root is None or path.relative_to(root).parts[0] in RESERVED_PARENTS:
        raise LayoutError("custom SQLite endpoint is outside supported database parents")
    # Split identities may not exist yet. Every custom intermediate directory
    # must already grant the shared group traversal; do not repair operator paths.
    ancestor = path.parent
    while ancestor != root:
        parent_fd = _directory(ancestor)
        try:
            parent = os.fstat(parent_fd)
            if (
                parent.st_uid not in owners
                or parent.st_gid != group
                or stat.S_IMODE(parent.st_mode) not in (0o770, 0o2770)
            ):
                raise LayoutError("custom SQLite parent needs existing nonsticky shared custody")
        finally:
            os.close(parent_fd)
        if not _base_acl(ancestor):
            raise LayoutError("custom SQLite ACL policy is unsupported; metadata was not changed")
        ancestor = ancestor.parent
    for suffix in _family(path, owners):
        member = Path(str(path) + suffix)
        descriptor = _file(member)
        try:
            info = os.fstat(descriptor)
            if info.st_gid != group or stat.S_IMODE(info.st_mode) != 0o660:
                raise LayoutError("custom SQLite file needs existing shared custody")
        finally:
            os.close(descriptor)
        if not _base_acl(member):
            raise LayoutError("custom SQLite ACL policy is unsupported; metadata was not changed")


def build_plan(
    install: Path, storage: Path, endpoints: dict[str, Any], *, api_uid: int, shared_gid: int, allowed_uids: set[int]
) -> dict[str, Any]:
    check_pending(install)
    receipt = _receipt(install)
    owners = allowed_uids | {os.geteuid()}
    operational = install / "storage"
    target = operational / "databases"
    entries: list[dict[str, Any]] = []
    external = {}
    replacements = {}
    seen: set[tuple[int, int]] = set()
    for role, (name, field, prefix) in FIELDS.items():
        endpoint = endpoints.get(role, {})
        if endpoint.get("kind") == "external" and role != "application":
            external[role] = endpoint
            continue
        if endpoint.get("kind") != "sqlite" or not isinstance(endpoint.get("path"), str):
            raise LayoutError(f"{role}: unsupported configured database endpoint")
        path = Path(endpoint["path"])
        family = _family(path, owners)
        for member in family.values():
            identity = (member["dev"], member["ino"])
            if identity in seen:
                raise LayoutError("configured database endpoints alias each other")
            seen.add(identity)
        migrate = path == operational / name and receipt is None
        committed = path == target / name and receipt is not None and receipt.get("mapping", {}).get(role) == str(path)
        if migrate:
            if target.exists() or target.is_symlink():
                raise LayoutError("database destination already exists without migration provenance")
            destination = target / name
            replacements[field] = prefix + str(destination)
        else:
            destination = path
            if committed:
                _shared_custody(path, api_uid, shared_gid, owners)
            else:
                _custom(path, (operational, storage), shared_gid, owners)
        entries.append(
            {"role": role, "source": str(path), "destination": str(destination), "migrate": migrate, "family": family}
        )
    env_path = install / "config/astinuslab.env"
    env = _record(env_path, owners)
    configuration = _record(install / "astinuslab.toml", owners) if (install / "astinuslab.toml").exists() else None
    required = sum(member["size"] for entry in entries if entry["migrate"] for member in entry["family"].values())
    if shutil.disk_usage(operational).free < 2 * required + 16 * 1024 * 1024:
        raise LayoutError("insufficient space for database staging and recovery")
    if install.stat().st_dev != operational.stat().st_dev:
        raise LayoutError("database retirement crosses the supported filesystem boundary")
    return {
        "version": VERSION,
        "install": str(install),
        "install_identity": _record_directory(install),
        "storage": str(storage),
        "transaction_id": secrets.token_hex(16),
        "api_uid": api_uid,
        "group_gid": shared_gid,
        "owners": sorted(owners),
        "entries": entries,
        "external": external,
        "replacements": replacements,
        "environment": env,
        "phase": "planned",
        "created": [],
        "retired": [],
        "previous_receipt": receipt,
        "configuration": configuration,
    }


def _transaction(plan: dict[str, Any]) -> Path:
    identifier = plan.get("transaction_id")
    if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise LayoutError("invalid database transaction identity")
    return Path(plan["install"]) / "backups" / f"sqlite-layout-{identifier}"


def _save(plan: dict[str, Any]) -> None:
    path = _transaction(plan) / "transaction.json"
    _write_json(path, plan, replace=path.exists())


def _load(snapshot: Path, expected_install: Path | None = None) -> dict[str, Any] | None:
    try:
        reference = _read_json(snapshot / "sqlite-layout.json")
    except FileNotFoundError:
        return None
    if expected_install is not None and reference.get("install") != str(expected_install):
        raise LayoutError("database snapshot belongs to a different installation")
    transaction = _transaction(reference)
    if reference.get("transaction_dir") != str(transaction):
        raise LayoutError("database snapshot redirects recovery")
    plan = _read_json(transaction / "transaction.json")
    if _transaction(plan) != transaction:
        raise LayoutError("database journal identity differs from snapshot")
    if _record_directory(Path(plan["install"])) != plan["install_identity"]:
        raise LayoutError("database installation changed identity")
    return plan


def _copy(source: Path, destination: Path, record: dict[str, Any]) -> None:
    _matches(source, record)
    source_fd = _file(source)
    parent_fd = _directory(destination.parent)
    destination_fd = -1
    try:
        destination_fd = os.open(
            destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd
        )
        while block := os.read(source_fd, 1024 * 1024):
            view = memoryview(block)
            while view:
                view = view[os.write(destination_fd, view) :]
        os.fsync(destination_fd)
        os.fsync(parent_fd)
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)
        os.close(parent_fd)
    _matches(source, record)
    if _record(destination, {os.geteuid()})["sha256"] != record["sha256"]:
        raise LayoutError("database copy verification failed")


def _secure(path: Path, uid: int, gid: int, *, directory: bool = False) -> None:
    descriptor = _directory(path) if directory else _file(path)
    try:
        os.fchown(descriptor, uid, gid)
        acl = "u::rwx,g::rwx,o::---,d:u::rwx,d:g::rwx,d:o::---" if directory else "u::rw-,g::rw-,o::---"
        subprocess.run(
            ["setfacl", "--set", acl, "--", f"/proc/self/fd/{descriptor}"],
            check=True,
            pass_fds=(descriptor,),
            capture_output=True,
        )
        os.fchmod(descriptor, 0o2770 if directory else 0o660)
    finally:
        os.close(descriptor)


def private_backups(install: Path) -> Path:
    backups = install / "backups"
    if not backups.exists():
        backups.mkdir(mode=0o700)
    descriptor = _directory(backups)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LayoutError("database backups directory has unsafe custody")
    finally:
        os.close(descriptor)
    return backups


def stage(plan: dict[str, Any], snapshot: Path) -> None:
    install = Path(plan["install"])
    check_pending(install)
    _matches(install / "config/astinuslab.env", plan["environment"])
    transaction = _transaction(plan)
    private_backups(install)
    transaction.mkdir(mode=0o700)
    _save(plan)
    _copy(install / "config/astinuslab.env", transaction / "environment.before", plan["environment"])
    reference = {
        "version": VERSION,
        "install": str(install),
        "transaction_id": plan["transaction_id"],
        "transaction_dir": str(transaction),
    }
    _write_json(snapshot / "sqlite-layout.json", reference)
    migrating = [entry for entry in plan["entries"] if entry["migrate"]]
    if migrating:
        _write_json(install / ".sqlite-layout.pending.json", reference)
        destination_dir = install / "storage/databases"
        destination_dir.mkdir(mode=0o700)
        plan["created_directory"] = _record_directory(destination_dir)
        _save(plan)
        for entry in migrating:
            source, destination = Path(entry["source"]), Path(entry["destination"])
            for suffix, record in entry["family"].items():
                target = Path(str(destination) + suffix)
                plan["created"].append(str(target))
                _save(plan)
                _copy(Path(str(source) + suffix), target, record)
            connection = sqlite3.connect(destination)
            try:
                if connection.execute("pragma integrity_check").fetchone() != ("ok",):
                    raise LayoutError("staged database integrity check failed")
                connection.execute("pragma wal_checkpoint(truncate)")
            finally:
                connection.close()
            for member in _family(destination, set(plan["owners"])):
                _secure(Path(str(destination) + member), plan["api_uid"], plan["group_gid"])
        _secure(destination_dir, plan["api_uid"], plan["group_gid"], directory=True)
    plan["phase"] = "staged"
    _save(plan)


def _record_directory(path: Path) -> dict[str, int]:
    descriptor = _directory(path)
    try:
        info = os.fstat(descriptor)
        return {"dev": info.st_dev, "ino": info.st_ino}
    finally:
        os.close(descriptor)


def _audit_created(plan: dict[str, Any]) -> None:
    expected = plan.get("created_directory")
    if not expected:
        if (
            any(entry["migrate"] for entry in plan["entries"])
            and (Path(plan["install"]) / "storage/databases").exists()
        ):
            raise LayoutError("unexpected unrecorded database directory; retain recovery state")
        return
    root = Path(plan["install"]) / "storage/databases"
    if plan.get("reconciliation_started") and not root.exists():
        root = _transaction(plan) / "reconciled-candidate"
    if _record_directory(root) != expected:
        raise LayoutError("created database directory changed identity")
    allowed = {
        FIELDS[entry["role"]][0] + suffix for entry in plan["entries"] if entry["migrate"] for suffix in SUFFIXES
    }
    descriptor = _directory(root)
    try:
        names = os.listdir(descriptor)
    finally:
        os.close(descriptor)
    if set(names) - allowed:
        raise LayoutError("unexpected entry in created database directory")
    for name in names:
        _record(root / name, set(plan["owners"]))


def retain_candidate(snapshot: Path) -> None:
    plan = _load(snapshot)
    if plan is None or plan.get("retained"):
        return
    _check_environment(plan, allow_partial=True)
    _audit_created(plan)
    root = _transaction(plan) / "candidate-on-rollback"
    if root.exists():
        raise LayoutError("partial candidate retention requires reconciliation")
    root.mkdir(mode=0o700)
    for entry in plan["entries"]:
        source = Path(entry["destination"])
        if not source.exists() and plan["phase"] == "planned":
            continue
        family = _family(source, set(plan["owners"]))
        role_root = root / entry["role"]
        role_root.mkdir(mode=0o700)
        for suffix, record in family.items():
            _copy(Path(str(source) + suffix), role_root / (FIELDS[entry["role"]][0] + suffix), record)
        plan.setdefault("retained_families", {})[entry["role"]] = _family(
            role_root / FIELDS[entry["role"]][0], {os.geteuid()}
        )
        _save(plan)
    plan["retained"] = True
    _save(plan)


def rollback(snapshot: Path) -> None:
    plan = _load(snapshot)
    if plan is None or plan["phase"] == "rolled_back":
        return
    install = Path(plan["install"])
    receipt = _receipt(install)
    if receipt and receipt.get("transaction_id") == plan["transaction_id"]:
        raise LayoutError("database layout already committed; rollback is forbidden")
    _check_environment(plan, allow_partial=True)
    _audit_created(plan)
    retain_candidate(snapshot)
    plan = _load(snapshot)
    if plan is None:
        raise LayoutError("database recovery reference disappeared")
    for item in reversed(plan["retired"]):
        source = Path(item["source"])
        archived = _transaction(plan) / "originals" / item["name"]
        if source.exists() or source.is_symlink():
            if not archived.exists():
                _matches(source, item["record"])
                continue  # Recorded move failed before changing either name.
            raise LayoutError("original database path reappeared during rollback")
        _matches(archived, item["record"])
        os.rename(archived, source)
    if plan.get("created_directory"):
        root = install / "storage/databases"
        # Keep post-schema-restore bytes as well as the pre-restore candidate.
        destination = _transaction(plan) / "reconciled-candidate"
        _audit_created(plan)
        if root.exists():
            plan["reconciliation_started"] = True
            _save(plan)
            descriptor = _directory(root)
            try:
                for name in os.listdir(descriptor):
                    member = _file(root / name)
                    try:
                        os.fchown(member, os.geteuid(), os.getegid())
                        os.fchmod(member, 0o600)
                    finally:
                        os.close(member)
                os.fchown(descriptor, os.geteuid(), os.getegid())
                os.fchmod(descriptor, 0o700)
            finally:
                os.close(descriptor)
            os.rename(root, destination)
            for parent in (root.parent, destination.parent):
                descriptor = _directory(parent)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    if (install / ".sqlite-layout.pending.json").exists():
        (install / ".sqlite-layout.pending.json").unlink()
    plan["phase"] = "rolled_back"
    _save(plan)


def _current(plan: dict[str, Any]) -> dict[str, Any]:
    return _read_json(_transaction(plan) / "transaction.json")


def _check_environment(plan: dict[str, Any], *, allow_partial: bool = False) -> None:
    config_path = Path(plan["install"]) / "astinuslab.toml"
    if plan["configuration"] is not None:
        _matches(config_path, plan["configuration"])
    elif config_path.exists() or config_path.is_symlink():
        raise LayoutError("database configuration appeared during migration")
    path = Path(plan["install"]) / "config/astinuslab.env"
    _matches(path, plan["environment"], content=False)
    before_fd = _file(_transaction(plan) / "environment.before")
    current_fd = _file(path)
    try:
        before = os.read(before_fd, plan["environment"]["size"] + 1)
        if hashlib.sha256(before).hexdigest() != plan["environment"]["sha256"]:
            raise LayoutError("retained database environment changed")
        desired = database_endpoint_payload(before, plan["replacements"])
        current = os.read(current_fd, len(desired) + 1)
    finally:
        os.close(before_fd)
        os.close(current_fd)
    if current == desired or (allow_partial and current == before):
        return
    if allow_partial and desired.startswith(current) and current.startswith(before):
        return  # Only the recorded append was partially written.
    raise LayoutError("database environment changed outside the recorded transaction")


def configure(plan: dict[str, Any]) -> None:
    current = _current(plan)
    set_database_endpoints(
        Path(current["install"]) / "config/astinuslab.env", current["replacements"], current["environment"]["sha256"]
    )


def verify(plan: dict[str, Any], effective: dict[str, Any]) -> None:
    current = _current(plan)
    _check_environment(current)
    if any(effective.get(role) != endpoint for role, endpoint in current["external"].items()):
        raise LayoutError("nonlocal database endpoint changed during migration")
    for entry in current["entries"]:
        if effective.get(entry["role"]) != {"kind": "sqlite", "path": entry["destination"]}:
            raise LayoutError("installed database endpoint differs from its plan")
        destination = Path(entry["destination"])
        if destination.parent == Path(current["install"]) / "storage/databases":
            _shared_custody(destination, current["api_uid"], current["group_gid"], set(current["owners"]))
        else:
            _custom(
                destination,
                (Path(current["install"]) / "storage", Path(current["storage"])),
                current["group_gid"],
                set(current["owners"]),
            )
    _audit_created(current)
    current["phase"] = "configured"
    _save(current)


def retire(plan: dict[str, Any], snapshot: Path) -> None:
    current = _load(snapshot)
    if current is None or current["transaction_id"] != plan["transaction_id"] or current["phase"] != "configured":
        raise LayoutError("database retirement requires the verified transaction")
    _check_environment(current)
    originals = _transaction(current) / "originals"
    originals.mkdir(mode=0o700)
    # Audit the complete family before moving the first member.
    for entry in current["entries"]:
        if entry["migrate"]:
            for suffix, record in entry["family"].items():
                _matches(Path(entry["source"] + suffix), record)
    for entry in current["entries"]:
        if not entry["migrate"]:
            continue
        for suffix, record in entry["family"].items():
            source = Path(entry["source"] + suffix)
            item = {"source": str(source), "name": source.name, "record": record}
            current["retired"].append(item)
            _save(current)  # Intent precedes rename so a failed move is reconcilable.
            _matches(source, record)
            os.rename(source, originals / source.name)
            descriptor = _file(originals / source.name)
            try:
                os.fchown(descriptor, os.geteuid(), os.getegid())
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            for directory in (source.parent, originals):
                descriptor = _directory(directory)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    current["phase"] = "retired"
    _save(current)


def commit(plan: dict[str, Any], snapshot: Path) -> bool:
    """Return false only for cleanup failure AFTER durable receipt publication."""
    current = _load(snapshot)
    if current is None or current["phase"] != "retired":
        raise LayoutError("database commit requires verified retirement")
    install = Path(current["install"])
    previous = _receipt(install)
    if previous != current["previous_receipt"]:
        raise LayoutError("committed database receipt changed during upgrade")
    receipt = {
        "version": VERSION,
        "install": str(install),
        "transaction_id": current["transaction_id"],
        "phase": "committed",
        "mapping": {
            entry["role"]: entry["destination"]
            for entry in current["entries"]
            if Path(entry["destination"]).parent == install / "storage/databases"
        },
    }
    destination = install / ".sqlite-layout.json"
    try:
        _write_json(destination, receipt, replace=previous is not None)
    except BaseException:
        # Before the commit point, compensate a published-but-unflushed new
        # receipt only when its exact transaction identity is still ours.
        if destination.exists() and _read_json(destination) == receipt:
            if previous is not None:
                _write_json(destination, previous, replace=True)
            else:
                destination.unlink()
            descriptor = _directory(install)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        raise
    # The live layout is committed. Nothing below may trigger rollback.
    try:
        current["phase"] = "committed"
        _save(current)
        pending = install / ".sqlite-layout.pending.json"
        if pending.exists():
            pending.unlink()
        descriptor = _directory(install)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, LayoutError):
        return False
    return True


ENDPOINT_CODE = """
import hashlib, json
from pathlib import Path
from sqlalchemy.engine import make_url
from astinuslab.core.config import settings

def endpoint(value, prefix):
    if not value.startswith(prefix):
        return ({'kind': 'external', 'sha256': hashlib.sha256(value.encode()).hexdigest()}
                if 'sqlite' not in value.partition('://')[0].lower() else {'kind': 'unsupported'})
    try:
        url = make_url(value[len(prefix) - len('sqlite:///'):])
        path = Path(url.database or '')
        if url.query or url.host or not path.is_absolute() or '..' in path.parts:
            return {'kind': 'unsupported'}
        return {'kind': 'sqlite', 'path': str(path)}
    except Exception:
        return {'kind': 'unsupported'}

print(json.dumps({
    'application': {'kind': 'sqlite', 'path': str(settings.db_path)},
    'broker': endpoint(settings.celery_broker_url, 'sqla+sqlite:///'),
    'result': endpoint(settings.celery_result_backend, 'db+sqlite:///'),
}))
"""


def resolve_endpoints(install: Path, release: Path, user: str) -> dict[str, Any]:
    command = [
        "/usr/bin/python3",
        "-I",
        str(Path(__file__).with_name("runtime_environment.py")),
        "--data-dir",
        str(install),
        "--settings-only",
        "--user",
        user,
        "--",
        str(release / ".venv/bin/python"),
        "-c",
        ENDPOINT_CODE,
    ]
    result = subprocess.run(
        command, cwd=release, env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=60
    )
    if result.returncode:
        # Settings failures can include secret-bearing environment values.
        raise LayoutError("installed database endpoint resolution failed")
    try:
        endpoints = json.loads(result.stdout)
    except ValueError:
        raise LayoutError("installed database endpoint resolution was not structured") from None
    if not isinstance(endpoints, dict):
        raise LayoutError("installed database endpoint resolution was not a mapping")
    return endpoints


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "pending",
            "backups",
            "preflight",
            "prepare",
            "verify",
            "retain",
            "rollback",
            "retire",
            "commit",
            "committed",
        ),
    )
    parser.add_argument("--install", type=Path, required=True)
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--api")
    parser.add_argument("--runner")
    parser.add_argument("--analysis")
    parser.add_argument("--legacy")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("the installed database transaction requires root and quiesced services")
    if args.action == "pending":
        check_pending(args.install)
        return 0
    if args.action == "backups":
        private_backups(args.install)
        return 0
    if args.action in ("preflight", "prepare"):
        if not all((args.storage, args.release, args.legacy, args.api, args.runner, args.analysis)):
            parser.error("endpoint resolution requires storage, release and all identities")
        legacy = pwd.getpwnam(args.legacy)
        identities = {}
        for name in (args.api, args.runner, args.analysis, args.legacy):
            try:
                identities[name] = pwd.getpwnam(name).pw_uid
            except KeyError:
                if args.action != "preflight":
                    raise LayoutError("split database identity is missing") from None
        try:
            group = grp.getgrnam("astinuslab").gr_gid
        except KeyError:
            if args.action != "preflight":
                raise LayoutError("split database group is missing") from None
            group = legacy.pw_gid
        user = args.api if args.api in identities else args.legacy
        endpoints = resolve_endpoints(args.install, args.release, user)
        plan = build_plan(
            args.install,
            args.storage,
            endpoints,
            api_uid=identities.get(args.api, legacy.pw_uid),
            shared_gid=group,
            allowed_uids=set(identities.values()),
        )
        if args.action == "preflight":
            return 0
        if args.snapshot is None:
            parser.error("prepare requires a private migration snapshot")
        stage(plan, args.snapshot)
        configure(plan)
        verify(plan, resolve_endpoints(args.install, args.release, args.api))
        return 0
    if args.snapshot is None:
        parser.error("recovery and commit actions require a private migration snapshot")
    saved_plan = _load(args.snapshot, args.install)
    if args.action == "committed":
        if saved_plan is None:
            return 1
        receipt = _receipt(args.install)
        return 0 if receipt and receipt.get("transaction_id") == saved_plan["transaction_id"] else 1
    if saved_plan is None:
        if args.action in ("retain", "rollback"):
            return 0  # Failure before staging has nothing to reconcile.
        raise LayoutError("database transaction reference is missing")
    plan = saved_plan
    if args.action == "retain":
        retain_candidate(args.snapshot)
    elif args.action == "rollback":
        rollback(args.snapshot)
    elif args.action == "retire":
        retire(plan, args.snapshot)
    elif args.action == "commit":
        if not commit(plan, args.snapshot):
            print("database layout committed; private cleanup remains", file=sys.stderr)
    elif args.action == "verify":
        if not args.release or not args.api:
            parser.error("verification requires the installed release and API identity")
        verify(plan, resolve_endpoints(args.install, args.release, args.api))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LayoutError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        # No environment values, URLs, or DB contents enter public diagnostics.
        reason = str(error) if isinstance(error, LayoutError) else type(error).__name__
        print(f"split database transaction refused: {reason}; preserve its private recovery state", file=sys.stderr)
        raise SystemExit(1) from None
