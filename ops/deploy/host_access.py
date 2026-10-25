"""Grant, record and reverse the host access an installation needs.

Three things share one record format and therefore belong in one module:

* ``grant`` adds the `u:<qemu>:x` traversal entries that let libvirt's qemu
  identity reach the storage root, and writes a durable manifest line for each
  entry it actually created.
* ``record`` appends a supplementary rollback entry to the host-access
  transaction log, for a change whose durable record lives elsewhere.
* ``rollback`` reads both back and takes away exactly what this installation
  added -- nothing an operator already had, and nothing it did not write.
* ``revoke-all`` is what ``ops/deploy/uninstall.sh`` calls: the same walk with
  no baseline to keep, because an uninstall is taking the whole installation
  away. It had its own copy of this logic as two `python3 - <<'PY'` heredocs,
  which had to agree with these manifests by inspection alone. They now agree
  the same way the writer and reader here do -- by being one implementation.

The writer used to live in ``ops/deploy/install.sh`` and the reader in
``ops/deploy/configure-privileged-roots.sh``, as two `python3 - <<'PY'`
heredocs. A heredoc body is a shell string, so Ruff, Mypy and coverage never
read either one, and the two had to agree byte for byte about a tab-separated
record and a JSON line by inspection alone. They now agree by construction:
``format_traversal_record`` writes what ``parse_traversal_record`` reads.

Invoked as a subprocess by the shell functions that own each phase, which pass
the arguments they always passed after the subcommand:

    host_access.py grant STORAGE STOP_AT QEMU_USER MANIFEST TRANSACTION_LOG
    host_access.py record LOG KIND TARGET VALUE EXTRA_NAME EXTRA_VALUE
    host_access.py rollback ACL_MANIFEST ACL_BEFORE ACL_EXISTED \\
        MAC_MANIFEST MAC_BEFORE MAC_EXISTED STORAGE_ROOT TRANSACTION_LOG
    host_access.py revoke-all acl|mac MANIFEST STORAGE_ROOT
"""

from __future__ import annotations

import enum
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

# Traversal manifest lines are tab-separated and versioned in column one, so a
# manifest written by an older installer is refused rather than misparsed.
TRAVERSAL_RECORD_VERSION = "v2"

TRANSACTION_LOG_CUSTODY = "host-access transaction log must be a protected regular installer-owned file"


class Reversal(enum.Enum):
    """What revoking one manifest line actually did to the host.

    Two outcomes are not enough here. A line can be reversed, it can fail and
    have to stay in the manifest for a retry, or it can turn out to have nothing
    left to reverse -- the directory is gone, or the entry this installation
    recorded adding is already absent. That third case drains from the manifest
    exactly like a success, which is why a bool was enough for `rollback`, but it
    changed nothing on the host and `uninstall.sh` prints the count.

    The heredoc this module replaced reached `continue` for that case, so it never
    hit its counter. Collapsing it into the success return made uninstall report
    grants removed where nothing was touched. The distinction is explicit now.

    `__bool__` keeps the truthiness the callers were written against: truthy means
    the line may leave the manifest, which is what `rollback` asks.
    """

    REVERSED = "reversed"
    NOTHING_TO_REVERSE = "nothing to reverse"
    FAILED = "failed"

    def __bool__(self) -> bool:
        return self is not Reversal.FAILED


def format_traversal_record(qemu_user: str, device: int, inode: int, path: str) -> str:
    """The one spelling of a traversal manifest line, without its newline."""
    return "\t".join((TRAVERSAL_RECORD_VERSION, qemu_user, str(device), str(inode), path))


def parse_traversal_record(raw: str) -> tuple[str, tuple[int, int], Path]:
    """Read a manifest line back, raising ValueError on anything else."""
    version, qemu_user, device_text, inode_text, path_text = raw.rstrip("\n").split("\t", 4)
    if version != TRAVERSAL_RECORD_VERSION:
        raise ValueError("unsupported traversal record")
    return qemu_user, (int(device_text), int(inode_text)), Path(path_text)


