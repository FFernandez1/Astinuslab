from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Literal

from ..virtualization.host_helpers import libvirt_storage_admin_helper

STORAGE_ADMIN_SUBPROCESS_TIMEOUT_SECONDS = 30
_LIBVIRT_NETWORK_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PROTECTED_TMP_INFRASTRUCTURE = frozenset(
    {
        "package-instance-locks",
        "package-instance-reservation-locks",
    }
)


def _timeout_message(command: str) -> str:
    return f"Timed out while running {command}."


_RemovalOutcome = Literal["deleted", "protected", "changed", "missing"]


def _remove_path_under_root(
    path: Path,
    root: Path,
    *,
    protected_tmp_root: Path | None = None,
    recreate_directory: bool = False,
) -> _RemovalOutcome:
    """Remove one lexical root child, keeping recreation on the same descriptor."""

    safe_root = root.resolve()
    if path.parent.resolve() != safe_root:
        raise ValueError(f"refusing to delete outside storage root: {path}")
    name = _require_tmp_entry_name(path.name)
    if recreate_directory:
        safe_root.mkdir(parents=True, exist_ok=True)
    opened = _open_cleanup_root(safe_root, "storage")
    if opened is None:
        return "missing"
    _, root_fd = opened
    try:
        outcome = _remove_entry(root_fd, safe_root, name, protected_tmp_root=protected_tmp_root)
        if recreate_directory and outcome in {"deleted", "missing"}:
            try:
                os.mkdir(name, dir_fd=root_fd)
            except FileExistsError:
                # Another entry won the name. Never follow it, even if it
                # currently appears to be a directory.
                return "changed"
        return outcome
    finally:
        os.close(root_fd)


def _is_protected_tmp_target(path: Path, tmp_root: Path) -> bool:
    """Refuse lock infrastructure, descendants, and containing ancestors."""

    path = path.resolve()
    tmp_root = tmp_root.resolve()
    for name in _PROTECTED_TMP_INFRASTRUCTURE:
        protected = (tmp_root / name).resolve()
        if path == protected or path.is_relative_to(protected) or protected.is_relative_to(path):
            return True
    return False


def _require_tmp_entry_name(name: object) -> str:
    """Accept only one lexical child of an already-open temporary root."""

    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or Path(name).is_absolute()
        or Path(name).parts != (name,)
    ):
        raise ValueError(f"refusing to delete non-top-level temporary entry: {name!r}")
    return name


def _open_tmp_root(tmp_root: Path, storage_root: Path) -> tuple[Path, int] | None:
    """Open the canonical temporary root without following a last-minute alias."""

    safe_storage_root = storage_root.resolve()
    if tmp_root.name != "tmp" or tmp_root.parent.resolve() != safe_storage_root:
        raise ValueError(f"refusing to delete outside storage root: {tmp_root}")
    safe_tmp_root = safe_storage_root / "tmp"
    return _open_cleanup_root(safe_tmp_root, "temporary")


def _open_cleanup_root(safe_root: Path, description: str) -> tuple[Path, int] | None:
    """Anchor cleanup to an opened, inode-validated directory."""

    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(safe_root, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"refusing to delete unsafe {description} root: {safe_root}") from exc

    try:
        opened = os.fstat(root_fd)
        current = os.stat(safe_root, follow_symlinks=False)
        if not os.path.samestat(opened, current) or not stat.S_ISDIR(current.st_mode):
            raise ValueError(f"refusing to delete changed {description} root: {safe_root}")
    except FileNotFoundError:
        os.close(root_fd)
        return None
    except BaseException:
        os.close(root_fd)
        raise
    return safe_root, root_fd


def _same_directory_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return os.path.samestat(left, right) and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)


def _remove_tmp_entry(root_fd: int, tmp_root: Path, raw_name: object) -> Literal["deleted", "protected", "changed"]:
    """Preserve the temporary-entry caller contract around shared deletion."""

    outcome = _remove_entry(root_fd, tmp_root, raw_name, protected_tmp_root=tmp_root)
    return "changed" if outcome == "missing" else outcome


