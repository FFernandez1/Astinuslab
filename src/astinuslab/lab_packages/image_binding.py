"""Safe binding of immutable package disk layers to a launch instance.

Recipient-side package content is input, even when its digest was checked at
import time.  This module performs the small set of checks required before a
qcow2 can become an overlay backing file; it deliberately never converts or
rewrites the layer.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

from .content_store import ContentStore, ContentStoreError
from .models import LabVM


class ImageBindingError(ValueError):
    """A packaged disk image is not safe to bind to a guest."""


def immutable_boot_layer_path(vm: LabVM, store: ContentStore) -> Path:
    """Return a checked, immutable boot-layer path for one locked VM."""

    layer = vm.effective_boot_layer
    if layer is None:
        raise ImageBindingError("missing-clean-layer")
    try:
        if not store.has(layer.sha256, size_bytes=layer.size_bytes):
            raise ImageBindingError("missing-clean-layer")
    except ContentStoreError as exc:
        raise ImageBindingError("missing-clean-layer") from exc
    path = store.path_for(layer.sha256)
    return validate_bound_immutable_layer(
        path,
        sha256=layer.sha256,
        size_bytes=layer.size_bytes,
        virtual_size_limit=vm.disk_gb * 1024**3,
    )


def validate_bound_immutable_layer(
    path: Path,
    *,
    sha256: str,
    size_bytes: int,
    virtual_size_limit: int,
) -> Path:
    """Recheck a persisted runtime binding immediately before guest boot."""

    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256.lower())
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 1
    ):
        raise ImageBindingError("unsafe-boot-layer")
    _validate_regular_file(path, expected_size=size_bytes)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ImageBindingError("unsafe-boot-layer") from exc
    if digest.hexdigest() != sha256.lower():
        raise ImageBindingError("unsafe-boot-layer")
    _validate_qcow2(path, virtual_size_limit=virtual_size_limit)
    _apply_qemu_acl(path)
    return path


def _validate_regular_file(path: Path, *, expected_size: int) -> None:
    try:
        inspected = path.lstat()
    except OSError as exc:
        raise ImageBindingError("unsafe-boot-layer") from exc
    if stat.S_ISLNK(inspected.st_mode) or not stat.S_ISREG(inspected.st_mode) or inspected.st_size != expected_size:
        raise ImageBindingError("unsafe-boot-layer")


def _validate_qcow2(path: Path, *, virtual_size_limit: int) -> None:
    """Reject external backing/data-file chains in a bounded qemu-img probe."""

    try:
        completed = subprocess.run(
            ["qemu-img", "info", "--output=json", str(path)],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        info = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError) as exc:
        raise ImageBindingError("unsafe-boot-layer") from exc
    if not isinstance(info, dict) or info.get("format") != "qcow2":
        raise ImageBindingError("unsafe-boot-layer")
    virtual_size = info.get("virtual-size")
    if (
        isinstance(virtual_size, bool)
        or not isinstance(virtual_size, int)
        or not 0 < virtual_size <= virtual_size_limit
    ):
        raise ImageBindingError("unsafe-boot-layer")
    # qemu-img has used both spellings across supported versions. Any value
    # means the image can cause qemu to open a path outside the content store.
    if _contains_external_reference(info):
        raise ImageBindingError("unsafe-boot-layer")


def _contains_external_reference(value: object) -> bool:
    """Catch qemu-img's top-level and format-specific external references."""

    unsafe_fields = {"backing-filename", "full-backing-filename", "data-file", "data-file-raw"}
    if isinstance(value, dict):
        return any(
            key in unsafe_fields and item not in (None, "") or _contains_external_reference(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_external_reference(item) for item in value)
    return False


def _apply_qemu_acl(path: Path) -> None:
    """Grant libvirt access only after validation has fixed the content path."""

    try:
        from ..virtualization.host_helpers import fix_libvirt_path_acl_helper

        subprocess.run(
            ["sudo", "-n", str(fix_libvirt_path_acl_helper()), str(path.parent)],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageBindingError("unsafe-boot-layer") from exc