def open_no_follow(path: Path, flags: int = DIRECTORY_FLAGS) -> int:
    """Open *path* component by component, refusing to follow any symlink."""
    fd = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def append_transaction(name: str, record: dict[str, Any]) -> None:
    """Append one JSON rollback record, leaving no partial line behind."""
    fd = os.open(name, APPEND_FLAGS)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise SystemExit(TRANSACTION_LOG_CUSTODY)
        payload = (json.dumps(record, sort_keys=True) + "\n").encode()
        offset = os.lseek(fd, 0, os.SEEK_END)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short write while recording host-access transaction")
                remaining = remaining[written:]
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, offset)
            os.fsync(fd)
            raise
    finally:
        os.close(fd)


def record(name: str, kind: str, target: str, value: str, extra_name: str, extra_value: str) -> None:
    """Record a host-access change whose durable evidence lives elsewhere."""
    entry: dict[str, Any] = {"kind": kind, "target": target, "value": value}
    if extra_name:
        entry[extra_name] = extra_value
    append_transaction(name, entry)


def _acl_entry(fd: int, qemu_user: str) -> tuple[str, str] | None:
    """The declared and effective permissions of *qemu_user*, if it has an entry."""
    result = subprocess.run(
        ["getfacl", "-cpe", "--", f"/proc/self/fd/{fd}"],
        check=True,
        capture_output=True,
        text=True,
        pass_fds=(fd,),
    )
    prefix = f"user:{qemu_user}:"
    for line in result.stdout.splitlines():
        if not line.startswith(prefix):
            continue
        declared, _, effective = line.partition("#effective:")
        permissions = declared[len(prefix) :].strip()
        return permissions, effective.strip() if effective else permissions
    return None