def _remove_entry(
    root_fd: int,
    entry_root: Path,
    raw_name: object,
    *,
    protected_tmp_root: Path | None,
) -> _RemovalOutcome:
    """Delete one direct child without following it or an inode replacement.

    The caller owns a stable descriptor for ``entry_root``. A non-deleted
    outcome lets callers report safety skips rather than claiming deletion.
    """

    name = _require_tmp_entry_name(raw_name)
    try:
        observed = os.lstat(name, dir_fd=root_fd)
    except FileNotFoundError:
        return "missing"

    entry_path = entry_root / name
    try:
        if protected_tmp_root is not None and _is_protected_tmp_target(entry_path, protected_tmp_root):
            return "protected"
    except (OSError, RuntimeError):
        return "changed"

    try:
        current = os.lstat(name, dir_fd=root_fd)
    except FileNotFoundError:
        return "changed"
    if not _same_directory_entry(observed, current):
        return "changed"

    if stat.S_ISDIR(current.st_mode):
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            raise RuntimeError("safe temporary directory removal is unavailable")
        try:
            shutil.rmtree(name, dir_fd=root_fd)
        except FileNotFoundError:
            return "changed"
        except OSError:
            try:
                replacement = os.lstat(name, dir_fd=root_fd)
            except FileNotFoundError:
                return "changed"
            if not _same_directory_entry(current, replacement):
                return "changed"
            raise
        return "deleted"

    try:
        replacement = os.lstat(name, dir_fd=root_fd)
    except FileNotFoundError:
        return "changed"
    if not _same_directory_entry(current, replacement):
        return "changed"
    try:
        os.unlink(name, dir_fd=root_fd)
    except FileNotFoundError:
        return "changed"
    return "deleted"


def _is_valid_libvirt_network_name(network: str) -> bool:
    return bool(_LIBVIRT_NETWORK_NAME_RE.fullmatch(network))


def storage_stats(settings) -> dict:
    def _dir_stats(path: Path) -> tuple[int, float]:
        if not path.exists():
            return 0, 0.0
        count = 0
        total_bytes = 0
        try:
            for f in path.rglob("*"):
                try:
                    if f.is_file():
                        count += 1
                        total_bytes += f.stat().st_size
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            pass
        return count, total_bytes / (1024 * 1024)

    dumps_dir = settings.storage_dir / "dumps"
    logs_dir = settings.storage_dir / "logs"
    cache_dir = settings.storage_dir / "workload-cache"
    symbols_dir = getattr(settings, "vol3_symbols_path", None)

    dc, dm = _dir_stats(dumps_dir)
    lc, lm = _dir_stats(logs_dir)
    cc, cm = _dir_stats(cache_dir)
    sc, sm = _dir_stats(Path(symbols_dir)) if symbols_dir else (0, 0.0)

    storage_dir = settings.storage_dir
    storage_dir.mkdir(parents=True, exist_ok=True)
    try:
        free_gb = shutil.disk_usage(storage_dir).free / (1024**3)
    except Exception:
        free_gb = 0.0

    return {
        "dumps_count": dc,
        "dumps_mb": dm,
        "logs_count": lc,
        "logs_mb": lm,
        "workload_cache_count": cc,
        "workload_cache_mb": cm,
        "symbols_count": sc,
        "symbols_mb": sm,
        "free_gb": free_gb,
    }


def clear_vol3_symbol_cache(settings) -> dict:
    """Delete the contents of the Volatility 3 symbol/ISF cache, keeping the dir.

    Regenerable: the next analysis rebuilds whatever symbol tables it needs.
    """
    root = Path(settings.vol3_symbols_path)
    if not root.exists():
        return {"action": "clear_symbols", "ok": True, "message": "Symbol cache already empty.", "removed": 0}
    removed = 0
    skipped = 0
    for child in list(root.iterdir()):
        try:
            outcome = _remove_path_under_root(child, root)
        except (ValueError, OSError, RuntimeError) as exc:
            return {"action": "clear_symbols", "ok": False, "message": str(exc), "removed": removed, "skipped": skipped}
        if outcome == "deleted":
            removed += 1
        elif outcome != "missing":
            skipped += 1
    return {
        "action": "clear_symbols",
        "ok": True,
        "message": f"Symbol cache cleared: {removed} entries. Kept for safety: {skipped}.",
        "removed": removed,
        "skipped": skipped,
    }


