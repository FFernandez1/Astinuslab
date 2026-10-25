from __future__ import annotations

import os
from pathlib import Path

PRIVILEGED_HELPER_DIR = Path("/usr/local/libexec/astinuslab")
_ALLOWED_HELPERS = {
    "libvirt_storage_admin.sh",
    "fix_libvirt_path_acl.sh",
    "network_isolation_admin.sh",
}


def privileged_helper_path(name: str) -> Path:
    if name not in _ALLOWED_HELPERS:
        raise ValueError(f"Unsupported privileged helper: {name}")
    return PRIVILEGED_HELPER_DIR / name


def libvirt_storage_admin_helper() -> Path:
    return privileged_helper_path("libvirt_storage_admin.sh")


def fix_libvirt_path_acl_helper() -> Path:
    return privileged_helper_path("fix_libvirt_path_acl.sh")


def network_isolation_admin_helper() -> Path:
    return privileged_helper_path("network_isolation_admin.sh")


EVIDENCE_DIR_MODE = 0o750


def ensure_evidence_dir(path: Path, root: Path) -> Path:
    """Create `path` under `root` with modes the privileged ACL helper accepts.

    The helper refuses to walk a group- or world-writable path component, and a
    bare mkdir inherits the umask: 0002 in a developer shell yields 0775, so
    every capture is denied before any ACL is applied. Create missing components
    explicitly and strip write bits from ones that already exist.

    `root` itself is never modified. On an installed host it is 2770 by design so
    the service group can create SQLite WAL/SHM siblings, and the root-owned
    policy names that group in storage_groups; taking its group-write bit away
    here would break the very layout the installer builds.
    """
    root = Path(root)
    path = Path(path)
    try:
        relative = path.relative_to(root)
    except ValueError:
        # Configured outside the storage root (a custom playground capture dir,
        # say). Still create it tightly, but do not walk unrelated ancestors.
        path.mkdir(parents=True, exist_ok=True)
        _drop_write_bits(path)
        return path

    current = root
    for segment in relative.parts:
        current = current / segment
        if not current.exists():
            current.mkdir(mode=EVIDENCE_DIR_MODE)
        _drop_write_bits(current)
    return path


def _drop_write_bits(directory: Path) -> None:
    """Remove group and other write bits, leaving read and execute untouched."""
    mode = directory.stat().st_mode
    if mode & 0o022:
        os.chmod(directory, mode & ~0o022)


def storage_root(settings) -> Path:
    """Resolve the storage root, tolerating settings stubs without the field.

    Mirrors storage_tmp_dir's handling so callers that only need the root do not
    have to duplicate the fallback.
    """
    storage_dir = getattr(settings, "storage_dir", None)
    if not isinstance(storage_dir, str | Path):
        storage_dir = Path(_mutable_root(settings)) / "storage"
    return Path(storage_dir)


def _mutable_root(settings) -> Path:
    """Root that owns mutable state, which a versioned release separates from
    the directory the code was loaded from. Settings stubs that predate the
    split fall back to the code root."""
    data_dir = getattr(settings, "data_dir", None)
    if isinstance(data_dir, str | Path):
        return Path(data_dir)
    return Path(settings.base_dir)


def storage_tmp_dir(settings) -> Path:
    """Resolve `<storage_dir>/tmp`, falling back to `<base_dir>/storage/tmp`.

    Mirrors the open-coded pattern previously duplicated across
    capture_support, playground_orchestrator, and vol3_support.
    """
    storage_dir = getattr(settings, "storage_dir", None)
    if not isinstance(storage_dir, str | Path):
        storage_dir = Path(_mutable_root(settings)) / "storage"
    return Path(storage_dir) / "tmp"
