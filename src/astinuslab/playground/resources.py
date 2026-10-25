from __future__ import annotations

type VMResourceTuple = tuple[int, int, int | None]

VCPUS_MIN = 1
VCPUS_MAX = 64
MEMORY_MB_MIN = 128
MEMORY_MB_MAX = 131072
DISK_GB_MIN = 1
DISK_GB_MAX = 2048


def validate_vm_resources(vcpus: object, memory_mb: object, disk_gb: object) -> VMResourceTuple:
    """Validate and return VM resources without coercing malformed values."""
    if type(vcpus) is not int or not VCPUS_MIN <= vcpus <= VCPUS_MAX:  # noqa: E721
        raise ValueError(f"vcpus must be an integer between {VCPUS_MIN} and {VCPUS_MAX}")
    if type(memory_mb) is not int or not MEMORY_MB_MIN <= memory_mb <= MEMORY_MB_MAX:  # noqa: E721
        raise ValueError(f"memory_mb must be an integer between {MEMORY_MB_MIN} and {MEMORY_MB_MAX}")
    if disk_gb is not None and (type(disk_gb) is not int or not DISK_GB_MIN <= disk_gb <= DISK_GB_MAX):  # noqa: E721
        raise ValueError(f"disk_gb must be None or an integer between {DISK_GB_MIN} and {DISK_GB_MAX}")
    return vcpus, memory_mb, disk_gb