def list_tmp_entries(session_scope, repo_cls, settings) -> list[dict]:
    tmp_root = settings.storage_dir / "tmp"
    if not tmp_root.exists():
        return []

    with session_scope() as session:
        repo = repo_cls(session)
        entries: list[dict] = []
        try:
            for path in sorted(tmp_root.iterdir(), key=lambda p: p.name):
                size_bytes = 0
                try:
                    if path.is_dir():
                        for child in path.rglob("*"):
                            try:
                                if child.is_file():
                                    size_bytes += child.stat().st_size
                            except (OSError, PermissionError):
                                continue
                    elif path.is_file():
                        size_bytes = path.stat().st_size
                except (OSError, PermissionError):
                    pass

                removable = True
                reason = "aux"
                if _is_protected_tmp_target(path, tmp_root):
                    removable = False
                    reason = "lock_infrastructure"
                elif path.name.startswith("job-"):
                    try:
                        job = repo.get_job(path.name)
                        if job is not None:
                            removable = False
                            reason = f"job_exists:{job.status}"
                        else:
                            reason = "orphan_job_tmp"
                    except Exception:
                        reason = "lookup_failed"

                entries.append(
                    {
                        "name": path.name,
                        "is_dir": path.is_dir(),
                        "size_mb": size_bytes / (1024 * 1024),
                        "removable": removable,
                        "reason": reason,
                    }
                )
        except (OSError, PermissionError):
            pass
    return entries


def has_active_jobs(session_scope, repo_cls, active_statuses) -> bool:
    with session_scope() as session:
        repo = repo_cls(session)
        return any(job.status in active_statuses for job in repo.list_jobs())


def stop_pid_from_file(pidfile: Path) -> bool:
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pidfile.unlink(missing_ok=True)
        return False

    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        return False
    except PermissionError:
        return False

    return True


def schedule_stop_services(settings, stop_pid_from_file_fn) -> dict:
    api_pid = settings.storage_dir / "api.pid"
    runner_pid = settings.storage_dir / "runner.pid"

    def _stop() -> None:
        stop_pid_from_file_fn(runner_pid)
        stop_pid_from_file_fn(api_pid)

    threading.Timer(0.5, _stop).start()
    return {"action": "stop_services", "ok": True, "message": "API and runner shutdown scheduled."}


