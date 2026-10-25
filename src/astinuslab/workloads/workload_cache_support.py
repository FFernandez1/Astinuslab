from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def workload_cache_key(target_id: str, workload_id: str, image_path: Path, workload_fingerprint: str = "") -> str:
    """16-char hex key derived from target, workload, fingerprint, and base image identity."""
    st = image_path.stat()
    raw = f"{target_id}:{workload_id}:{workload_fingerprint}:{image_path.name}:{st.st_size}:{st.st_mtime_ns}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def workload_cache_dir(settings) -> Path:
    return Path(settings.storage_dir) / "workload-cache"


def _ensure_cache_directory(path: Path) -> Path:
    if path.is_symlink():
        raise OSError(f"workload cache directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"workload cache path is not a directory: {path}")
    path.chmod(0o750)
    return path


def ensure_workload_cache_dir(settings) -> Path:
    """Create or repair the cache directory without unsafe group-write access."""
    return _ensure_cache_directory(workload_cache_dir(settings))


def workload_cache_path(settings, target_id: str, workload_id: str, key: str) -> Path:
    return workload_cache_dir(settings) / f"{target_id}-{workload_id}-{key}.qcow2"


def check_workload_cache(
    logger,
    settings,
    target_id: str,
    workload_id: str,
    image_path: Path,
    *,
    cache_key_fn=None,
    cache_path_fn=None,
) -> Path | None:
    """Return the cached qcow2 path if a valid cache entry exists, else None."""
    if cache_key_fn is None:
        cache_key_fn = workload_cache_key
    try:
        key = cache_key_fn(target_id, workload_id, image_path)
    except OSError:
        return None
    if cache_path_fn is None:
        cache_path_fn = lambda target_id, workload_id, key: workload_cache_path(settings, target_id, workload_id, key)
    path = cache_path_fn(target_id, workload_id, key)
    if path.exists() and path.stat().st_size > 0:
        logger.info("Workload cache hit: %s", path)
        return path
    return None


def update_workload_cache_index(
    settings,
    workload_cache_lock,
    target_id: str,
    workload_id: str,
    key: str,
    cache_path: Path,
    image_path: Path,
) -> None:
    index_path = ensure_workload_cache_dir(settings) / "index.json"
    with workload_cache_lock:
        index: dict = {}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "workload cache index at %s is unreadable (%s); rebuilding from empty",
                    index_path,
                    exc,
                )
        index[key] = {
            "target_id": target_id,
            "workload_id": workload_id,
            "cache_file": cache_path.name,
            "base_image": image_path.name,
            "created_at": datetime.now(UTC).isoformat(),
            "size_bytes": cache_path.stat().st_size,
        }
        index_path.write_text(json.dumps(index, indent=2))


def save_workload_cache(
    logger,
    settings,
    run_command,
    job_disk_image_path,
    update_index,
    cache_key_fn,
    cache_path_fn,
    *,
    job_id: str,
    target_id: str,
    workload_id: str,
    image_path: Path,
    flush_guest_filesystems: Callable[[], None],
) -> None:
    """Take an external disk-only snapshot, then copy the frozen backing image as the cache. Non-fatal."""
    try:
        key = cache_key_fn(target_id, workload_id, image_path)
    except OSError as exc:
        logger.warning("[%s] workload cache save: cannot compute key: %s", job_id, exc)
        return

    cache_path = cache_path_fn(target_id, workload_id, key)
    partial_path = cache_path.with_suffix(".qcow2.partial")
    _ensure_cache_directory(cache_path.parent)

    job_disk = job_disk_image_path(job_id, target_id)
    if not job_disk.exists():
        logger.warning("[%s] workload cache save: job disk not found at %s", job_id, job_disk)
        return

    vm_name = f"astinuslab-{job_id}"
    # The frozen disk holds only what the guest has written back. On ext4, files
    # written seconds before the snapshot otherwise come back zero-length, so a
    # cache-booted workload serves empty content. Flush through the guest, and
    # also freeze its filesystems for the snapshot when the agent answers.
    try:
        flush_guest_filesystems()
        flushed = True
    except Exception as exc:
        logger.warning("[%s] workload cache: guest filesystem flush failed: %s", job_id, exc)
        flushed = False
    try:
        run_command(
            ["virsh", "-c", settings.libvirt_uri, "qemu-agent-command", vm_name, '{"execute":"guest-ping"}'],
            job_id=job_id,
            quiet_fail=True,
        )
        quiesce = True
    except Exception:
        quiesce = False
    if not (flushed or quiesce):
        logger.warning("[%s] workload cache not saved: guest filesystems could not be flushed", job_id)
        return

    logger.info("[%s] saving workload cache: %s", job_id, cache_path)
    try:
        # Create external snapshot. The current job_disk becomes read-only, and the VM starts writing to a new overlay.
        snapshot = [
            "virsh",
            "-c",
            settings.libvirt_uri,
            "snapshot-create-as",
            vm_name,
            "workload-cache",
            "--disk-only",
            "--atomic",
            "--no-metadata",
        ]
        if quiesce:
            snapshot.append("--quiesce")
        run_command(snapshot, job_id=job_id)
        # We can now safely copy the frozen job_disk. Using cp instead of convert since it's already an overlay.
        run_command(
            ["cp", str(job_disk), str(partial_path)],
            job_id=job_id,
        )
        partial_path.rename(cache_path)
        update_index(target_id, workload_id, key, cache_path, image_path)
        logger.info("[%s] workload cache saved: %s (%.1f GB)", job_id, cache_path, cache_path.stat().st_size / 1e9)
    except Exception as exc:
        logger.warning("[%s] workload cache save failed (non-fatal): %s", job_id, exc)
        partial_path.unlink(missing_ok=True)
