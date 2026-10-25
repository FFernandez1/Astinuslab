"""Snapshot and restore the configuration files an installation manages.

An upgrade edits the installation's own configuration in place. If a later
phase fails, rollback has to put back exactly what the previous installation
had -- the same bytes, mode and owner for a file that existed, and no file at
all where the candidate created one. Both halves therefore have to agree about
one record format, and the reader has to be able to prove that the file it is
about to overwrite is still the file the writer measured.

``capture`` and ``restore`` used to be two ``python3 - <<'PY'`` heredocs inside
``ops/deploy/install.sh``. A heredoc body is a shell string, so Ruff, Mypy and
coverage never read either one, and the two agreed about the JSON record by
inspection alone. They now agree by construction: ``capture`` emits what
``_load_records`` validates, and :data:`MANAGED_PATHS` is one list rather than
two copies.

Every path is resolved component by component with ``dir_fd`` and
``O_NOFOLLOW``, so a symlink planted mid-tree between capture and restore
cannot redirect either half outside the installation root, and identity is
re-checked by ``(st_dev, st_ino)`` before any write.

The same set of files is also written and validated here, so the list of what
counts as managed configuration, the rule that a managed file is a plain
unshared regular file, and the identity re-check around every in-place rewrite
all live in one place instead of six heredocs.

Invoked as a subprocess by the shell functions that own each phase:

    managed_config.py capture INSTALL_DIR DESTINATION
    managed_config.py restore INSTALL_DIR SNAPSHOT
    managed_config.py install-config SOURCE DESTINATION
    managed_config.py validate-destinations INSTALL_DIR RELATIVE...
    managed_config.py neutralise-env-auth ENV_FILE
    managed_config.py configure-admin-auth

``configure-admin-auth`` reads AUTH_CONFIG_PATH, AUTH_TOKEN_HASH and
AUTH_ENABLED from the environment rather than argv, and that is not an
oversight to tidy up: ``/proc/<pid>/cmdline`` is world-readable and
``/proc/<pid>/environ`` is not, so an argument would publish the admin token
hash to every local account for the lifetime of the call.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from .runtime_environment import read_environment
else:
    from runtime_environment import read_environment

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
WRITE_FLAGS = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

READ_CHUNK = 1024 * 1024

# The two phases have always worded their cross-device refusal differently.
# Keep both spellings rather than unify them: an operator runbook or log
# filter matching either one would otherwise stop matching.
CAPTURE_BOUNDARY = "managed config crosses a filesystem boundary"
RESTORE_BOUNDARY = "managed config restore crosses a filesystem boundary"

# The files an installation owns and may rewrite during an upgrade, relative to
# the installation root. Key generation uses the fixed `storage/ssh_keys` names,
# so recording their *absence* is what makes a post-key rollback remove every
# credential the candidate created.
MANAGED_PATHS: tuple[str, ...] = (
    ".sqlite-layout.json",
    "astinuslab.toml",
    "config/astinuslab.env",
    "config/catalog/targets.yaml",
    "config/catalog/workloads.yaml",
    "config/catalog/scenarios.yaml",
    "config/catalog/attackers.yaml",
    "config/catalog/profiles.yaml",
    "config/catalog/network_nodes.yaml",
    "storage/ssh_keys/.active",
    "storage/ssh_keys/astinuslab-default",
    "storage/ssh_keys/astinuslab-default.pub",
)


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write every byte, since one os.write is not obliged to take them all."""
    view = memoryview(payload)
    while view:
        view = view[os.write(descriptor, view) :]


def _read_all(descriptor: int) -> bytes:
    payload = bytearray()
    while chunk := os.read(descriptor, READ_CHUNK):
        payload.extend(chunk)
    return bytes(payload)


