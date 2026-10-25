#!/usr/bin/env python3
"""Root-owned policy validation for the libvirt ACL helper."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

POLICY_PATH = Path("/etc/astinuslab/acl-policy.json")
SCHEMA_VERSION = "astinuslab-acl-policy/v1"

# One table is authoritative.  True means only the prefix itself is rejected;
# descendants are valid dedicated deployment roots.
ROOT_REJECTION_TABLE: tuple[tuple[str, bool], ...] = (
    ("/", True),
    ("/etc", False),
    ("/usr", False),
    ("/bin", False),
    ("/sbin", False),
    ("/lib*", False),
    ("/boot", False),
    ("/root", False),
    ("/tmp", False),  # noqa: S108 - deliberate rejected system prefix
    ("/dev", False),
    ("/proc", False),
    ("/sys", False),
    ("/run", False),
    ("/home", True),
    ("/var", True),
    ("/opt", True),
    ("/srv", True),
)


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def reject_broad_root(path: str) -> str | None:
    """Return rejection reason, or None when path is registrable."""
    normalized = os.path.normpath(path)
    for prefix, allow_subdirectory in ROOT_REJECTION_TABLE:
        prefix_match = fnmatch.fnmatchcase(normalized, prefix)
        descendant_match = (
            normalized.startswith(prefix.rstrip("*") + "/") if prefix.endswith("*") else _under(normalized, prefix)
        )
        if prefix_match:
            return f"root {normalized} is not a registrable storage directory"
        if not allow_subdirectory and descendant_match:
            return f"root {normalized} is under prohibited prefix {prefix}"
    return None


def validate_root_syntax(path: str | Path) -> Path:
    value = os.fspath(path)
    if not os.path.isabs(value):
        raise ValueError("allowed roots must be absolute")
    normalized = Path(os.path.normpath(value))
    reason = reject_broad_root(str(normalized))
    if reason:
        raise ValueError(reason)
    if re.fullmatch(r"/[A-Za-z0-9._/@+/-]+", str(normalized)) is None:
        raise ValueError("storage root contains unsupported characters for systemd/AppArmor policy")
    return normalized


def validate_root(path: str | Path) -> Path:
    normalized = validate_root_syntax(path)
    try:
        info = normalized.lstat()
    except OSError as exc:
        raise ValueError("allowed root must be an existing directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("allowed root must be a non-symlink directory")
    return normalized


def validate_trusted_parent_chain(path: str | Path) -> None:
    """Require every parent of a custom root to be stable against service users.

    The storage directory itself is intentionally runtime-writable. Its parent
    must therefore be root-owned and not writable by group/other, otherwise a
    service identity can rename it and substitute a different inode after the
    policy path was validated.
    """
    current = Path(os.path.normpath(os.fspath(path))).parent
    while True:
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError(f"storage root parent does not exist: {current}") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError(
                f"custom storage root parent chain must be root-owned and not group/other-writable: {current}"
            )
        if current == current.parent:
            return
        current = current.parent


def validate_storage_root_for_install(path: str | Path, install_dir: str | Path) -> Path:
    """Allow the default storage child or a root disjoint from the install tree."""
    root_value = os.fspath(path)
    install_value = os.fspath(install_dir)
    if not os.path.isabs(root_value) or not os.path.isabs(install_value):
        raise ValueError("storage root and install directory must be absolute")
    normalized_root = validate_root_syntax(root_value)
    normalized_install = Path(os.path.normpath(install_value))
    resolved_root = Path(os.path.realpath(normalized_root))
    resolved_install = Path(os.path.realpath(normalized_install))
    resolved_default = Path(os.path.realpath(normalized_install / "storage"))
    if resolved_root == resolved_default:
        return validate_root(normalized_root) if normalized_root.exists() else normalized_root
    common = Path(os.path.commonpath((resolved_root, resolved_install)))
    if common in (resolved_root, resolved_install):
        raise ValueError(
            "custom storage root and install directory must be path-disjoint; only INSTALL_DIR/storage may be nested"
        )
    validated = validate_root(normalized_root)
    validate_trusted_parent_chain(validated)
    return validated


def normalize_roots(values: list[str]) -> list[str]:
    roots = sorted({str(validate_root(value)) for value in values}, key=lambda item: (item.count("/"), item))
    result: list[str] = []
    for root in roots:
        if not any(_under(root, parent) for parent in result):
            result.append(root)
    return result


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise ValueError("ACL policy must be root-owned and non-symlink")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("ACL policy must not be writable by group or other")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read ACL policy") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported ACL policy schema")
    callers = payload.get("allowed_callers")
    roots = payload.get("allowed_roots")
    if not isinstance(callers, list) or not all(isinstance(item, str) and item for item in callers):
        raise ValueError("allowed_callers must be a non-empty list of names")
    if not isinstance(roots, list) or not roots or not all(isinstance(item, str) for item in roots):
        raise ValueError("allowed_roots must be a non-empty list")
    # Optional: groups whose write bit is tolerated on path components. The
    # supported installed layout gives storage mode 2770 so the service group can
    # create SQLite WAL/SHM siblings, which a blanket group-writable rejection
    # would refuse. Absent or empty means no group write is tolerated anywhere.
    storage_groups = payload.get("storage_groups", [])
    if not isinstance(storage_groups, list) or not all(isinstance(item, str) and item for item in storage_groups):
        raise ValueError("storage_groups must be a list of names")
    evidence_read_groups = payload.get("evidence_read_groups", [])
    if not isinstance(evidence_read_groups, list) or not all(
        isinstance(item, str) and item for item in evidence_read_groups
    ):
        raise ValueError("evidence_read_groups must be a list of names")
    return {
        "schema_version": SCHEMA_VERSION,
        "allowed_callers": sorted(set(callers)),
        "allowed_roots": normalize_roots(roots),
        "storage_groups": sorted(set(storage_groups)),
        "evidence_read_groups": sorted(set(evidence_read_groups)),
    }


def select_root(target: str | Path, roots: list[str]) -> Path:
    target_path = validate_target(target)
    normalized = normalize_roots(roots)
    matches = [Path(root) for root in normalized if _under(str(target_path), root)]
    if not matches:
        raise ValueError("target is outside every allowed ACL root")
    return max(matches, key=lambda item: len(str(item)))


def validate_target(path: str | Path) -> Path:
    value = os.fspath(path)
    if not os.path.isabs(value) or "\x00" in value:
        raise ValueError("target must be an absolute path")
    target = Path(os.path.normpath(value))
    try:
        info = target.lstat()
    except OSError as exc:
        raise ValueError("target must be an existing directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("target must be a non-symlink directory")
    return target