def grant(storage: str, stop_at: str, qemu_user: str, manifest_name: str, transaction_name: str) -> None:
    """Give *qemu_user* execute on every directory from *stop_at* to *storage*.

    A component that already carried the entry was never ours to grant, so it
    is left out of the manifest and uninstall leaves it alone.
    """
    if any(char in storage for char in "\t\r\n"):
        raise SystemExit("traversal path contains a control character that cannot be recorded safely")
    storage_path = Path(os.path.normpath(storage))
    stop_path = Path(os.path.normpath(stop_at))
    if not storage_path.is_absolute() or not stop_path.is_absolute():
        raise SystemExit("traversal paths must be absolute")

    def record_transaction(path: Path, info: os.stat_result) -> None:
        if not transaction_name:
            return
        append_transaction(
            transaction_name,
            {
                "kind": "acl",
                "user": qemu_user,
                "device": info.st_dev,
                "inode": info.st_ino,
                "target": str(path),
            },
        )

    components: list[tuple[Path, int, os.stat_result, tuple[str, str] | None]] = []
    current = storage_path
    while current != stop_path:
        if current == current.parent or not current.is_relative_to(stop_path):
            raise SystemExit(f"traversal stop {stop_path} is not an ancestor of {storage_path}")
        fd = open_no_follow(current)
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            os.close(fd)
            raise SystemExit(f"traversal component is not a directory: {current}")
        entry = _acl_entry(fd, qemu_user)
        if entry is not None and "x" not in entry[1]:
            os.close(fd)
            for _, open_fd, _, _ in components:
                os.close(open_fd)
            raise SystemExit(f"existing {qemu_user} ACL lacks effective execute on {current}")
        components.append((current, fd, info, entry))
        current = current.parent

    manifest = Path(manifest_name)
    base_flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    created = False
    try:
        manifest_fd = os.open(manifest, base_flags)
    except FileNotFoundError:
        manifest_fd = os.open(manifest, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    try:
        manifest_info = os.fstat(manifest_fd)
        if (
            not stat.S_ISREG(manifest_info.st_mode)
            or manifest_info.st_uid != os.geteuid()
            or (not created and manifest_info.st_mode & 0o022)
        ):
            raise SystemExit("traversal manifest must be a regular file owned by the installer identity")
        os.fchmod(manifest_fd, 0o600)
        os.lseek(manifest_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(manifest_fd), encoding="utf-8") as handle:
            existing_records = set(handle.read().splitlines())
        os.lseek(manifest_fd, 0, os.SEEK_END)
        for path, fd, info, entry in reversed(components):
            if entry is not None:
                continue
            line = format_traversal_record(qemu_user, info.st_dev, info.st_ino, str(path))
            subprocess.run(
                ["setfacl", "-m", f"u:{qemu_user}:x", "--", f"/proc/self/fd/{fd}"],
                check=True,
                pass_fds=(fd,),
            )
            verified = _acl_entry(fd, qemu_user)
            if verified is None or "x" not in verified[1]:
                subprocess.run(
                    ["setfacl", "-x", f"u:{qemu_user}", "--", f"/proc/self/fd/{fd}"],
                    check=True,
                    pass_fds=(fd,),
                )
                raise SystemExit(f"traversal grant is not effective on {path}")
            if line not in existing_records:
                payload = (line + "\n").encode()
                record_offset = os.lseek(manifest_fd, 0, os.SEEK_END)
                try:
                    if os.write(manifest_fd, payload) != len(payload):
                        raise OSError("short write while recording traversal grant")
                    os.fsync(manifest_fd)
                except BaseException:
                    try:
                        os.ftruncate(manifest_fd, record_offset)
                        os.fsync(manifest_fd)
                    finally:
                        subprocess.run(
                            ["setfacl", "-x", f"u:{qemu_user}", "--", f"/proc/self/fd/{fd}"],
                            check=True,
                            pass_fds=(fd,),
                        )
                    raise
                existing_records.add(line)
            # The transaction log is supplementary rollback evidence. It is never
            # allowed to claim a grant until the verified durable ownership record
            # exists.
            record_transaction(path, info)
    finally:
        os.close(manifest_fd)
        for _, fd, _, _ in components:
            os.close(fd)


def _read_current(name: str, existed_before: bool) -> tuple[list[str], os.stat_result | None]:
    path = Path(name)
    try:
        fd = os.open(path, READ_FLAGS)
    except FileNotFoundError:
        if existed_before:
            raise SystemExit(f"manifest disappeared during rollback: {name}") from None
        return [], None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise SystemExit(f"manifest is not a protected regular installer-owned file: {name}")
        with os.fdopen(os.dup(fd), encoding="utf-8") as handle:
            return handle.readlines(), info
    finally:
        os.close(fd)


def _finish_manifest(
    name: str,
    original: os.stat_result | None,
    records: list[str],
    existed_before: bool,
) -> None:
    path = Path(name)
    if original is not None:
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise SystemExit(f"manifest changed during rollback: {name}")
    if records or existed_before:
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=path.parent)
        try:
            os.fchmod(temp_fd, 0o600)
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                handle.writelines(records)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    elif original is not None:
        path.unlink()


def _declared_acl(descriptor: int, user: str) -> str | None:
    result = subprocess.run(
        ["getfacl", "-cp", "--", f"/proc/self/fd/{descriptor}"],
        check=True,
        capture_output=True,
        text=True,
        pass_fds=(descriptor,),
    )
    prefix = f"user:{user}:"
    for entry in result.stdout.splitlines():
        if entry.startswith(prefix):
            return entry[len(prefix) :].split("\t", 1)[0].strip()
    return None


