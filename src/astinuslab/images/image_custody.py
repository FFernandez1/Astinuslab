"""Serialize image removal with Playground admission and retained disk custody."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import catalog
from ..db.models import JobVM
from ..db.transactions import reserve_sqlite_write


def _catalog_sources(images_dir: Path) -> dict[tuple[str, str], Path]:
    sources = {}
    for role, entries in (
        ("target", catalog.load_targets()),
        ("attacker", catalog.load_attackers()),
        ("network", catalog.load_network_nodes()),
    ):
        for entry in entries:
            raw = entry.get("path") or (images_dir / entry["image_ref"] if entry.get("image_ref") else None)
            if raw:
                sources[(role, entry["id"])] = Path(raw).resolve()
    return sources


def _disk_dependencies(disk: Path, disk_root: Path, *, require_qcow2: bool = True) -> set[Path]:
    if disk.is_symlink() or not disk.resolve().is_relative_to(disk_root.resolve()):
        raise ValueError("retained overlay is outside the owned disk root")
    if not stat.S_ISREG(disk.stat().st_mode):
        raise ValueError("retained overlay is not a regular file")
    result = subprocess.run(
        ["qemu-img", "info", "--force-share", "--backing-chain", "--output=json", str(disk.resolve())],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise ValueError("retained overlay backing chain is unavailable")
    chain = json.loads(result.stdout)
    if not isinstance(chain, list) or not chain or not isinstance(chain[0], dict):
        raise ValueError("retained overlay backing chain is invalid")
    if require_qcow2 and chain[0].get("format") != "qcow2":
        raise ValueError("retained overlay format is invalid")
    dependencies = set()
    parent = disk.parent
    for entry in chain:
        if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
            raise ValueError("retained overlay backing chain is incomplete")
        filename = Path(entry["filename"])
        current = (filename if filename.is_absolute() else parent / filename).resolve()
        dependencies.add(current)
        if entry.get("format") == "qcow2":
            format_specific = entry.get("format-specific")
            if not isinstance(format_specific, dict) or format_specific.get("type") != "qcow2":
                raise ValueError("qcow2 dependency metadata is unavailable")
            format_data = format_specific.get("data")
            if not isinstance(format_data, dict):
                raise ValueError("qcow2 dependency metadata is invalid")
            if "data-file" in format_data:
                data_file = format_data["data-file"]
                if not isinstance(data_file, str) or not data_file:
                    raise ValueError("qcow2 external data path is invalid")
                data_path = Path(data_file)
                # QEMU can resolve external data against its runtime cwd. That
                # process context is not retained, so relative identity is unsafe.
                if not data_path.is_absolute():
                    raise ValueError("qcow2 external data path is relative")
                resolved_data = data_path.resolve()
                if not resolved_data.is_file():
                    raise ValueError("qcow2 external data file is unavailable")
                dependencies.add(resolved_data)
            elif format_data.get("data-file-raw"):
                raise ValueError("qcow2 external data path is unavailable")
        elif entry.get("format") != "raw":
            raise ValueError("backing image dependency format is unsupported")
        backing = entry.get("full-backing-filename") or entry.get("backing-filename")
        if backing:
            if not isinstance(backing, str):
                raise ValueError("retained overlay backing path is invalid")
            path = Path(backing)
            dependencies.add((path if path.is_absolute() else current.parent / path).resolve())
        parent = current.parent
    return dependencies


def delete_images(
    session: Session,
    paths: list[Path],
    *,
    images_dir: Path,
    disk_root: Path,
    missing_ok: bool = False,
) -> int:
    """Prevalidate the complete selection before unlinking under the writer lock.

    register_vm holds the same reservation through its pending-row commit. A
    retained overlay is checked even when its catalog entry has since changed
    or its VM is stopped/failed; a destroyed row with no disk releases custody.
    """
    reserve_sqlite_write(session)
    selected = []
    for path in dict.fromkeys(paths):
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                continue
            raise HTTPException(404, "Image not found") from None
        if path.parent.resolve() != images_dir.resolve() or not stat.S_ISREG(info.st_mode):
            raise HTTPException(409, "Image deletion requires a regular file in the image directory")
        selected.append(path)
    if not selected:
        return 0
    candidates = {path.resolve() for path in selected}
    vms = list(session.scalars(select(JobVM)))
    sources = _catalog_sources(images_dir) if vms else {}
    for vm in vms:
        disk = Path(vm.disk_path)
        retained_disk = disk.exists() or disk.is_symlink()
        if vm.state == "destroyed" and not retained_disk:
            continue
        source = sources.get(("target" if vm.role == "vm" else vm.role, vm.image_id))
        if vm.state != "destroyed" and source in candidates:
            raise HTTPException(409, f"Image is retained by Playground VM {vm.label}; remove the VM first")
        try:
            if retained_disk:
                dependencies = _disk_dependencies(disk, disk_root)
                if dependencies & candidates:
                    raise HTTPException(409, f"Image backs retained Playground disk {vm.label}; remove the VM first")
            elif source is None or vm.state != "pending":
                raise ValueError("pending or retained image identity is unavailable")
            elif _disk_dependencies(source, images_dir, require_qcow2=False) & candidates:
                raise HTTPException(409, f"Image backs pending Playground VM {vm.label}; remove the VM first")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise HTTPException(
                409, f"Cannot establish image custody for Playground VM {vm.label}; reconcile it first"
            ) from exc
    for path in selected:
        path.unlink()
    return len(selected)
