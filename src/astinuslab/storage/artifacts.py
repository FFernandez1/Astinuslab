from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core import fake_mode

MANIFEST_SCHEMA_VERSION = "artifact-manifest/v1"


def compute_file_provenance(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size_bytes += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size_bytes


def manifest_path_for(artifact_path: Path) -> Path:
    return artifact_path.with_suffix(artifact_path.suffix + ".manifest.json")


def sha256_sidecar_path_for(artifact_path: Path) -> Path:
    return artifact_path.with_suffix(artifact_path.suffix + ".sha256")


def write_sha256_sidecar(artifact_path: Path, sha256: str) -> Path:
    sidecar_path = sha256_sidecar_path_for(artifact_path)
    tmp = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
    tmp.write_text(f"{sha256}  {artifact_path.name}\n")
    tmp.rename(sidecar_path)
    return sidecar_path


def build_artifact_manifest(
    *,
    artifact_path: Path,
    kind: str,
    producer: str,
    sha256: str,
    size_bytes: int,
    artifact_id: str | None = None,
    case_id: str | None = None,
    job_id: str | None = None,
    session_id: str | None = None,
    acquisition_id: str | None = None,
    vm_id: str | None = None,
    vm_label: str | None = None,
    domain_name: str | None = None,
    image_id: str | None = None,
    workload_id: str | None = None,
    disk_path: str | None = None,
    guest_ip: str | None = None,
    distro: str | None = None,
    version: str | None = None,
    kernel_release: str | None = None,
    architecture: str | None = None,
    capture_method: str | None = None,
    forensic_caveat: str | None = None,
    raw_sha256: str | None = None,
    captured_at_utc: str | None = None,
    capture_finished_at_utc: str | None = None,
    vm_state_at_capture_start: str | None = None,
    network_ids: list[str] | None = None,
    nics: list[dict[str, Any]] | None = None,
    networks: list[dict[str, Any]] | None = None,
    domain_xml: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_session_id = session_id or job_id
    domain_xml_sha256 = hashlib.sha256(domain_xml.encode()).hexdigest() if domain_xml else None
    domain_xml_excerpt = domain_xml[:4096] if domain_xml else None
    resolved_network_ids = network_ids or []
    resolved_nics = nics or []
    resolved_networks = networks or []
    sha256_sidecar_path = str(sha256_sidecar_path_for(artifact_path))

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "case_id": case_id,
        "job_id": job_id,
        "session_id": resolved_session_id,
        "acquisition_id": acquisition_id,
        "producer": producer,
        "kind": kind,
        "path": str(artifact_path),
        "filename": artifact_path.name,
        "sha256": sha256,
        "sha256_sidecar_path": sha256_sidecar_path,
        "size_bytes": size_bytes,
        # created_at_utc is manifest-build time. captured_at_utc/_finished are the
        # real acquisition window measured by the capturing code; null for
        # producers that are not captures (reports, derived outputs).
        "created_at_utc": datetime.now(UTC).isoformat(),
        "captured_at_utc": captured_at_utc,
        "capture_finished_at_utc": capture_finished_at_utc,
        "target": {
            "vm_id": vm_id,
            "vm_label": vm_label,
            "domain_name": domain_name,
            "image_id": image_id,
            "workload_id": workload_id,
            "disk_path": disk_path,
            "guest_ip": guest_ip,
            "distro": distro,
            "version": version,
            "kernel_release": kernel_release,
            "architecture": architecture,
        },
        "capture": {
            "capture_method": capture_method,
            "forensic_caveat": forensic_caveat,
            "vm_state_at_capture_start": vm_state_at_capture_start,
        },
        "network": {
            "network_ids": resolved_network_ids,
            "nics": resolved_nics,
            "networks": resolved_networks,
        },
        "integrity": {
            "raw_sha256": raw_sha256,
            "domain_xml_sha256": domain_xml_sha256,
            "domain_xml_excerpt": domain_xml_excerpt,
            # True when this artifact was produced by a test double rather than a
            # real hypervisor.  Absent on manifests written before 2026-07-09;
            # treat absence as "unknown", never as "false".
            "fake_mode": fake_mode.fake_mode_active(),
            "fake_mode_flags": fake_mode.fake_mode_flags(),
        },
        "inputs": inputs or [],
        # Transitional top-level keys consumed by existing Playground/UI code.
        "vm_id": vm_id,
        "vm_label": vm_label,
        "network_ids": resolved_network_ids,
        "image_id": image_id,
        "workload_id": workload_id,
        "disk_path": disk_path,
        "vm_state_at_capture_start": vm_state_at_capture_start,
        "capture_method": capture_method,
        "nics": resolved_nics,
        "networks": resolved_networks,
        "domain_xml_sha256": domain_xml_sha256,
        "domain_xml_excerpt": domain_xml_excerpt,
    }
    return manifest


def write_artifact_manifest(artifact_path: Path, manifest: dict[str, Any]) -> Path:
    manifest_path = manifest_path_for(artifact_path)
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    tmp.rename(manifest_path)
    return manifest_path