def _open_parent(
    root_fd: int, components: list[str], root_device: int, relative: str, boundary: str
) -> tuple[int, bool]:
    """Walk to the leaf's parent one component at a time, never following a link.

    Returns the parent descriptor and whether the walk stopped early because a
    directory on the way does not exist. The caller owns the descriptor.
    ``boundary`` is the caller's own cross-device refusal wording, which the two
    phases spell differently and operators may already be matching on.
    """
    descriptor = os.dup(root_fd)
    try:
        for component in components[:-1]:
            try:
                child = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                return descriptor, True
            os.close(descriptor)
            descriptor = child
            if os.fstat(descriptor).st_dev != root_device:
                raise SystemExit(f"{boundary}: {relative}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, False


def capture(root: Path, destination: Path) -> list[dict[str, Any]]:
    """Record the current state of every managed path under ``root``."""
    records: list[dict[str, Any]] = []
    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        root_device = os.fstat(root_fd).st_dev
        for relative in MANAGED_PATHS:
            components = relative.split("/")
            parent_fd, parent_missing = _open_parent(root_fd, components, root_device, relative, CAPTURE_BOUNDARY)
            try:
                if parent_missing:
                    records.append({"path": relative, "exists": False})
                    continue
                try:
                    leaf_fd = os.open(components[-1], READ_FLAGS, dir_fd=parent_fd)
                except FileNotFoundError:
                    records.append({"path": relative, "exists": False})
                    continue
                try:
                    info = os.fstat(leaf_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != root_device:
                        raise SystemExit(f"managed config is not a safe regular file: {relative}")
                    records.append(
                        {
                            "path": relative,
                            "exists": True,
                            "mode": stat.S_IMODE(info.st_mode),
                            "uid": info.st_uid,
                            "gid": info.st_gid,
                            "dev": info.st_dev,
                            "ino": info.st_ino,
                            "payload": base64.b64encode(_read_all(leaf_fd)).decode("ascii"),
                        }
                    )
                finally:
                    os.close(leaf_fd)
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)

    snapshot_fd = os.open(destination, CREATE_FLAGS, 0o600)
    try:
        _write_all(snapshot_fd, json.dumps(records, sort_keys=True, separators=(",", ":")).encode())
        os.fsync(snapshot_fd)
    finally:
        os.close(snapshot_fd)
    return records


def _load_records(snapshot: Path) -> list[dict[str, Any]]:
    """Read the snapshot back, refusing one another identity could have written."""
    snapshot_fd = os.open(snapshot, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(snapshot_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SystemExit("managed config snapshot is not protected")
        with os.fdopen(os.dup(snapshot_fd), encoding="utf-8") as handle:
            records: list[dict[str, Any]] = json.load(handle)
    finally:
        os.close(snapshot_fd)
    return records


def _remove_candidate_file(parent_fd: int, name: str, relative: str) -> None:
    """Take away a file the candidate created, but only a plain unshared one."""
    try:
        leaf_fd = os.open(name, READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        info = os.fstat(leaf_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SystemExit(f"refusing to remove unsafe candidate config: {relative}")
    finally:
        os.close(leaf_fd)
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _restore_recorded_file(parent_fd: int, name: str, record: dict[str, Any], root_device: int) -> None:
    """Put back the exact bytes, mode and owner recorded for one managed file."""
    relative = record["path"]
    try:
        current_fd = os.open(name, WRITE_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise SystemExit(f"managed config disappeared during rollback: {relative}") from exc
    try:
        info = os.fstat(current_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_dev != root_device
            or (info.st_dev, info.st_ino) != (record["dev"], record["ino"])
        ):
            raise SystemExit(f"refusing replaced candidate config: {relative}")
        payload = base64.b64decode(record["payload"], validate=True)
        os.ftruncate(current_fd, 0)
        os.lseek(current_fd, 0, os.SEEK_SET)
        _write_all(current_fd, payload)
        os.fchown(current_fd, record["uid"], record["gid"])
        os.fchmod(current_fd, record["mode"])
        os.fsync(current_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (record["dev"], record["ino"]):
            raise SystemExit(f"managed config changed identity during rollback: {relative}")
    finally:
        os.close(current_fd)
    os.fsync(parent_fd)


def restore(root: Path, snapshot: Path) -> None:
    """Undo every managed-config change the candidate installation made."""
    records = _load_records(snapshot)
    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        root_device = os.fstat(root_fd).st_dev
        for record in records:
            relative = record["path"]
            components = relative.split("/")
            if any(component in {"", ".", ".."} for component in components):
                raise SystemExit(f"unsafe managed config snapshot path: {relative}")
            parent_fd, parent_missing = _open_parent(root_fd, components, root_device, relative, RESTORE_BOUNDARY)
            try:
                name = components[-1]
                if not record["exists"]:
                    if parent_missing:
                        continue
                    _remove_candidate_file(parent_fd, name, relative)
                    continue
                if parent_missing:
                    raise SystemExit(f"managed config parent disappeared during rollback: {relative}")
                _restore_recorded_file(parent_fd, name, record, root_device)
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)


# --------------------------------------------------------------------------
# Authoring: install a default, validate the set, and edit the auth keys.
# --------------------------------------------------------------------------


def install_config_if_missing(source: Path, destination: Path) -> str:
    """Install a default config file, but never over one that already exists.

    Returns the outcome the installer prints and branches on: ``preserved``
    when the operator already has the file, ``missing-source`` when the release
    does not ship a default for it, and ``installed`` otherwise.
    """
    parent, name = os.path.split(destination)
    directory_fd = os.open(parent, DIRECTORY_FLAGS)
    try:
        try:
            existing_fd: int | None = os.open(name, READ_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            existing_fd = None
        if existing_fd is not None:
            try:
                info = os.fstat(existing_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SystemExit("managed config destination is non-regular or multiply linked")
            finally:
                os.close(existing_fd)
            return "preserved"

        try:
            source_fd = os.open(source, READ_FLAGS)
        except FileNotFoundError:
            return "missing-source"
        try:
            source_info = os.fstat(source_fd)
            if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
                raise SystemExit("managed config source is non-regular or multiply linked")
            temporary = f".{name}.tmp.{secrets.token_hex(8)}"
            temporary_fd = os.open(temporary, CREATE_FLAGS, 0o640, dir_fd=directory_fd)
            try:
                while chunk := os.read(source_fd, READ_CHUNK):
                    if os.write(temporary_fd, chunk) != len(chunk):
                        raise SystemExit("short write while installing managed config")
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
            try:
                os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
            except FileExistsError:
                # A concurrent creator wins; validate it on the next invocation
                # instead of replacing or following its directory entry.
                raise SystemExit("managed config destination appeared during installation") from None
            finally:
                os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(source_fd)
        return "installed"
    finally:
        os.close(directory_fd)


def validate_destinations(root: Path, relatives: list[str]) -> None:
    """Refuse any managed destination that is not a plain unshared file.

    A path that does not exist yet is fine -- this runs before the defaults are
    installed. What it will not accept is a destination someone has turned into
    a hard link, a device or a directory, which is how a write meant for the
    installation ends up somewhere else.
    """
    root_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        for directory in ("config", "config/catalog"):
            try:
                descriptor = os.open(os.path.join(root, directory), DIRECTORY_FLAGS)
            except FileNotFoundError:
                continue
            else:
                os.close(descriptor)
        for relative in relatives:
            try:
                descriptor = os.open(relative, READ_FLAGS, dir_fd=root_fd)
            except FileNotFoundError:
                continue
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SystemExit(f"managed config is non-regular or multiply linked: {relative}")
            finally:
                os.close(descriptor)
    finally:
        os.close(root_fd)


AUTH_ENV_KEYS = re.compile(r"\s*(ASTINUSLAB_AUTH_ENABLED|ASTINUSLAB_ADMIN_API_TOKEN_HASH)\s*=")

DATABASE_ENV_KEYS = frozenset(
    ("ASTINUSLAB_DB_PATH", "ASTINUSLAB_CELERY_BROKER_URL", "ASTINUSLAB_CELERY_RESULT_BACKEND")
)


def database_endpoint_payload(before: bytes, replacements: dict[str, str]) -> bytes:
    """Exact append payload, also used by the installer's rollback CAS check."""
    if not replacements:
        return before

    def quote(value: str) -> str:
        return '"' + re.sub(r'([\\"$`])', r"\\\1", value) + '"'

    suffix = "# Installed split SQLite endpoints\n" + "".join(
        f"{key}={quote(value)}\n" for key, value in sorted(replacements.items())
    )
    return before + (b"\n" if before and not before.endswith(b"\n") else b"") + suffix.encode()


def set_database_endpoints(env_file: Path, replacements: dict[str, str], expected_sha256: str) -> str:
    """Append only selected installed DB bindings, preserving existing bytes/inode.

    EnvironmentFile uses the final assignment. Appending avoids accidentally
    rewriting apparent assignments inside an unrelated multiline quoted value.
    The quiesced installer retains the exact original bytes for rollback.
    """
    if not replacements.keys() <= DATABASE_ENV_KEYS or any(
        not isinstance(value, str) or any(ord(character) < 32 for character in value) for value in replacements.values()
    ):
        raise SystemExit("invalid managed database environment assignment")
    parent_fd = os.open(env_file.parent, DIRECTORY_FLAGS)
    descriptor = -1
    try:
        descriptor = os.open(env_file.name, WRITE_FLAGS, dir_fd=parent_fd)
        original = os.fstat(descriptor)
        if (
            not stat.S_ISREG(original.st_mode)
            or original.st_nlink != 1
            or original.st_dev != os.fstat(parent_fd).st_dev
        ):
            raise SystemExit("database environment has unsafe custody")
        before = _read_all(descriptor)
        if hashlib.sha256(before).hexdigest() != expected_sha256:
            raise SystemExit("database environment changed before update")
        actual = read_environment(Path(f"/proc/self/fd/{descriptor}"))
        if all(actual.get(key) == value for key, value in replacements.items()):
            return expected_sha256

        payload = database_endpoint_payload(before, replacements)
        _assert_same_file(parent_fd, env_file.name, original, "database environment changed during update")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if _read_all(descriptor) != before:
            raise SystemExit("database environment changed during update")
        os.lseek(descriptor, 0, os.SEEK_SET)
        _write_all(descriptor, payload)
        os.ftruncate(descriptor, len(payload))
        os.fsync(descriptor)
        _assert_same_file(parent_fd, env_file.name, original, "database environment changed during update")
        os.fsync(parent_fd)
        return hashlib.sha256(payload).hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def neutralise_env_auth_overrides(env_file: Path) -> None:
    """Comment out auth keys in the launcher env so they cannot beat the TOML.

    The systemd unit sources this file, so an ``ASTINUSLAB_AUTH_ENABLED=false``
    left here silently overrides ``astinuslab.toml`` -- which is how a release
    once served unauthenticated while the installer printed a token. The lines
    are commented rather than deleted, so an operator can still see what they
    had and where it moved to.
    """
    parent, name = os.path.split(env_file)
    directory_fd = os.open(parent, DIRECTORY_FLAGS)
    descriptor = os.open(name, WRITE_FLAGS, dir_fd=directory_fd)
    try:
        original = os.fstat(descriptor)
        if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1:
            raise SystemExit("launcher environment is non-regular or multiply linked")
        with os.fdopen(os.dup(descriptor), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        out = []
        changed = False
        for line in lines:
            if AUTH_ENV_KEYS.match(line):
                out.append(f"# {line.strip()}   # managed in astinuslab.toml [auth]")
                changed = True
            else:
                out.append(line)
        if changed:
            payload = ("\n".join(out) + "\n").encode()
            _assert_same_file(directory_fd, name, original, "launcher environment changed during update")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            _assert_same_file(directory_fd, name, original, "launcher environment changed during update")
            os.fsync(directory_fd)
    finally:
        os.close(descriptor)
        os.close(directory_fd)


def _assert_same_file(directory_fd: int, name: str, original: os.stat_result, message: str) -> None:
    """Refuse to keep writing if the directory entry now names another file."""
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
        raise SystemExit(message)


def _rewrite_auth_section(text: str, enabled: str, token_hash: str) -> str:
    """Set both auth keys, adding the section or the keys when they are absent."""
    if "[auth]" not in text:
        return text.rstrip() + f'\n\n[auth]\nenabled = {enabled}\nadmin_api_token_hash = "{token_hash}"\n'
    section = re.compile(r"(?ms)(^\[auth\]\s*$)(.*?)(?=^\[|\Z)")
    match = section.search(text)
    if not match:
        raise SystemExit("invalid auth section")
    body = match.group(2)
    # [ \t] rather than \s: \s matches newlines, so a file whose [auth] header is
    # followed directly by the key had the separating newline eaten and became
    # "[auth]enabled = true" - invalid TOML. The shipped example has comments in
    # between, which hid it; an operator who trims them would not be so lucky.
    body = re.sub(r"(?m)^[ \t]*enabled[ \t]*=.*$", f"enabled = {enabled}", body, count=1)
    body = re.sub(
        r"(?m)^[ \t]*admin_api_token_hash[ \t]*=.*$",
        f'admin_api_token_hash = "{token_hash}"',
        body,
        count=1,
    )
    if "enabled =" not in body:
        body = f"\nenabled = {enabled}" + body
    if "admin_api_token_hash" not in body:
        body += f'\nadmin_api_token_hash = "{token_hash}"'
    return text[: match.start(2)] + body + text[match.end(2) :]


def configure_admin_auth(path: Path, *, enabled: str, token_hash: str) -> None:
    """Write the auth section of astinuslab.toml, creating the file if needed."""
    parent, name = os.path.split(path)
    directory_fd = os.open(parent, DIRECTORY_FLAGS)
    parent_device = os.fstat(directory_fd).st_dev
    # The descriptor and the stat that authorised it travel together, so the
    # rewrite below cannot check one file's identity and write to another.
    existing: tuple[int, os.stat_result] | None
    try:
        descriptor = os.open(name, WRITE_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        existing = None
        text = ""
    else:
        original = os.fstat(descriptor)
        if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1 or original.st_dev != parent_device:
            raise SystemExit("admin config is non-regular, multiply linked, or crosses a filesystem boundary")
        existing = (descriptor, original)
        with os.fdopen(os.dup(descriptor), encoding="utf-8") as handle:
            text = handle.read()

    payload = _rewrite_auth_section(text, enabled, token_hash).encode()
    try:
        if existing is not None:
            descriptor, original = existing
            _assert_same_file(directory_fd, name, original, "admin config changed during update")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            _write_all(descriptor, payload)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            _assert_same_file(directory_fd, name, original, "admin config changed during update")
        else:
            _create_admin_config(directory_fd, name, payload)
        os.fsync(directory_fd)
    finally:
        if existing is not None:
            os.close(existing[0])
        os.close(directory_fd)


def _create_admin_config(directory_fd: int, name: str, payload: bytes) -> None:
    """Create the file through a temporary, so no reader sees a partial config."""
    temporary = f".{name}.tmp.{secrets.token_hex(12)}"
    temporary_fd = os.open(temporary, CREATE_FLAGS, 0o600, dir_fd=directory_fd)
    try:
        _write_all(temporary_fd, payload)
        os.fchmod(temporary_fd, 0o600)
        os.fsync(temporary_fd)
        os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        os.unlink(temporary, dir_fd=directory_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(temporary_fd)


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit("usage: managed_config.py ACTION ...")
    action, arguments = argv[0], argv[1:]
    if action == "capture":
        capture(Path(arguments[0]), Path(arguments[1]))
    elif action == "restore":
        restore(Path(arguments[0]), Path(arguments[1]))
    elif action == "install-config":
        print(install_config_if_missing(Path(arguments[0]), Path(arguments[1])))
    elif action == "validate-destinations":
        validate_destinations(Path(arguments[0]), arguments[1:])
    elif action == "neutralise-env-auth":
        neutralise_env_auth_overrides(Path(arguments[0]))
    elif action == "configure-admin-auth":
        configure_admin_auth(
            Path(os.path.abspath(os.environ["AUTH_CONFIG_PATH"])),
            enabled=os.environ["AUTH_ENABLED"],
            token_hash=os.environ["AUTH_TOKEN_HASH"],
        )
    else:
        raise SystemExit(f"unknown managed_config.py action: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
