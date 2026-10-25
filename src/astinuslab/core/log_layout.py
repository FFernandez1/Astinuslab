"""Public log identifiers and descriptor-bound access to installed role logs."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

SERVICE_LOG_FILES = {"api": "api.log", "runner": "runner.log", "analysis": "analysis-worker.log"}
LOG_FILE_NAMES = tuple(SERVICE_LOG_FILES.values())
MARKER = ".astinuslab-service-logs.json"
SCHEMA = "astinuslab-service-logs/v1"
# Analysis can traverse the anchor without enumerating runner diagnostics.
_DIRECTORY_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_directory(path: Path) -> int:
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise ValueError("log directory contains a parent traversal")
    descriptor = os.open(absolute.anchor, _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_layout(directory_fd: int) -> dict[str, int] | None:
    try:
        descriptor = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        anchor = os.fstat(directory_fd)
        info = os.fstat(descriptor)
        if (
            anchor.st_uid != 0
            or not anchor.st_mode & stat.S_ISVTX
            or anchor.st_mode & 0o007
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o644
            or info.st_size > 4096
        ):
            raise ValueError("installed log layout has unsafe custody")
        payload = json.loads(os.read(descriptor, 4097))
        roles = payload.get("roles") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "roles"}
            or payload["schema"] != SCHEMA
            or not isinstance(roles, dict)
            or set(roles) != set(SERVICE_LOG_FILES)
            or any(type(uid) is not int or uid <= 0 for uid in roles.values())
            or len(set(roles.values())) != len(roles)
        ):
            raise ValueError("installed log layout is invalid")
        return roles
    finally:
        os.close(descriptor)


def read_layout(storage: Path) -> dict[str, int] | None:
    try:
        descriptor = _open_directory(Path(storage) / "logs")
    except FileNotFoundError:
        return None
    try:
        return _read_layout(descriptor)
    finally:
        os.close(descriptor)


def _selection(name: str, roles: dict[str, int] | None) -> tuple[str | None, str]:
    if name in LOG_FILE_NAMES:
        role = next(role for role, filename in SERVICE_LOG_FILES.items() if filename == name)
        return (role if roles is not None else None), name
    if roles is not None and name.startswith("legacy-") and name.removeprefix("legacy-") in LOG_FILE_NAMES:
        return None, name.removeprefix("legacy-")
    raise ValueError("unknown log identifier")


def log_path(storage: Path, name: str) -> Path:
    role, filename = _selection(name, read_layout(storage))
    root = Path(storage) / "logs"
    return root / role / filename if role else root / filename


@contextmanager
def open_log(storage: Path, name: str, *, writable: bool = False, create_flat: bool = False) -> Iterator[BinaryIO]:
    """Open an existing selected file; never follow a service-planted link."""
    directory_fd = _open_directory(Path(storage) / "logs")
    try:
        roles = _read_layout(directory_fd)
        role, filename = _selection(name, roles)
        if writable and name.startswith("legacy-"):
            raise ValueError("retained log history is read-only")
        if role is not None:
            if roles is None:
                raise ValueError("missing installed role policy")
            child = os.open(role, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
            if os.fstat(directory_fd).st_uid != roles[role]:
                raise ValueError("log role directory has unexpected custody")
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if create_flat and writable and roles is None:
            flags |= os.O_CREAT
        descriptor = os.open(filename, flags, 0o600, dir_fd=directory_fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("log is not a single-link regular file")
            if role is not None and roles is not None and info.st_uid != roles[role]:
                raise ValueError("log file has unexpected custody")
            with os.fdopen(descriptor, "r+b" if writable else "rb", closefd=False) as stream:
                yield stream
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def available_log_names(storage: Path) -> tuple[str, ...]:
    names = list(LOG_FILE_NAMES)
    if read_layout(storage) is not None:
        for filename in LOG_FILE_NAMES:
            alias = f"legacy-{filename}"
            try:
                with open_log(storage, alias):
                    names.append(alias)
            except (OSError, ValueError):
                continue
    return tuple(names)