def destroy_lab_vms_and_disks(settings) -> dict:
    uri = settings.libvirt_uri
    vm_prefix = "astinuslab-"
    result = {"action": "destroy_vms", "ok": True, "message": "", "details": []}

    try:
        listed = subprocess.run(
            ["virsh", "-c", uri, "list", "--all", "--name"],
            text=True,
            capture_output=True,
            timeout=STORAGE_ADMIN_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"action": "destroy_vms", "ok": False, "message": _timeout_message("virsh list")}
    if listed.returncode != 0:
        return {"action": "destroy_vms", "ok": False, "message": (listed.stderr or listed.stdout or "").strip()}

    for vm_name in [line.strip() for line in listed.stdout.splitlines() if line.strip().startswith(vm_prefix)]:
        try:
            subprocess.run(
                ["virsh", "-c", uri, "destroy", vm_name],
                capture_output=True,
                text=True,
                timeout=STORAGE_ADMIN_SUBPROCESS_TIMEOUT_SECONDS,
            )
            undef = subprocess.run(
                ["virsh", "-c", uri, "undefine", vm_name, "--nvram"],
                capture_output=True,
                text=True,
                timeout=STORAGE_ADMIN_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            result["ok"] = False
            result["message"] = _timeout_message(f"virsh remove {vm_name}")
            return result
        if undef.returncode != 0:
            try:
                subprocess.run(
                    ["virsh", "-c", uri, "undefine", vm_name],
                    capture_output=True,
                    text=True,
                    timeout=STORAGE_ADMIN_SUBPROCESS_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                result["ok"] = False
                result["message"] = _timeout_message(f"virsh undefine {vm_name}")
                return result
        result["details"].append(f"VM removed: {vm_name}")  # type: ignore[attr-defined]

    helper = libvirt_storage_admin_helper()
    try:
        removed = subprocess.run(
            ["sudo", "-n", str(helper), "clear_storage_root"],
            text=True,
            capture_output=True,
            timeout=STORAGE_ADMIN_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        result["ok"] = False
        result["message"] = _timeout_message("storage helper clear_storage_root")
        return result
    if removed.returncode != 0:
        result["ok"] = False
        result["message"] = (removed.stderr or removed.stdout or "").strip()
        return result

    result["message"] = "Lab VMs/disks removed."
    return result


def reset_libvirt_network(settings) -> dict:
    uri = settings.libvirt_uri
    network = settings.libvirt_network
    if not _is_valid_libvirt_network_name(network):
        return {"action": "reset_network", "ok": False, "message": f"Invalid libvirt network name: {network}"}
    leases = Path(f"/var/lib/libvirt/dnsmasq/{network}.leases")
    status = Path(f"/var/lib/libvirt/dnsmasq/{network}.status")
    for path in (leases, status):
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            pass

    for command in ("net-destroy", "net-start", "net-autostart"):
        try:
            proc = subprocess.run(
                ["virsh", "-c", uri, command, network],
                text=True,
                capture_output=True,
                timeout=STORAGE_ADMIN_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {"action": "reset_network", "ok": False, "message": _timeout_message(f"virsh {command}")}
        if command == "net-destroy":
            continue
        if proc.returncode != 0:
            return {"action": "reset_network", "ok": False, "message": (proc.stderr or proc.stdout or "").strip()}

    return {"action": "reset_network", "ok": True, "message": f"libvirt network restarted: {network}."}


def purge_tmp_orphans(settings, list_tmp_entries_fn) -> dict:
    tmp_root = settings.storage_dir / "tmp"
    deleted = 0
    skipped = 0
    try:
        opened = _open_tmp_root(tmp_root, settings.storage_dir)
    except ValueError as exc:
        return {"action": "purge_tmp", "ok": False, "message": str(exc), "deleted": deleted, "skipped": skipped}
    if opened is None:
        return {"action": "purge_tmp", "ok": True, "message": "storage/tmp does not exist.", "deleted": 0, "skipped": 0}
    safe_tmp_root, root_fd = opened
    try:
        for entry in list_tmp_entries_fn():
            if not entry["removable"]:
                skipped += 1
                continue
            try:
                outcome = _remove_tmp_entry(root_fd, safe_tmp_root, entry["name"])
            except ValueError as exc:
                return {
                    "action": "purge_tmp",
                    "ok": False,
                    "message": str(exc),
                    "deleted": deleted,
                    "skipped": skipped,
                }
            if outcome == "deleted":
                deleted += 1
            else:
                skipped += 1
    finally:
        os.close(root_fd)

    return {
        "action": "purge_tmp",
        "ok": True,
        "message": f"Temporary files deleted: {deleted}. Kept for safety: {skipped}.",
        "deleted": deleted,
        "skipped": skipped,
    }


def truncate_active_logs(settings) -> dict:
    from ..core.log_layout import open_log, read_layout
    from ..core.logging_config import LOG_FILE_NAMES

    logs_dir = settings.storage_dir / "logs"
    try:
        installed = read_layout(settings.storage_dir) is not None
    except (OSError, ValueError) as exc:
        return {"action": "truncate_logs", "ok": False, "message": str(exc)}
    if not installed:
        logs_dir.mkdir(parents=True, exist_ok=True)
    truncated = 0
    preserved = 0
    for name in LOG_FILE_NAMES:
        try:
            with open_log(settings.storage_dir, name, writable=True, create_flat=not installed) as stream:
                os.ftruncate(stream.fileno(), 0)
            truncated += 1
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            preserved += 1
    if preserved:
        return {
            "action": "truncate_logs",
            "ok": False,
            "message": f"Active logs truncated: {truncated}. Kept for safety: {preserved}.",
        }
    return {
        "action": "truncate_logs",
        "ok": True,
        "message": f"Active logs truncated: {truncated}. Running processes will resume writing new lines.",
    }


def _clear_unprotected_tmp_entries(tmp_root: Path, storage_root: Path) -> tuple[int, int]:
    """Remove runtime scratch data without replacing package lock inodes."""

    opened = _open_tmp_root(tmp_root, storage_root)
    if opened is None:
        return 0, 0
    safe_tmp_root, root_fd = opened
    deleted = 0
    skipped = 0
    try:
        for name in sorted(os.listdir(root_fd)):
            outcome = _remove_tmp_entry(root_fd, safe_tmp_root, name)
            if outcome == "deleted":
                deleted += 1
            else:
                skipped += 1
    finally:
        os.close(root_fd)
    return deleted, skipped


def clear_runtime_traces(settings, has_active_jobs_fn) -> dict:
    if has_active_jobs_fn():
        return {
            "action": "clear_runtime",
            "ok": False,
            "message": "Active jobs exist; runtime traces were not deleted.",
        }

    fixed_deleted = 0
    fixed_skipped = 0
    try:
        from ..core.log_layout import read_layout

        # Existing cleanup classifies/unlinks aliases without traversing them.
        # Only an ordinary directory can contain an installed role marker.
        try:
            installed_logs = read_layout(settings.storage_dir) is not None
        except OSError as exc:
            if exc.errno not in {errno.ENOTDIR, errno.ELOOP}:
                raise
            installed_logs = False
        if installed_logs:
            result = truncate_active_logs(settings)
            if not result["ok"]:
                return {**result, "action": "clear_runtime"}
            fixed_skipped += 1
        tmp_deleted, tmp_skipped = _clear_unprotected_tmp_entries(settings.storage_dir / "tmp", settings.storage_dir)
        for name in ("logs", "dumps", "api.pid", "runner.pid"):
            if name == "logs" and installed_logs:
                continue
            outcome = _remove_path_under_root(
                settings.storage_dir / name,
                settings.storage_dir,
                protected_tmp_root=settings.storage_dir / "tmp",
                recreate_directory=name == "logs",
            )
            if outcome == "deleted":
                fixed_deleted += 1
            elif outcome != "missing":
                fixed_skipped += 1
    except (ValueError, OSError, RuntimeError) as exc:
        return {"action": "clear_runtime", "ok": False, "message": str(exc)}
    message = f"Runtime trace entries deleted: {fixed_deleted + tmp_deleted}."
    if tmp_skipped:
        message += f" Temporary entries kept for safety: {tmp_skipped}."
    if fixed_skipped:
        message += f" Fixed entries kept for safety: {fixed_skipped}."
    return {
        "action": "clear_runtime",
        "ok": True,
        "message": message,
        "tmp_deleted": tmp_deleted,
        "tmp_skipped": tmp_skipped,
        "fixed_deleted": fixed_deleted,
        "fixed_skipped": fixed_skipped,
    }


def clear_storage_state(settings, has_active_jobs_fn) -> dict:
    if has_active_jobs_fn():
        return {"action": "clear_state", "ok": False, "message": "Active jobs exist; DB and SSH keys were not deleted."}

    legacy_database = settings.storage_dir / "astinuslab.db"
    configured_database = Path(getattr(settings, "db_path", legacy_database))
    if configured_database.absolute() != legacy_database.absolute():
        # This control only owns flat storage entries. Relocated SQLite families
        # require quiesced operator maintenance, including their WAL/SHM custody.
        return {
            "action": "clear_state",
            "ok": False,
            "message": "The configured database requires stopped-service operator maintenance; DB and SSH keys were not deleted.",
        }

    fixed_deleted = 0
    fixed_skipped = 0
    for name in ("astinuslab.db", "ssh_keys"):
        try:
            outcome = _remove_path_under_root(
                settings.storage_dir / name,
                settings.storage_dir,
                protected_tmp_root=settings.storage_dir / "tmp",
            )
        except (ValueError, OSError, RuntimeError) as exc:
            return {"action": "clear_state", "ok": False, "message": str(exc)}
        if outcome == "deleted":
            fixed_deleted += 1
        elif outcome != "missing":
            fixed_skipped += 1
    message = f"DB and SSH key entries deleted: {fixed_deleted}."
    if fixed_skipped:
        message += f" Fixed entries kept for safety: {fixed_skipped}."
    return {
        "action": "clear_state",
        "ok": True,
        "message": message,
        "fixed_deleted": fixed_deleted,
        "fixed_skipped": fixed_skipped,
    }
