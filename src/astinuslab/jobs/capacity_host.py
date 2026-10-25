"""Read-only host observations for runner admission and cleanup confirmation."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..core import fake_mode
from .capacity import GIB, HostCapacity, Requirements


@dataclass(frozen=True)
class CleanupProof:
    job_id: str
    domains_absent: bool
    disks_absent: bool


def _domain_inventory(config) -> dict[str, tuple[int, bool]]:
    """Map each defined domain to its configured RAM MiB and whether it runs."""
    if fake_mode.FAKE_LIBVIRT:
        return {}  # explicitly synthetic; never considered real-host evidence
    import libvirt

    conn = libvirt.openReadOnly(config.libvirt_uri)
    if conn is None:
        raise RuntimeError("libvirt inventory unavailable")
    try:
        # Include inactive definitions: they still retain disks and can restart.
        return {
            domain.name(): ((domain.maxMemory() + 1023) // 1024, bool(domain.isActive()))
            for domain in conn.listAllDomains()
        }
    finally:
        conn.close()


def _disk_usage(path: Path):
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(path)


def read_host_capacity(config, *, include_domains=True) -> HostCapacity:
    if fake_mode.FAKE_LIBVIRT:
        # Deterministic simulated host, never substitute these values for host
        # validation. Scarcity tests inject explicit HostCapacity observations.
        return HostCapacity(1024 * 1024, 1024 * 1024, 100000 * GIB, 100000 * GIB, {}, synthetic=True)
    try:
        meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        total = int(meminfo["MemTotal"].split()[0]) // 1024
        available = int(meminfo["MemAvailable"].split()[0]) // 1024
        disks = [_disk_usage(Path(config.storage_dir)), _disk_usage(Path(config.libvirt_image_base_dir))]
        inventory = _domain_inventory(config) if include_domains else {}
        return HostCapacity(
            total,
            available,
            min(d.total for d in disks),
            min(d.free for d in disks),
            {name: memory for name, (memory, _) in inventory.items()},
            frozenset(name for name, (_, active) in inventory.items() if not active),
        )
    except (OSError, ValueError, KeyError, RuntimeError, ImportError) as exc:
        return HostCapacity(0, 0, 0, 0, {}, error=f"host resource inventory unavailable ({type(exc).__name__})")
    except Exception as exc:  # libvirt's own exception type is optional at import
        return HostCapacity(0, 0, 0, 0, {}, error=f"VM inventory unavailable ({type(exc).__name__})")


def probe_cleanup(config, job_id: str, vm_count: int) -> CleanupProof:
    ids = [job_id] + ([f"{job_id}-attacker"] if vm_count > 1 else [])
    try:
        domains = _domain_inventory(config)
        absent = all(f"astinuslab-{identifier}" not in domains for identifier in ids)
        disks_absent = True
        for identifier in ids:
            for root in (Path(config.libvirt_image_base_dir), Path(config.storage_dir) / "tmp"):
                path = root / identifier
                # stat permission/errors cannot establish absence; symlinks also
                # retain ownership claims even if their referent disappeared.
                try:
                    path.lstat()
                    disks_absent = False
                except FileNotFoundError:
                    pass
        return CleanupProof(job_id, absent, disks_absent)
    except Exception:
        return CleanupProof(job_id, False, False)


def freeze_requirements(payload, target: dict, workload: dict | None, config):
    """Freeze VM sizes in Job inputs so catalog edits cannot outrun reservations."""
    count = 2 if payload.attacker_vm_id or (workload and workload.get("attacker_vm")) else 1
    memory = payload.vm_memory_mb or target.get("memory_mb") or 2048
    disk = payload.vm_disk_gb or target.get("disk_gb") or 40
    attacker_memory = payload.attacker_vm_memory_mb or 2048
    attacker_disk = payload.attacker_vm_disk_gb or 40
    fields = dict(
        vm_memory_mb=memory, vm_disk_gb=disk, attacker_vm_memory_mb=attacker_memory, attacker_vm_disk_gb=attacker_disk
    )
    total_memory = memory + (attacker_memory if count == 2 else 0) + count * config.capacity_vm_overhead_mb
    storage = disk + (attacker_disk if count == 2 else 0)
    # Worst-case simultaneous raw+compressed disk capture; RAM capture plus
    # uncompressed memory staging. Persistent artifacts subsequently count in
    # filesystem free space after this reservation is released.
    if payload.capture_type in {"memory", "both"}:
        storage += 2 * memory / 1024
    if payload.capture_type in {"disk", "both"}:
        storage += 2 * disk
    return fields, Requirements(count, total_memory, int(storage * GIB), bool(target.get("keep_failed_vm")))
