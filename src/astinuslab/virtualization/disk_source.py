"""Resolve the disk currently attached to a Job, including external snapshots."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def current_job_disk(conn: Any, vm_name: str, original_disk: Path) -> Path:
    """Refuse an unknown or foreign primary disk instead of capturing old bytes."""
    try:
        root = ET.fromstring(conn.lookupByName(vm_name).XMLDesc(0))  # noqa: S314 -- local libvirt-generated XML
        disks = []
        for disk in root.findall("./devices/disk"):
            target = disk.find("target")
            if disk.get("device") == "disk" and target is not None and target.get("dev") == "vda":
                disks.append(disk)
        if len(disks) != 1 or disks[0].get("type") != "file":
            raise ValueError("expected one file-backed primary disk")
        source = disks[0].find("source")
        value = source.get("file") if source is not None else None
        if not value or not Path(value).is_absolute():
            raise ValueError("primary disk source is missing or relative")
        path = Path(value).resolve(strict=True)
        if path.parent != original_disk.parent.resolve() or not path.is_file():
            raise ValueError("primary disk is outside the Job storage directory")
        return path
    except Exception as exc:
        raise RuntimeError(f"Cannot determine current Job disk for {vm_name}: {exc}") from exc
