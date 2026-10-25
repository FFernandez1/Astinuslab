from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from astinuslab.lab_packages.archive import inspect_archive, write_archive
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)
from astinuslab.lab_packages.package_catalog import (
    LabPackageRepository,
    PackageRecordConflictError,
)
from astinuslab.lab_packages.signing import SignatureResult, SignatureStatus


def _inspection(*, runbook: str = "# Demo\n"):
    definition = LabDefinition(
        package_id="demo-lab",
        version="1.0.0",
        title="Demo lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )
    archive = write_archive(definition, runbook)
    return inspect_archive(archive), archive


def _write_archive(tmp_path: Path, *, runbook: str = "# Demo\n"):
    inspection, archive = _inspection(runbook=runbook)
    path = tmp_path / "demo.astinuslab"
    path.write_bytes(archive)
    return inspection, path, archive


def test_register_persists_immutable_identity_and_canonical_documents(in_memory_session_scope, tmp_path: Path):
    inspection, path, archive = _write_archive(tmp_path)

    with in_memory_session_scope() as session:
        repository = LabPackageRepository(session, archive_root=tmp_path)
        record = repository.register_inspection(inspection, archive_path=path)

        assert record.package_id == "demo-lab"
        assert record.version == "1.0.0"
        assert record.title == "Demo lab"
        assert record.archive_path == str(path)
        assert record.archive_sha256 == hashlib.sha256(archive).hexdigest()
        assert record.archive_size_bytes == len(archive)
        assert record.manifest_sha256 == inspection.manifest_digest
        assert record.signature_status == SignatureStatus.UNSIGNED.value
        assert record.definition_json
        assert record.manifest_json

    with in_memory_session_scope() as session:
        loaded = LabPackageRepository(session, archive_root=tmp_path).get("demo-lab", "1.0.0")
        assert loaded is not None
        assert loaded.id.startswith("lp-")


def test_reimport_of_identical_archive_is_idempotent(in_memory_session_scope, tmp_path: Path):
    inspection, path, _ = _write_archive(tmp_path)

    with in_memory_session_scope() as session:
        repository = LabPackageRepository(session, archive_root=tmp_path)
        first = repository.register_inspection(inspection, archive_path=path)
        second = repository.register_inspection(inspection, archive_path=path)
        assert second.id == first.id
        assert len(repository.list_packages()) == 1


def test_same_identity_with_different_archive_is_rejected(in_memory_session_scope, tmp_path: Path):
    first_inspection, first_path, _ = _write_archive(tmp_path, runbook="# First\n")
    first_path = tmp_path / "first.astinuslab"
    first_path.write_bytes(write_archive(first_inspection.definition, "# First\n"))
    second_inspection, second_path, _ = _write_archive(tmp_path, runbook="# Second\n")
    # The helper writes the same destination; retain both bytes so the conflict
    # path cannot accidentally compare only filesystem metadata.
    second_path = tmp_path / "second.astinuslab"
    second_path.write_bytes(write_archive(second_inspection.definition, "# Second\n"))

    with in_memory_session_scope() as session:
        repository = LabPackageRepository(session, archive_root=tmp_path)
        repository.register_inspection(first_inspection, archive_path=first_path)
        with pytest.raises(PackageRecordConflictError, match="already registered"):
            repository.register_inspection(second_inspection, archive_path=second_path)


def test_signature_metadata_and_last_used_are_persisted(in_memory_session_scope, tmp_path: Path):
    inspection, path, _ = _write_archive(tmp_path)
    signature = SignatureResult(SignatureStatus.VALID_UNTRUSTED_KEY, "a" * 64)

    with in_memory_session_scope() as session:
        repository = LabPackageRepository(session, archive_root=tmp_path)
        record = repository.register_inspection(inspection, archive_path=path, signature=signature)
        assert record.signature_status == SignatureStatus.VALID_UNTRUSTED_KEY.value
        assert record.creator_key_fingerprint == "a" * 64
        assert record.last_used_at is None
        touched = repository.touch_last_used(record.id)
        assert touched.last_used_at is not None


def test_archive_path_must_be_regular_non_symlink(in_memory_session_scope, tmp_path: Path):
    inspection, path, _ = _write_archive(tmp_path)
    link = tmp_path / "link.astinuslab"
    link.symlink_to(path)

    with in_memory_session_scope() as session:
        with pytest.raises(PackageRecordConflictError, match="regular non-symlink"):
            LabPackageRepository(session, archive_root=tmp_path).register_inspection(inspection, archive_path=link)


def test_archive_path_must_be_inside_managed_root(in_memory_session_scope, tmp_path: Path):
    inspection, path, _ = _write_archive(tmp_path)
    managed = tmp_path / "managed"
    managed.mkdir()

    with in_memory_session_scope() as session:
        with pytest.raises(PackageRecordConflictError, match="managed package storage"):
            LabPackageRepository(session, archive_root=managed).register_inspection(inspection, archive_path=path)


def test_inspection_must_match_retained_archive(in_memory_session_scope, tmp_path: Path):
    first_inspection, first_path, _ = _write_archive(tmp_path, runbook="# First\n")
    second_inspection, second_path, _ = _write_archive(tmp_path, runbook="# Second\n")
    second_path = tmp_path / "second.astinuslab"
    second_path.write_bytes(write_archive(second_inspection.definition, "# Second\n"))

    with in_memory_session_scope() as session:
        with pytest.raises(PackageRecordConflictError, match="does not match"):
            LabPackageRepository(session, archive_root=tmp_path).register_inspection(
                first_inspection, archive_path=second_path
            )


def test_immutable_record_fields_cannot_be_mutated(in_memory_session_scope, tmp_path: Path):
    inspection, path, _ = _write_archive(tmp_path)

    with in_memory_session_scope() as session:
        record = LabPackageRepository(session, archive_root=tmp_path).register_inspection(inspection, archive_path=path)
        record.title = "Changed"
        with pytest.raises(ValueError, match="immutable"):
            session.flush()
        session.rollback()
