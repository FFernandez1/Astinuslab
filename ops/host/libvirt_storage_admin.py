#!/usr/bin/env python3
"""Descriptor-safe filesystem operations for the privileged storage wrapper."""

from __future__ import annotations

import hashlib
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

_INSTALL_ROOT = "@@INSTALL_DIR@@"
_INSTALL_DEV = "@@INSTALL_DEV@@"
_INSTALL_INO = "@@INSTALL_INO@@"
_LIBVIRT_ROOT = "@@LIBVIRT_IMAGE_ROOT@@"


def deny(message: str) -> NoReturn:
    raise SystemExit(f"DENY: {message}")


def _configured_install() -> tuple[str, tuple[int, int]]:
    if _INSTALL_ROOT.startswith("@"):
        root = str(Path(__file__).resolve().parents[2])
        info = os.stat(root, follow_symlinks=False)
        return root, (info.st_dev, info.st_ino)
    try:
        return _INSTALL_ROOT, (int(_INSTALL_DEV), int(_INSTALL_INO))
    except ValueError:
        deny("installation root identity was not bound by the root installer")


def _configured_libvirt_root() -> str:
    if _LIBVIRT_ROOT.startswith("@"):
        return "/var/lib/libvirt/images/astinuslab"
    return _LIBVIRT_ROOT


def _open_component(name: str, parent_fd: int) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        deny(f"cannot open directory without following symlinks: {exc}")


def _open_absolute_directory(
    path: str,
    *,
    expected_identity: tuple[int, int] | None = None,
    create_final: bool = False,
    label: str = "configured root",
) -> int:
    if not os.path.isabs(path):
        deny(f"{label} must be absolute")
    parts = [part for part in os.path.normpath(path).split(os.sep) if part]
    current_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parts):
            try:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                if not create_final or index != len(parts) - 1:
                    # Every other refusal in this script names what it refused.
                    # A root bound at install time whose tree was later renamed
                    # or removed has to as well: re-raising leaves the operator
                    # a stack trace whose only clue is a bare path component.
                    deny(f"{label} {path} is missing at {part!r}")
                os.mkdir(part, mode=0o755, dir_fd=current_fd)
                child_fd = _open_component(part, current_fd)
            os.close(current_fd)
            current_fd = child_fd
        if expected_identity is not None:
            info = os.fstat(current_fd)
            if (info.st_dev, info.st_ino) != expected_identity:
                deny(f"{label} {path} identity changed after helper installation")
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _parts_beneath(path: str, root: str, *, allow_root: bool = False) -> list[str]:
    if not os.path.isabs(path):
        deny("path must be absolute")
    normalized_root = os.path.normpath(root)
    normalized_path = os.path.normpath(path)
    try:
        if os.path.commonpath((normalized_root, normalized_path)) != normalized_root:
            deny("path is outside its installed allowed root")
    except ValueError:
        deny("path is outside its installed allowed root")
    relative = os.path.relpath(normalized_path, normalized_root)
    parts = [] if relative == "." else relative.split(os.sep)
    if (not allow_root and not parts) or len(parts) > 50 or any(part in {"", ".", ".."} for part in parts):
        deny("path has an unsafe relative form")
    return parts


def _safe_leaf(value: str, *, role: str) -> str:
    if ".." in value or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", value) is None:
        deny(f"{role} has an unsafe name")
    return value


def _base_directory_parts(path: str, root: str) -> list[str]:
    parts = _parts_beneath(path, root)
    if parts != ["base"]:
        deny("base image directory must be the installed shared base directory")
    return parts


def _base_file_parts(path: str, root: str) -> list[str]:
    parts = _parts_beneath(path, root)
    if len(parts) != 2 or parts[0] != "base":
        deny("base image must be a direct child of the installed shared base directory")
    _safe_leaf(parts[1], role="base image")
    return parts