def revoke_acl(raw: str, *, operation: str = "roll back") -> Reversal:
    """Take back one traversal grant this installation recorded.

    *operation* names what the caller is doing, because a failed line means
    different things to an operator mid-rollback and mid-uninstall, and the
    message is the only place they find out which paths to correct.
    """
    target = Path(raw.rstrip("\n").split("\t")[-1] or "?")
    try:
        qemu_user, expected, target = parse_traversal_record(raw)
        try:
            leaf = target.lstat()
        except FileNotFoundError:
            return Reversal.NOTHING_TO_REVERSE
        if not target.is_absolute() or stat.S_ISLNK(leaf.st_mode) or not stat.S_ISDIR(leaf.st_mode):
            raise ValueError("traversal path changed since installation")
        fd = open_no_follow(target)
        try:
            actual = os.fstat(fd)
            if (actual.st_dev, actual.st_ino) != expected:
                raise ValueError("traversal path identity changed since installation")
            entry = _declared_acl(fd, qemu_user)
            if entry is None:
                return Reversal.NOTHING_TO_REVERSE
            if entry != "--x":
                raise ValueError("traversal ACL changed since installation")
            result = subprocess.run(
                ["setfacl", "-x", f"u:{qemu_user}", "--", f"/proc/self/fd/{fd}"],
                capture_output=True,
                text=True,
                pass_fds=(fd,),
            )
            if result.returncode != 0:
                raise ValueError(result.stderr.strip() or "setfacl failed")
        finally:
            os.close(fd)
        return Reversal.REVERSED
    except (OSError, ValueError) as exc:
        print(f"could not {operation} traversal grant for {target}: {exc}", file=sys.stderr)
        return Reversal.FAILED


def _revoke_selinux(record_data: dict[str, Any], version: int, storage_root: str) -> None:
    value = record_data["value"]
    expected_type = record_data.get("selinux_type", "svirt_image_t" if version == 1 else None)
    relabel_root = record_data.get("target")
    if version == 1 and not relabel_root:
        # Legacy records describe an earlier storage root, not this run's root.
        relabel_root = value.removesuffix("(/.*)?") if value.endswith("(/.*)?") else storage_root
    if not isinstance(expected_type, str) or not expected_type:
        raise ValueError("missing expected SELinux type")
    if not isinstance(relabel_root, str) or not Path(relabel_root).is_absolute():
        raise ValueError("invalid SELinux storage root")
    listed = subprocess.run(["semanage", "fcontext", "-l"], capture_output=True, text=True)
    if listed.returncode != 0:
        raise ValueError(listed.stderr.strip() or "could not inspect SELinux fcontexts")
    contexts = [
        line.split()[-1]
        for line in listed.stdout.splitlines()
        if line.startswith(value) and line[len(value) : len(value) + 1].isspace()
    ]
    if contexts and not all(f":{expected_type}:" in context for context in contexts):
        raise ValueError("SELinux expression changed to an unexpected type")
    if contexts:
        result = subprocess.run(["semanage", "fcontext", "-d", value], capture_output=True, text=True)
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "semanage failed")
    relabeled = subprocess.run(["restorecon", "-Rv", relabel_root], capture_output=True, text=True)
    if relabeled.returncode != 0:
        raise ValueError(relabeled.stderr.strip() or "restorecon failed")


def open_protected_apparmor_template(path: Path) -> tuple[int, os.stat_result, int, os.stat_result]:
    parent_fd = os.open(path.parent, DIRECTORY_FLAGS)
    try:
        parent_info = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
            raise ValueError("AppArmor template parent is not a protected root-owned directory")
        template_fd = os.open(path.name, READ_FLAGS, dir_fd=parent_fd)
        try:
            template_info = os.fstat(template_fd)
            if not stat.S_ISREG(template_info.st_mode) or template_info.st_uid != 0 or template_info.st_mode & 0o022:
                raise ValueError("AppArmor template is not a protected root-owned file")
        except BaseException:
            os.close(template_fd)
            raise
        return parent_fd, parent_info, template_fd, template_info
    except BaseException:
        os.close(parent_fd)
        raise


def require_stable_apparmor_parent(path: Path, expected: os.stat_result) -> None:
    current = path.parent.lstat()
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != 0
        or current.st_mode & 0o022
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise ValueError("AppArmor template parent changed during rollback")


def create_temporary_at(parent_fd: int, template_name: str) -> tuple[int, str]:
    name = f".{template_name}.tmp.{secrets.token_hex(8)}"
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_fd,
    )
    return descriptor, name


