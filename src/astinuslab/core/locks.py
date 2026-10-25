"""Capture locks for single-host runner flows.

Correctness assumes one worker process on one host: flock handles cross-thread
and crash cleanup on the host, while in-process locks cover same-process races.
Multiple workers/hosts require distributed locks plus broker redesign.
"""

from __future__ import annotations

import contextlib
import fcntl
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

_capture_locks: dict[tuple[Path, str], threading.Lock] = {}
_capture_locks_guard = threading.Lock()


class CaptureLockBusy(RuntimeError):
    """Raised when a non-blocking capture lock cannot be acquired."""


class CaptureLockHandle:
    """Combined in-process + on-disk lock handle."""

    def __init__(self, *, lock_key: tuple[Path, str], thread_lock: threading.Lock, file_handle: TextIO) -> None:
        self.lock_key = lock_key
        self.thread_lock = thread_lock
        self.file_handle = file_handle


def _safe_lock_name(vm_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(vm_id))


def capture_lock_path(vm_id: str, *, base_dir: Path) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / f"{_safe_lock_name(vm_id)}.lock"


def capture_lock_dir(settings: Any, prefix: str) -> Path:
    """Resolve the on-disk directory for capture lock files."""
    if prefix == "playground":
        storage_dir = getattr(settings, "storage_dir", None)
        if storage_dir is None:
            return Path(settings.playground_capture_dir).parent / "tmp" / "playground-capture-locks"
        return Path(storage_dir) / "tmp" / "playground-capture-locks"

    from ..virtualization.host_helpers import storage_tmp_dir

    return storage_tmp_dir(settings) / f"{prefix}-capture-locks"


def acquire_capture_lock(vm_id: str, *, base_dir: Path) -> CaptureLockHandle | None:
    resolved_base = Path(base_dir).resolve()
    lock_key = (resolved_base, vm_id)
    with _capture_locks_guard:
        thread_lock = _capture_locks.setdefault(lock_key, threading.Lock())
    if not thread_lock.acquire(blocking=False):
        return None

    file_handle: TextIO | None = None
    try:
        file_handle = capture_lock_path(vm_id, base_dir=resolved_base).open("w")
        try:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file_handle.close()
            thread_lock.release()
            return None
    except OSError:
        if file_handle is not None:
            file_handle.close()
        thread_lock.release()
        raise
    return CaptureLockHandle(lock_key=lock_key, thread_lock=thread_lock, file_handle=file_handle)


def release_capture_lock(handle: CaptureLockHandle) -> None:
    try:
        fcntl.flock(handle.file_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.file_handle.close()
        try:
            handle.thread_lock.release()
        except RuntimeError:
            pass


@contextlib.contextmanager
def capture_lock(vm_id: str, *, base_dir: Path) -> Iterator[None]:
    handle = acquire_capture_lock(vm_id, base_dir=base_dir)
    if handle is None:
        raise CaptureLockBusy(f"capture already in progress for {vm_id}")
    try:
        yield
    finally:
        release_capture_lock(handle)


def clear_in_process_capture_locks() -> None:
    """Test helper: simulate a separate process while file locks remain held."""
    with _capture_locks_guard:
        _capture_locks.clear()


@contextlib.contextmanager
def blocking_file_lock(lock_path: Path) -> Iterator[None]:
    """Blocking exclusive flock on *lock_path* (file created if missing).

    Unlike capture_lock this WAITS for the holder instead of raising.
    Linux flock conflicts between separate open file descriptions even
    within one process, so this serializes both threads and processes.
    Use it for same-job work that must not interleave (Vol3 staging+runs).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
