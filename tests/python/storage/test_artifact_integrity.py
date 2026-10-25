"""Artifact integrity: provenance on real bytes, checksum agreement across the
file/sidecar/manifest triad, tamper detection, and atomic-write cleanup.

These complement test_artifacts.py (field-shape assertions) and
forensics/test_memory_dump.py (ELF validation). The focus here is the property
that makes a capture forensically usable: the checksum the manifest claims is
provably the checksum of the bytes on disk, and a half-written sidecar/manifest
is never observable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astinuslab.storage.artifacts import (
    build_artifact_manifest,
    compute_file_provenance,
    manifest_path_for,
    sha256_sidecar_path_for,
    write_artifact_manifest,
    write_sha256_sidecar,
)

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# --- Provenance on real bytes (streaming reader, chunk boundary) ---


def test_provenance_matches_hashlib_across_chunk_boundary(tmp_path):
    # compute_file_provenance reads in 1 MiB chunks; cross the boundary so the
    # accumulation loop (rolling hash + size counter) runs over more than one
    # chunk. The 3-byte case in test_artifacts.py never exercises this.
    data = bytes(range(256)) * 4096 + b"tail"  # 1 MiB + 4 bytes
    assert len(data) == 1024 * 1024 + 4
    artifact = tmp_path / "memory.dump"
    artifact.write_bytes(data)

    sha256, size_bytes = compute_file_provenance(artifact)

    assert sha256 == hashlib.sha256(data).hexdigest()
    assert size_bytes == len(data) == artifact.stat().st_size


def test_provenance_of_empty_file(tmp_path):
    artifact = tmp_path / "memory.dump"
    artifact.write_bytes(b"")

    sha256, size_bytes = compute_file_provenance(artifact)

    assert sha256 == _EMPTY_SHA256
    assert size_bytes == 0


# --- Integrity round-trip: file bytes <-> sidecar <-> manifest all agree ---


def test_bytes_sidecar_and_manifest_checksums_agree(tmp_path):
    data = b"ELF-ish memory dump bytes \x00\x01\x02 payload " * 100
    artifact = tmp_path / "ubuntu-22.04-kernel-x-memory.dump"
    artifact.write_bytes(data)

    sha256, size_bytes = compute_file_provenance(artifact)
    manifest = build_artifact_manifest(
        artifact_path=artifact,
        kind="memory",
        producer="job_capture",
        sha256=sha256,
        size_bytes=size_bytes,
        raw_sha256=sha256,
        job_id="job-int",
    )
    manifest_path = write_artifact_manifest(artifact, manifest)
    sidecar_path = write_sha256_sidecar(artifact, sha256)

    on_disk = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest_data = json.loads(manifest_path.read_text())
    sidecar_digest = sidecar_path.read_text().split()[0]

    # The single most important property: the checksum the manifest claims, the
    # checksum in the sidecar, and the checksum of the actual file on disk are
    # one and the same value.
    assert on_disk == sha256 == sidecar_digest == manifest_data["sha256"]
    assert manifest_data["integrity"]["raw_sha256"] == on_disk
    assert manifest_data["size_bytes"] == size_bytes == len(data)
    # The manifest must point at the sidecar that was actually written.
    assert manifest_data["sha256_sidecar_path"] == str(sidecar_path)
    assert manifest_data["filename"] == artifact.name


def test_recorded_checksum_detects_post_capture_corruption(tmp_path):
    data = b"original dump contents, exactly this many bytes"
    artifact = tmp_path / "memory.dump"
    artifact.write_bytes(data)

    sha256, size_bytes = compute_file_provenance(artifact)
    manifest_path = write_artifact_manifest(
        artifact,
        build_artifact_manifest(
            artifact_path=artifact,
            kind="memory",
            producer="job_capture",
            sha256=sha256,
            size_bytes=size_bytes,
        ),
    )
    recorded = json.loads(manifest_path.read_text())["sha256"]

    # Flip a single bit, keeping the file length identical: the manifest must
    # still detect it. This is what makes the recorded checksum a corruption /
    # tamper witness rather than a mere size check.
    corrupted = bytearray(data)
    corrupted[0] ^= 0x01
    artifact.write_bytes(bytes(corrupted))

    assert artifact.stat().st_size == size_bytes
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() != recorded


# --- Atomic write / partial-failure cleanup ---


def test_successful_writes_leave_no_tmp_orphans(tmp_path):
    artifact = tmp_path / "disk.qcow2"
    artifact.write_bytes(b"x")
    manifest = build_artifact_manifest(
        artifact_path=artifact,
        kind="disk",
        producer="job_capture",
        sha256="a" * 64,
        size_bytes=1,
    )

    write_artifact_manifest(artifact, manifest)
    write_sha256_sidecar(artifact, "a" * 64)

    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp"))
    assert leftovers == []
    assert manifest_path_for(artifact).exists()
    assert sha256_sidecar_path_for(artifact).exists()


def test_sidecar_write_is_atomic_no_torn_final_on_crash(tmp_path, monkeypatch):
    artifact = tmp_path / "memory.dump"
    artifact.write_bytes(b"x")

    def boom(self, target):  # simulate a crash during the publish (rename) step
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(Path, "rename", boom)

    with pytest.raises(OSError, match="simulated crash"):
        write_sha256_sidecar(artifact, "a" * 64)

    # The final sidecar must never exist half-written: content lands in a temp
    # file and only an atomic rename publishes it.
    assert not sha256_sidecar_path_for(artifact).exists()


def test_manifest_write_is_atomic_no_torn_final_on_crash(tmp_path, monkeypatch):
    artifact = tmp_path / "memory.dump"
    artifact.write_bytes(b"x")
    manifest = build_artifact_manifest(
        artifact_path=artifact,
        kind="memory",
        producer="job_capture",
        sha256="a" * 64,
        size_bytes=1,
    )

    def boom(self, target):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(Path, "rename", boom)

    with pytest.raises(OSError, match="simulated crash"):
        write_artifact_manifest(artifact, manifest)

    assert not manifest_path_for(artifact).exists()