def _revoke_apparmor(record_data: dict[str, Any]) -> None:
    value = record_data["value"]
    target = record_data.get("target")
    if not isinstance(target, str) or not Path(target).is_absolute():
        raise ValueError("invalid AppArmor template")
    path = Path(target)
    parent_fd, parent_info, template_fd, info = open_protected_apparmor_template(path)
    try:
        with os.fdopen(os.dup(template_fd), encoding="utf-8") as handle:
            text = handle.read()
        lines = text.splitlines()
        if value in lines:
            lines.remove(value)
            replacement = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
            temp_fd, temp_name = create_temporary_at(parent_fd, path.name)
            try:
                os.fchmod(temp_fd, stat.S_IMODE(info.st_mode))
                os.fchown(temp_fd, info.st_uid, info.st_gid)
                with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                    handle.write(replacement)
                    handle.flush()
                    os.fsync(handle.fileno())
                require_stable_apparmor_parent(path, parent_info)
                current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_uid != 0
                    or current.st_mode & 0o022
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise ValueError("AppArmor template changed during rollback")
                os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            except BaseException:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except OSError:
                    pass
                raise
        # Both consumers load the policy; re-establish custody before reloading.
        require_stable_apparmor_parent(path, parent_info)
        verification_fd = os.open(path.name, READ_FLAGS, dir_fd=parent_fd)
        try:
            verification_info = os.fstat(verification_fd)
            if (
                not stat.S_ISREG(verification_info.st_mode)
                or verification_info.st_uid != 0
                or verification_info.st_mode & 0o022
            ):
                raise ValueError("AppArmor template lost protected root custody before reload")
        finally:
            os.close(verification_fd)
        for unit in ("apparmor", "libvirtd"):
            result = subprocess.run(["systemctl", "restart", unit], capture_output=True, text=True)
            if result.returncode != 0:
                raise ValueError(result.stderr.strip() or f"could not restart {unit}")
    finally:
        os.close(template_fd)
        os.close(parent_fd)


def _read_transactions(name: str) -> list[dict[str, Any]]:
    if not name:
        return []
    fd = os.open(name, READ_FLAGS)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise SystemExit("host-access transaction log is not a protected regular installer-owned file")
        with os.fdopen(os.dup(fd), encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
    finally:
        os.close(fd)
    if not all(isinstance(entry, dict) for entry in records):
        raise SystemExit("invalid host-access transaction record")
    return records


def revoke_mac(raw: str, storage_root: str, *, operation: str = "roll back") -> Reversal:
    """Take back one recorded AppArmor line or SELinux fcontext.

    *storage_root* is only consulted for a version-1 SELinux record that names
    no target of its own. Uninstall has no current storage root to offer and
    passes an empty string, which leaves such a record refused and pending --
    the same outcome its own copy of this reached.

    Never `NOTHING_TO_REVERSE`: both branches touch the host even when the
    recorded item is already absent. The SELinux branch runs `restorecon -Rv`
    over the relabel root whether or not an fcontext was there to delete, and the
    AppArmor branch restarts `apparmor` and `libvirtd` whether or not the line was
    still in the template. Either is work an operator can observe, so a success
    here is a reversal.
    """
    try:
        record_data = json.loads(raw)
        version = record_data.get("version")
        if version not in {1, 2} or record_data.get("kind") not in {"apparmor", "selinux"}:
            raise ValueError("unsupported MAC record")
        value = record_data.get("value")
        if not isinstance(value, str) or not value:
            raise ValueError("missing MAC value")
        if record_data["kind"] == "selinux":
            _revoke_selinux(record_data, version, storage_root)
        else:
            _revoke_apparmor(record_data)
        return Reversal.REVERSED
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"could not {operation} MAC change: {exc}", file=sys.stderr)
        return Reversal.FAILED