def _job_directory_parts(path: str, root: str) -> list[str]:
    parts = _parts_beneath(path, root)
    if len(parts) != 1 or parts[0] == "base":
        deny("job directory must be a direct non-base child of the installed libvirt root")
    _safe_leaf(parts[0], role="job directory")
    return parts


def _job_file_parts(path: str, root: str) -> list[str]:
    parts = _parts_beneath(path, root)
    if len(parts) != 2 or parts[0] == "base":
        deny("job artifact must be a direct child of a safe job directory")
    _safe_leaf(parts[0], role="job directory")
    _safe_leaf(parts[1], role="job artifact")
    return parts


def _open_directory(root_fd: int, parts: list[str], *, create: bool = False) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                child_fd = _open_component(part, current_fd)
            except SystemExit:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
                child_fd = _open_component(part, current_fd)
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_parent(root_fd: int, parts: list[str], *, create: bool = False) -> tuple[int, str]:
    if not parts:
        deny("operation requires a path below its allowed root")
    return _open_directory(root_fd, parts[:-1], create=create), parts[-1]


def _try_open_regular(parent_fd: int, name: str, flags: int = os.O_RDONLY, mode: int = 0o600) -> int | None:
    try:
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        deny(f"cannot open regular file without following symlinks: {exc}")
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        deny("path is not a regular file")
    return descriptor


def _open_regular(parent_fd: int, name: str, flags: int = os.O_RDONLY, mode: int = 0o600) -> int:
    descriptor = _try_open_regular(parent_fd, name, flags, mode)
    if descriptor is None:
        deny("regular file does not exist")
    return descriptor


def _copy(source_fd: int, destination_fd: int) -> None:
    while chunk := os.read(source_fd, 1024 * 1024):
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            view = view[written:]


