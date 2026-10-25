from __future__ import annotations

import json

from astinuslab.storage.artifacts import (
    build_artifact_manifest,
    compute_file_provenance,
    manifest_path_for,
    write_artifact_manifest,
    write_sha256_sidecar,
)


def test_compute_file_provenance_returns_sha256_and_size(tmp_path):
    artifact = tmp_path / "memory.dump"
    artifact.write_bytes(b"abc")

    sha256, size_bytes = compute_file_provenance(artifact)

    assert sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert size_bytes == 3


def test_manifest_path_for_appends_manifest_json_after_suffix(tmp_path):
    artifact = tmp_path / "memory.dump"

    assert manifest_path_for(artifact) == tmp_path / "memory.dump.manifest.json"


def test_write_sha256_sidecar_writes_standard_checksum_line(tmp_path):
    artifact = tmp_path / "disk.qcow2"
    artifact.write_bytes(b"x")

    sidecar = write_sha256_sidecar(artifact, "a" * 64)

    assert sidecar == tmp_path / "disk.qcow2.sha256"
    assert sidecar.read_text() == f"{'a' * 64}  disk.qcow2\n"


def test_write_artifact_manifest_preserves_legacy_and_versioned_fields(tmp_path):
    artifact = tmp_path / "memory.dump"
    artifact.write_bytes(b"x")
    manifest = build_artifact_manifest(
        artifact_path=artifact,
        kind="memory",
        producer="job_capture",
        sha256="b" * 64,
        size_bytes=1,
        job_id="job-1",
        acquisition_id="acq-1",
        vm_id="vm-1",
        vm_label="target",
        domain_name="domain-1",
        capture_method="memory_dump",
        nics=[{"nic_id": "nic-1"}],
        networks=[{"id": "net-1"}],
        domain_xml="<domain />",
    )

    path = write_artifact_manifest(artifact, manifest)

    data = json.loads(path.read_text())
    assert data["schema_version"] == "artifact-manifest/v1"
    assert data["producer"] == "job_capture"
    assert data["job_id"] == "job-1"
    assert data["session_id"] == "job-1"
    assert data["acquisition_id"] == "acq-1"
    assert data["vm_id"] == "vm-1"
    assert data["vm_label"] == "target"
    assert data["kind"] == "memory"
    assert data["sha256"] == "b" * 64
    assert data["target"]["domain_name"] == "domain-1"
    assert data["capture"]["capture_method"] == "memory_dump"
    assert data["network"]["nics"] == [{"nic_id": "nic-1"}]
    assert data["network"]["networks"] == [{"id": "net-1"}]
    assert data["domain_xml_sha256"] == "3377cb54771984819e131da4c82450a6c9ee4d25daf53c723c06b5d22a6988de"
    assert data["integrity"]["domain_xml_sha256"] == data["domain_xml_sha256"]


def test_manifest_records_forensic_caveat(tmp_path):
    artifact = tmp_path / "disk.img.gz"
    artifact.write_bytes(b"x")

    default_manifest = build_artifact_manifest(
        artifact_path=artifact,
        kind="disk",
        producer="job_capture",
        sha256="c" * 64,
        size_bytes=1,
    )
    caveat_manifest = build_artifact_manifest(
        artifact_path=artifact,
        kind="disk",
        producer="job_capture",
        sha256="c" * 64,
        size_bytes=1,
        forensic_caveat="live copy",
    )

    assert default_manifest["capture"]["forensic_caveat"] is None
    assert caveat_manifest["capture"]["forensic_caveat"] == "live copy"


def test_manifest_records_capture_window_when_supplied(tmp_path):
    artifact = tmp_path / "memory.dump"
    artifact.write_bytes(b"x")
    manifest = build_artifact_manifest(
        artifact_path=artifact,
        kind="memory",
        producer="job_capture",
        sha256="a" * 64,
        size_bytes=1,
        job_id="job-1",
        captured_at_utc="2026-06-12T10:00:00+00:00",
        capture_finished_at_utc="2026-06-12T10:02:30+00:00",
    )
    assert manifest["captured_at_utc"] == "2026-06-12T10:00:00+00:00"
    assert manifest["capture_finished_at_utc"] == "2026-06-12T10:02:30+00:00"
    assert manifest["captured_at_utc"] <= manifest["capture_finished_at_utc"]