def revoke_all(kind: str, manifest_name: str, storage_root: str) -> int:
    """Reverse every grant a manifest records, for uninstall rather than rollback.

    Rollback subtracts a baseline: it reverses what this installation added and
    keeps what the operator already had. Uninstall has no baseline to keep, so
    this is the same walk with an empty one -- which is why it reuses
    ``revoke_acl`` and ``revoke_mac`` rather than restating them. Every line
    that could not be reversed stays in the manifest so a second uninstall
    retries exactly those, and the manifest is removed only when none are left.

    Prints the number of records actually reversed -- not the number that
    drained. A record whose target is already gone leaves the manifest without
    being counted, because `uninstall.sh` prints this number and reporting a grant
    removed where nothing was touched tells an operator something untrue about
    their host. Exits non-zero if any line is still pending.
    """
    if kind == "acl":

        def revoke(raw: str) -> Reversal:
            return revoke_acl(raw, operation="revoke")

    elif kind == "mac":

        def revoke(raw: str) -> Reversal:
            return revoke_mac(raw, storage_root, operation="revoke")

    else:
        raise SystemExit(f"unsupported manifest kind: {kind}")

    current, info = _read_current(manifest_name, True)
    pending: list[str] = []
    removed = 0
    for raw in current:
        if not raw.strip():
            continue
        outcome = revoke(raw)
        if outcome is Reversal.REVERSED:
            removed += 1
        elif outcome is Reversal.FAILED:
            pending.append(raw)
    _finish_manifest(manifest_name, info, pending, False)
    print(removed)
    return 1 if pending else 0


def rollback(
    acl_name: str,
    acl_before_name: str,
    acl_existed: str,
    mac_name: str,
    mac_before_name: str,
    mac_existed: str,
    storage_root: str,
    transaction_name: str,
) -> int:
    """Reverse this installation's host-access delta. Returns an exit status."""
    failed = False

    def revoke_mac_record(raw: str) -> Reversal:
        return revoke_mac(raw, storage_root)

    def revoke_transaction(entry: dict[str, Any]) -> Reversal:
        kind = entry.get("kind")
        if kind == "acl":
            try:
                raw = (
                    format_traversal_record(
                        str(entry["user"]),
                        entry["device"],
                        entry["inode"],
                        str(entry["target"]),
                    )
                    + "\n"
                )
            except KeyError:
                print("could not roll back host-access transaction: invalid ACL record", file=sys.stderr)
                return Reversal.FAILED
            return revoke_acl(raw)
        if kind in {"apparmor", "selinux"}:
            payload = dict(entry)
            payload["version"] = 2
            return revoke_mac_record(json.dumps(payload, sort_keys=True) + "\n")
        print("could not roll back host-access transaction: unsupported record", file=sys.stderr)
        return Reversal.FAILED

    def rollback_manifest(name: str, before_name: str, existed: str, revoke: Callable[[str], Reversal]) -> None:
        nonlocal failed
        baseline = Path(before_name).read_text(encoding="utf-8").splitlines(keepends=True)
        current, info = _read_current(name, existed == "true")
        baseline_set = set(baseline)
        pending = []
        for raw in current:
            if raw in baseline_set:
                continue
            if not revoke(raw):
                pending.append(raw)
                failed = True
        _finish_manifest(name, info, baseline + pending, existed == "true")

    rollback_manifest(acl_name, acl_before_name, acl_existed, revoke_acl)
    rollback_manifest(mac_name, mac_before_name, mac_existed, revoke_mac_record)
    for transaction in reversed(_read_transactions(transaction_name)):
        if not revoke_transaction(transaction):
            failed = True
    return 1 if failed else 0


def main(argv: list[str]) -> None:
    if len(argv) == 6 and argv[0] == "grant":
        grant(*argv[1:])
    elif len(argv) == 7 and argv[0] == "record":
        record(*argv[1:])
    elif len(argv) == 9 and argv[0] == "rollback":
        raise SystemExit(rollback(*argv[1:]))
    elif len(argv) == 4 and argv[0] == "revoke-all":
        raise SystemExit(revoke_all(*argv[1:]))
    else:
        raise SystemExit(
            "usage: host_access.py grant STORAGE STOP_AT QEMU_USER MANIFEST TRANSACTION_LOG"
            " | record LOG KIND TARGET VALUE EXTRA_NAME EXTRA_VALUE"
            " | rollback ACL_MANIFEST ACL_BEFORE ACL_EXISTED MAC_MANIFEST MAC_BEFORE"
            " MAC_EXISTED STORAGE_ROOT TRANSACTION_LOG"
            " | revoke-all acl|mac MANIFEST STORAGE_ROOT"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