def _digest(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _unlink_if_same(parent_fd: int, name: str, descriptor: int) -> None:
    expected = os.fstat(descriptor)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
        os.unlink(name, dir_fd=parent_fd)


def _caller_uid() -> int | None:
    caller = os.environ.get("SUDO_USER", "")
    if not caller:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", caller):
        deny("invalid sudo caller user")
    try:
        return pwd.getpwnam(caller).pw_uid
    except KeyError:
        deny("sudo caller user does not exist")


def _ensure_dirs(libvirt_fd: int, root: str, arguments: list[str]) -> None:
    if len(arguments) != 2:
        deny("ensure_dirs requires base_image_dir and job_dir")
    base_parts = _base_directory_parts(arguments[0], root)
    job_parts = _job_directory_parts(arguments[1], root)
    base_fd = _open_directory(libvirt_fd, base_parts, create=True)
    os.close(base_fd)
    job_fd = _open_directory(libvirt_fd, job_parts, create=True)
    try:
        caller_uid = _caller_uid()
        if caller_uid is not None:
            os.fchown(job_fd, caller_uid, -1)
    finally:
        os.close(job_fd)


def _install_base_image(
    install_fd: int,
    install_root: str,
    libvirt_fd: int,
    libvirt_root: str,
    arguments: list[str],
) -> None:
    if len(arguments) != 3:
        deny("install_base_image requires allowed_source_prefix, source, and destination")
    allowed_prefix, source, destination = arguments
    _parts_beneath(allowed_prefix, install_root, allow_root=True)
    source_parts = _parts_beneath(source, install_root)
    prefix_path = os.path.normpath(allowed_prefix)
    if os.path.commonpath((prefix_path, os.path.normpath(source))) != prefix_path:
        deny("source is outside allowed source prefix")
    destination_parts = _base_file_parts(destination, libvirt_root)

    source_parent, source_name = _open_parent(install_fd, source_parts)
    destination_parent, destination_name = _open_parent(libvirt_fd, destination_parts, create=True)
    source_fd = -1
    destination_fd = -1
    try:
        source_fd = _open_regular(source_parent, source_name)
        existing_destination_fd = _try_open_regular(destination_parent, destination_name)
        destination_fd = -1 if existing_destination_fd is None else existing_destination_fd
        if destination_fd >= 0:
            if _digest(source_fd) != _digest(destination_fd):
                deny("installed base image conflicts with the requested source")
            os.fchmod(destination_fd, 0o644)
            print(f"install_base_image: {destination_name} already installed, verified")
            return
        try:
            destination_fd = os.open(
                destination_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=destination_parent,
            )
        except OSError as exc:
            deny(f"cannot create base image without clobbering: {exc}")
        _copy(source_fd, destination_fd)
        os.fchmod(destination_fd, 0o644)
        os.fsync(destination_fd)
        os.fsync(destination_parent)
    finally:
        for descriptor in (source_fd, destination_fd, source_parent, destination_parent):
            if descriptor >= 0:
                os.close(descriptor)


def _chmod_artifacts(libvirt_fd: int, root: str, arguments: list[str]) -> None:
    if len(arguments) != 2:
        deny("finalize_job_artifacts requires job_image and seed_iso")
    for path in arguments:
        parts = _job_file_parts(path, root)
        parent_fd, name = _open_parent(libvirt_fd, parts)
        descriptor = _open_regular(parent_fd, name)
        try:
            os.fchmod(descriptor, 0o644)
        finally:
            os.close(descriptor)
            os.close(parent_fd)


def _qemu_output(parent_fd: int, name: str, command: list[str], pass_fds: tuple[int, ...]) -> None:
    try:
        output_fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        deny(f"cannot create image output without clobbering: {exc}")
    try:
        subprocess.run([*command, f"/proc/self/fd/{output_fd}"], check=True, pass_fds=(*pass_fds, output_fd))
        os.fchmod(output_fd, 0o644)
        os.fsync(output_fd)
        os.fsync(parent_fd)
    except BaseException:
        _unlink_if_same(parent_fd, name, output_fd)
        raise
    finally:
        os.close(output_fd)


def _create_overlay(libvirt_fd: int, root: str, arguments: list[str]) -> None:
    if len(arguments) != 3 or not re.fullmatch(r"[1-9][0-9]{0,3}", arguments[2]):
        deny("create_job_overlay requires backing_image, job_image, and a safe size_gb")
    backing, overlay, size = arguments
    backing_parts = _base_file_parts(backing, root)
    overlay_parts = _job_file_parts(overlay, root)
    backing_parent, backing_name = _open_parent(libvirt_fd, backing_parts)
    overlay_parent, overlay_name = _open_parent(libvirt_fd, overlay_parts, create=True)
    backing_fd = _open_regular(backing_parent, backing_name)
    try:
        command = ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", os.path.normpath(backing), "-o"]
        command.append(f"size={size}G")
        for attempt in range(3):
            try:
                _qemu_output(overlay_parent, overlay_name, command, (backing_fd,))
                return
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
                time.sleep(1)
    finally:
        os.close(backing_fd)
        os.close(backing_parent)
        os.close(overlay_parent)


def _flatten(
    install_fd: int,
    install_root: str,
    libvirt_fd: int,
    libvirt_root: str,
    arguments: list[str],
) -> None:
    if len(arguments) != 2:
        deny("flatten_image requires source_overlay and destination")
    source_parts = _job_file_parts(arguments[0], libvirt_root)
    source_parent, source_name = _open_parent(libvirt_fd, source_parts)
    source_fd = _open_regular(source_parent, source_name)
    definitions_root = os.path.join(install_root, "images", "definitions")
    close_destination_root = False
    if os.path.commonpath((os.path.normpath(arguments[1]), libvirt_root)) == libvirt_root:
        destination_root_fd = libvirt_fd
        destination_root = libvirt_root
    else:
        definitions_parts = _parts_beneath(definitions_root, install_root)
        destination_root_fd = _open_directory(install_fd, definitions_parts, create=True)
        close_destination_root = True
        destination_root = definitions_root
    destination_parts = _parts_beneath(arguments[1], destination_root)
    destination_parent, destination_name = _open_parent(destination_root_fd, destination_parts, create=True)
    try:
        _qemu_output(
            destination_parent,
            destination_name,
            ["qemu-img", "convert", "-f", "qcow2", "-O", "qcow2", f"/proc/self/fd/{source_fd}"],
            (source_fd,),
        )
    finally:
        os.close(source_fd)
        os.close(source_parent)
        os.close(destination_parent)
        if close_destination_root:
            os.close(destination_root_fd)


def _remove_tree(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_component(name, directory_fd)
            try:
                _remove_tree(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _clear_storage_root(libvirt_fd: int, arguments: list[str]) -> None:
    if arguments:
        deny("clear_storage_root does not accept arguments")
    _remove_tree(libvirt_fd)
    os.fsync(libvirt_fd)


def _remove_job_dir(libvirt_fd: int, root: str, arguments: list[str]) -> None:
    if len(arguments) != 1:
        deny("remove_job_dir requires job_dir")
    parts = _job_directory_parts(arguments[0], root)
    parent_fd, name = _open_parent(libvirt_fd, parts)
    try:
        try:
            job_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            deny(f"cannot open job directory without following symlinks: {exc}")
        try:
            _remove_tree(job_fd)
        finally:
            os.close(job_fd)
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _validate_arguments(subcommand: str, arguments: list[str], install_root: str, libvirt_root: str) -> None:
    if subcommand == "install_base_image" and len(arguments) == 3:
        allowed_prefix, source, destination = map(os.path.normpath, arguments)
        try:
            _parts_beneath(allowed_prefix, install_root, allow_root=True)
        except SystemExit:
            deny("allowed source prefix is outside the installed application root")
        try:
            if os.path.commonpath((allowed_prefix, source)) != allowed_prefix:
                deny("source is outside allowed source prefix")
        except ValueError:
            deny("source is outside allowed source prefix")
        try:
            _parts_beneath(destination, libvirt_root)
        except SystemExit:
            deny("destination is outside allowed prefix")
    elif subcommand == "flatten_image" and len(arguments) == 2:
        destination = os.path.normpath(arguments[1])
        definitions_root = os.path.join(install_root, "images", "definitions")
        destination_allowed = False
        for root in (libvirt_root, definitions_root):
            try:
                _parts_beneath(destination, root)
            except SystemExit:
                continue
            destination_allowed = True
            break
        if not destination_allowed:
            deny("destination is outside allowed prefixes")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        deny("missing storage subcommand")
    subcommand, arguments = argv[1], argv[2:]
    install_root, install_identity = _configured_install()
    libvirt_root = _configured_libvirt_root()
    install_fd = _open_absolute_directory(
        install_root,
        expected_identity=install_identity,
        label="installation root",
    )
    _validate_arguments(subcommand, arguments, install_root, libvirt_root)
    libvirt_fd = _open_absolute_directory(libvirt_root, create_final=True, label="libvirt image root")
    try:
        if subcommand == "ensure_dirs":
            _ensure_dirs(libvirt_fd, libvirt_root, arguments)
        elif subcommand == "install_base_image":
            _install_base_image(install_fd, install_root, libvirt_fd, libvirt_root, arguments)
        elif subcommand == "finalize_job_artifacts":
            _chmod_artifacts(libvirt_fd, libvirt_root, arguments)
        elif subcommand == "create_job_overlay":
            _create_overlay(libvirt_fd, libvirt_root, arguments)
        elif subcommand == "flatten_image":
            _flatten(install_fd, install_root, libvirt_fd, libvirt_root, arguments)
        elif subcommand == "remove_job_dir":
            _remove_job_dir(libvirt_fd, libvirt_root, arguments)
        elif subcommand == "clear_storage_root":
            _clear_storage_root(libvirt_fd, arguments)
        else:
            deny(f"unsupported storage subcommand: {subcommand}")
        return 0
    finally:
        os.close(libvirt_fd)
        os.close(install_fd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
